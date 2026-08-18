"""Command-line interface: ``python -m firepipe <command>``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from .aoi import list_layers
from .config import DEFAULT_CONFIG_YAML, Config
from .firms import FirmsClient
from .pipeline import Pipeline, detection_density
from .plots import render
from .seasons import SeasonCalendar


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    for noisy in ("matplotlib", "PIL", "fiona", "pyogrio", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _show(df: pd.DataFrame, max_rows: int = 60) -> None:
    with pd.option_context("display.max_rows", max_rows, "display.width", 160):
        print(df.to_string(index=False))


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_init(args) -> int:
    dest = Path(args.output)
    if dest.exists() and not args.force:
        print(f"{dest} already exists; pass --force to overwrite.", file=sys.stderr)
        return 1
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(DEFAULT_CONFIG_YAML)
    print(f"Wrote starter config to {dest}")
    print("Next: edit the gpkg paths, then run  python -m firepipe plan -c", dest)
    return 0


def cmd_layers(args) -> int:
    for gpkg in args.gpkg:
        print(f"\n=== {gpkg} ===")
        _show(list_layers(gpkg))
    return 0


def cmd_key(args) -> int:
    cfg = Config.from_yaml(args.config) if args.config else None
    client = FirmsClient(cfg.firms if cfg else __import__("firepipe").FirmsConfig(), map_key=args.map_key)
    status = client.map_key_status()
    _show(pd.Series(status).rename("value").to_frame().reset_index(names="field"))
    return 0


def cmd_availability(args) -> int:
    cfg = Config.from_yaml(args.config) if args.config else None
    client = FirmsClient(cfg.firms if cfg else __import__("firepipe").FirmsConfig(), map_key=args.map_key)
    _show(client.data_availability())
    return 0


def cmd_plan(args) -> int:
    cfg = Config.from_yaml(args.config)
    pipe = Pipeline(cfg, map_key=args.map_key)
    cal = SeasonCalendar(cfg.seasons)

    print("\nSeason definition")
    _show(cal.qc_table())
    print("\n" + cal.exclusion_text())

    plan = pipe.plan()
    print("\nAPI workload")
    _show(plan)
    print(
        f"\nTotal API requests: {plan.attrs['total_requests']:,} "
        f"(bbox efficiency {plan.attrs['bbox_efficiency']:.2f}, "
        "FIRMS allows 5,000 transactions per 10 minutes)"
    )
    print("Cached blocks are reused, so re-running after an interruption is cheap.")
    return 0


def cmd_run(args) -> int:
    cfg = Config.from_yaml(args.config)
    if args.out:
        cfg.out_dir = args.out
    pipe = Pipeline(cfg, map_key=args.map_key)
    df = pipe.run(from_csv=args.from_csv)
    if df.empty:
        print("No detections after filtering. Check the plan and QC funnel.", file=sys.stderr)
        return 2

    print("\nFilter funnel")
    _show(pipe.funnel.to_frame())

    if not args.no_plots and cfg.plots.enabled:
        paths = render(cfg, df, regions=pipe.regions)
        print(f"\nRendered {len(paths)} figure files under {cfg.out_path / 'figs'}")

    if args.density and pipe.regions:
        dens = detection_density(df, pipe.regions)
        dens.to_csv(cfg.out_path / "qc" / "detection_density.csv", index=False)
        print(f"Wrote detection density to {cfg.out_path / 'qc' / 'detection_density.csv'}")

    print(f"\nOutputs: {cfg.out_path}")
    return 0


def cmd_plot(args) -> int:
    cfg = Config.from_yaml(args.config)
    src = Path(args.data) if args.data else cfg.out_path / "data" / "detections.parquet"
    df = pd.read_parquet(src) if src.suffix == ".parquet" else pd.read_csv(src)
    from .aoi import load_regions

    paths = render(cfg, df, regions=load_regions(cfg.regions))
    print(f"Rendered {len(paths)} figure files under {cfg.out_path / 'figs'}")
    return 0


def cmd_serve(args) -> int:
    from .mcp_server import main as serve_main

    serve_main(config_path=args.config)
    return 0


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="firepipe",
        description="NASA FIRMS agricultural-fire extraction and figure pipeline",
    )
    p.add_argument("-v", "--verbose", action="store_true")

    # Shared parent so `-v` is accepted after the subcommand too, which is
    # where people naturally type it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true")

    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="write a starter config", parents=[common])
    s.add_argument("-o", "--output", default="config.yaml")
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("layers", help="inspect layers and fields in GeoPackages", parents=[common])
    s.add_argument("gpkg", nargs="+")
    s.set_defaults(func=cmd_layers)

    s = sub.add_parser("key", help="check MAP_KEY status and transaction count", parents=[common])
    s.add_argument("-c", "--config")
    s.add_argument("--map-key")
    s.set_defaults(func=cmd_key)

    s = sub.add_parser("availability", help="date coverage of each FIRMS dataset", parents=[common])
    s.add_argument("-c", "--config")
    s.add_argument("--map-key")
    s.set_defaults(func=cmd_availability)

    s = sub.add_parser("plan", help="dry run: seasons, bboxes and API cost", parents=[common])
    s.add_argument("-c", "--config", required=True)
    s.add_argument("--map-key")
    s.set_defaults(func=cmd_plan)

    s = sub.add_parser("run", help="fetch, filter, mask and plot", parents=[common])
    s.add_argument("-c", "--config", required=True)
    s.add_argument("--map-key")
    s.add_argument("--out", help="override out_dir")
    s.add_argument("--from-csv", help="ingest an existing FIRMS export instead of calling the API")
    s.add_argument("--no-plots", action="store_true")
    s.add_argument("--density", action="store_true", help="also write per-district density")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("plot", help="re-render figures from saved detections", parents=[common])
    s.add_argument("-c", "--config", required=True)
    s.add_argument("--data", help="parquet or csv of detections")
    s.set_defaults(func=cmd_plot)

    s = sub.add_parser("serve", help="run the MCP server over stdio", parents=[common])
    s.add_argument("-c", "--config", help="default config the tools operate on")
    s.set_defaults(func=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return args.func(args)
    except Exception as exc:  # surface actionable errors, not tracebacks
        logging.getLogger("firepipe").error("%s: %s", type(exc).__name__, exc)
        if args.verbose:
            raise
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
