"""Configuration models for the FIRMS agricultural-fire pipeline.

Every analytical parameter lives here. Captions, subtitles and QC exports are
derived from these objects, never hand-typed, so figure text cannot drift away
from the filter that actually produced the data.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, ClassVar, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

# --------------------------------------------------------------------------
# Seasons
# --------------------------------------------------------------------------


class SeasonWindow(BaseModel):
    """A named calendar window, given as MM-DD anchors.

    ``start`` may be later in the calendar than ``end``; the window then wraps
    across the new year (e.g. a Nov-15 -> Feb-15 season). The *season year* of
    a wrapping window is always the year in which the window opened.
    """

    name: str
    start: str = Field(description="Window opens, 'MM-DD', e.g. '03-01'")
    end: str = Field(description="Window closes inclusive, 'MM-DD', e.g. '06-30'")
    colour: str | None = Field(default=None, description="Hex colour for plots")
    label: str | None = Field(default=None, description="Long label for captions")

    @field_validator("start", "end")
    @classmethod
    def _check_md(cls, v: str) -> str:
        try:
            m, d = v.split("-")
            date(2000, int(m), int(d))  # 2000 is a leap year: allows 02-29
        except Exception as exc:  # pragma: no cover - defensive
            raise ValueError(f"'{v}' is not a valid MM-DD anchor") from exc
        return v

    @property
    def start_md(self) -> tuple[int, int]:
        m, d = self.start.split("-")
        return int(m), int(d)

    @property
    def end_md(self) -> tuple[int, int]:
        m, d = self.end.split("-")
        return int(m), int(d)

    @property
    def wraps(self) -> bool:
        return self.start_md > self.end_md

    @property
    def display_label(self) -> str:
        return self.label or self.name


class SeasonConfig(BaseModel):
    windows: list[SeasonWindow]
    drop_unassigned: bool = Field(
        default=True,
        description=(
            "Drop detections falling outside every window. When False they are "
            "kept and labelled 'Off-season'."
        ),
    )

    @model_validator(mode="after")
    def _unique_names(self) -> "SeasonConfig":
        names = [w.name for w in self.windows]
        if len(names) != len(set(names)):
            raise ValueError(f"Season names must be unique, got {names}")
        if not names:
            raise ValueError("At least one season window is required")
        return self


# --------------------------------------------------------------------------
# Regions
# --------------------------------------------------------------------------


class RegionConfig(BaseModel):
    """One analytical region, backed by a GeoPackage of admin boundaries."""

    name: str
    gpkg: str
    boundary_layer: str = "state_boundary"
    admin_layer: str | None = "district_boundary"
    admin_field: str = "DISTRICT"
    parent_field: str | None = "STATE_UT"
    where: str | None = Field(
        default=None,
        description="Optional pandas .query() expression to subset the admin layer",
    )
    bbox_pad_deg: float = Field(
        default=0.1, ge=0, description="Padding added to the query bbox, degrees"
    )

    @field_validator("gpkg")
    @classmethod
    def _expand(cls, v: str) -> str:
        return str(Path(v).expanduser())


# --------------------------------------------------------------------------
# Sensors / FIRMS
# --------------------------------------------------------------------------

PLATFORM_SOURCES: dict[str, list[str]] = {
    "VIIRS_SNPP": ["VIIRS_SNPP_SP", "VIIRS_SNPP_NRT"],
    "VIIRS_NOAA20": ["VIIRS_NOAA20_SP", "VIIRS_NOAA20_NRT"],
    "VIIRS_NOAA21": ["VIIRS_NOAA21_NRT"],
    "MODIS": ["MODIS_SP", "MODIS_NRT"],
}


class SensorConfig(BaseModel):
    platforms: list[Literal["VIIRS_SNPP", "VIIRS_NOAA20", "VIIRS_NOAA21", "MODIS"]] = [
        "VIIRS_SNPP",
        "VIIRS_NOAA20",
        "MODIS",
    ]
    processing: Literal["auto", "SP", "NRT"] = Field(
        default="auto",
        description=(
            "'auto' routes each date to Standard Processing where the archive "
            "covers it and falls back to NRT for the recent tail."
        ),
    )
    viirs_confidence: list[Literal["h", "n", "l"]] = ["h", "n"]
    modis_confidence_min: int = Field(default=30, ge=0, le=100)
    daynight: Literal["both", "D", "N"] = "both"
    headline_platform_group: Literal["VIIRS", "MODIS", "BOTH"] = Field(
        default="VIIRS",
        description=(
            "Which platform group drives the headline figures. VIIRS and MODIS "
            "footprints (375 m vs 1 km) are not count-comparable; 'BOTH' pools "
            "them anyway and every caption relabels itself accordingly."
        ),
    )

    @property
    def viirs_platforms(self) -> list[str]:
        return [p for p in self.platforms if p.startswith("VIIRS")]


# --------------------------------------------------------------------------
# Cropland mask
# --------------------------------------------------------------------------


class MaskConfig(BaseModel):
    kind: Literal["none", "esa_worldcover", "dynamic_world", "precomputed_raster"] = (
        "esa_worldcover"
    )

    # ESA WorldCover
    esa_version: Literal["v100", "v200"] = "v200"
    esa_year: int = 2021
    esa_remote: bool = Field(
        default=True,
        description="Read tiles over /vsicurl instead of downloading them whole",
    )

    # Dynamic World (needs earthengine-api and an authenticated project)
    dw_project: str | None = None
    dw_reducer: Literal["mode", "any"] = "mode"

    # Precomputed raster
    raster_path: str | None = None

    crop_classes: list[int] = Field(
        default=[40],
        description="ESA WorldCover 40 = cropland; Dynamic World 4 = crops",
    )
    sampling: Literal["point", "fraction"] = Field(
        default="point",
        description=(
            "'point': the pixel under the detection centroid must be cropland. "
            "'fraction': at least min_crop_fraction of the pixels within the "
            "sensor footprint radius must be cropland."
        ),
    )
    min_crop_fraction: float = Field(default=0.5, ge=0, le=1)
    footprint_radius_m: dict[str, float] = Field(
        default={"VIIRS": 187.5, "MODIS": 500.0},
        description="Half the nominal pixel size of each platform group",
    )
    cache_dir: str = "~/.cache/firepipe/masks"
    cache_samples: bool = Field(
        default=True,
        description=(
            "Persist sampled land-cover values by coordinate. Sampling 400k "
            "points over /vsicurl takes ~25 minutes; with the cache a re-run "
            "costs seconds. Keyed by mask source, so changing the mask "
            "invalidates it."
        ),
    )

    @model_validator(mode="after")
    def _check_sources(self) -> "MaskConfig":
        if self.kind == "precomputed_raster" and not self.raster_path:
            raise ValueError("mask.raster_path is required when kind='precomputed_raster'")
        if self.kind == "dynamic_world" and self.crop_classes == [40]:
            # 40 is a WorldCover code; Dynamic World uses 4 for crops.
            self.crop_classes = [4]
        return self

    @property
    def description(self) -> str:
        if self.kind == "none":
            return "No cropland mask applied"
        if self.kind == "esa_worldcover":
            base = (
                f"Cropland mask: ESA WorldCover {self.esa_version} ({self.esa_year}), "
                f"class(es) {self.crop_classes}"
            )
        elif self.kind == "dynamic_world":
            base = (
                f"Cropland mask: Dynamic World V1 seasonal {self.dw_reducer}, "
                f"class(es) {self.crop_classes}"
            )
        else:
            base = f"Cropland mask: precomputed raster {Path(self.raster_path).name}"
        if self.sampling == "fraction":
            base += f"; \u2265{self.min_crop_fraction:.0%} cropland within sensor footprint"
        else:
            base += "; sampled at detection centroid"
        return base


# --------------------------------------------------------------------------
# Output / plots
# --------------------------------------------------------------------------


class DateMarker(BaseModel):
    """A calendar date drawn as a dashed vertical line on date-axis figures."""

    date: str = Field(description="'MM-DD', e.g. '10-15'")
    label: str | None = Field(
        default=None, description="Optional text beside the line; the date itself is always a tick"
    )

    @field_validator("date")
    @classmethod
    def _check_md(cls, v: str) -> str:
        try:
            m, d = v.split("-")
            date(2000, int(m), int(d))
        except Exception as exc:
            raise ValueError(f"date marker '{v}' is not a valid MM-DD anchor") from exc
        return v

    @property
    def md(self) -> tuple[int, int]:
        m, d = self.date.split("-")
        return int(m), int(d)


class PlotConfig(BaseModel):
    enabled: bool = True
    dpi: int = 200
    formats: list[Literal["png", "pdf", "svg"]] = ["png"]
    figures: list[str] = Field(
        default=["all"],
        description="Figure ids to render, or ['all']",
    )
    detail_window: dict[str, str] | None = Field(
        default=None,
        description=(
            "Optional MM-DD sub-window for the 5-day bin figures, e.g. "
            "{'start': '09-15', 'end': '11-30'}. Defaults to each season window."
        ),
    )
    annotate_covid: bool = False
    covid_year: int = 2020
    top_n_districts: int = 15
    date_markers: list[DateMarker] = Field(
        default=[
            DateMarker(date="04-01"),
            DateMarker(date="06-01"),
            DateMarker(date="10-15"),
            DateMarker(date="12-15"),
        ],
        description=(
            "Dashed vertical lines on every calendar-date axis (figures 05, 06, "
            "16, 17, 19). Plain 'MM-DD' strings or {date, label} objects. A "
            "marker is drawn only where it falls inside the plotted window. "
            "Set [] for none."
        ),
    )
    district_map_metric: Literal["density", "count"] = Field(
        default="density",
        description=(
            "Quantity mapped by the per-year district maps (14, 15): detections "
            "per 1,000 km2 of district area, or raw detections. Figure 13 always "
            "shows both."
        ),
    )
    district_map_style: Literal["discrete", "continuous"] = Field(
        default="discrete",
        description=(
            "Colour scale of the per-year district maps (14, 15). 'discrete' "
            "draws equal-width classes from 0 to the rounded maximum; "
            "'continuous' draws a smooth ramp over the same range."
        ),
    )
    district_map_round: int = Field(
        default=500,
        gt=0,
        description=(
            "The top of the colour scale is the maximum value rounded up to the "
            "next multiple of this (e.g. 1,083 -> 1,500). The bottom is always 0."
        ),
    )
    district_map_classes: int = Field(
        default=10,
        ge=2,
        le=20,
        description="Number of equal-width colour classes when district_map_style is 'discrete'.",
    )
    district_map_scales: list[Literal["global", "local"]] = Field(
        default=["global", "local"],
        min_length=1,
        description=(
            "Scales rendered for the individual per-year maps. 'global' shares "
            "one scale across every year of the family (comparable between "
            "years); 'local' rescales to each year's own maximum (shows the "
            "spatial pattern within the year). Each goes to its own subfolder. "
            "The faceted overview always uses the global scale."
        ),
    )
    partial_year_titles: bool = Field(
        default=True,
        description=(
            "Append '(record ends <date>)' to year labels in titles of figures "
            "14-19 when the last detection falls before the window closes. The "
            "test uses the last detection, not the fetched coverage, so it can "
            "flag a complete season whose final days had no detections. The "
            "caption NOTE in full figures is unaffected."
        ),
    )

    @field_validator("date_markers", mode="before")
    @classmethod
    def _markers_from_strings(cls, v):
        if v is None:
            return []
        return [{"date": m} if isinstance(m, str) else m for m in v]


class FirmsConfig(BaseModel):
    map_key: str | None = Field(
        default=None,
        description="FIRMS MAP_KEY. Prefer the FIRMS_MAP_KEY environment variable.",
    )
    cache_dir: str = "~/.cache/firepipe/firms"
    max_day_range: int = Field(
        default=5,
        ge=1,
        le=10,
        description=(
            "Days per API request. FIRMS documents 1..10 but currently enforces "
            "1..5; the client reads the real ceiling from the server's error "
            "message and re-splits automatically if this is set too high."
        ),
    )
    requests_per_10min: int = Field(default=4500, ge=1, le=5000)
    bbox_strategy: Literal["union", "per_region"] = Field(
        default="union",
        description=(
            "'union' issues one set of API calls over the bounding box of all "
            "regions and splits them by spatial join afterwards - far cheaper "
            "in transactions when regions are adjacent."
        ),
    )
    timeout_s: int = 120
    max_retries: int = 4


class Config(BaseModel):
    project: str = "firms_agri"
    out_dir: str = "./output"
    start_date: date
    end_date: date
    regions: list[RegionConfig]
    seasons: SeasonConfig
    sensors: SensorConfig = SensorConfig()
    mask: MaskConfig = MaskConfig()
    firms: FirmsConfig = FirmsConfig()
    plots: PlotConfig = PlotConfig()

    @model_validator(mode="after")
    def _headline_group_is_present(self) -> "Config":
        """The headline group must actually be among the platforms fetched.

        Otherwise every figure filters down to an empty frame and the run
        produces no figures at all, silently.
        """
        plats = self.sensors.platforms
        has_viirs = any(p.startswith("VIIRS") for p in plats)
        has_modis = "MODIS" in plats
        group = self.sensors.headline_platform_group

        if group == "VIIRS" and not has_viirs:
            raise ValueError(
                f"headline_platform_group is 'VIIRS' but platforms is {plats}, which "
                "contains no VIIRS sensor. Every figure would be empty. Set "
                "headline_platform_group: MODIS."
            )
        if group == "MODIS" and not has_modis:
            raise ValueError(
                f"headline_platform_group is 'MODIS' but platforms is {plats}, which "
                "does not include MODIS. Every figure would be empty. Set "
                "headline_platform_group to a VIIRS group."
            )
        if group == "BOTH" and not (has_viirs and has_modis):
            present = "VIIRS" if has_viirs else "MODIS"
            raise ValueError(
                f"headline_platform_group is 'BOTH' but platforms is {plats}. 'BOTH' "
                "means pooling VIIRS with MODIS across instruments, and only "
                f"{present} is present here.\n"
                f"Set headline_platform_group: {present}. Note that a group already "
                "pools every satellite within it, so 'VIIRS' with "
                "[VIIRS_SNPP, VIIRS_NOAA20] sums both satellites - which is what "
                "you want, and is a sounder sum than 'BOTH' because the footprints "
                "match."
            )
        return self

    @model_validator(mode="after")
    def _check_dates(self) -> "Config":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        if not self.regions:
            raise ValueError("At least one region is required")
        return self

    # -- io ---------------------------------------------------------------

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Config":
        with open(Path(path).expanduser()) as fh:
            raw = yaml.safe_load(fh)
        return cls.model_validate(raw)

    REDACTED: ClassVar[str] = "<redacted: set FIRMS_MAP_KEY in the environment>"

    def to_yaml(self, path: str | Path, redact_secrets: bool = True) -> None:
        """Write the resolved config.

        The MAP_KEY is redacted by default. This file is written into every
        output directory and is exactly the kind of thing that gets committed
        to a repository by accident.
        """
        payload = json.loads(self.model_dump_json())
        if redact_secrets and payload.get("firms", {}).get("map_key"):
            payload["firms"]["map_key"] = self.REDACTED
        with open(Path(path).expanduser(), "w") as fh:
            yaml.safe_dump(payload, fh, sort_keys=False, allow_unicode=True)

    @property
    def out_path(self) -> Path:
        p = Path(self.out_dir).expanduser()
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def fingerprint(self) -> str:
        """Short stable hash of the analytical settings, for provenance."""
        payload: dict[str, Any] = json.loads(self.model_dump_json())
        payload.pop("out_dir", None)
        # Figure settings do not change a single detection. Hashing them made
        # every new plot option look like a different analysis and tripped
        # the out_dir overwrite warning on a plain re-run.
        payload.pop("plots", None)
        payload.get("firms", {}).pop("map_key", None)
        blob = json.dumps(payload, sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:12]


DEFAULT_CONFIG_YAML = """\
project: igp_agri_fires
out_dir: ./output
start_date: 2019-01-01
end_date: 2026-12-31

regions:
  - name: Uttar Pradesh
    gpkg: ./gpkg/uttar_pradesh_admin.gpkg
  - name: Punjab
    gpkg: ./gpkg/punjab_admin.gpkg
  - name: Haryana
    gpkg: ./gpkg/haryana_admin.gpkg
  - name: Madhya Pradesh
    gpkg: ./gpkg/madhya_pradesh_admin.gpkg

seasons:
  drop_unassigned: true
  windows:
    - name: Rabi
      start: "03-01"
      end: "06-30"
      colour: "#F0A202"
      label: Rabi (wheat residue)
    - name: Kharif
      start: "10-01"
      end: "12-31"
      colour: "#EE3B33"
      label: Kharif (paddy residue)

sensors:
  platforms: [VIIRS_SNPP, VIIRS_NOAA20, MODIS]
  processing: auto
  viirs_confidence: [h, n]
  modis_confidence_min: 30
  headline_platform_group: VIIRS

mask:
  kind: esa_worldcover
  esa_version: v200
  esa_year: 2021
  crop_classes: [40]
  sampling: point

firms:
  bbox_strategy: union

plots:
  enabled: true
  dpi: 200
  top_n_districts: 15
"""
