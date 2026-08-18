"""Tests for the parts where a silent error would corrupt a headline number."""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from firepipe.config import Config, MaskConfig, SeasonConfig, SeasonWindow, SensorConfig
from firepipe.firms import BBox, _normalise, ingest_csv
from firepipe.seasons import OFF_SEASON, SeasonCalendar

RABI = SeasonWindow(name="Rabi", start="03-01", end="06-30")
KHARIF = SeasonWindow(name="Kharif", start="10-01", end="12-31")
WRAP = SeasonWindow(name="Winter", start="11-15", end="02-15")


def cal(*windows, drop=True) -> SeasonCalendar:
    return SeasonCalendar(SeasonConfig(windows=list(windows), drop_unassigned=drop))


# --------------------------------------------------------------------------
# season windows
# --------------------------------------------------------------------------


def test_wrap_detection():
    assert not RABI.wraps
    assert WRAP.wraps


def test_overlapping_windows_rejected():
    with pytest.raises(ValueError, match="overlap"):
        cal(RABI, SeasonWindow(name="Clash", start="06-01", end="08-31"))


def test_covered_and_excluded_months():
    c = cal(RABI, KHARIF)
    assert c.covered_months() == [3, 4, 5, 6, 10, 11, 12]
    assert c.excluded_months() == [1, 2, 7, 8, 9]
    assert "Jan, Feb, Jul, Aug, Sep" in c.exclusion_text()


def test_assignment_basic():
    c = cal(RABI, KHARIF)
    s = pd.Series(pd.to_datetime(["2024-03-01", "2024-06-30", "2024-07-01", "2024-12-31"]))
    out = c.assign(s)
    assert list(out["season"]) == ["Rabi", "Rabi", OFF_SEASON, "Kharif"]
    assert list(out["season_year"]) == [2024, 2024, 2024, 2024]


def test_wrapping_season_year_is_the_opening_year():
    """A January detection in a Nov->Feb window belongs to the previous season."""
    c = cal(WRAP)
    s = pd.Series(pd.to_datetime(["2024-11-20", "2025-01-10", "2025-02-15", "2025-03-01"]))
    out = c.assign(s)
    assert list(out["season"]) == ["Winter", "Winter", "Winter", OFF_SEASON]
    assert list(out["season_year"]) == [2024, 2024, 2024, 2025]


def test_boundaries_are_inclusive():
    c = cal(RABI)
    out = c.assign(pd.Series(pd.to_datetime(["2024-02-29", "2024-03-01", "2024-06-30", "2024-07-01"])))
    assert list(out["season"]) == [OFF_SEASON, "Rabi", "Rabi", OFF_SEASON]


# --------------------------------------------------------------------------
# fetch scheduling
# --------------------------------------------------------------------------


def test_fetch_ranges_cover_only_in_season_days():
    c = cal(RABI, KHARIF)
    ranges = c.fetch_ranges(date(2024, 1, 1), date(2024, 12, 31))
    assert sum(r.n_days for r in ranges) == 122 + 92
    assert all(r.season in {"Rabi", "Kharif"} for r in ranges)


def test_fetch_ranges_clip_to_requested_span():
    c = cal(RABI)
    ranges = c.fetch_ranges(date(2024, 4, 1), date(2024, 4, 30))
    assert len(ranges) == 1
    assert ranges[0].start == date(2024, 4, 1) and ranges[0].end == date(2024, 4, 30)


def test_chunking_respects_the_api_ceiling():
    c = cal(RABI)
    chunks = c.chunk(c.fetch_ranges(date(2024, 1, 1), date(2024, 12, 31)), max_days=10)
    assert all(ch.n_days <= 10 for ch in chunks)
    assert sum(ch.n_days for ch in chunks) == 122
    # contiguous, no gaps or overlaps
    ordered = sorted(chunks, key=lambda ch: ch.start)
    for a, b in zip(ordered, ordered[1:]):
        assert (b.start - a.end).days == 1


def test_wrapping_window_fetch_crosses_the_year():
    c = cal(WRAP)
    ranges = c.fetch_ranges(date(2024, 11, 1), date(2025, 3, 1))
    covered = {r.start.year for r in ranges} | {r.end.year for r in ranges}
    assert covered == {2024, 2025}


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------


