"""End-to-end orchestration: FIRMS -> admin clip -> confidence -> mask -> seasons.

Every stage records how many rows it removed. The funnel table is written next
to the data, so any figure can be traced back to the filter that produced it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .aoi import Region, admin_area_km2, bbox_efficiency, load_regions, union_bbox, attach_region
from .config import Config
from .firms import BBox, FirmsClient, ingest_csv, parse_acq_datetime
from .masks import build_mask, class_names
from .seasons import SeasonCalendar

log = logging.getLogger("firepipe.pipeline")


@dataclass
class Funnel:
    rows: list[dict] = field(default_factory=list)

    def record(self, stage: str, n: int, note: str = "") -> None:
        prev = self.rows[-1]["n_records"] if self.rows else n
        self.rows.append(
            {
                "stage": stage,
                "n_records": int(n),
                "removed": int(prev - n),
                "retained_pct": round(100 * n / prev, 2) if prev else 100.0,
                "note": note,
            }
        )

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows)


class Pipeline:
    def __init__(self, cfg: Config, map_key: str | None = None):
        self.cfg = cfg
        self.calendar = SeasonCalendar(cfg.seasons)
        self.client = FirmsClient(cfg.firms, map_key=map_key)
        self.regions: list[Region] = []
        self.funnel = Funnel()
        self.manifest: dict = {}

    # -- stages ------------------------------------------------------------

    def load_regions(self) -> list[Region]:
        if not self.regions:
            log.info("Loading %d region(s)", len(self.cfg.regions))
            self.regions = load_regions(self.cfg.regions)
            for r in self.regions:
                log.info(
                    "  %s: bbox %s, %d admin units",
                    r.name,
                    r.bbox.as_param(),
                    r.n_admin,
                )
        return self.regions

    def plan(self) -> pd.DataFrame:
        """Dry-run: the exact API workload this config implies."""
        regions = self.load_regions()
        ranges = self.calendar.fetch_ranges(self.cfg.start_date, self.cfg.end_date)
        chunks = self.calendar.chunk(ranges, self.cfg.firms.max_day_range)
        n_platforms = len(self.cfg.sensors.platforms)
        boxes = (
            [union_bbox(regions)]
            if self.cfg.firms.bbox_strategy == "union"
            else [r.bbox for r in regions]
        )
        rows = []
        for bb in boxes:
            rows.append(
                {
                    "bbox": bb.as_param(),
                    "in_season_days": sum(r.n_days for r in ranges),
                    "blocks_per_platform": len(chunks),
                    "platforms": n_platforms,
                    "api_requests": len(chunks) * n_platforms,
                }
            )
        df = pd.DataFrame(rows)
        df.attrs["bbox_efficiency"] = round(bbox_efficiency(regions), 3)
        df.attrs["total_requests"] = int(df["api_requests"].sum())
        return df

    def fetch(self, from_csv: str | Path | None = None) -> pd.DataFrame:
        regions = self.load_regions()

        if from_csv:
            log.info("Ingesting existing archive: %s", from_csv)
            raw = ingest_csv(from_csv)
            self.funnel.record("ingested_from_csv", len(raw), str(from_csv))
            return raw

        ranges = self.calendar.fetch_ranges(self.cfg.start_date, self.cfg.end_date)
        chunks = self.calendar.chunk(ranges, self.cfg.firms.max_day_range)
        log.info(
            "Season windows imply %d in-season days -> %d API blocks per platform",
            sum(r.n_days for r in ranges),
            len(chunks),
        )

        if self.cfg.firms.bbox_strategy == "union":
            bb = union_bbox(regions)
            log.info(
                "Union bbox %s (efficiency %.2f)", bb.as_param(), bbox_efficiency(regions)
            )
            raw = self.client.fetch_ranges(chunks, bb, self.cfg.sensors)
        else:
            frames = [
                self.client.fetch_ranges(chunks, r.bbox, self.cfg.sensors)
                for r in regions
            ]
            raw = pd.concat(frames, ignore_index=True).drop_duplicates(
                subset=["latitude", "longitude", "acq_date", "acq_time", "source"]
            )

        log.info(
            "FIRMS: %d requests, %d cache hits, %d raw detections",
            self.client.stats["requests"],
            self.client.stats["cache_hits"],
            len(raw),
        )
        self.funnel.record("firms_raw", len(raw), "detections returned by the API")
        return raw

    def clip_to_regions(self, raw: pd.DataFrame) -> pd.DataFrame:
        frames = [attach_region(raw, r) for r in self.load_regions()]
        out = pd.concat(frames, ignore_index=True) if frames else raw.iloc[:0]
        self.funnel.record(
            "within_admin_boundary", len(out), "clipped to region polygons"
        )
        return out

    def filter_confidence(self, df: pd.DataFrame) -> pd.DataFrame:
        s = self.cfg.sensors
        conf_chr = (
            df["confidence_class"]
            if "confidence_class" in df
            else df["confidence"].astype(str).str.strip().str.lower()
        )
        conf_num = (
            df["confidence_value"]
            if "confidence_value" in df
            else pd.to_numeric(df["confidence"], errors="coerce")
        )

        is_modis = df["platform"].eq("MODIS")
        keep = np.where(
            is_modis,
            conf_num.ge(s.modis_confidence_min).fillna(False),
            conf_chr.isin(s.viirs_confidence),
        )
        out = df[keep].copy()
        self.funnel.record(
            "confidence_filter",
            len(out),
            f"VIIRS in {s.viirs_confidence}; MODIS >= {s.modis_confidence_min}",
        )

        if s.daynight != "both":
            out = out[out["daynight"].astype(str).str.upper().eq(s.daynight)]
            self.funnel.record("daynight_filter", len(out), f"daynight == {s.daynight}")
        return out

    def apply_mask(self, df: pd.DataFrame) -> pd.DataFrame:
        mask = build_mask(self.cfg.mask)
        out = mask.apply(df)
        if self.cfg.mask.kind != "none":
            kept = out[out["is_cropland"].fillna(False)].copy()
            self.funnel.record("cropland_mask", len(kept), self.cfg.mask.description)
            return kept
        self.funnel.record("cropland_mask", len(out), "no mask applied")
        return out

    def assign_seasons(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        ts = pd.to_datetime(out["acq_date"])
        out["acq_datetime_utc"] = parse_acq_datetime(out)
        out["year"] = ts.dt.year.astype("int32")
        out["month"] = ts.dt.month.astype("int8")
        out["doy"] = ts.dt.dayofyear.astype("int16")
        out[["season", "season_year"]] = self.calendar.assign(ts)

        if self.cfg.seasons.drop_unassigned:
            before = len(out)
            out = out[out["season"].isin(self.calendar.names)]
            self.funnel.record(
                "season_window",
                len(out),
                self.calendar.definition_text()
                + (f"; dropped {before - len(out)} off-season" if before != len(out) else ""),
            )
        else:
            self.funnel.record("season_window", len(out), "off-season retained")
        return out.reset_index(drop=True)

    # -- driver ------------------------------------------------------------

    def run(self, from_csv: str | Path | None = None) -> pd.DataFrame:
        t0 = datetime.now()
        raw = self.fetch(from_csv=from_csv)
        if raw.empty:
            log.warning("No detections returned; nothing to process.")
            return raw

        df = self.clip_to_regions(raw)
        df = self.filter_confidence(df)
        df = self.assign_seasons(df)  # before masking: Dynamic World needs seasons
        df = self.apply_mask(df)
        df = df.sort_values(["region", "acq_date", "acq_time"]).reset_index(drop=True)

        self._write(df)
        log.info(
            "Pipeline finished in %.1f s: %d analysis-ready detections",
            (datetime.now() - t0).total_seconds(),
            len(df),
        )
        return df

    # -- outputs -----------------------------------------------------------

    def _write(self, df: pd.DataFrame) -> None:
        out = self.cfg.out_path
        data_dir = out / "data"
        qc_dir = out / "qc"
        data_dir.mkdir(parents=True, exist_ok=True)
        qc_dir.mkdir(parents=True, exist_ok=True)

        # CSV first: it always succeeds, so a parquet/Arrow problem can never
        # destroy a run that took an hour of API calls and mask sampling.
        df.to_csv(data_dir / "detections.csv", index=False)
        try:
            df.to_parquet(data_dir / "detections.parquet", index=False)
        except Exception as exc:
            log.warning(
                "Parquet write failed (%s). Retrying with mixed-type columns cast "
                "to text; CSV output is already written and complete.",
                exc,
            )
            safe = df.copy()
            for col in safe.columns:
                if safe[col].dtype == object:
                    types = {type(v) for v in safe[col].dropna().head(1000)}
                    if len(types) > 1:
                        log.info("  casting mixed column '%s' to text", col)
                        safe[col] = safe[col].astype(str)
            safe.to_parquet(data_dir / "detections.parquet", index=False)
        for region, grp in df.groupby("region"):
            slug = _slug(str(region))
            grp.to_csv(data_dir / f"detections_{slug}.csv", index=False)

        self.funnel.to_frame().to_csv(qc_dir / "qc_funnel.csv", index=False)
        self.calendar.qc_table().to_csv(qc_dir / "qc_season_definition.csv", index=False)
        for name, table in qc_tables(df, self.cfg).items():
            table.to_csv(qc_dir / f"{name}.csv", index=False)

        self.manifest = {
            "project": self.cfg.project,
            "generated_utc": datetime.utcnow().isoformat(timespec="seconds"),
            "config_fingerprint": self.cfg.fingerprint,
            "date_range": [str(self.cfg.start_date), str(self.cfg.end_date)],
            "regions": [r.name for r in self.regions],
            "admin_units": {r.name: r.n_admin for r in self.regions},
            "platforms": self.cfg.sensors.platforms,
            "processing": self.cfg.sensors.processing,
            "headline_platform_group": self.cfg.sensors.headline_platform_group,
            "seasons": self.calendar.definition_text(),
            "excluded_months": self.calendar.exclusion_text(),
            "mask": self.cfg.mask.description,
            "n_detections": int(len(df)),
            "firms_stats": self.client.stats,
            "funnel": self.funnel.rows,
        }
        with open(out / "manifest.json", "w") as fh:
            json.dump(self.manifest, fh, indent=2)
        self.cfg.to_yaml(out / "config.resolved.yaml")
        log.info("Wrote outputs to %s", out)


# --------------------------------------------------------------------------
# QC tables
# --------------------------------------------------------------------------


def qc_tables(df: pd.DataFrame, cfg: Config) -> dict[str, pd.DataFrame]:
    """The checks that can invalidate a headline trend, computed every run."""
    tables: dict[str, pd.DataFrame] = {}
    if df.empty:
        return tables

    # Platform mix per year: if this drifts, a pooled trend is contaminated.
    mix = (
        df.groupby(["region", "year", "platform"], observed=True)
        .size()
        .rename("n")
        .reset_index()
    )
    mix["share"] = mix.groupby(["region", "year"])["n"].transform(lambda s: (s / s.sum()).round(3))
    tables["qc_platform_by_year"] = mix

    # Per-satellite availability: a sensor entering mid-record inflates counts.
    tables["qc_sensor_by_year"] = (
        df.groupby(["region", "year", "sensor"], observed=True)
        .size()
        .rename("n")
        .reset_index()
    )

    # Confidence composition of what was retained.
    tables["qc_confidence_composition"] = (
        df.assign(conf=df["confidence"].astype(str))
        .groupby(["platform", "conf"], observed=True)
        .size()
        .rename("n")
        .reset_index()
    )

    # Record extent: exposes partial years before anyone reads a decline.
    extent = (
        df.groupby(["region", "year"])
        .agg(
            first_detection=("acq_date", "min"),
            last_detection=("acq_date", "max"),
            n=("acq_date", "size"),
        )
        .reset_index()
    )
    tables["qc_record_extent"] = extent

    # Land-cover composition of what the mask let through.
    if "land_cover" in df and df["land_cover"].notna().any():
        names = class_names(cfg.mask)
        lc = df.groupby("land_cover", dropna=False).size().rename("n").reset_index()
        lc["class_name"] = lc["land_cover"].map(
            lambda v: names.get(int(v), "unknown") if pd.notna(v) else "no data"
        )
        tables["qc_land_cover"] = lc

    # Season totals: the numbers behind every seasonal figure.
    tables["qc_season_year"] = (
        df.groupby(["region", "season_year", "season"], observed=True)
        .agg(n=("acq_date", "size"), total_frp=("frp", "sum"))
        .reset_index()
    )
    return tables


def detection_density(df: pd.DataFrame, regions: list[Region]) -> pd.DataFrame:
    """Detections per 1,000 km2 per admin unit - comparable across districts."""
    counts = (
        df.groupby(["region", "admin_unit", "season_year", "season"], observed=True)
        .size()
        .rename("fires")
        .reset_index()
    )
    areas = pd.concat(
        [admin_area_km2(r).assign(region=r.name) for r in regions], ignore_index=True
    )
    out = counts.merge(areas, on=["region", "admin_unit"], how="left")
    out["fires_per_1000km2"] = (out["fires"] / out["area_km2"] * 1000).round(2)
    return out


def _slug(s: str) -> str:
    return "".join(c.lower() if c.isalnum() else "_" for c in s).strip("_")
