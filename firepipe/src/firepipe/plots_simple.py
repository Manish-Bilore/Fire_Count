"""Stripped-down figures for slides and posters.

Same data, same colours, same figure bodies as :mod:`firepipe.plots` - only the
explanatory furniture is removed:

* no analytical summary caption
* no subtitle
* no mean reference line
* no in-plot total

Everything is inherited, so a change to a figure in ``plots.py`` appears here
automatically. Only the presentation hooks are overridden.

Output goes to ``simple_plots/`` beside the standard ``figs/``, never on top of
it, because the two are not interchangeable.

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
from pathlib import Path

import pandas as pd

from .config import Config
from .plots import FigureSuite

log = logging.getLogger("firepipe.plots_simple")


class SimpleFigureSuite(FigureSuite):
    """FigureSuite with the explanatory furniture switched off."""

    #: Written alongside figs/, not into it.
    SUBDIR = "simple_plots"

    def _frame(self, title, subtitle, caption, **kw):
        # Title only. _frame reclaims the subtitle and caption bands rather
        # than leaving white space where the text used to be.
        #
        # self._title is still applied: a pooled-platform figure keeps that
        # warning even here, because the caption that would otherwise carry it
        # is exactly what this mode removes.
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
    """Render the simple figure set into ``simple_plots/``."""
    out = Path(out_dir) if out_dir else cfg.out_path / SimpleFigureSuite.SUBDIR
    paths = SimpleFigureSuite(cfg, df, regions=regions).render_all(out)
    log.info(
        "Simple figures written to %s - these carry no method text, so cite "
        "manifest.json alongside them",
        out,
    )
    return paths


def render_both(
    cfg: Config, df: pd.DataFrame, regions=None
) -> tuple[list[Path], list[Path]]:
    """Render the annotated set and the simple set in one pass."""
    from .plots import render

    return render(cfg, df, regions=regions), render_simple(cfg, df, regions=regions)