def test_end_before_start_rejected(tmp_path):
    with pytest.raises(ValueError):
        Config(
            start_date=date(2025, 1, 1),
            end_date=date(2024, 1, 1),
            regions=[{"name": "X", "gpkg": "x.gpkg"}],
            seasons=SeasonConfig(windows=[RABI]),
        )


def test_fingerprint_ignores_output_location():
    base = dict(
        start_date=date(2019, 1, 1),
        end_date=date(2025, 12, 31),
        regions=[{"name": "X", "gpkg": "x.gpkg"}],
        seasons=SeasonConfig(windows=[RABI]),
    )
    a = Config(out_dir="./a", **base)
    b = Config(out_dir="./b", **base)
    assert a.fingerprint == b.fingerprint

    c = Config(out_dir="./a", sensors=SensorConfig(modis_confidence_min=80), **base)
    assert c.fingerprint != a.fingerprint


def test_dynamic_world_class_code_is_corrected():
    """40 is a WorldCover code; Dynamic World crops is 4."""
    m = MaskConfig(kind="dynamic_world", crop_classes=[40])
    assert m.crop_classes == [4]


def test_mask_description_tracks_sampling_mode():
    point = MaskConfig(kind="esa_worldcover", sampling="point")
    frac = MaskConfig(kind="esa_worldcover", sampling="fraction", min_crop_fraction=0.6)
    assert "centroid" in point.description
    assert "60%" in frac.description


# --------------------------------------------------------------------------
# FIRMS normalisation
# --------------------------------------------------------------------------


def test_viirs_and_modis_normalise_to_one_schema():
    viirs = pd.DataFrame(
        {
            "latitude": [27.0],
            "longitude": [81.0],
            "bright_ti4": [330.1],
            "bright_ti5": [290.0],
            "acq_date": ["2024-11-05"],
            "acq_time": ["745"],
            "satellite": ["N"],
            "confidence": ["n"],
            "frp": [4.2],
            "daynight": ["D"],
        }
    )
    modis = viirs.rename(columns={"bright_ti4": "brightness", "bright_ti5": "bright_t31"}).assign(
        confidence=[72]
    )
    v = _normalise(viirs, "VIIRS_SNPP_SP")
    m = _normalise(modis, "MODIS_SP")
    assert list(v.columns) == list(m.columns)
    assert v["platform"].iloc[0] == "VIIRS" and m["platform"].iloc[0] == "MODIS"
    assert v["brightness"].iloc[0] == 330.1
    assert v["acq_time"].iloc[0] == "0745"  # zero padded for later parsing


def test_legacy_source_labels_are_mapped(tmp_path):
    csv = tmp_path / "legacy.csv"
    pd.DataFrame(
        {
            "source": ["MODIS", "SUOMI", "VIIRS_J1"],
            "latitude": [27.0, 27.1, 27.2],
            "longitude": [81.0, 81.1, 81.2],
            "acq_date": ["2024-11-05"] * 3,
            "acq_time": ["0745"] * 3,
            "confidence": [72, "n", "h"],
            "frp": [4.2, 3.1, 8.0],
        }
    ).to_csv(csv, index=False)

    out = ingest_csv(csv)
    assert set(out["source"]) == {"MODIS_SP", "VIIRS_SNPP_SP", "VIIRS_NOAA20_NRT"}
    assert set(out["platform"]) == {"MODIS", "VIIRS"}


# --------------------------------------------------------------------------
# geometry helpers
# --------------------------------------------------------------------------


def test_bbox_union_and_padding():
    a = BBox(80, 25, 82, 27)
    b = BBox(75, 29, 78, 31)
    u = a.union(b)
    assert (u.west, u.south, u.east, u.north) == (75, 25, 82, 31)
    p = a.padded(0.5)
    assert p.west == 79.5 and p.north == 27.5
    assert "80.0000,25.0000,82.0000,27.0000" == a.as_param()


def test_bbox_padding_clamps_to_valid_range():
    p = BBox(-179.9, -89.9, 179.9, 89.9).padded(1.0)
    assert (p.west, p.south, p.east, p.north) == (-180.0, -90.0, 180.0, 90.0)


