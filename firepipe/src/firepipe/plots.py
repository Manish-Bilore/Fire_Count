"""Publication figures.

Every subtitle and caption is assembled from the config objects, so the method
text on a figure cannot drift away from the filter that produced its data. If
the confidence classes change, the caption changes with them.
"""

from __future__ import annotations

import calendar
import logging
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.ticker import FuncFormatter, MaxNLocator

from .config import Config
from .seasons import SeasonCalendar

log = logging.getLogger("firepipe.plots")

RED = "#EE3B33"
RED_DARK = "#8B1A12"
BLUE_DARK = "#00008B"
GREY = "#4D4D4D"
PAL_PLATFORM = {"VIIRS": RED, "MODIS": "#3B6EA5"}
REGION_PALETTE = ["#EE3B33", "#3B6EA5", "#F0A202", "#4C9A2A", "#7B4397", "#00838F"]

MONTH_ABB = list(calendar.month_abbr)[1:]

#: Calendar-date axes place every season year on one reference year so years
#: overlay. 2000 is a leap year, so a 29 Feb anchor still has a date.
REF_YEAR = 2000
WEEK = 7
MARKER_COLOUR = "#333333"

#: Name of the single top-level folder that holds the SVG mirror of the whole
#: deck.  Kept beside the per-region raster folders rather than inside them, so
#: the vector art can be grabbed (or shipped) as one directory.
SVG_DIRNAME = "svg"

_PLATFORM_WORDS = {
    "VIIRS_SNPP": "S-NPP",
    "VIIRS_NOAA20": "NOAA-20",
    "VIIRS_NOAA21": "NOAA-21",
}

plt.rcParams.update(
    {
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "axes.edgecolor": "#BBBBBB",
        "axes.grid": True,
        "grid.color": "#E6E6E6",
        "grid.linewidth": 0.8,
        "axes.axisbelow": True,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "font.size": 12,
        "axes.labelsize": 13,
        "xtick.color": "#4D4D4D",
        "ytick.color": "#4D4D4D",
        "legend.frameon": False,
    }
)


def _comma(x, _pos=None) -> str:
    return f"{int(round(x)):,}"


COMMA = FuncFormatter(_comma)


# --------------------------------------------------------------------------
# caption engine
# --------------------------------------------------------------------------


class Captions:
    """Method text derived from config, never hand-typed."""

    def __init__(self, cfg: Config, calendar_: SeasonCalendar, df: pd.DataFrame):
        self.cfg = cfg
        self.cal = calendar_
        self.df = df
        s = cfg.sensors
        group = s.headline_platform_group

        if group == "VIIRS":
            self.sensor_note = (
                "Sensor: VIIRS 375 m active-fire product ("
                + " + ".join(_PLATFORM_WORDS.get(p, p) for p in s.viirs_platforms)
                + ")"
            )
            self.conf_note = (
                "Confidence: classes {" + ", ".join(s.viirs_confidence) + "} retained"
            )
            self.sub_tag = "VIIRS, " + _class_words(s.viirs_confidence) + " confidence"
        elif group == "MODIS":
            self.sensor_note = "Sensor: MODIS 1 km active-fire product (Terra + Aqua)"
            self.conf_note = f"Confidence: numeric \u2265 {s.modis_confidence_min}"
            self.sub_tag = f"MODIS, confidence \u2265 {s.modis_confidence_min}"
        else:
            self.sensor_note = (
                "Sensors: VIIRS 375 m + MODIS 1 km POOLED \u2014 detection counts "
                "are not footprint-comparable"
            )
            self.conf_note = (
                "Confidence: VIIRS {" + ", ".join(s.viirs_confidence) + "}; MODIS \u2265 "
                f"{s.modis_confidence_min} (matched strictness)"
            )
            self.sub_tag = "MODIS + VIIRS pooled, matched confidence"

        self.mask_note = cfg.mask.description
        self.season_note = "Seasons: " + calendar_.definition_text()
        self.exclusion_note = calendar_.exclusion_text()
        self.processing_note = {
            "auto": "Processing: Standard Processing where archived, NRT for the recent tail",
            "SP": "Processing: Standard Processing only",
            "NRT": "Processing: Near Real-Time only",
        }[s.processing]

    def partial_note(self, sub: pd.DataFrame) -> str | None:
        """Flag a final year whose record stops before its season window does."""
        if sub.empty:
            return None
        last = pd.to_datetime(sub["acq_date"]).max().date()
        last_year = int(sub["season_year"].max())
        expected = []
        for w in self.cal.windows:
            end_year = last_year + 1 if w.wraps else last_year
            try:
                expected.append(date(end_year, *w.end_md))
            except ValueError:
                pass
        if expected and last < max(expected):
            return (
                f"NOTE: the {last_year} record ends {last:%d %b %Y}, before the season "
                "window closes \u2014 partial year, do not read as a decline"
            )
        return None

    def block(
        self,
        sub: pd.DataFrame,
        *lines: str,
        sensor_note: str | None = None,
        conf_note: str | None = None,
    ) -> str:
        bullets = [ln for ln in lines if ln]
        bullets += [
            self.season_note,
            self.exclusion_note,
            sensor_note or self.sensor_note,
            conf_note or self.conf_note,
            self.processing_note,
            self.mask_note,
            "Data source: NASA FIRMS active-fire archive",
            "Counts are satellite detections, not discrete fire events",
        ]
        partial = self.partial_note(sub)
        if partial:
            bullets.append(partial)
        return "ANALYTICAL SUMMARY:\n" + "\n".join("\u2022 " + b for b in bullets)

    def short(self, text: str) -> str:
        return text


def _class_words(classes: list[str]) -> str:
    words = {"h": "high", "n": "nominal", "l": "low"}
    return " + ".join(words.get(c, c) for c in classes)


# --------------------------------------------------------------------------
# figure scaffolding
# --------------------------------------------------------------------------


class FigureWriter:
    """Collects rendered figures and mirrors every one as SVG.

    Rasters land in ``out_dir``; an SVG copy of each figure lands under
    ``svg_root`` at the same relative name.  Passing ``svg_root`` per writer
    (rather than deriving it from ``out_dir``) is what lets the whole SVG tree
    live in one folder beside the region folders instead of scattering a
    ``svg/`` into each of them.  Both copies are written from the same Figure
    before it is closed, so the two trees cannot drift apart.

    Set ``svg_root=None`` to suppress the SVG mirror entirely (useful for tests
    and throwaway runs).
    """

    def __init__(
        self,
        out_dir: Path,
        dpi: int = 200,
        formats: list[str] | None = None,
        svg_root: Path | None = None,
    ):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.dpi = dpi
        self.formats = formats or ["png"]
        self.svg_root = Path(svg_root) if svg_root is not None else None
        self.written: list[Path] = []

    def save(self, fig: plt.Figure, name: str) -> None:
        for ext in self.formats:
            path = self.out_dir / f"{name}.{ext}"
            path.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(path, dpi=self.dpi, bbox_inches="tight", facecolor="white")
            self.written.append(path)
        if self.svg_root is not None:
            # ``name`` may carry a sub-path (the per-year heatmap families do),
            # so the parent has to be created exactly as above.
            svg = self.svg_root / f"{name}.svg"
            svg.parent.mkdir(parents=True, exist_ok=True)
            fig.savefig(svg, bbox_inches="tight", facecolor="white")
            self.written.append(svg)
        plt.close(fig)


def _frame(
    title: str,
    subtitle: str,
    caption: str,
    width: float = 14,
    height: float = 8,
    nrows: int = 1,
    ncols: int = 1,
    **kw,
):
    """Figure with the deck's title / subtitle / caption furniture in place."""
    # An empty subtitle or caption reclaims its band rather than leaving a gap,
    # which is what makes the stripped-down variant in plots_simple look
    # deliberate instead of merely missing its text.
    has_caption = bool(caption and caption.strip())
    has_subtitle = bool(subtitle and subtitle.strip())
    cap_lines = (caption.count("\n") + 1) if has_caption else 0
    cap_h = 0.24 * cap_lines + (0.35 if has_caption else 0.75)
    head_h = (1.15 if has_subtitle else 0.75) + (0.45 if nrows * ncols > 1 else 0.0)
    total_h = height + cap_h + head_h
    fig, axes = plt.subplots(nrows, ncols, figsize=(width, total_h), squeeze=False, **kw)
    fig.subplots_adjust(
        top=1 - head_h / total_h,
        bottom=cap_h / total_h,
        hspace=0.45 if nrows > 1 else 0.2,
        wspace=0.22 if ncols > 1 else 0.2,
    )
    fig.text(0.5, 1 - 0.45 / total_h, title, ha="center", va="top", fontsize=21, fontweight="bold")
    if has_subtitle:
        fig.text(
            0.5, 1 - 0.95 / total_h, subtitle, ha="center", va="top", fontsize=14,
            color="#222222",
        )
    if has_caption:
        fig.text(
            0.02, 0.012, caption, ha="left", va="bottom", fontsize=9.5,
            color="#333333", linespacing=1.45,
        )
    return fig, axes


def _bar_labels(ax, bars, values, colour=RED_DARK, size=10, fmt=_comma):
    for b, v in zip(bars, values):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            continue
        ax.annotate(
            fmt(v),
            (b.get_x() + b.get_width() / 2, b.get_height()),
            ha="center",
            va="bottom",
            fontsize=size,
            fontweight="bold",
            color=colour,
            xytext=(0, 2),
            textcoords="offset points",
        )


