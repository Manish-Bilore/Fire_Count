"""MCP server exposing the pipeline as tools over stdio.

    python -m firepipe serve -c config.yaml

Or register with an MCP client:

    {"command": "python", "args": ["-m", "firepipe", "serve", "-c", "config.yaml"]}

Design notes:
* Tools are prefixed ``firms_`` and named for the action they perform.
* Read-only tools are marked as such; ``firms_run_pipeline`` is the only one
  that writes, and it is idempotent for a given config.
* Every tool returns compact structured data plus a short text summary, so an
  agent does not have to parse a wall of prose to act on the result.
* Long tables are truncated with an explicit ``truncated`` flag and a pointer
  to the file on disk, rather than flooding the context window.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, Any, Literal

import pandas as pd
from pydantic import Field

try:
    # MCP SDK >= 2.0
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations
except ImportError:  # pragma: no cover - SDK 1.x fallback
    try:
        from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore
        from mcp.types import ToolAnnotations  # type: ignore
    except ImportError as exc:
        raise SystemExit(
            "The MCP server needs the MCP SDK: pip install 'mcp[cli]'"
        ) from exc

from .aoi import list_layers, load_regions
from .config import Config, DEFAULT_CONFIG_YAML
from .firms import FirmsClient, FirmsError
from .pipeline import Pipeline, detection_density
from .plots import render
from .plots_simple import render_simple
from .seasons import SeasonCalendar

log = logging.getLogger("firepipe.mcp")

mcp = MCPServer(
    "firepipe",
    instructions=(
        "Extract NASA FIRMS active-fire detections for admin regions defined in "
        "GeoPackages, apply a cropland mask, split by agricultural season, and "
        "render figures. Typical order: firms_list_gpkg_layers -> "
        "firms_describe_config -> firms_plan_extraction -> firms_run_pipeline -> "
        "firms_summarise_detections / firms_get_qc_report."
    ),
)


def _ann(**kw) -> ToolAnnotations:
    """ToolAnnotations from snake_case kwargs, tolerant of SDK field renames."""
    fields = set(ToolAnnotations.model_fields)
    return ToolAnnotations(**{k: v for k, v in kw.items() if k in fields})

STATE: dict[str, Any] = {"config_path": None, "detections": None, "regions": None}
MAX_ROWS = 200


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _load_config(config_path: str | None) -> Config:
    path = config_path or STATE.get("config_path")
    if not path:
        raise ValueError(
            "No config supplied. Pass config_path, or start the server with "
            "`-c config.yaml`. Use firms_write_template_config to create one."
        )
    return Config.from_yaml(path)


def _table(df: pd.DataFrame, limit: int = MAX_ROWS) -> dict:
    out = {
        "n_rows": int(len(df)),
        "columns": list(df.columns),
        "rows": json.loads(
            df.head(limit).to_json(orient="records", date_format="iso")
        ),
    }
    if len(df) > limit:
        out["truncated"] = True
        out["note"] = f"Showing the first {limit} of {len(df):,} rows."
    return out


def _detections(config_path: str | None) -> tuple[Config, pd.DataFrame]:
    cfg = _load_config(config_path)
    cached = STATE.get("detections")
    if cached is not None:
        return cfg, cached
    path = cfg.out_path / "data" / "detections.parquet"
    if not path.exists():
        raise ValueError(
            f"No detections at {path}. Run firms_run_pipeline first (or point "
            "config.out_dir at an existing run)."
        )
    df = pd.read_parquet(path)
    STATE["detections"] = df
    return cfg, df


# --------------------------------------------------------------------------
# configuration and discovery
# --------------------------------------------------------------------------


@mcp.tool(annotations=_ann(read_only_hint=False, destructive_hint=False, idempotent_hint=True))
def firms_write_template_config(
    path: Annotated[str, Field(description="Where to write the YAML, e.g. './config.yaml'")],
    overwrite: Annotated[bool, Field(description="Replace an existing file")] = False,
) -> dict:
    """Write a starter pipeline config covering regions, seasons, sensors and mask.

    Returns the path written and the YAML body, so it can be edited in place.
    """
    p = Path(path).expanduser()
    if p.exists() and not overwrite:
        return {
            "ok": False,
            "error": f"{p} exists. Call again with overwrite=true, or edit it directly.",
        }
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(DEFAULT_CONFIG_YAML)
    return {"ok": True, "path": str(p), "yaml": DEFAULT_CONFIG_YAML}


@mcp.tool(annotations=_ann(read_only_hint=True, open_world_hint=False))
def firms_describe_config(
    config_path: Annotated[str | None, Field(description="Path to a pipeline YAML")] = None,
) -> dict:
    """Summarise a config: regions, season windows, sensors, mask and date span."""
    cfg = _load_config(config_path)
    cal = SeasonCalendar(cfg.seasons)
    return {
        "project": cfg.project,
        "date_range": [str(cfg.start_date), str(cfg.end_date)],
        "regions": [
            {"name": r.name, "gpkg": r.gpkg, "admin_layer": r.admin_layer}
            for r in cfg.regions
        ],
        "seasons": cal.definition_text(),
        "excluded_months": cal.exclusion_text(),
        "season_table": _table(cal.qc_table()),
        "platforms": cfg.sensors.platforms,
        "headline_platform_group": cfg.sensors.headline_platform_group,
        "confidence": {
            "viirs_classes": cfg.sensors.viirs_confidence,
            "modis_min": cfg.sensors.modis_confidence_min,
        },
        "mask": cfg.mask.description,
        "out_dir": str(cfg.out_path),
        "config_fingerprint": cfg.fingerprint,
    }


@mcp.tool(annotations=_ann(read_only_hint=True, open_world_hint=False))
def firms_list_gpkg_layers(
    gpkg_path: Annotated[str, Field(description="Path to a .gpkg file")],
) -> dict:
    """List layers, geometry types, feature counts and fields in a GeoPackage.

    Use this before writing a region config, to confirm the boundary layer name
    and the district field name.
    """
    return _table(list_layers(gpkg_path))


# --------------------------------------------------------------------------
# FIRMS account and archive
# --------------------------------------------------------------------------


@mcp.tool(annotations=_ann(read_only_hint=True, open_world_hint=True))
def firms_check_map_key(
    config_path: Annotated[str | None, Field(description="Path to a pipeline YAML")] = None,
    map_key: Annotated[str | None, Field(description="Override the MAP_KEY")] = None,
) -> dict:
    """Check the FIRMS MAP_KEY and how many transactions it has used.

    The allowance is 5,000 transactions per rolling 10-minute window.
    """
    cfg = _load_config(config_path) if (config_path or STATE.get("config_path")) else None
    client = FirmsClient(cfg.firms if cfg else __import__("firepipe").FirmsConfig(), map_key=map_key)
    try:
        return {"ok": True, "status": client.map_key_status()}
    except FirmsError as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=_ann(read_only_hint=True, open_world_hint=True))
def firms_data_availability(
    config_path: Annotated[str | None, Field(description="Path to a pipeline YAML")] = None,
    map_key: Annotated[str | None, Field(description="Override the MAP_KEY")] = None,
) -> dict:
    """Date coverage of each FIRMS dataset (MODIS/VIIRS, SP and NRT).

    Use this to check whether a requested date range is inside the Standard
    Processing archive or only in the Near Real-Time tail.
    """
    cfg = _load_config(config_path) if (config_path or STATE.get("config_path")) else None
    client = FirmsClient(cfg.firms if cfg else __import__("firepipe").FirmsConfig(), map_key=map_key)
    return _table(client.data_availability())


# --------------------------------------------------------------------------
# planning and execution
# --------------------------------------------------------------------------


@mcp.tool(annotations=_ann(read_only_hint=True, open_world_hint=False))
def firms_plan_extraction(
    config_path: Annotated[str | None, Field(description="Path to a pipeline YAML")] = None,
) -> dict:
    """Dry run: how many API requests this config implies, and over which bbox.

    Nothing is downloaded. Run this before firms_run_pipeline to confirm the
    workload fits inside the MAP_KEY allowance.
    """
    cfg = _load_config(config_path)
    pipe = Pipeline(cfg)
    plan = pipe.plan()
    cal = SeasonCalendar(cfg.seasons)
    return {
        "seasons": cal.definition_text(),
        "in_season_days": int(plan["in_season_days"].iloc[0]),
        "total_api_requests": int(plan.attrs["total_requests"]),
        "bbox_efficiency": plan.attrs["bbox_efficiency"],
        "bbox_strategy": cfg.firms.bbox_strategy,
        "plan": _table(plan),
        "note": (
            "FIRMS allows 5,000 transactions per 10 minutes. Retrieved blocks are "
            "cached on disk, so an interrupted run resumes cheaply."
        ),
    }


@mcp.tool(annotations=_ann(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True))
def firms_run_pipeline(
    config_path: Annotated[str | None, Field(description="Path to a pipeline YAML")] = None,
    from_csv: Annotated[
        str | None,
        Field(description="Ingest an existing FIRMS export instead of calling the API"),
    ] = None,
    render_figures: Annotated[bool, Field(description="Render the figure suite")] = True,
    map_key: Annotated[str | None, Field(description="Override the MAP_KEY")] = None,
) -> dict:
    """Run the full extraction: fetch, clip to admin units, filter, mask, seasons.

    Writes detections (parquet + csv), QC tables, a filter funnel and a manifest
    under the config's out_dir, then optionally renders the figures. Returns the
    funnel and per-region counts, not the detections themselves.
    """
    cfg = _load_config(config_path)
    pipe = Pipeline(cfg, map_key=map_key)
    df = pipe.run(from_csv=from_csv)
    STATE["detections"] = df
    STATE["regions"] = pipe.regions

    figures: list[str] = []
    if render_figures and not df.empty and cfg.plots.enabled:
        figures = [str(p) for p in render(cfg, df, regions=pipe.regions)]

    return {
        "ok": bool(len(df)),
        "n_detections": int(len(df)),
        "out_dir": str(cfg.out_path),
        "funnel": pipe.funnel.rows,
        "by_region": json.loads(
            df.groupby("region").size().rename("detections").reset_index().to_json(
                orient="records"
            )
        )
        if not df.empty
        else [],
        "figures": figures,
        "manifest": str(cfg.out_path / "manifest.json"),
    }


@mcp.tool(annotations=_ann(read_only_hint=False, destructive_hint=False, idempotent_hint=True))
def firms_render_figures(
    config_path: Annotated[str | None, Field(description="Path to a pipeline YAML")] = None,
    figures: Annotated[
        list[str] | None,
        Field(description="Figure ids to render, e.g. ['01','08','20']; omit for all"),
    ] = None,
    style: Annotated[
        Literal["full", "simple", "both"],
        Field(
            description=(
                "'full': annotated figures in figs/, each stating its own sensor, "
                "confidence, mask and season filter. 'simple': no caption, subtitle "
                "or mean line, written to simple_plots/ for slides - these carry no "
                "method text, so quote manifest.json alongside them."
            )
        ),
    ] = "full",
) -> dict:
    """Re-render figures from an existing run without re-fetching anything.

    Useful after changing season labels, the headline platform, or plot options.
    """
    cfg, df = _detections(config_path)
    if figures:
        cfg.plots.figures = figures
    regions = STATE.get("regions") or load_regions(cfg.regions)
    STATE["regions"] = regions
    out: dict[str, Any] = {"ok": True}
    if style in ("full", "both"):
        paths = render(cfg, df, regions=regions)
        out["annotated"] = {"n_files": len(paths), "dir": str(cfg.out_path / "figs")}
    if style in ("simple", "both"):
        paths = render_simple(cfg, df, regions=regions)
        out["simple"] = {
            "n_files": len(paths),
            "dir": str(cfg.out_path / "simple_plots"),
            "note": "No method text on these; cite manifest.json with them.",
        }
    return out


# --------------------------------------------------------------------------
# querying a completed run
# --------------------------------------------------------------------------


@mcp.tool(annotations=_ann(read_only_hint=True, open_world_hint=False))
def firms_summarise_detections(
    config_path: Annotated[str | None, Field(description="Path to a pipeline YAML")] = None,
    group_by: Annotated[
        list[Literal["region", "season_year", "season", "month", "platform", "sensor", "admin_unit"]],
        Field(description="Columns to group by, in order"),
    ] = ["region", "season_year", "season"],
    region: Annotated[str | None, Field(description="Restrict to one region")] = None,
    season: Annotated[str | None, Field(description="Restrict to one season")] = None,
    top_n: Annotated[int, Field(description="Keep only the largest N groups", ge=1, le=500)] = 100,
) -> dict:
    """Aggregate a completed run: detection counts and summed FRP by any grouping.

    Answers questions like "which districts burned most in Kharif 2024" without
    loading the detection table into context.
    """
    cfg, df = _detections(config_path)
    if region:
        df = df[df["region"] == region]
    if season:
        df = df[df["season"] == season]
    if df.empty:
        return {"n_rows": 0, "note": "No detections match those filters."}

    agg = (
        df.groupby(group_by, observed=True)
        .agg(detections=("acq_date", "size"), total_frp=("frp", "sum"))
        .reset_index()
        .sort_values("detections", ascending=False)
        .head(top_n)
    )
    agg["total_frp"] = agg["total_frp"].round(1)
    return _table(agg)


@mcp.tool(annotations=_ann(read_only_hint=True, open_world_hint=False))
def firms_get_qc_report(
    config_path: Annotated[str | None, Field(description="Path to a pipeline YAML")] = None,
) -> dict:
    """The checks that can invalidate a headline trend, from the last run.

    Includes the filter funnel, platform mix by year, per-sensor availability,
    record extent per year, and the season definition in force.
    """
    cfg = _load_config(config_path)
    qc_dir = cfg.out_path / "qc"
    if not qc_dir.exists():
        raise ValueError(f"No QC directory at {qc_dir}. Run firms_run_pipeline first.")
    out: dict[str, Any] = {"qc_dir": str(qc_dir)}
    for f in sorted(qc_dir.glob("*.csv")):
        out[f.stem] = _table(pd.read_csv(f), limit=60)
    manifest = cfg.out_path / "manifest.json"
    if manifest.exists():
        out["manifest"] = json.loads(manifest.read_text())
    return out


@mcp.tool(annotations=_ann(read_only_hint=True, open_world_hint=False))
def firms_detection_density(
    config_path: Annotated[str | None, Field(description="Path to a pipeline YAML")] = None,
    top_n: Annotated[int, Field(description="Rows to return", ge=1, le=500)] = 50,
) -> dict:
    """Detections per 1,000 km2 per admin unit - comparable across districts.

    Raw counts favour large districts; this normalises them by polygon area.
    """
    cfg, df = _detections(config_path)
    regions = STATE.get("regions") or load_regions(cfg.regions)
    STATE["regions"] = regions
    dens = detection_density(df, regions).sort_values(
        "fires_per_1000km2", ascending=False
    )
    return _table(dens, limit=top_n)


# --------------------------------------------------------------------------


def main(config_path: str | None = None) -> None:
    logging.basicConfig(level=logging.INFO)
    if config_path:
        STATE["config_path"] = str(Path(config_path).expanduser())
        log.info("Default config: %s", STATE["config_path"])
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