# --------------------------------------------------------------------------
# caption integrity - the audit discipline this pipeline exists to enforce
# --------------------------------------------------------------------------


def _captions(sensors: SensorConfig, seasons: SeasonConfig):
    from firepipe.plots import Captions

    cfg = Config(
        start_date=date(2019, 1, 1),
        end_date=date(2025, 12, 31),
        regions=[{"name": "X", "gpkg": "x.gpkg"}],
        seasons=seasons,
        sensors=sensors,
    )
    return Captions(cfg, SeasonCalendar(seasons), pd.DataFrame())


def test_caption_confidence_follows_the_config():
    seasons = SeasonConfig(windows=[RABI, KHARIF])
    c = _captions(SensorConfig(viirs_confidence=["h"]), seasons)
    assert "{h}" in c.conf_note and "high confidence" in c.sub_tag

    c2 = _captions(SensorConfig(viirs_confidence=["h", "n"]), seasons)
    assert "high + nominal" in c2.sub_tag


def test_caption_flags_pooled_platforms_as_incomparable():
    c = _captions(SensorConfig(headline_platform_group="BOTH"), SeasonConfig(windows=[RABI]))
    assert "POOLED" in c.sensor_note
    assert "not footprint-comparable" in c.sensor_note


def test_caption_season_text_follows_the_windows():
    c = _captions(SensorConfig(), SeasonConfig(windows=[RABI, KHARIF]))
    assert "Rabi = 1 Mar to 30 Jun" in c.season_note
    assert "Kharif = 1 Oct to 31 Dec" in c.season_note
    assert "Jan, Feb, Jul, Aug, Sep" in c.exclusion_note


def test_changing_a_window_changes_the_caption():
    """The regression this pipeline is built to prevent: text drifting from filter."""
    a = _captions(SensorConfig(), SeasonConfig(windows=[RABI, KHARIF]))
    b = _captions(
        SensorConfig(),
        SeasonConfig(windows=[SeasonWindow(name="Rabi", start="01-01", end="06-30"), KHARIF]),
    )
    assert a.season_note != b.season_note
    assert a.exclusion_note != b.exclusion_note


# --------------------------------------------------------------------------
# DAY_RANGE ceiling - FIRMS documents 1..10 but enforces 1..5
# --------------------------------------------------------------------------


class _Resp:
    def __init__(self, code, text):
        self.status_code, self.text = code, text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


_HEADER = (
    "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,"
    "instrument,confidence,version,bright_ti5,frp,daynight\n"
)


def _fake_firms(max_days=5, calls=None):
    """A FIRMS stand-in that rejects over-long day ranges the way the real one does."""

    def get(url, timeout=None):
        if calls is not None:
            calls.append(url)
        days = int(url.rstrip("/").split("/")[-2])
        if days > max_days:
            return _Resp(400, f"Invalid day range. Expects [1..{max_days}].")
        rows = "\n".join(
            f"27.{i},81.{i},330,0.4,0.4,2019-03-0{(i % 5) + 1},0745,N,VIIRS,n,2,290,5.1,D"
            for i in range(days)
        )
        return _Resp(200, _HEADER + rows)

    return get


def test_day_range_limit_parsed_from_server_message():
    from firepipe.firms import _parse_day_range_limit

    assert _parse_day_range_limit("Invalid day range. Expects [1..5].") == 5
    assert _parse_day_range_limit("Invalid day range. Expects [1..10].") == 10
    assert _parse_day_range_limit("latitude,longitude,brightness") is None


def test_oversized_block_is_split_not_failed(tmp_path):
    """A rejected 10-day block must come back as 5+5 with no rows lost."""
    from unittest.mock import patch

    from firepipe.config import FirmsConfig
    from firepipe.firms import BBox, FirmsClient

    calls: list[str] = []
    client = FirmsClient(
        FirmsConfig(cache_dir=str(tmp_path), max_day_range=10), map_key="TESTKEY"
    )
    with patch.object(client.session, "get", side_effect=_fake_firms(5, calls)):
        df = client.fetch_block("VIIRS_SNPP_SP", BBox(76.9, 23.7, 84.7, 30.5), date(2019, 3, 1), 10)

    assert len(df) == 10
    assert client.max_day_range == 5              # ceiling learned from the server
    assert [int(u.rstrip("/").split("/")[-2]) for u in calls] == [10, 5, 5]