def _legend(ax, title=None, ncol=1, loc="upper center"):
    """In-axes legend on a white backing, so it never fights the title band."""
    leg = ax.legend(
        title=title,
        ncol=ncol,
        loc=loc,
        frameon=True,
        framealpha=0.92,
        edgecolor="none",
        fontsize=10,
    )
    if leg and leg.get_frame() is not None:
        leg.get_frame().set_facecolor("white")
    return leg


def _headroom(ax, factor: float = 1.18) -> None:
    lo, hi = ax.get_ylim()
    ax.set_ylim(lo, hi * factor)


# --------------------------------------------------------------------------
# the figure suite
# --------------------------------------------------------------------------


class FigureSuite:
    def __init__(self, cfg: Config, df: pd.DataFrame, regions=None):
        self.cfg = cfg
        self.cal = SeasonCalendar(cfg.seasons)
        self.regions = regions or []
        self.pal = self.cal.palette
        self.all = df
        self.df = _apply_headline_platform(df, cfg.sensors.headline_platform_group)
        self.cap = Captions(cfg, self.cal, self.df)

    # -- overridable furniture --------------------------------------------
    #
    # Every figure builds its frame and its summary marks through these three
    # methods. Subclasses change the presentation by overriding them, without
    # touching a single figure body. See plots_simple.SimpleFigureSuite.

    def _title(self, title: str) -> str:
        """Mark pooled-platform figures in the title itself.

        Pooling MODIS with VIIRS is stated in the caption, but the simple
        figures have no caption. The title is the only text that survives every
        presentation mode, so the warning belongs there too.
        """
        group = self.cfg.sensors.headline_platform_group
        if group == "BOTH":
            return f"{title} \u2014 MODIS + VIIRS pooled"
        if group == "VIIRS":
            # A group pools every satellite in it, so two VIIRS platforms means
            # one fire can be counted twice, ~50 minutes apart. Footprints match
            # so the sum is coherent, but it is still a sum.
            sats = [
                _PLATFORM_WORDS.get(p, p) for p in self.cfg.sensors.viirs_platforms
            ]
            if len(sats) > 1:
                return f"{title} \u2014 {' + '.join(sats)} pooled"
        return title

    def _frame(self, title, subtitle, caption, **kw):
        return _frame(self._title(title), subtitle, caption, **kw)

    def _mean_line(self, ax, mean: float, label_fmt: str = "mean {:,.0f}") -> None:
        """Dashed reference line at the series mean, labelled on the axis."""
        ax.axhline(mean, ls="--", color="#666666", lw=1)
        ax.text(
            -0.008, mean, label_fmt.format(mean), transform=ax.get_yaxis_transform(),
            ha="right", va="center", fontsize=10, color="#555555",
        )

    def _headroom(self, ax, factor: float = 1.18) -> None:
        """Space above the tallest bar for value labels and in-plot notes."""
        _headroom(ax, factor)

    def _headline_note(self, ax, text: str) -> None:
        """Large in-plot total, top left."""
        ax.text(
            0.01, 0.95, text, transform=ax.transAxes, fontsize=15,
            fontweight="bold", color=BLUE_DARK, va="top",
        )

    # -- driver ------------------------------------------------------------

    def render_all(self, out_dir: Path) -> list[Path]:
        written: list[Path] = []
        wanted = self.cfg.plots.figures
        pick = (lambda fid: True) if "all" in wanted else (lambda fid: fid in wanted)
        # One SVG tree for the whole deck, sitting beside the region folders.
        svg_root = Path(out_dir) / SVG_DIRNAME

        for region, sub in self.df.groupby("region", observed=True):
            slug = _slug(str(region))
            w = FigureWriter(
                Path(out_dir) / slug,
                self.cfg.plots.dpi,
                self.cfg.plots.formats,
                svg_root=svg_root / slug,
            )
            raw_sub = self.all[self.all["region"] == region]
            label = str(region)
            if pick("01"):
                self.f01_yearly(sub, label, w)
            if pick("02"):
                self.f02_season_by_year(sub, label, w)
            if pick("03"):
                self.f03_season_share(sub, label, w)
            if pick("04"):
                self.f04_monthly_by_year(sub, label, w)
            if pick("04b"):
                self.f04b_heatmap(sub, label, w)
            if pick("05"):
                self.f05_pentad(sub, label, w)
            if pick("06"):
                self.f06_cumulative(sub, label, w)
            if pick("07"):
                self.f07_frp(sub, label, w)
            if pick("08"):
                self.f08_platform(raw_sub, label, w)
            if pick("09"):
                self.f09_season_yearly(sub, label, w)
            if pick("10"):
                self.f10_season_monthly(sub, label, w)
            if pick("11"):
                self.f11_districts(sub, label, w)
            if pick("12"):
                self.f12_district_heatmap(sub, label, w)
            if pick("13"):
                self.f13_district_map(sub, label, w)
            if pick("14"):
                self.f14_district_map_by_year(sub, label, w)
            if pick("15"):
                self.f15_season_district_map_by_year(sub, label, w)
            if pick("16"):
                self.f16_weekly_by_year(sub, label, w)
            if pick("17"):
                self.f17_season_weekly_by_year(sub, label, w)
            if pick("18"):
                self.f18_district_month_heatmap_by_year(sub, label, w)
            if pick("19"):
                self.f19_season_district_week_heatmap_by_year(sub, label, w)
            written += w.written

        if len(self.df["region"].unique()) > 1:
            w = FigureWriter(
                Path(out_dir) / "_comparison",
                self.cfg.plots.dpi,
                self.cfg.plots.formats,
                svg_root=svg_root / "_comparison",
            )
            if pick("20"):
                self.f20_region_totals(w)
            if pick("21"):
                self.f21_region_index(w)
            if pick("22"):
                self.f22_region_season_grid(w)
            written += w.written

        log.info("Rendered %d figure files", len(written))
        return written

    # -- per-region figures ------------------------------------------------

    def f01_yearly(self, sub, region, w):
        yr = sub.groupby("season_year").size().rename("fires").reset_index()
        if yr.empty:
            return
        mean = yr["fires"].mean()
        cap = self.cap.block(
            sub,
            f"Period: {pd.to_datetime(sub['acq_date']).min():%Y-%m-%d} to "
            f"{pd.to_datetime(sub['acq_date']).max():%Y-%m-%d}",
            f"Total detections: {len(sub):,}",
            f"Annual mean: {mean:,.0f}  |  peak year: "
            f"{int(yr.loc[yr['fires'].idxmax(), 'season_year'])}",
            "Annual totals cover only the season windows above, not the full calendar year",
        )
        fig, ax = self._frame(
            f"Agricultural Fire Detections, {region}: {_span(yr)}",
            f"{self.cap.sub_tag} \u2014 total {len(sub):,}",
            cap,
        )
        ax = ax[0][0]
        bars = ax.bar(yr["season_year"].astype(str), yr["fires"], color=RED, width=0.72)
        _bar_labels(ax, bars, yr["fires"])
        self._mean_line(ax, mean)
        self._headline_note(ax, f"Total: {len(sub):,} detections")
        ax.set_xlabel("Season year")
        ax.set_ylabel("Fire count")
        ax.yaxis.set_major_formatter(COMMA)
        ax.grid(axis="x", visible=False)
        self._headroom(ax, 1.25)
        if self.cfg.plots.annotate_covid:
            self._covid(ax, yr)
        w.save(fig, "01_yearly_counts")

    def f02_season_by_year(self, sub, region, w):
        ys = _season_year_counts(sub, self.cal)
        if ys.empty:
            return
        cap = self.cap.block(
            sub,
            "Bars are directly comparable only within a season: window lengths differ "
            f"({', '.join(f'{n} = {d} d' for n, d in _window_days(self.cal))})",
        )
        fig, ax = self._frame(
            f"Seasonal Fire Detections by Year, {region}",
            f"{self.cap.sub_tag} \u2014 {self.cal.definition_text()}",
            cap,
            width=15,
        )
        ax = ax[0][0]
        self._grouped_season_bars(ax, ys)
        ax.set_xlabel("Season year")
        ax.set_ylabel("Fire count")
        ax.yaxis.set_major_formatter(COMMA)
        ax.grid(axis="x", visible=False)
        _legend(ax, title="Season", ncol=len(self.cal.names))
        self._headroom(ax, 1.3)
        w.save(fig, "02_season_by_year")

    def f03_season_share(self, sub, region, w):
        ys = _season_year_counts(sub, self.cal)
        if ys.empty:
            return
        wide = ys.pivot(index="season_year", columns="season", values="fires").fillna(0)
        shares = wide.div(wide.sum(axis=1).replace(0, np.nan), axis=0)
        cap = self.cap.block(
            sub, "Composition, not magnitude: a rising share can coincide with falling counts"
        )
        fig, ax = self._frame(
            f"Seasonal Composition of Fire Detections, {region}",
            self.cap.sub_tag,
            cap,
        )
        ax = ax[0][0]
        bottom = np.zeros(len(shares))
        x = shares.index.astype(str)
        for name in self.cal.names:
            if name not in shares:
                continue
            vals = shares[name].to_numpy()
            ax.bar(x, vals, bottom=bottom, color=self.pal[name], label=name, width=0.72)
            for xi, (v, b) in enumerate(zip(vals, bottom)):
                if v > 0.06:
                    ax.text(
                        xi,
                        b + v / 2,
                        f"{v:.0%}",
                        ha="center",
                        va="center",
                        fontweight="bold",
                        color="white",
                        fontsize=11,
                    )
            bottom += np.nan_to_num(vals)
        ax.set_ylim(0, 1)
        ax.set_xlabel("Season year")
        ax.set_ylabel("Share of detections")
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:.0%}"))
        ax.grid(axis="x", visible=False)
        leg = ax.legend(
            title="Season", loc="upper left", bbox_to_anchor=(1.01, 1), frameon=False, fontsize=10
        )
        w.save(fig, "03_season_share")

    def f04_monthly_by_year(self, sub, region, w):
        months = self.cal.covered_months()
        mo = (
            sub.groupby(["season_year", "month"], observed=True)
            .size()
            .rename("fires")
            .reset_index()
        )
        years = sorted(mo["season_year"].unique())
        if not years:
            return
        ncols = min(4, len(years))
        nrows = int(np.ceil(len(years) / ncols))
        cap = self.cap.block(
            sub,
            "Panels share a y-axis; only months inside a season window are plotted",
        )
        fig, axes = self._frame(
            f"Monthly Fire Detections by Year, {region}",
            self.cap.sub_tag,
            cap,
            width=16,
            height=3.0 * nrows + 1,
            nrows=nrows,
            ncols=ncols,
            sharey=True,
        )
        ymax = mo["fires"].max() * 1.25
        for i, yr in enumerate(years):
            ax = axes[i // ncols][i % ncols]
            d = mo[mo["season_year"] == yr].set_index("month")["fires"].reindex(months, fill_value=0)
            colours = [self.pal.get(_season_of_month(self.cal, m), GREY) for m in months]
            ax.bar([MONTH_ABB[m - 1] for m in months], d, color=colours, width=0.75)
            ax.set_title(str(int(yr)), fontweight="bold", fontsize=13)
            ax.set_ylim(0, ymax)
            ax.yaxis.set_major_formatter(COMMA)
            ax.grid(axis="x", visible=False)
            ax.tick_params(axis="x", labelrotation=45, labelsize=9)
        for j in range(len(years), nrows * ncols):
            axes[j // ncols][j % ncols].axis("off")
        w.save(fig, "04_monthly_by_year")

    def f04b_heatmap(self, sub, region, w):
        months = self.cal.covered_months()
        mo = (
            sub.groupby(["season_year", "month"], observed=True)
            .size()
            .rename("fires")
            .reset_index()
        )
        if mo.empty:
            return
        grid = (
            mo.pivot(index="season_year", columns="month", values="fires")
            .reindex(columns=months)
            .fillna(0)
        )
        cap = self.cap.block(
            sub, "Cells are counts; blank columns are months outside every season window"
        )
        fig, ax = self._frame(
            f"Month \u00d7 Year Detection Matrix, {region}",
            self.cap.sub_tag,
            cap,
            width=15,
            height=1.0 * len(grid) + 3,
        )
        ax = ax[0][0]
        vals = grid.to_numpy()
        im = ax.imshow(vals, cmap="YlOrRd", aspect="auto")
        cut = 0.55 * np.nanmax(vals) if vals.size else 0
        for i in range(vals.shape[0]):
            for j in range(vals.shape[1]):
                v = vals[i, j]
                ax.text(
                    j,
                    i,
                    _comma(v),
                    ha="center",
                    va="center",
                    fontsize=10,
                    fontweight="bold",
                    color="white" if v > cut else "#262626",
                )
        ax.set_xticks(range(len(months)), [MONTH_ABB[m - 1] for m in months])
        ax.set_yticks(range(len(grid)), [str(int(y)) for y in grid.index])
        ax.set_xlabel("Month")
        ax.set_ylabel("Season year")
        ax.grid(False)
        fig.colorbar(im, ax=ax, shrink=0.8, label="Detections")
        w.save(fig, "04b_month_year_heatmap")

    def f05_pentad(self, sub, region, w):
        """Five-day bins through each season window, all years overlaid."""
        for season in self.cal.names:
            d = sub[sub["season"] == season]
            if d.empty:
                continue
            win = self._detail_window(season)
            span = _window_span_days(win)
            offs = _day_offset(d, win)
            d = d.assign(day_off=offs)
            d = d[d["day_off"].between(0, span)]
            d["bin"] = (d["day_off"] // 5 * 5).astype(int)
            binned = (
                d.groupby(["season_year", "bin"], observed=True)
                .size()
                .rename("fires")
                .reset_index()
            )
            # Each point sits at the centre of the days its bin covers. The
            # last bin of a window can be shorter than five days.
            blen = np.minimum(5, span + 1 - binned["bin"])
            binned["x"] = mdates.date2num(_ref_start(win)) + binned["bin"] + blen / 2
            start, stop = _ref_window(win, span)
            tail = (span + 1) % 5
            cap = self.cap.block(
                sub,
                f"Five-day bins measured from the {season} window opening "
                f"({win.start} \u2192 {win.end}); each point is plotted at the centre "
                "of its bin",
                (
                    f"The final bin covers only {_n_days(tail)}, "
                    "so its low value is not a decline"
                ) if tail else "",
                "Peak timing, not peak height, is the comparable quantity across years",
                self._marker_note(start, stop),
            )
            fig, ax = self._frame(
                f"{season} Burning Window, 5-Day Intervals: {region}",
                f"{self.cap.sub_tag} \u2014 {win.display_label}",
                cap,
                width=16,
                height=8,
            )
            ax = ax[0][0]
            years = sorted(binned["season_year"].unique())
            cmap = plt.get_cmap("YlOrRd")
            for i, yr in enumerate(years):
                dd = binned[binned["season_year"] == yr].sort_values("bin")
                ax.plot(
                    dd["x"],
                    dd["fires"],
                    marker="o",
                    ms=4,
                    lw=2,
                    label=str(int(yr)),
                    color=cmap(0.25 + 0.7 * i / max(len(years) - 1, 1)),
                )
            ax.set_xlabel("Date (5-day bins)")
            ax.set_ylabel("Fire count")
            ax.yaxis.set_major_formatter(COMMA)
            self._calendar_axis(ax, start, stop)
            self._mark_dates(ax, start, stop)
            _legend(ax, title="Season year", ncol=max(1, min(len(years), 4)), loc="upper left")
            self._headroom(ax, 1.35)
            w.save(fig, f"05_{_slug(season)}_pentad")

    def f06_cumulative(self, sub, region, w):
        for season in self.cal.names:
            d = sub[sub["season"] == season]
            if d.empty:
                continue
            win = self._detail_window(season)
            span = _window_span_days(win)
            start, stop = _ref_window(win, span)
            d = d.assign(day_off=_day_offset(d, win))
            d = d[d["day_off"].between(0, span)]
            cum = (
                d.groupby(["season_year", "day_off"], observed=True)
                .size()
                .rename("n")
                .reset_index()
                .sort_values("day_off")
            )
            cum["cum"] = cum.groupby("season_year")["n"].cumsum()
            cap = self.cap.block(
                sub,
                "Curves are cumulative within the season window; a steeper rise means "
                "burning compressed into fewer days",
                "Curves are restricted to in-season days, so they have no flat gaps",
                self._marker_note(start, stop),
            )
            fig, ax = self._frame(
                f"Cumulative {season} Detections, {region}",
                self.cap.sub_tag,
                cap,
            )
            ax = ax[0][0]
            years = sorted(cum["season_year"].unique())
            cmap = plt.get_cmap("YlOrRd")
            for i, yr in enumerate(years):
                dd = cum[cum["season_year"] == yr]
                ax.plot(
                    mdates.date2num(start) + dd["day_off"],
                    dd["cum"],
                    lw=2.2,
                    label=str(int(yr)),
                    color=cmap(0.25 + 0.7 * i / max(len(years) - 1, 1)),
                )
            ax.set_xlabel("Date")
            ax.set_ylabel("Cumulative detections")
            ax.yaxis.set_major_formatter(COMMA)
            self._calendar_axis(ax, start, stop)
            self._mark_dates(ax, start, stop)
            _legend(ax, title="Season year", ncol=max(1, min(len(years), 2)), loc="upper left")
            self._headroom(ax, 1.15)
            w.save(fig, f"06_{_slug(season)}_cumulative")

    def f07_frp(self, sub, region, w):
        if "frp" not in sub or sub["frp"].isna().all():
            return
        frp = (
            sub.groupby(["season_year", "season"], observed=True)["frp"]
            .sum()
            .rename("total_frp")
            .reset_index()
        )
        cap = self.cap.block(
            sub,
            "Fire radiative power sums energy release, so it separates many small "
            "fires from fewer intense ones",
            "FRP is not comparable across sensors with different footprints",
        )
        fig, ax = self._frame(
            f"Seasonal Fire Radiative Power Load, {region}",
            f"{self.cap.sub_tag} \u2014 summed FRP (MW)",
            cap,
            width=15,
        )
        ax = ax[0][0]
        self._grouped_season_bars(ax, frp.rename(columns={"total_frp": "fires"}), fmt=lambda v: f"{v:,.0f}")
        ax.set_xlabel("Season year")
        ax.set_ylabel("Total FRP (MW)")
        ax.yaxis.set_major_formatter(COMMA)
        ax.grid(axis="x", visible=False)
        _legend(ax, title="Season", ncol=len(self.cal.names))
        self._headroom(ax, 1.3)
        w.save(fig, "07_frp_seasonal")

    def f08_platform(self, raw_sub, region, w):
        """Cross-check: the two platforms as independent series, never pooled."""
        cross = (
            raw_sub.groupby(["season_year", "platform"], observed=True)
            .size()
            .rename("fires")
            .reset_index()
        )
        if cross["platform"].nunique() < 2:
            return
        cap = self.cap.block(
            raw_sub,
            "Shown as parallel series, deliberately not pooled: 375 m and 1 km "
            "footprints do not produce comparable counts",
            "Agreement in direction is the check; agreement in level is not expected",
            sensor_note=(
                "Sensors: VIIRS 375 m (S-NPP, NOAA-20) and MODIS 1 km (Terra, Aqua), "
                "shown separately"
            ),
            conf_note=(
                "Confidence: VIIRS {"
                + ", ".join(self.cfg.sensors.viirs_confidence)
                + "}; MODIS \u2265 "
                + str(self.cfg.sensors.modis_confidence_min)
            ),
        )
        fig, axes = self._frame(
            f"Independent Sensor Cross-Check, {region}",
            "MODIS 1 km vs VIIRS 375 m \u2014 counts and normalised trend",
            cap,
            width=16,
            height=7,
            ncols=2,
        )
        ax1, ax2 = axes[0]
        years = sorted(cross["season_year"].unique())
        plats = sorted(cross["platform"].unique())
        width = 0.8 / len(plats)
        for i, p in enumerate(plats):
            d = cross[cross["platform"] == p].set_index("season_year")["fires"].reindex(years, fill_value=0)
            xs = np.arange(len(years)) + (i - (len(plats) - 1) / 2) * width
            ax1.bar(xs, d, width=width, label=p, color=PAL_PLATFORM.get(p, GREY))
        ax1.set_xticks(range(len(years)), [str(int(y)) for y in years])
        ax1.set_title("Detection counts", fontweight="bold")
        ax1.set_xlabel("Season year")
        ax1.set_ylabel("Fire count")
        ax1.yaxis.set_major_formatter(COMMA)
        ax1.grid(axis="x", visible=False)
        _legend(ax1)
        self._headroom(ax1, 1.15)

        base_year = years[0]
        for p in plats:
            d = cross[cross["platform"] == p].set_index("season_year").reindex(years)
            base = d["fires"].dropna().iloc[0] if d["fires"].notna().any() else np.nan
            if not base:
                continue
            ax2.plot(
                years,
                d["fires"] / base * 100,
                marker="o",
                lw=2.4,
                label=p,
                color=PAL_PLATFORM.get(p, GREY),
            )
        ax2.axhline(100, ls="--", color="#888888", lw=1)
        ax2.set_title(f"Normalised trend ({base_year} = 100)", fontweight="bold")
        ax2.set_xlabel("Season year")
        ax2.set_ylabel("Index")
        ax2.xaxis.set_major_locator(MaxNLocator(integer=True))
        _legend(ax2)
        w.save(fig, "08_platform_crosscheck")

    def f09_season_yearly(self, sub, region, w):
        """Season-isolated deck figure: one season, no summary block."""
        for season in self.cal.names:
            d = sub[sub["season"] == season]
            if d.empty:
                continue
            yr = d.groupby("season_year").size().rename("fires").reset_index()
            win = self.cal.window(season)
            fig, ax = self._frame(
                f"{season} Season Fire Detections by Year: {region}",
                f"{win.display_label} \u2014 {win.start} to {win.end} \u2014 {self.cap.sub_tag}",
                f"{self.cap.sensor_note}  |  {self.cap.conf_note}  |  {self.cap.mask_note}\n"
                "Source: NASA FIRMS active-fire archive",
            )
            ax = ax[0][0]
            bars = ax.bar(
                yr["season_year"].astype(str), yr["fires"], color=self.pal[season], width=0.72
            )
            _bar_labels(ax, bars, yr["fires"], colour="#333333")
            mean = yr["fires"].mean()
            self._mean_line(ax, mean)
            ax.set_xlabel("Season year")
            ax.set_ylabel("Fire count")
            ax.yaxis.set_major_formatter(COMMA)
            ax.grid(axis="x", visible=False)
            self._headroom(ax, 1.2)
            w.save(fig, f"09_{_slug(season)}_yearly")

    def f10_season_monthly(self, sub, region, w):
        for season in self.cal.names:
            d = sub[sub["season"] == season]
            if d.empty:
                continue
            win = self.cal.window(season)
            months = sorted(d["month"].unique())
            mo = (
                d.groupby(["season_year", "month"], observed=True)
                .size()
                .rename("fires")
                .reset_index()
            )
            years = sorted(mo["season_year"].unique())
            ncols = min(4, len(years))
            nrows = int(np.ceil(len(years) / ncols))
            fig, axes = self._frame(
                f"{season} Monthly Profile by Year: {region}",
                f"{win.display_label} \u2014 {self.cap.sub_tag}",
                f"{self.cap.sensor_note}  |  {self.cap.conf_note}  |  {self.cap.mask_note}\n"
                "Source: NASA FIRMS active-fire archive",
                width=15,
                height=2.9 * nrows + 1,
                nrows=nrows,
                ncols=ncols,
                sharey=True,
            )
            ymax = mo["fires"].max() * 1.25
            for i, yr in enumerate(years):
                ax = axes[i // ncols][i % ncols]
                dd = mo[mo["season_year"] == yr].set_index("month")["fires"].reindex(months, fill_value=0)
                ax.bar([MONTH_ABB[m - 1] for m in months], dd, color=self.pal[season], width=0.7)
                ax.set_title(str(int(yr)), fontweight="bold", fontsize=13)
                ax.set_ylim(0, ymax)
                ax.yaxis.set_major_formatter(COMMA)
                ax.grid(axis="x", visible=False)
            for j in range(len(years), nrows * ncols):
                axes[j // ncols][j % ncols].axis("off")
            w.save(fig, f"10_{_slug(season)}_monthly")

    def f11_districts(self, sub, region, w):
        if "admin_unit" not in sub or sub["admin_unit"].isna().all():
            return
        n = self.cfg.plots.top_n_districts
        tot = (
            sub.groupby("admin_unit", observed=True)
            .size()
            .rename("fires")
            .reset_index()
            .sort_values("fires", ascending=False)
            .head(n)
        )
        share = tot["fires"].sum() / len(sub)
        cap = self.cap.block(
            sub,
            f"Top {len(tot)} of {sub['admin_unit'].nunique()} districts, "
            f"{share:.0%} of all detections in {region}",
            "Counts are not area-normalised: larger districts accumulate more detections",
        )
        fig, ax = self._frame(
            f"Districts by Fire Detections, {region}",
            f"{self.cap.sub_tag} \u2014 {_span_dates(sub)}",
            cap,
            width=14,
            height=0.42 * len(tot) + 3,
        )
        ax = ax[0][0]
        y = np.arange(len(tot))[::-1]
        ax.barh(y, tot["fires"], color=RED, height=0.72)
        for yi, v in zip(y, tot["fires"]):
            ax.annotate(
                _comma(v), (v, yi), xytext=(4, 0), textcoords="offset points",
                va="center", fontsize=10, fontweight="bold", color=RED_DARK
            )
        ax.set_yticks(y, [str(v).title() for v in tot["admin_unit"]])
        ax.set_xlabel("Fire count")
        ax.xaxis.set_major_formatter(COMMA)
        ax.grid(axis="y", visible=False)
        ax.set_xlim(0, tot["fires"].max() * 1.12)
        w.save(fig, "11_districts_top")

    def f12_district_heatmap(self, sub, region, w):
        """District x season year, once over all seasons and once per season.

        The per-season figures are not slices of the combined one: each ranks
        and scales on its own season, because the seasons have different
        hotspots and differ severalfold in magnitude, so Kharif's geography is
        washed out on a Rabi-dominated scale.
        """
        if "admin_unit" not in sub or sub["admin_unit"].isna().all():
            return
        self._district_year_heatmap(
            sub, sub, region, w, "12_district_year_heatmap",
            f"District \u00d7 Year Detections, {region}",
            self.cap.sub_tag,
            ["Season windows are pooled, so a year's column mixes them"],
        )
        for season in self.cal.names:
            d = sub[sub["season"] == season]
            if d.empty:
                continue
            win = self.cal.window(season)
            self._district_year_heatmap(
                sub, d, region, w, f"12_{_slug(season)}_district_year_heatmap",
                f"{season} District \u00d7 Year Detections, {region}",
                f"{win.display_label} \u2014 {self.cap.sub_tag}",
                [
                    f"{season} window only: {_md_words(win.start)} to {_md_words(win.end)}",
                    f"Ranked and scaled on {season} alone, so rows and colours do not "
                    "match the pooled or other-season version of this figure",
                ],
            )

    def _district_year_heatmap(self, sub, d, region, w, name, title, subtitle, notes):
        n = self.cfg.plots.top_n_districts
        top = d["admin_unit"].value_counts().head(n).index
        grid = (
            d[d["admin_unit"].isin(top)]
            .groupby(["admin_unit", "season_year"], observed=True)
            .size()
            .unstack(fill_value=0)
            .reindex(top)
            .astype(float)
        )
        # A season year still running is a short column, not a low one.
        last = _record_end(sub)
        wins = [self.cal.window(s) for s in self.cal.names] if d is sub else [
            self.cal.window(str(d["season"].iloc[0]))
        ]
        partial = [
            int(y) for y in grid.columns
            if self.cfg.plots.partial_year_titles
            and last < max(_window_end(win, int(y)) for win in wins)
        ]
        cap = self.cap.block(
            sub,
            *notes,
            f"Rows: top {len(grid)} districts by total detections; a row that "
            "brightens over time is a district where burning intensified",
            (
                f"{_join_words([str(y) for y in partial])} "
                f"{'is' if len(partial) == 1 else 'are'} marked * : the record ends "
                f"{_day_words(last)} {last.year}, inside the window, so the column is "
                "short, not low"
            ) if partial else "",
        )
        fig, ax = self._frame(
            title, f"Top {len(grid)} districts \u2014 {subtitle}", cap,
            width=14, height=0.42 * len(grid) + 3,
        )
        ax = ax[0][0]
        self._district_heatmap(
            ax, fig, grid, float(np.nanmax(grid.to_numpy())) if grid.size else 1.0,
            [f"{int(c)}*" if int(c) in partial else str(int(c)) for c in grid.columns],
        )
        ax.set_xlabel("Season year")
        w.save(fig, name)

    def f13_district_map(self, sub, region, w):
        reg = next((r for r in self.regions if r.name == region), None)
        if reg is None or reg.admin is None:
            return
        from .aoi import admin_area_km2

        counts = sub.groupby("admin_unit", observed=True).size().rename("fires")
        areas = admin_area_km2(reg).set_index("admin_unit")["area_km2"]
        gdf = reg.admin.copy()
        key = reg.cfg.admin_field
        gdf["fires"] = gdf[key].map(counts).fillna(0)
        gdf["area_km2"] = gdf[key].map(areas)
        gdf["density"] = gdf["fires"] / gdf["area_km2"] * 1000

        cap = self.cap.block(
            sub,
            "Left: raw counts. Right: detections per 1,000 km\u00b2, which removes the "
            "district-size effect and is the comparable quantity",
        )
        fig, axes = self._frame(
            f"Spatial Distribution of Fire Detections, {region}",
            f"{self.cap.sub_tag} \u2014 {_span_dates(sub)}",
            cap,
            width=16,
            height=5.5,
            ncols=2,
        )
        for ax, col, label, cmap in (
            (axes[0][0], "fires", "Detections", "YlOrRd"),
            (axes[0][1], "density", "Detections per 1,000 km\u00b2", "YlOrRd"),
        ):
            gdf.plot(
                column=col, ax=ax, cmap=cmap, edgecolor="white", linewidth=0.4,
                legend=True, legend_kwds={"label": label, "shrink": 0.7},
            )
            ax.set_title(label, fontweight="bold")
            ax.set_axis_off()
            ax.grid(False)
        w.save(fig, "13_district_map")

    # -- per-year / per-season families (14-19) ---------------------------
    #
    # Each family renders one faceted figure plus one file per panel. The
    # individual files reuse the facet's colour scale or y-axis, so a deck of
    # single-year slides stays as comparable as the facet it was cut from.
    # Individual files go in a subfolder named after the facet.

    def f14_district_map_by_year(self, sub, region, w):
        years = sorted(int(y) for y in sub["season_year"].unique())
        groups = [
            (y, sub[sub["season_year"] == y], self._year_label(sub, y)) for y in years
        ]
        self._map_family(
            sub, region, w, groups,
            facet_name="14_district_map_by_year",
            indiv_stem="14_district_map",
            facet_title=f"District Fire Detections by Year, {region}",
            indiv_title=lambda lab: f"District Fire Detections, {region}: {lab}",
            notes=["Season years combine every season window"],
        )

    def f15_season_district_map_by_year(self, sub, region, w):
        for season in self.cal.names:
            d = sub[sub["season"] == season]
            if d.empty:
                continue
            years = sorted(int(y) for y in d["season_year"].unique())
            groups = [
                (y, d[d["season_year"] == y], self._year_label(sub, y, season))
                for y in years
            ]
            slug = _slug(season)
            self._map_family(
                sub, region, w, groups,
                facet_name=f"15_{slug}_district_map_by_year",
                indiv_stem=f"15_{slug}_district_map",
                facet_title=f"{season} District Fire Detections by Year, {region}",
                indiv_title=lambda lab, s=season: f"{s} District Fire Detections, {region}: {lab}",
                notes=[
                    f"{season} window only: {_md_words(self.cal.window(season).start)} "
                    f"to {_md_words(self.cal.window(season).end)}"
                ],
            )

    def f16_weekly_by_year(self, sub, region, w):
        wins = [win for win in self.cal.windows if (sub["season"] == win.name).any()]
        if not wins:
            return
        years = sorted(int(y) for y in sub["season_year"].unique())
        start = min(_ref_start(win) for win in wins)
        stop = max(_ref_window(win, _window_span_days(win))[1] for win in wins)
        self._weekly_family(
            sub, region, w, wins, years, start, stop,
            facet_name="16_weekly_by_year",
            indiv_stem="16_weekly",
            facet_title=f"Weekly Fire Detections by Year, {region}",
            indiv_title=lambda y, lab: f"Weekly Fire Detections, {region}: {lab}",
            label_of=lambda y: self._year_label(sub, y),
        )

    def f17_season_weekly_by_year(self, sub, region, w):
        for win in self.cal.windows:
            d = sub[sub["season"] == win.name]
            if d.empty:
                continue
            years = sorted(int(y) for y in d["season_year"].unique())
            start, stop = _ref_window(win, _window_span_days(win))
            slug = _slug(win.name)
            self._weekly_family(
                sub, region, w, [win], years, start, stop,
                facet_name=f"17_{slug}_weekly_by_year",
                indiv_stem=f"17_{slug}_weekly",
                facet_title=f"Weekly {win.name} Fire Detections by Year, {region}",
                indiv_title=lambda y, lab, s=win.name: f"Weekly {s} Fire Detections, {region}: {lab}",
                label_of=lambda y, s=win.name: self._year_label(sub, y, s),
            )

    def f18_district_month_heatmap_by_year(self, sub, region, w):
        if "admin_unit" not in sub or sub["admin_unit"].isna().all():
            return
        n = self.cfg.plots.top_n_districts
        top = sub["admin_unit"].value_counts().head(n).index
        months = self.cal.covered_months()
        last = _record_end(sub)
        years = sorted(int(y) for y in sub["season_year"].unique())

        grids = {}
        for y in years:
            d = sub[(sub["season_year"] == y) & sub["admin_unit"].isin(top)]
            g = (
                d.groupby(["admin_unit", "month"], observed=True)
                .size()
                .unstack(fill_value=0)
                .reindex(index=top, columns=months, fill_value=0)
                .astype(float)
            )
            for m in months:
                if self._month_start(y, m) > last:
                    g[m] = np.nan  # not yet observed: blank, not zero
            grids[y] = g
        vmax = max((np.nanmax(g.to_numpy()) for g in grids.values() if g.notna().any().any()), default=1.0) or 1.0

        cap = self.cap.block(
            sub,
            f"Rows: top {len(top)} districts over the full record, in the same order in "
            "every year so rows line up across files",
            f"Colour scale fixed at 0\u2013{vmax:,.0f} across all years",
            "Blank cells are months after the record ends, not zero detections"
            if any(g.isna().any().any() for g in grids.values()) else "",
        )
        for y in years:
            lab = self._year_label(sub, y)
            g = grids[y]
            fig, ax = self._frame(
                f"District \u00d7 Month Detections, {region}: {lab}",
                f"Top {len(g)} districts \u2014 {self.cap.sub_tag}",
                cap,
                width=14,
                height=0.42 * len(g) + 3,
            )
            ax = ax[0][0]
            self._district_heatmap(ax, fig, g, vmax, [MONTH_ABB[m - 1] for m in months])
            ax.set_xlabel("Month")
            w.save(
                fig,
                f"18_district_month_heatmap_by_year/18_district_month_heatmap_{y}",
            )

    def f19_season_district_week_heatmap_by_year(self, sub, region, w):
        if "admin_unit" not in sub or sub["admin_unit"].isna().all():
            return
        n = self.cfg.plots.top_n_districts
        last = _record_end(sub)
        for win in self.cal.windows:
            d = sub[sub["season"] == win.name]
            if d.empty:
                continue
            top = d["admin_unit"].value_counts().head(n).index
            span = _window_span_days(win)
            edges = _bin_edges(span, WEEK)
            bins = [b for b, _ in edges]
            ref0 = _ref_start(win)
            years = sorted(int(y) for y in d["season_year"].unique())
            dt = d[d["admin_unit"].isin(top)]
            dt = dt.assign(day_off=_day_offset(dt, win))
            dt = dt[dt["day_off"].between(0, span)]
            dt = dt.assign(bin=(dt["day_off"] // WEEK * WEEK).astype(int))

            grids = {}
            for y in years:
                g = (
                    dt[dt["season_year"] == y]
                    .groupby(["admin_unit", "bin"], observed=True)
                    .size()
                    .unstack(fill_value=0)
                    .reindex(index=top, columns=bins, fill_value=0)
                    .astype(float)
                )
                opened = _window_start(win, y)
                for b in bins:
                    if opened + pd.Timedelta(days=b) > last:
                        g[b] = np.nan
                grids[y] = g
            vmax = max((np.nanmax(g.to_numpy()) for g in grids.values() if g.notna().any().any()), default=1.0) or 1.0

            xlabels = []
            for b, blen in edges:
                t = ref0 + pd.Timedelta(days=b)
                xlabels.append(_day_words(t) + (f"\n({blen} d)" if blen < WEEK else ""))
            stop = ref0 + pd.Timedelta(days=span + 1)
            short = [blen for _, blen in edges if blen < WEEK]
            cap = self.cap.block(
                sub,
                f"Rows: top {len(top)} {win.name} districts over the full record, fixed "
                "across years; columns: 7-day bins from the window opening, labelled "
                "by their first day",
                f"The final column covers only {_n_days(short[0])}" if short else "",
                f"Colour scale fixed at 0\u2013{vmax:,.0f} across all {win.name} years",
                "Blank cells are weeks after the record ends, not zero detections"
                if any(g.isna().any().any() for g in grids.values()) else "",
                self._marker_note(ref0, stop),
            )
            slug = _slug(win.name)
            for y in years:
                g = grids[y]
                fig, ax = self._frame(
                    f"{win.name} District \u00d7 Week Detections, {region}: "
                    f"{self._year_label(sub, y, win.name)}",
                    f"Top {len(g)} districts \u2014 {win.display_label} \u2014 {self.cap.sub_tag}",
                    cap,
                    width=16,
                    height=0.42 * len(g) + 3,
                )
                ax = ax[0][0]
                self._district_heatmap(
                    ax, fig, g, vmax, xlabels, fontsize=8 if len(bins) > 12 else 9,
                    rotate=True,
                )
                # Column k spans days [7k, 7k + 7): a date d days in sits at d/7 - 0.5.
                for t, label in self._marker_dates_in(ref0, stop):
                    x = (t - ref0).days / WEEK - 0.5
                    ax.vlines(x, -0.5, len(g) - 0.5, colors=MARKER_COLOUR,
                              linestyles=(0, (5, 3)), linewidth=1.4, zorder=2)
                ax.set_xlabel("Week starting")
                w.save(
                    fig,
                    f"19_{slug}_district_week_heatmap_by_year/"
                    f"19_{slug}_district_week_heatmap_{y}",
                )

    # -- family builders ---------------------------------------------------

    def _map_family(
        self, sub, region, w, groups, *, facet_name, indiv_stem, facet_title,
        indiv_title, notes,
    ):
        """One faceted map plus individual per-year maps on each configured scale.

        Scale top = the maximum rounded up to the next ``district_map_round``
        (bottom is always 0). 'global' takes the maximum over every year in the
        family, 'local' over the one year being drawn. The facet is always
        global: panels that shared a figure but not a scale would invite exactly
        the comparison they cannot support.
        """
        base = self._admin_base(region)
        if base is None or not groups:
            return
        gdf, key = base
        pc = self.cfg.plots
        density = pc.district_map_metric == "density"
        metric = "Detections per 1,000 km\u00b2" if density else "Detections"

        vals = {}
        for y, d, _ in groups:
            counts = d.groupby("admin_unit", observed=True).size()
            v = gdf[key].map(counts).fillna(0).astype(float)
            vals[y] = v / gdf["area_km2"] * 1000 if density else v
        peak = {y: float(np.nanmax(v.to_numpy())) if len(v) else 0.0 for y, v in vals.items()}
        years = [y for y, _, _ in groups]
        span = f"{min(years)}\u2013{max(years)}" if len(years) > 1 else str(years[0])
        g_top = _scale_top(max(peak.values()), pc.district_map_round)

        def scale_note(top, scope):
            kind = (
                f"{pc.district_map_classes} equal classes"
                if pc.district_map_style == "discrete" else "continuous ramp"
            )
            return (
                f"Colour scale 0\u2013{top:,.0f} ({kind}), {scope}; top = maximum "
                f"rounded up to the next multiple of {pc.district_map_round:,}"
            )

        head = [
            *notes,
            f"Mapped quantity: {metric.lower()}"
            + (" of total district area, which removes the district-size effect"
               if density else "; not area-normalised, so larger districts accumulate more"),
        ]
        g_scope = f"shared by every year {span}, so years are directly comparable"
        g_cap = self.cap.block(sub, *head, scale_note(g_top, g_scope))
        g_label = f"{metric}\nscale: {span}"

        ncols = min(4, len(groups))
        nrows = int(np.ceil(len(groups) / ncols))
        fig, axes = self._frame(
            facet_title,
            f"{self.cap.sub_tag} \u2014 {metric}",
            g_cap,
            width=16,
            height=nrows * (13.0 / ncols) * _map_aspect(gdf) + 0.35 * nrows,
            nrows=nrows,
            ncols=ncols,
        )
        fig.subplots_adjust(left=0.02, right=0.88, wspace=0.05, hspace=0.18)
        flat = [ax for row in axes for ax in row]
        scale = self._map_scale(g_top)
        for ax, (y, _, lab) in zip(flat, groups):
            self._choropleth(ax, gdf, vals[y], scale)
            ax.set_title(lab, fontweight="bold", fontsize=12)
        for ax in flat[len(groups):]:
            ax.axis("off")
        self._map_colourbar(fig, flat, scale, g_label, shrink=0.6, fraction=0.03)
        w.save(fig, facet_name)

        for kind in pc.district_map_scales:
            for y, d, lab in groups:
                if kind == "global":
                    top, cap, label = g_top, g_cap, g_label
                else:
                    top = _scale_top(peak[y], pc.district_map_round)
                    cap = self.cap.block(
                        sub, *head,
                        scale_note(top, f"fitted to {y} alone; do not compare colours across years"),
                    )
                    label = f"{metric}\nscale: {y} only"
                fig, ax = self._frame(
                    indiv_title(lab),
                    f"{self.cap.sub_tag} \u2014 {metric} \u2014 {len(d):,} detections",
                    cap,
                    width=12,
                    height=8,
                )
                ax = ax[0][0]
                scale = self._map_scale(top)
                self._choropleth(ax, gdf, vals[y], scale)
                self._map_colourbar(fig, ax, scale, label, shrink=0.7)
                w.save(fig, f"{facet_name}/{kind}/{indiv_stem}_{y}")

    def _map_scale(self, top: float, cmap: str = "YlOrRd"):
        """(norm, colormap, tick edges) for a 0..top district map scale."""
        from matplotlib.colors import BoundaryNorm, Normalize

        pc = self.cfg.plots
        if pc.district_map_style == "discrete":
            edges = np.linspace(0.0, top, pc.district_map_classes + 1)
            cm = plt.get_cmap(cmap, pc.district_map_classes)
            return BoundaryNorm(edges, cm.N, clip=True), cm, edges
        return Normalize(0.0, top, clip=True), plt.get_cmap(cmap), np.linspace(0.0, top, 6)

    def _weekly_family(
        self, sub, region, w, wins, years, start, stop, *, facet_name, indiv_stem,
        facet_title, indiv_title, label_of,
    ):
        last = _record_end(sub)
        # (year, season) -> list of (x, width, count), observed bins only
        bars: dict[tuple[int, str], list[tuple[float, int, int]]] = {}
        for win in wins:
            d = sub[sub["season"] == win.name]
            span = _window_span_days(win)
            d = d.assign(day_off=_day_offset(d, win))
            d = d[d["day_off"].between(0, span)]
            counts = (
                d.assign(bin=(d["day_off"] // WEEK * WEEK).astype(int))
                .groupby(["season_year", "bin"], observed=True)
                .size()
            )
            ref0 = mdates.date2num(_ref_start(win))
            for y in years:
                opened = _window_start(win, y)
                rows = []
                for b, blen in _bin_edges(span, WEEK):
                    if opened + pd.Timedelta(days=b) > last:
                        break  # no observation yet: draw nothing, not a zero
                    rows.append((ref0 + b, blen, int(counts.get((y, b), 0))))
                if rows:
                    bars[(y, win.name)] = rows
        if not bars:
            return
        ymax = max(c for rows in bars.values() for _, _, c in rows) or 1

        spans = _window_days_text(wins, WEEK)
        cap = self.cap.block(
            sub,
            "Weekly = 7-day bins counted from each window's opening ("
            + "; ".join(f"{win.name}: {_md_words(win.start)}" for win in wins)
            + "); each bar spans the dates it covers",
            (
                f"The final bin of a window is shorter ({spans}) and drawn narrower: "
                "its height is not a full week's count"
            ) if spans else "",
            "Y-axis fixed across every panel and every individual figure in this set",
            self._marker_note(start, stop),
        )

        def draw(ax, y):
            for win in wins:
                rows = bars.get((y, win.name))
                if not rows:
                    continue
                xs, ws, cs = zip(*rows)
                ax.bar(
                    xs, cs, width=ws, align="edge", color=self.pal[win.name],
                    edgecolor="white", linewidth=0.6, label=win.name,
                )
            ax.set_ylim(0, ymax)
            ax.yaxis.set_major_formatter(COMMA)
            ax.grid(axis="x", visible=False)

        ncols = min(4, len(years))
        nrows = int(np.ceil(len(years) / ncols))
        fig, axes = self._frame(
            facet_title,
            f"{self.cap.sub_tag} \u2014 7-day bins",
            cap,
            width=16,
            height=3.0 * nrows + 1,
            nrows=nrows,
            ncols=ncols,
            sharey=True,
        )
        flat = [ax for row in axes for ax in row]
        for i, (ax, y) in enumerate(zip(flat, years)):
            draw(ax, y)
            self._headroom(ax, 1.15)
            ax.set_title(label_of(y), fontweight="bold", fontsize=12)
            self._calendar_axis(ax, start, stop)
            self._mark_dates(ax, start, stop, labels=False)
            if i % ncols == 0:
                ax.set_ylabel("Fire count")
            if i == 0 and len(wins) > 1:
                _legend(ax, ncol=len(wins), loc="upper left")
        for ax in flat[len(years):]:
            ax.axis("off")
        w.save(fig, facet_name)

        for y in years:
            if not any((y, win.name) in bars for win in wins):
                continue
            total = sum(c for win in wins for _, _, c in bars.get((y, win.name), []))
            lab = label_of(y)
            fig, ax = self._frame(
                indiv_title(y, lab),
                f"{self.cap.sub_tag} \u2014 7-day bins \u2014 {total:,} detections",
                cap,
                width=16,
                height=7,
            )
            ax = ax[0][0]
            draw(ax, y)
            self._headroom(ax, 1.22)
            self._headline_note(ax, f"Total: {total:,} detections")
            self._calendar_axis(ax, start, stop)
            self._mark_dates(ax, start, stop)
            ax.set_xlabel("Date")
            ax.set_ylabel("Fire count")
            if len(wins) > 1:
                _legend(ax, title="Season", ncol=len(wins), loc="upper right")
            w.save(fig, f"{facet_name}/{indiv_stem}_{y}")

    # -- map and heatmap drawing ---------------------------------------------

    def _admin_base(self, region):
        reg = next((r for r in self.regions if r.name == region), None)
        if reg is None or reg.admin is None:
            return None
        from .aoi import admin_area_km2

        key = reg.cfg.admin_field
        areas = admin_area_km2(reg).set_index("admin_unit")["area_km2"]
        gdf = reg.admin.copy()
        gdf["area_km2"] = gdf[key].map(areas)
        return gdf, key

    @staticmethod
    def _choropleth(ax, gdf, values, scale):
        norm, cmap, _ = scale
        gdf.assign(_v=values.to_numpy()).plot(
            column="_v", ax=ax, cmap=cmap, norm=norm,
            edgecolor="white", linewidth=0.4,
        )
        ax.set_axis_off()
        ax.grid(False)

    @staticmethod
    def _map_colourbar(fig, ax, scale, label, shrink=0.7, fraction=0.15):
        from matplotlib.cm import ScalarMappable

        norm, cmap, edges = scale
        sm = ScalarMappable(norm=norm, cmap=cmap)
        cb = fig.colorbar(
            sm, ax=ax, shrink=shrink, fraction=fraction, label=label,
            ticks=edges, spacing="proportional",
        )
        cb.ax.yaxis.set_major_formatter(COMMA)

    @staticmethod
    def _district_heatmap(ax, fig, grid, vmax, xlabels, fontsize=9, rotate=False):
        cmap = plt.get_cmap("YlOrRd").with_extremes(bad="#F2F2F2")
        vals = grid.to_numpy(dtype=float)
        im = ax.imshow(
            np.ma.masked_invalid(vals), cmap=cmap, vmin=0, vmax=vmax, aspect="auto"
        )
        cut = 0.55 * vmax
        for i in range(vals.shape[0]):
            for j in range(vals.shape[1]):
                v = vals[i, j]
                if np.isnan(v):
                    continue
                ax.text(
                    j, i, _comma(v), ha="center", va="center", fontsize=fontsize,
                    fontweight="bold", color="white" if v > cut else "#262626",
                )
        ax.set_xticks(range(vals.shape[1]), xlabels)
        if rotate:
            ax.tick_params(axis="x", labelrotation=45, labelsize=9)
            for t in ax.get_xticklabels():
                t.set_ha("right")
        ax.set_yticks(range(len(grid)), [str(v).title() for v in grid.index])
        ax.grid(False)
        cb = fig.colorbar(im, ax=ax, shrink=0.8, label="Detections")
        cb.ax.yaxis.set_major_formatter(COMMA)

    # -- cross-region figures ---------------------------------------------

    def f20_region_totals(self, w):
        d = (
            self.df.groupby(["region", "season_year"], observed=True)
            .size()
            .rename("fires")
            .reset_index()
        )
        cap = self.cap.block(
            self.df,
            "States differ enormously in cropland extent; compare shapes over time, "
            "not bar heights between states",
        )
        fig, ax = self._frame(
            "Agricultural Fire Detections by State",
            f"{self.cap.sub_tag} \u2014 {self.cal.definition_text()}",
            cap,
            width=16,
        )
        ax = ax[0][0]
        years = sorted(d["season_year"].unique())
        regions = sorted(d["region"].unique())
        width = 0.8 / len(regions)
        for i, reg in enumerate(regions):
            dd = d[d["region"] == reg].set_index("season_year")["fires"].reindex(years, fill_value=0)
            xs = np.arange(len(years)) + (i - (len(regions) - 1) / 2) * width
            ax.bar(xs, dd, width=width, label=reg,
                   color=REGION_PALETTE[i % len(REGION_PALETTE)])
        ax.set_xticks(range(len(years)), [str(int(y)) for y in years])
        ax.set_xlabel("Season year")
        ax.set_ylabel("Fire count")
        ax.yaxis.set_major_formatter(COMMA)
        ax.grid(axis="x", visible=False)
        _legend(ax, ncol=min(len(regions), 4))
        self._headroom(ax, 1.32)
        w.save(fig, "20_region_totals")

    def f21_region_index(self, w):
        d = (
            self.df.groupby(["region", "season_year"], observed=True)
            .size()
            .rename("fires")
            .reset_index()
        )
        years = sorted(d["season_year"].unique())
        base = years[0]
        cap = self.cap.block(
            self.df,
            f"Each state indexed to its own {base} total = 100, which makes trajectories "
            "comparable across states of very different size",
        )
        fig, ax = self._frame(
            "Normalised Inter-Annual Trend by State",
            f"{base} = 100 \u2014 {self.cap.sub_tag}",
            cap,
        )
        ax = ax[0][0]
        for i, reg in enumerate(sorted(d["region"].unique())):
            dd = d[d["region"] == reg].set_index("season_year").reindex(years)
            b = dd["fires"].dropna()
            if b.empty or not b.iloc[0]:
                continue
            ax.plot(years, dd["fires"] / b.iloc[0] * 100, marker="o", lw=2.4, label=reg,
                    color=REGION_PALETTE[i % len(REGION_PALETTE)])
        ax.axhline(100, ls="--", color="#888888", lw=1)
        ax.set_xlabel("Season year")
        ax.set_ylabel("Index")
        ax.xaxis.set_major_locator(MaxNLocator(integer=True))
        _legend(ax, ncol=3, loc="upper left")
        w.save(fig, "21_region_trend_index")

    def f22_region_season_grid(self, w):
        d = (
            self.df.groupby(["region", "season", "season_year"], observed=True)
            .size()
            .rename("fires")
            .reset_index()
        )
        regions = sorted(d["region"].unique())
        ncols = min(2, len(regions))
        nrows = int(np.ceil(len(regions) / ncols))
        cap = self.cap.block(
            self.df, "One panel per state; y-axes are independent so each state's own "
            "seasonal balance is legible"
        )
        fig, axes = self._frame(
            "Seasonal Balance by State and Year",
            f"{self.cap.sub_tag} \u2014 {self.cal.definition_text()}",
            cap,
            width=16,
            height=4.0 * nrows + 1,
            nrows=nrows,
            ncols=ncols,
        )
        for i, reg in enumerate(regions):
            ax = axes[i // ncols][i % ncols]
            self._grouped_season_bars(ax, d[d["region"] == reg], labels=False)
            ax.set_title(reg, fontweight="bold", fontsize=14)
            ax.set_ylabel("Fire count")
            ax.yaxis.set_major_formatter(COMMA)
            ax.grid(axis="x", visible=False)
            if i == 0:
                _legend(ax, title="Season", ncol=len(self.cal.names))
        for j in range(len(regions), nrows * ncols):
            axes[j // ncols][j % ncols].axis("off")
        w.save(fig, "22_region_season_grid")

    def _detail_window(self, season: str):
        """The season window, or the narrower MM-DD override from the config."""
        win = self.cal.window(season)
        dw = self.cfg.plots.detail_window
        if not dw:
            return win
        return win.model_copy(update={"start": dw["start"], "end": dw["end"]})

    # -- calendar-date axes ---------------------------------------------------

    def _year_label(self, sub, season_year, season=None) -> str:
        """'2026 (record ends 28 Jun)' when the record stops inside the window.

        Goes in the panel or figure title, because the simple figures drop the
        caption that would otherwise carry the partial-year warning.
        """
        label = str(int(season_year))
        if sub.empty or not self.cfg.plots.partial_year_titles:
            return label
        last = _record_end(sub)
        wins = [self.cal.window(season)] if season else self.cal.windows
        ends = [_window_end(win, int(season_year)) for win in wins]
        if ends and last < max(ends):
            when = _day_words(last) + ("" if last.year == season_year else f" {last.year}")
            label += f" (record ends {when})"
        return label

    def _month_start(self, season_year: int, month: int) -> pd.Timestamp:
        """First day of ``month`` in calendar terms, for a given season year."""
        name = _season_of_month(self.cal, month)
        year = season_year
        if name:
            win = self.cal.window(name)
            if win.wraps and month < win.start_md[0]:
                year += 1
        return pd.Timestamp(year, month, 1)

    def _marker_dates_in(self, start: pd.Timestamp, stop: pd.Timestamp):
        """Configured markers falling in ``[start, stop)``, as reference dates."""
        out = []
        for m in self.cfg.plots.date_markers:
            for year in range(start.year, stop.year + 1):
                try:
                    t = pd.Timestamp(year, *m.md)
                except ValueError:  # 29 Feb in a common year
                    continue
                if start <= t < stop:
                    out.append((t, m.label))
        return sorted(out)

    def _marker_note(self, start, stop) -> str:
        found = self._marker_dates_in(start, stop)
        if not found:
            return ""
        words = [_day_words(t) + (f" ({lab})" if lab else "") for t, lab in found]
        return "Dashed vertical lines mark " + _join_words(words)

    def _calendar_axis(self, ax, start: pd.Timestamp, stop: pd.Timestamp) -> None:
        """Day-of-month tick labels over ``[start, stop)``.

        Ticks fall on the 1st and 15th, or on the 1st only when the axis is too
        narrow for both. Configured marker dates are always ticks, in bold, so
        each dashed line is labelled by the axis itself.
        """
        days = pd.date_range(start, stop - pd.Timedelta(days=1), freq="D")
        ax_in = ax.get_position().width * ax.figure.get_figwidth()
        both = days[days.day.isin([1, 15])]
        use = both if len(both) and ax_in / len(both) >= 0.34 else days[days.day == 1]
        marks = {t for t, _ in self._marker_dates_in(start, stop)}
        # A regular tick closer to a marker than one label width would print on
        # top of it; the marker wins.
        min_gap = len(days) / max(ax_in, 0.1) * 0.22  # days per 0.22 in
        use = [
            t for t in use
            if t in marks or all(abs((t - m).days) >= min_gap for m in marks)
        ]
        ticks = sorted(set(use) | marks)
        ax.set_xticks(
            [mdates.date2num(t) for t in ticks], [_day_words(t) for t in ticks]
        )
        roomy = bool(ticks) and ax_in / len(ticks) >= 0.8
        ax.tick_params(axis="x", labelrotation=0 if roomy else 45, labelsize=10 if roomy else 9)
        for t, lab in zip(ticks, ax.get_xticklabels()):
            if not roomy:
                lab.set_ha("right")
                lab.set_rotation_mode("anchor")
            if t in marks:
                lab.set_fontweight("bold")
                lab.set_color("#111111")
        ax.set_xlim(mdates.date2num(start) - 1, mdates.date2num(stop) + 1)

    def _mark_dates(self, ax, start, stop, labels: bool = True) -> None:
        """Dashed vertical lines at the configured marker dates.

        Drawn with ``vlines`` (a LineCollection), not ``axvline``: data series
        stay the only Line2D artists, which the one-line-per-year tests rely on.
        """
        for t, label in self._marker_dates_in(start, stop):
            x = mdates.date2num(t)
            ax.vlines(
                x, 0, 1, transform=ax.get_xaxis_transform(), colors=MARKER_COLOUR,
                linestyles=(0, (5, 3)), linewidth=1.1, zorder=2.5,
            )
            if label and labels:
                ax.text(
                    x, 0.98, f" {label}", transform=ax.get_xaxis_transform(),
                    rotation=90, ha="left", va="top", fontsize=9, color=MARKER_COLOUR,
                )

    # -- shared drawing ----------------------------------------------------

    def _grouped_season_bars(self, ax, d, labels=True, fmt=_comma):
        years = sorted(d["season_year"].unique())
        names = [n for n in self.cal.names if n in set(d["season"])]
        width = 0.8 / max(len(names), 1)
        for i, name in enumerate(names):
            dd = d[d["season"] == name].set_index("season_year")["fires"].reindex(years, fill_value=0)
            xs = np.arange(len(years)) + (i - (len(names) - 1) / 2) * width
            bars = ax.bar(xs, dd, width=width, label=name, color=self.pal[name])
            if labels:
                _bar_labels(ax, bars, dd.to_numpy(), colour="#262626", size=9, fmt=fmt)
        ax.set_xticks(range(len(years)), [str(int(y)) for y in years])

    def _covid(self, ax, yr):
        cy = self.cfg.plots.covid_year
        if cy not in set(yr["season_year"]):
            return
        i = list(yr["season_year"]).index(cy)
        v = float(yr.loc[yr["season_year"] == cy, "fires"].iloc[0])
        ax.annotate(
            "COVID-19\nharvest-labour\ndisruption",
            (i, v * 0.55),
            ha="center",
            va="center",
            fontsize=10,
            fontweight="bold",
            color="#262626",
            bbox=dict(boxstyle="round,pad=0.35", fc="white", ec="none", alpha=0.9),
        )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _apply_headline_platform(df: pd.DataFrame, group: str) -> pd.DataFrame:
    if group == "BOTH" or df.empty:
        return df
    return df[df["platform"] == group]


def _season_year_counts(sub: pd.DataFrame, cal: SeasonCalendar) -> pd.DataFrame:
    d = (
        sub.groupby(["season_year", "season"], observed=True)
        .size()
        .rename("fires")
        .reset_index()
    )
    if d.empty:
        return d
    years = sorted(d["season_year"].unique())
    full = pd.MultiIndex.from_product([years, cal.names], names=["season_year", "season"])
    return d.set_index(["season_year", "season"]).reindex(full, fill_value=0).reset_index()


def _season_of_month(cal: SeasonCalendar, month: int) -> str:
    for w in cal.windows:
        if month in _months_of(w):
            return w.name
    return ""


def _months_of(w) -> set[int]:
    from .seasons import _window_months

    return set(_window_months(w))


def _day_offset(d: pd.DataFrame, w) -> pd.Series:
    """Days since the window opened, correct for wrapping windows."""
    dates = pd.to_datetime(d["acq_date"])
    starts = pd.to_datetime(
        {
            "year": d["season_year"].astype(int),
            "month": w.start_md[0],
            "day": w.start_md[1],
        }
    )
    starts.index = dates.index
    return (dates - starts).dt.days


def _window_span_days(w) -> int:
    from .seasons import _window_length

    return _window_length(w, 2001) - 1


def _window_days(cal: SeasonCalendar) -> list[tuple[str, int]]:
    from .seasons import _window_length

    return [(w.name, _window_length(w, 2001)) for w in cal.windows]


def _ref_start(w) -> pd.Timestamp:
    return pd.Timestamp(REF_YEAR, *w.start_md)


def _ref_window(w, span: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Reference-year ``[start, stop)`` of a window ``span + 1`` days long."""
    start = _ref_start(w)
    return start, start + pd.Timedelta(days=span + 1)


def _window_start(w, season_year: int) -> pd.Timestamp:
    return pd.Timestamp(int(season_year), *w.start_md)


def _window_end(w, season_year: int) -> pd.Timestamp:
    year = int(season_year) + (1 if w.wraps else 0)
    m, d = w.end_md
    try:
        return pd.Timestamp(year, m, d)
    except ValueError:  # 29 Feb in a common year
        return pd.Timestamp(year, m, d - 1)


def _record_end(sub: pd.DataFrame) -> pd.Timestamp:
    return pd.Timestamp(pd.to_datetime(sub["acq_date"]).max()).normalize()


def _scale_top(vmax: float, base: int) -> float:
    """``vmax`` rounded up to the next multiple of ``base``; never below ``base``.

    An exact multiple stays put (1,000 -> 1,000 with base 500). The small
    tolerance stops float noise in a density (999.9999999) from being read as
    just over a multiple and pushing the scale a whole step higher.
    """
    import math

    if not np.isfinite(vmax) or vmax <= 0:
        return float(base)
    return float(base * max(1, math.ceil(vmax / base - 1e-9)))


def _map_aspect(gdf) -> float:
    """Height / width of a lon-lat layer as geopandas draws it."""
    x0, y0, x1, y1 = gdf.total_bounds
    if x1 <= x0 or y1 <= y0:
        return 1.0
    return float((y1 - y0) / ((x1 - x0) * np.cos(np.radians((y0 + y1) / 2))))


def _bin_edges(span: int, width: int) -> list[tuple[int, int]]:
    """``(first day offset, n days)`` of each bin over a ``span + 1`` day window."""
    n_days = span + 1
    return [(b, min(width, n_days - b)) for b in range(0, n_days, width)]


def _window_days_text(wins, width: int) -> str:
    parts = []
    for win in wins:
        tail = (_window_span_days(win) + 1) % width
        if tail:
            parts.append(f"{win.name}: {tail} d")
    return "; ".join(parts)


def _n_days(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


def _day_words(t) -> str:
    return f"{t.day} {MONTH_ABB[t.month - 1]}"


def _join_words(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def _span(yr: pd.DataFrame) -> str:
    return f"{int(yr['season_year'].min())}\u2013{int(yr['season_year'].max())}"


def _span_dates(sub: pd.DataFrame) -> str:
    d = pd.to_datetime(sub["acq_date"])
    return f"{d.min():%b %Y} to {d.max():%b %Y}"


def _md_words(md: str) -> str:
    m, dd = md.split("-")
    return f"{int(dd)} {MONTH_ABB[int(m) - 1]}"


def _slug(s: str) -> str:
    return "".join(c.lower() if c.isalnum() else "_" for c in str(s)).strip("_")


def render(cfg: Config, df: pd.DataFrame, regions=None, out_dir: Path | None = None) -> list[Path]:
    out = Path(out_dir) if out_dir else cfg.out_path / "figs"
    return FigureSuite(cfg, df, regions=regions).render_all(out)