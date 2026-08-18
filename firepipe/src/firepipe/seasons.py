"""Season windows: assignment, wrap-around handling, and fetch scheduling.

The single source of truth for which dates belong to which season is
:class:`SeasonCalendar`. It also emits the exact date ranges the FIRMS client
should request, so no API transaction is ever spent on a date that the
analysis would immediately discard.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .config import SeasonConfig, SeasonWindow

OFF_SEASON = "Off-season"


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date
    season: str
    season_year: int

    @property
    def n_days(self) -> int:
        return (self.end - self.start).days + 1


class SeasonCalendar:
    def __init__(self, cfg: SeasonConfig):
        self.cfg = cfg
        self.windows: list[SeasonWindow] = list(cfg.windows)
        self._check_overlap()

    # -- validation --------------------------------------------------------

    def _check_overlap(self) -> None:
        """Reject overlapping windows; a detection must have one season."""
        probe = pd.date_range("2001-01-01", "2001-12-31", freq="D")
        hits = np.zeros(len(probe), dtype=int)
        for w in self.windows:
            hits += np.array([_in_window(d.month, d.day, w) for d in probe], dtype=int)
        if hits.max() > 1:
            clash = probe[hits > 1][0].strftime("%d %b")
            raise ValueError(
                f"Season windows overlap (e.g. {clash} falls in more than one). "
                "Adjust the start/end anchors so every date has one season."
            )

    # -- introspection -----------------------------------------------------

    @property
    def names(self) -> list[str]:
        return [w.name for w in self.windows]

    @property
    def palette(self) -> dict[str, str]:
        fallback = ["#EE3B33", "#F0A202", "#3B6EA5", "#4C9A2A", "#8B1A12"]
        return {
            w.name: (w.colour or fallback[i % len(fallback)])
            for i, w in enumerate(self.windows)
        }

    def covered_months(self) -> list[int]:
        months: set[int] = set()
        for w in self.windows:
            probe = pd.date_range("2001-01-01", "2001-12-31", freq="D")
            for d in probe:
                if _in_window(d.month, d.day, w):
                    months.add(d.month)
        return sorted(months)

    def excluded_months(self) -> list[int]:
        return [m for m in range(1, 13) if m not in self.covered_months()]

    def window(self, name: str) -> SeasonWindow:
        for w in self.windows:
            if w.name == name:
                return w
        raise KeyError(f"Unknown season '{name}'. Known: {self.names}")

    # -- assignment --------------------------------------------------------

    def assign(self, dates: pd.Series) -> pd.DataFrame:
        """Return a frame with ``season`` and ``season_year`` for each date."""
        d = pd.to_datetime(dates)
        season = pd.Series(OFF_SEASON, index=d.index, dtype=object)
        season_year = d.dt.year.astype("int64")

        for w in self.windows:
            mask = _in_window_vec(d, w)
            season = season.mask(mask, w.name)
            if w.wraps:
                # Dates in the tail of a wrapping window belong to the season
                # year in which that window opened, i.e. the previous year.
                tail = mask & (
                    (d.dt.month < w.start_md[0])
                    | ((d.dt.month == w.start_md[0]) & (d.dt.day < w.start_md[1]))
                )
                season_year = season_year.mask(tail, season_year - 1)

        return pd.DataFrame({"season": season, "season_year": season_year})

    # -- scheduling --------------------------------------------------------

    def fetch_ranges(self, start: date, end: date) -> list[DateRange]:
        """Contiguous in-season date ranges clipped to ``[start, end]``."""
        out: list[DateRange] = []
        for year in range(start.year - 1, end.year + 2):
            for w in self.windows:
                w_start = _safe_date(year, *w.start_md)
                if w_start is None:
                    continue
                end_year = year + 1 if w.wraps else year
                w_end = _safe_date(end_year, *w.end_md)
                if w_end is None:  # 29 Feb in a common year -> use 28 Feb
                    w_end = _safe_date(end_year, w.end_md[0], w.end_md[1] - 1)
                if w_end is None or w_end < start or w_start > end:
                    continue
                out.append(
                    DateRange(
                        start=max(w_start, start),
                        end=min(w_end, end),
                        season=w.name,
                        season_year=year,
                    )
                )
        if not self.cfg.drop_unassigned:
            out = _merge_full_span(out, start, end)
        return sorted(out, key=lambda r: (r.start, r.season))

    def chunk(self, ranges: list[DateRange], max_days: int = 10) -> list[DateRange]:
        """Split ranges into <= ``max_days`` blocks (the FIRMS API ceiling)."""
        chunks: list[DateRange] = []
        for r in ranges:
            cur = r.start
            while cur <= r.end:
                stop = min(cur + timedelta(days=max_days - 1), r.end)
                chunks.append(DateRange(cur, stop, r.season, r.season_year))
                cur = stop + timedelta(days=1)
        return chunks

    # -- caption text ------------------------------------------------------

    def definition_text(self) -> str:
        parts = [
            f"{w.name} = {_md_label(w.start)} to {_md_label(w.end)}"
            + (" (wraps into the following year)" if w.wraps else "")
            for w in self.windows
        ]
        return "; ".join(parts)

    def exclusion_text(self) -> str:
        ex = self.excluded_months()
        if not ex:
            return "All months are covered by a season window"
        if not self.cfg.drop_unassigned:
            return (
                "Months outside every window are retained and labelled "
                f"'{OFF_SEASON}': " + ", ".join(_MONTH_ABB[m] for m in ex)
            )
        return "Excluded from all figures (in no season window): " + ", ".join(
            _MONTH_ABB[m] for m in ex
        )

    def qc_table(self) -> pd.DataFrame:
        """Auditable dump of the season definition actually in force."""
        rows = []
        for w in self.windows:
            rows.append(
                {
                    "season": w.name,
                    "label": w.display_label,
                    "start_md": w.start,
                    "end_md": w.end,
                    "wraps_year": w.wraps,
                    "n_days_common_year": _window_length(w, 2001),
                    "n_days_leap_year": _window_length(w, 2000),
                    "months_touched": ",".join(
                        _MONTH_ABB[m] for m in _window_months(w)
                    ),
                }
            )
        df = pd.DataFrame(rows)
        df.attrs["excluded_months"] = ",".join(
            _MONTH_ABB[m] for m in self.excluded_months()
        )
        return df


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

_MONTH_ABB = {
    i: n
    for i, n in enumerate(
        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
        start=1,
    )
}


def _in_window(month: int, day: int, w: SeasonWindow) -> bool:
    key = (month, day)
    if w.wraps:
        return key >= w.start_md or key <= w.end_md
    return w.start_md <= key <= w.end_md


def _in_window_vec(d: pd.Series, w: SeasonWindow) -> pd.Series:
    key = d.dt.month * 100 + d.dt.day
    s = w.start_md[0] * 100 + w.start_md[1]
    e = w.end_md[0] * 100 + w.end_md[1]
    if w.wraps:
        return (key >= s) | (key <= e)
    return (key >= s) & (key <= e)


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _window_length(w: SeasonWindow, year: int) -> int:
    n = 0
    d = date(year, 1, 1)
    while d.year == year:
        if _in_window(d.month, d.day, w):
            n += 1
        d += timedelta(days=1)
    return n


def _window_months(w: SeasonWindow) -> list[int]:
    months = set()
    d = date(2000, 1, 1)
    while d.year == 2000:
        if _in_window(d.month, d.day, w):
            months.add(d.month)
        d += timedelta(days=1)
    return sorted(months)


def _md_label(md: str) -> str:
    m, dd = md.split("-")
    return f"{int(dd)} {_MONTH_ABB[int(m)]}"


def _merge_full_span(
    ranges: list[DateRange], start: date, end: date
) -> list[DateRange]:
    """When off-season data is kept, request the whole span instead."""
    return [DateRange(start, end, OFF_SEASON, start.year)]