def test_learned_ceiling_applies_to_later_blocks(tmp_path):
    """The ceiling is learned once, not rediscovered on every block."""
    from unittest.mock import patch

    from firepipe.config import FirmsConfig
    from firepipe.firms import BBox, FirmsClient

    calls: list[str] = []
    client = FirmsClient(
        FirmsConfig(cache_dir=str(tmp_path), max_day_range=10), map_key="TESTKEY"
    )
    bbox = BBox(76.9, 23.7, 84.7, 30.5)
    with patch.object(client.session, "get", side_effect=_fake_firms(5, calls)):
        client.fetch_block("VIIRS_SNPP_SP", bbox, date(2019, 3, 1), 10)
        n_after_first = len(calls)
        client.fetch_block("VIIRS_SNPP_SP", bbox, date(2019, 4, 1), 10)

    rejected = [u for u in calls[n_after_first:] if int(u.rstrip("/").split("/")[-2]) > 5]
    assert rejected == [], "second block should not repeat the rejected request"


def test_default_day_range_matches_enforced_ceiling():
    from firepipe.config import FirmsConfig

    assert FirmsConfig().max_day_range == 5


# --------------------------------------------------------------------------
# serialisation and mask sampling
# --------------------------------------------------------------------------


def test_mixed_confidence_survives_parquet(tmp_path):
    """VIIRS 'n' and MODIS 72 in one column must not break Arrow."""
    from firepipe.firms import _normalise

    viirs = pd.DataFrame(
        {
            "latitude": [27.0], "longitude": [81.0], "bright_ti4": [330.0],
            "bright_ti5": [290.0], "acq_date": ["2024-11-05"], "acq_time": ["745"],
            "satellite": ["N"], "confidence": ["n"], "frp": [4.2], "daynight": ["D"],
        }
    )
    modis = viirs.rename(
        columns={"bright_ti4": "brightness", "bright_ti5": "bright_t31"}
    ).assign(confidence=[72])

    df = pd.concat(
        [_normalise(viirs, "VIIRS_SNPP_SP"), _normalise(modis, "MODIS_SP")],
        ignore_index=True,
    )
    df.to_parquet(tmp_path / "d.parquet", index=False)   # the original crash

    assert df["confidence_class"].tolist() == ["n", None] or (
        df["confidence_class"].iloc[0] == "n" and pd.isna(df["confidence_class"].iloc[1])
    )
    assert pd.isna(df["confidence_value"].iloc[0])
    assert df["confidence_value"].iloc[1] == 72


def test_mask_dedup_matches_per_point_sampling(tmp_path):
    """Deduplicating repeated coordinates must not change a single value."""
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    from firepipe.config import MaskConfig
    from firepipe.masks import PrecomputedRasterMask

    rng = np.random.default_rng(0)
    arr = rng.choice([10, 30, 40, 50], size=(200, 200)).astype("uint8")
    path = tmp_path / "lc.tif"
    with rasterio.open(
        path, "w", driver="GTiff", height=200, width=200, count=1, dtype="uint8",
        crs="EPSG:4326", transform=from_origin(80.0, 28.0, 0.01, 0.01),
    ) as dst:
        dst.write(arr, 1)

    locs = np.column_stack([rng.uniform(80.05, 81.9, 300), rng.uniform(26.1, 27.9, 300)])
    pick = rng.integers(0, 300, 5000)
    df = pd.DataFrame(
        {"longitude": locs[pick, 0], "latitude": locs[pick, 1], "platform": "VIIRS"}
    )

    mask = PrecomputedRasterMask(
        MaskConfig(kind="precomputed_raster", raster_path=str(path))
    )
    got = mask.classify(df).to_numpy()
    with rasterio.open(path) as src:
        truth = np.array(
            [v[0] for v in src.sample(zip(df.longitude, df.latitude))], dtype=float
        )
    assert np.array_equal(got, truth)


