"""NASA FIRMS API client.

Endpoint shape (see https://firms.modaps.eosdis.nasa.gov/api/area/csv):

    /api/area/csv/[MAP_KEY]/[SOURCE]/[west,south,east,north]/[DAY_RANGE]/[DATE]

DAY_RANGE is capped at 10 days per request and the returned block covers
DATE .. DATE + DAY_RANGE - 1. The MAP_KEY allowance is 5,000 transactions per
rolling 10-minute window, and a multi-day request can count as more than one
transaction, so this client budgets conservatively and caches every block it
retrieves.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import os
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

from .config import PLATFORM_SOURCES, FirmsConfig, SensorConfig
from .seasons import DateRange

log = logging.getLogger("firepipe.firms")

BASE = "https://firms.modaps.eosdis.nasa.gov"

#: Columns every fetch is normalised to, whatever the sensor.
CANONICAL_COLUMNS = [
    "latitude",
    "longitude",
    "acq_date",
    "acq_time",
    "satellite",
    "instrument",
    "confidence",
    "version",
    "brightness",
    "brightness_secondary",
    "scan",
    "track",
    "frp",
    "daynight",
    "type",
    "confidence_class",
    "confidence_value",
    "source",
    "platform",
    "sensor",
]

SENSOR_LABELS = {
    "MODIS": "MODIS (Terra/Aqua, 1 km)",
    "VIIRS_SNPP": "VIIRS S-NPP (375 m)",
    "VIIRS_NOAA20": "VIIRS NOAA-20 (375 m)",
    "VIIRS_NOAA21": "VIIRS NOAA-21 (375 m)",
}


class FirmsError(RuntimeError):
    """Raised when FIRMS returns something that is not fire data."""


class DayRangeError(FirmsError):
    """The server rejected DAY_RANGE and told us its actual ceiling.

    FIRMS documents 1..10 but currently enforces 1..5, so the limit is read
    from the error message rather than hard-coded.
    """

    def __init__(self, max_days: int, message: str):
        super().__init__(message)
        self.max_days = max_days


@dataclass
class BBox:
    west: float
    south: float
    east: float
    north: float

    def as_param(self) -> str:
        return f"{self.west:.4f},{self.south:.4f},{self.east:.4f},{self.north:.4f}"

    def padded(self, deg: float) -> "BBox":
        return BBox(
            max(-180.0, self.west - deg),
            max(-90.0, self.south - deg),
            min(180.0, self.east + deg),
            min(90.0, self.north + deg),
        )

    def union(self, other: "BBox") -> "BBox":
        return BBox(
            min(self.west, other.west),
            min(self.south, other.south),
            max(self.east, other.east),
            max(self.north, other.north),
        )


class RateLimiter:
    """Rolling-window limiter matching the FIRMS 10-minute allowance."""

    def __init__(self, max_calls: int, window_s: int = 600):
        self.max_calls = max_calls
        self.window_s = window_s
        self._calls: deque[float] = deque()

    def acquire(self, weight: int = 1) -> None:
        now = time.monotonic()
        while self._calls and now - self._calls[0] > self.window_s:
            self._calls.popleft()
        if len(self._calls) + weight > self.max_calls:
            sleep_for = self.window_s - (now - self._calls[0]) + 1
            log.warning(
                "FIRMS rate ceiling reached, pausing %.0f s before continuing",
                sleep_for,
            )
            time.sleep(max(sleep_for, 1))
            self._calls.clear()
        for _ in range(weight):
            self._calls.append(time.monotonic())


class FirmsClient:
    def __init__(self, cfg: FirmsConfig, map_key: str | None = None):
        self.cfg = cfg
        self.map_key = map_key or cfg.map_key or os.environ.get("FIRMS_MAP_KEY")
        self.cache_dir = Path(cfg.cache_dir).expanduser()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "firepipe/1.0 (agricultural fire pipeline)"
        self.limiter = RateLimiter(cfg.requests_per_10min)
        self.max_day_range = cfg.max_day_range
        self._availability: pd.DataFrame | None = None
        self.stats = {"requests": 0, "cache_hits": 0, "rows": 0, "empty_blocks": 0}

    # -- key handling ------------------------------------------------------

    def _require_key(self) -> str:
        if not self.map_key:
            raise FirmsError(
                "No FIRMS MAP_KEY. Set the FIRMS_MAP_KEY environment variable or "
                "put map_key under 'firms:' in the config. Register for a free key "
                f"at {BASE}/api/map_key/"
            )
        return self.map_key

    def map_key_status(self) -> dict:
        key = self._require_key()
        url = f"{BASE}/mapserver/mapkey_status/?MAP_KEY={key}"
        r = self.session.get(url, timeout=self.cfg.timeout_s)
        r.raise_for_status()
        payload = r.json()
        if "error" in payload:
            raise FirmsError(
                f"FIRMS rejected the MAP_KEY: {payload['error']}. Check for stray "
                "quotes or whitespace, and confirm the key at "
                f"{BASE}/api/map_key/"
            )
        return payload

    # -- availability ------------------------------------------------------

    def data_availability(self, refresh: bool = False) -> pd.DataFrame:
        """Date coverage per dataset id, used to route SP vs NRT."""
        if self._availability is not None and not refresh:
            return self._availability
        key = self._require_key()
        url = f"{BASE}/api/data_availability/csv/{key}/all"
        text = self._get_text(url)
        df = pd.read_csv(io.StringIO(text))
        df.columns = [c.strip().lower() for c in df.columns]
        for col in ("min_date", "max_date"):
            if col in df:
                df[col] = pd.to_datetime(df[col], errors="coerce").dt.date
        self._availability = df
        return df

    def resolve_source(
        self, platform: str, day: date, sensors: SensorConfig
    ) -> str | None:
        """Pick the dataset id that actually covers ``day`` for ``platform``."""
        candidates = PLATFORM_SOURCES[platform]
        if sensors.processing == "SP":
            candidates = [c for c in candidates if c.endswith("_SP")]
        elif sensors.processing == "NRT":
            candidates = [c for c in candidates if c.endswith("_NRT")]
        if not candidates:
            return None
        try:
            avail = self.data_availability()
        except Exception as exc:  # offline / key problem -> fall back to order
            log.debug("data_availability unavailable (%s); using default order", exc)
            return candidates[0]

        col = "data_id" if "data_id" in avail.columns else avail.columns[0]
        for cand in candidates:  # SP first, then NRT
            row = avail[avail[col].astype(str).str.upper() == cand]
            if row.empty:
                continue
            lo, hi = row.iloc[0].get("min_date"), row.iloc[0].get("max_date")
            if pd.isna(lo) or pd.isna(hi):
                continue
            if lo <= day <= hi:
                return cand
        return None

    # -- fetching ----------------------------------------------------------

    def fetch_block(
        self, source: str, bbox: BBox, start: date, day_range: int
    ) -> pd.DataFrame:
        key = self._require_key()

        # Once the ceiling is known, split up front rather than spending a
        # transaction to be told 'no' again.
        if day_range > self.max_day_range:
            return self._fetch_split(source, bbox, start, day_range)
        cache_key = hashlib.sha1(
            f"{source}|{bbox.as_param()}|{start:%Y-%m-%d}|{day_range}".encode()
        ).hexdigest()[:20]
        cache_file = self.cache_dir / f"{source}_{start:%Y%m%d}_{cache_key}.csv.gz"

        if cache_file.exists():
            self.stats["cache_hits"] += 1
            df = pd.read_csv(cache_file, dtype={"acq_time": str})
        else:
            url = (
                f"{BASE}/api/area/csv/{key}/{source}/{bbox.as_param()}/"
                f"{day_range}/{start:%Y-%m-%d}"
            )
            try:
                text = self._get_text(url, weight=day_range)
            except DayRangeError as exc:
                # The server publishes its own ceiling; adopt it and re-split
                # rather than failing the run.
                if exc.max_days < self.max_day_range:
                    log.warning(
                        "FIRMS enforces DAY_RANGE <= %d (config asked for %d). "
                        "Splitting automatically for the rest of this run.",
                        exc.max_days,
                        self.max_day_range,
                    )
                    self.max_day_range = exc.max_days
                if day_range <= self.max_day_range:
                    raise
                return self._fetch_split(source, bbox, start, day_range)
            self.stats["requests"] += 1
            df = _parse_csv(text, source)
            df.to_csv(cache_file, index=False)

        if df.empty:
            self.stats["empty_blocks"] += 1
            return _empty_frame()
        self.stats["rows"] += len(df)
        return _normalise(df, source)

    def _fetch_split(
        self, source: str, bbox: BBox, start: date, day_range: int
    ) -> pd.DataFrame:
        """Re-issue an over-long block as several requests within the ceiling."""
        frames: list[pd.DataFrame] = []
        remaining = day_range
        cursor = start
        while remaining > 0:
            step = min(self.max_day_range, remaining)
            block = self.fetch_block(source, bbox, cursor, step)
            if not block.empty:
                frames.append(block)
            cursor += timedelta(days=step)
            remaining -= step
        return pd.concat(frames, ignore_index=True) if frames else _empty_frame()

    def fetch_ranges(
        self,
        ranges: list[DateRange],
        bbox: BBox,
        sensors: SensorConfig,
        progress: bool = True,
    ) -> pd.DataFrame:
        """Fetch every platform over every chunked date range."""
        frames: list[pd.DataFrame] = []
        total = len(ranges) * len(sensors.platforms)
        done = 0
        skipped: dict[str, int] = {}

        for platform in sensors.platforms:
            for r in ranges:
                done += 1
                source = self.resolve_source(platform, r.start, sensors)
                if source is None:
                    skipped[platform] = skipped.get(platform, 0) + 1
                    continue
                block = self.fetch_block(source, bbox, r.start, r.n_days)
                if not block.empty:
                    block["platform_group"] = platform
                    frames.append(block)
                if progress and done % 25 == 0:
                    log.info(
                        "  fetched %d/%d blocks (%d rows, %d cached)",
                        done,
                        total,
                        self.stats["rows"],
                        self.stats["cache_hits"],
                    )

        for platform, n in skipped.items():
            log.warning(
                "%s: %d date blocks had no covering dataset (outside archive range)",
                platform,
                n,
            )
        if not frames:
            return _empty_frame()
        out = pd.concat(frames, ignore_index=True)
        return out.drop_duplicates(
            subset=["latitude", "longitude", "acq_date", "acq_time", "source"]
        )

    # -- plumbing ----------------------------------------------------------

    def _get_text(self, url: str, weight: int = 1) -> str:
        last_exc: Exception | None = None
        for attempt in range(self.cfg.max_retries):
            self.limiter.acquire(weight)
            try:
                r = self.session.get(url, timeout=self.cfg.timeout_s)
            except requests.RequestException as exc:
                last_exc = exc
                wait = 2 ** attempt * 3
                log.warning(
                    "FIRMS connection failed (%s), retry %d/%d in %d s",
                    exc, attempt + 1, self.cfg.max_retries, wait,
                )
                time.sleep(wait)
                continue

            if r.status_code == 429:
                wait = min(60 * (attempt + 1), 600)
                log.warning("FIRMS returned 429; backing off %d s", wait)
                time.sleep(wait)
                continue

            # A 4xx is a rejected request, not a transient fault. Retrying it
            # wastes time and quota, and FIRMS explains itself in the body -
            # which raise_for_status() would discard.
            if 400 <= r.status_code < 500:
                # FIRMS publishes its real DAY_RANGE ceiling in the body when it
                # rejects one. Surface it as a typed error so the caller can
                # re-split instead of aborting the run.
                limit = _parse_day_range_limit(r.text)
                if limit:
                    raise DayRangeError(
                        limit,
                        f"FIRMS accepts a maximum DAY_RANGE of {limit}. "
                        f"It said: {r.text[:120]!r}",
                    )
                raise FirmsError(_explain_http_error(r, url, self.map_key))

            if r.status_code >= 500:
                last_exc = requests.HTTPError(f"{r.status_code} from FIRMS")
                wait = 2 ** attempt * 3
                log.warning(
                    "FIRMS server error %d, retry %d/%d in %d s",
                    r.status_code, attempt + 1, self.cfg.max_retries, wait,
                )
                time.sleep(wait)
                continue

            text = r.text
            _guard_response(text, url, self.map_key)
            return text

        raise FirmsError(
            f"FIRMS request failed after {self.cfg.max_retries} attempts: {last_exc}. "
            "Check network access to firms.modaps.eosdis.nasa.gov."
        )

    # -- diagnostics -------------------------------------------------------

    def probe(self, sources: list[str], bbox: BBox, day: date) -> pd.DataFrame:
        """Issue one small request per source and report exactly what came back.

        Cheap way to find out which datasets actually serve a given date and
        area before committing to a full extraction.
        """
        rows = []
        for src in sources:
            url = (
                f"{BASE}/api/area/csv/{self._require_key()}/{src}/"
                f"{bbox.as_param()}/1/{day:%Y-%m-%d}"
            )
            try:
                r = self.session.get(url, timeout=self.cfg.timeout_s)
                body = r.text.strip()
                head = body[:160].replace("\n", " | ")
                ok = r.status_code == 200 and (
                    body.lower().startswith(("latitude", "country_id"))
                )
                n = max(len(body.splitlines()) - 1, 0) if ok else 0
                rows.append(
                    {
                        "source": src,
                        "http": r.status_code,
                        "usable": ok,
                        "rows": n,
                        "response": "" if ok else head,
                    }
                )
            except requests.RequestException as exc:
                rows.append(
                    {"source": src, "http": None, "usable": False, "rows": 0,
                     "response": str(exc)[:160]}
                )
        return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# response handling
# --------------------------------------------------------------------------


def _explain_http_error(r, url: str, key: str | None) -> str:
    """Turn a FIRMS 4xx into something actionable."""
    safe_url = url.replace(key, "<MAP_KEY>") if key else url
    body = (r.text or "").strip()[:300].replace("\n", " ")
    source = ""
    parts = safe_url.split("/api/area/csv/")
    if len(parts) == 2:
        bits = parts[1].split("/")
        if len(bits) > 1:
            source = bits[1]

    msg = [f"FIRMS returned HTTP {r.status_code} for source '{source or 'unknown'}'."]
    if body:
        msg.append(f"FIRMS said: {body!r}")

    low = body.lower()
    if "map_key" in low or "key" in low and "invalid" in low:
        msg.append(
            f"The MAP_KEY looks wrong. Verify it at {BASE}/api/map_key/ and check "
            "for stray quotes or whitespace in FIRMS_MAP_KEY."
        )
    elif "no data" in low or "processing" in low:
        msg.append(
            f"That date is outside '{source}' coverage. Standard Processing runs "
            "at least three months behind and each dataset has its own start "
            "date. Run `firepipe availability` to see the real ranges, and "
            "`firepipe probe -c <config>` to test each source directly."
        )
    else:
        msg.append(
            "Run `firepipe probe -c <config>` to test each source against this "
            "area and date, and `firepipe availability` for dataset coverage."
        )
    msg.append(f"Request: {safe_url}")
    return " ".join(msg)


def _parse_day_range_limit(text: str) -> int | None:
    """Read the ceiling out of 'Invalid day range. Expects [1..5].'"""
    if "day range" not in text.lower():
        return None
    m = re.search(r"\[\s*\d+\s*\.\.\s*(\d+)\s*\]", text)
    return int(m.group(1)) if m else None


def _guard_response(text: str, url: str, key: str | None) -> None:
    head = text[:400].strip().lower()
    if head.startswith("latitude") or head.startswith("country_id") or head.startswith(
        "data_id"
    ) or head.startswith("sensor"):
        return
    safe_url = url.replace(key, "<MAP_KEY>") if key else url
    if "invalid" in head and "key" in head:
        raise FirmsError(
            "FIRMS rejected the MAP_KEY. Verify it at "
            f"{BASE}/api/map_key/ . Request was {safe_url}"
        )
    if "transaction" in head or "limit" in head:
        raise FirmsError(
            "FIRMS transaction limit hit (5,000 per 10 minutes). Wait 10 minutes, "
            "or lower firms.requests_per_10min in the config. Cached blocks are "
            "reused on the next run, so restarting is cheap."
        )
    if not text.strip():
        return  # genuinely empty block
    raise FirmsError(
        f"Unexpected FIRMS response for {safe_url}: {text[:200]!r}"
    )


def _parse_csv(text: str, source: str) -> pd.DataFrame:
    if not text.strip():
        return pd.DataFrame()
    df = pd.read_csv(io.StringIO(text), dtype={"acq_time": str})
    if df.empty:
        return df
    df["source"] = source
    return df


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=CANONICAL_COLUMNS + ["platform_group"])


def _normalise(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """Map MODIS and VIIRS column sets onto one schema."""
    out = df.copy()
    out.columns = [c.strip().lower() for c in out.columns]

    if "brightness" not in out and "bright_ti4" in out:
        out["brightness"] = out["bright_ti4"]
    if "brightness_secondary" not in out:
        for c in ("bright_t31", "bright_ti5"):
            if c in out:
                out["brightness_secondary"] = out[c]
                break

    out["source"] = source
    platform = "MODIS" if source.startswith("MODIS") else "VIIRS"
    out["platform"] = platform
    if source.startswith("MODIS"):
        group = "MODIS"
    elif "SNPP" in source:
        group = "VIIRS_SNPP"
    elif "NOAA20" in source:
        group = "VIIRS_NOAA20"
    else:
        group = "VIIRS_NOAA21"
    out["sensor"] = SENSOR_LABELS.get(group, group)

    for col in CANONICAL_COLUMNS:
        if col not in out:
            out[col] = pd.NA

    # VIIRS reports confidence as a class ('h'/'n'/'l'), MODIS as 0-100. Mixing
    # them in one object column breaks Arrow on write, so the canonical column
    # is always text and the two interpretations get their own typed columns.
    out["confidence"] = out["confidence"].astype(str).str.strip()
    # 'version' is 2.0 for Standard Processing but '2.0NRT' for Near Real-Time,
    # so it is text for the same reason confidence is.
    out["version"] = out["version"].astype(str).str.strip()
    out["confidence_class"] = out["confidence"].str.lower().where(
        out["confidence"].str.lower().isin(["h", "n", "l"])
    )
    out["confidence_value"] = pd.to_numeric(out["confidence"], errors="coerce")

    out["acq_date"] = pd.to_datetime(out["acq_date"], errors="coerce").dt.date
    out["acq_time"] = out["acq_time"].astype(str).str.zfill(4)
    for col in ("latitude", "longitude", "frp", "brightness", "brightness_secondary"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out[CANONICAL_COLUMNS]


# --------------------------------------------------------------------------
# offline ingestion
# --------------------------------------------------------------------------


def ingest_csv(path: str | Path, platform_hint: str | None = None) -> pd.DataFrame:
    """Load an existing FIRMS export (or an earlier pipeline output).

    Lets a previously downloaded archive be pushed through exactly the same
    filtering, masking and plotting path as a live API pull.
    """
    df = pd.read_csv(path, dtype={"acq_time": str}, low_memory=False)
    df.columns = [c.strip().lower() for c in df.columns]

    if "source" not in df and platform_hint:
        df["source"] = platform_hint
    if "source" not in df:
        raise FirmsError(
            "CSV has no 'source' column and no platform_hint was given; cannot "
            "tell MODIS rows from VIIRS rows."
        )

    # Map legacy short source labels onto dataset ids.
    legacy = {
        "modis": "MODIS_SP",
        "suomi": "VIIRS_SNPP_SP",
        "viirs_snpp": "VIIRS_SNPP_SP",
        "viirs_j1": "VIIRS_NOAA20_NRT",
        "noaa20": "VIIRS_NOAA20_NRT",
        "viirs_j2": "VIIRS_NOAA21_NRT",
    }
    df["source"] = (
        df["source"].astype(str).str.strip().str.lower().map(legacy).fillna(df["source"])
    )

    frames = [_normalise(g, str(src)) for src, g in df.groupby("source", dropna=False)]
    out = pd.concat(frames, ignore_index=True)
    out["platform_group"] = out["sensor"].map(
        {v: k for k, v in SENSOR_LABELS.items()}
    ).fillna(out["platform"])
    return out


def parse_acq_datetime(df: pd.DataFrame) -> pd.Series:
    """Combine acq_date and HHMM acq_time into a single UTC timestamp."""
    t = df["acq_time"].astype(str).str.zfill(4)
    return pd.to_datetime(
        df["acq_date"].astype(str) + " " + t.str[:2] + ":" + t.str[2:],
        errors="coerce",
        utc=True,
    )


def since(days: int) -> date:
    return (datetime.utcnow() - timedelta(days=days)).date()
