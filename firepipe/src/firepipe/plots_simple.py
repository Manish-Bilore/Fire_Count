"""Stripped-down figures for slides and posters, written as PNG and SVG.

Same data, same colours, same figure bodies as :mod:`firepipe.plots` - only the
explanatory furniture is removed:

* no analytical summary caption
* no subtitle
* no mean reference line
* no in-plot total

Everything is inherited, so a change to a figure in ``plots.py`` appears here
automatically. Only the presentation hooks are overridden.

Output splits by format, never inside either annotated folder:

* ``simple_plots/<region>/*.png``  - raster, what you drop into slides
* ``svg_simple/<region>/*.svg``    - vector, what you hand to a print pipeline

The base :class:`FigureSuite` already writes both (PNGs via ``formats`` and an
SVG mirror via ``svg_root``); this module only retargets the mirror and switches
the captions off.  Nothing here needs to touch ``savefig`` itself.

    from firepipe.plots_simple import render_simple
    render_simple(cfg, df, regions=pipe.regions)

**These figures do not carry their own method text.** The standard figures state
the sensor, confidence classes, mask and season windows on every panel, so a
figure lifted out of its folder still describes the filter that produced it.
A simple figure does not. Pair them with the method statement from
``manifest.json`` whenever they leave your machine.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pandas as pd

from .config import Config
from .plots import FigureSuite

log = logging.getLogger("firepipe.plots_simple")


class SimpleFigureSuite(FigureSuite):
    """FigureSuite with the explanatory furniture switched off."""

    #: PNG folder.  Sits beside figs/ and svg_simple/, not inside either.
    SUBDIR = "simple_plots"

    #: SVG mirror folder, sibling of SUBDIR.  The base class mirrors SVGs to
    #: <out_dir>/svg/ by default; render_simple() below moves that mirror here
    #: so the vector tree sits beside the raster tree rather than inside it.
    SVG_SUBDIR = "svg_simple"

    def _frame(self, title, subtitle, caption, **kw):
        # Title only. _frame reclaims the subtitle and caption bands rather
        # than leaving white space where the text used to be.
        from .plots import _frame as base_frame

        return base_frame(self._title(title), "", "", **kw)

    def _mean_line(self, ax, mean: float, label_fmt: str = "") -> None:
        return  # no reference line

    def _headroom(self, ax, factor: float = 1.18) -> None:
        # The extra space above the bars exists for the mean label and the
        # in-plot total. With neither drawn, keep just enough for the bar
        # value labels.
        from .plots import _headroom as base_headroom

        base_headroom(ax, 1 + (factor - 1) * 0.45)

    def _headline_note(self, ax, text: str) -> None:
        return  # no in-plot total


def render_simple(
    cfg: Config,
    df: pd.DataFrame,
    regions=None,
    out_dir: Path | None = None,
) -> list[Path]:
    """Render the simple figure set as PNGs and SVGs.

    PNGs land under ``simple_plots/<region>/``; the SVG mirror the base class
    writes into ``simple_plots/svg/`` is moved to a top-level ``svg_simple/``
    so the whole vector deck can be grabbed as one directory.
    """
    out = Path(out_dir) if out_dir else cfg.out_path / SimpleFigureSuite.SUBDIR
    paths = SimpleFigureSuite(cfg, df, regions=regions).render_all(out)

    src = out / "svg"
    dst = out.parent / SimpleFigureSuite.SVG_SUBDIR
    if src.exists() and src.resolve() != dst.resolve():
        if dst.exists():
            shutil.rmtree(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        rewritten: list[Path] = []
        for p in paths:
            try:
                rewritten.append(dst / p.relative_to(src))
            except ValueError:
                rewritten.append(p)
        paths = rewritten
        log.info("SVG mirror moved from %s to %s", src, dst)

    n_png = sum(1 for p in paths if p.suffix == ".png")
    n_svg = sum(1 for p in paths if p.suffix == ".svg")
    log.info(
        "Simple figures: %d PNG in %s, %d SVG in %s - these carry no method "
        "text, so cite manifest.json alongside them",
        n_png, out, n_svg, dst,
    )
    return paths


def render_both(
    cfg: Config, df: pd.DataFrame, regions=None
) -> tuple[list[Path], list[Path]]:
    """Render the annotated set and the simple set in one pass."""
    from .plots import render

    return render(cfg, df, regions=regions), render_simple(cfg, df, regions=regions)