def test_version_column_is_text():
    """'2.0' (SP) and '2.0NRT' (NRT) must not form a mixed-type column."""
    from firepipe.firms import _normalise

    base = dict(latitude=[27.0], longitude=[81.0], bright_ti4=[330.0],
                bright_ti5=[290.0], acq_date=["2024-11-05"], acq_time=["745"],
                satellite=["N"], confidence=["n"], frp=[4.2], daynight=["D"])
    sp = _normalise(pd.DataFrame({**base, "version": [2.0]}), "VIIRS_SNPP_SP")
    nrt = _normalise(pd.DataFrame({**base, "version": ["2.0NRT"]}), "VIIRS_SNPP_NRT")
    combined = pd.concat([sp, nrt], ignore_index=True)
    assert {type(v) for v in combined["version"]} == {str}


def test_figures_render_when_a_platform_year_is_missing(tmp_path):
    """reindex must not push 0 into a text column when a group has no rows."""
    from firepipe.config import Config, SeasonConfig, SeasonWindow
    from firepipe.plots import FigureSuite

    rows = []
    for year in (2019, 2020, 2021):
        for season, mon in (("Rabi", 4), ("Kharif", 11)):
            for plat in ("VIIRS", "MODIS"):
                if plat == "MODIS" and year == 2021:
                    continue                      # platform absent for a year
                if plat == "VIIRS" and year == 2020 and season == "Rabi":
                    continue                      # season absent for a platform
                rows += [
                    dict(region="Uttar Pradesh", season=season, season_year=year,
                         year=year, month=mon, doy=100, platform=plat,
                         sensor=f"{plat} sensor",
                         acq_date=pd.Timestamp(f"{year}-{mon:02d}-15").date(),
                         confidence="n", frp=5.0, admin_unit="SITAPUR",
                         latitude=27.0, longitude=81.0)
                ] * 10
    df = pd.DataFrame(rows)

    cfg = Config(
        start_date=date(2019, 1, 1), end_date=date(2021, 12, 31), out_dir=str(tmp_path),
        regions=[{"name": "Uttar Pradesh", "gpkg": "x.gpkg"}],
        seasons=SeasonConfig(windows=[RABI, KHARIF]),
    )
    paths = FigureSuite(cfg, df).render_all(tmp_path / "figs")
    assert len(paths) >= 15
    assert any("08_platform" in p.stem for p in paths)


def test_mask_sample_cache_is_exact_and_keyed(tmp_path):
    """Cached land-cover values must match fresh sampling, and not leak
    between different mask sources."""
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    from firepipe.config import MaskConfig
    from firepipe.masks import ESAWorldCoverMask, PrecomputedRasterMask

    rng = np.random.default_rng(1)
    arr = rng.choice([10, 30, 40, 50], size=(200, 200)).astype("uint8")
    path = tmp_path / "lc.tif"
    with rasterio.open(
        path, "w", driver="GTiff", height=200, width=200, count=1, dtype="uint8",
        crs="EPSG:4326", transform=from_origin(80.0, 28.0, 0.01, 0.01),
    ) as dst:
        dst.write(arr, 1)

    df = pd.DataFrame({
        "longitude": rng.uniform(80.05, 81.9, 3000),
        "latitude": rng.uniform(26.1, 27.9, 3000),
        "platform": "VIIRS",
    })
    cfg = MaskConfig(
        kind="precomputed_raster", raster_path=str(path), cache_dir=str(tmp_path / "c")
    )

    first = PrecomputedRasterMask(cfg).classify(df).to_numpy()
    second = PrecomputedRasterMask(cfg).classify(df).to_numpy()   # served from cache
    assert np.array_equal(first, second)

    with rasterio.open(path) as src:
        truth = np.array(
            [v[0] for v in src.sample(zip(df.longitude, df.latitude))], dtype=float
        )
    assert np.array_equal(second, truth)

    # a different mask source must get a different cache file
    esa = ESAWorldCoverMask(MaskConfig(kind="esa_worldcover", cache_dir=str(tmp_path / "c")))
    assert esa._cache_key() != PrecomputedRasterMask(cfg)._cache_key()
