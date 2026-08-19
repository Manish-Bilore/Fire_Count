# Changelog

All notable changes to firepipe. Versions follow [semantic versioning](https://semver.org).

## [1.1.0] - 2026-08-19

### Added
- A MAP_KEY is now required only on a genuine cache miss, so re-running a
  fully cached analysis works offline and without credentials. The dataset
  availability table is cached for 24 hours for the same reason: SP/NRT
  routing must give the same answer offline as it did when the blocks were
  fetched, or cache keys diverge.
- A run that would overwrite an out_dir holding a different config's output now
  logs a warning naming the differences (platforms, mask, seasons, regions).
  Copying a config to vary one setting and forgetting to change `out_dir` is
  easy to do and silently destroys the earlier result.
- `plots_simple.SimpleFigureSuite`: the same figures without the analytical
  summary caption, subtitle, mean reference line or in-plot total, written to
  `simple_plots/` alongside `figs/`. Figure bodies are inherited, so changes in
  `plots.py` propagate automatically; only presentation hooks are overridden.
- `--plots {full,simple,both}` on `firepipe run` and `firepipe plot`, and a
  `style` argument on the `firms_render_figures` MCP tool.
- Figure hooks `_frame`, `_mean_line`, `_headline_note` and `_headroom` on
  `FigureSuite`, so alternative presentations need no duplicated figure code.

- `--from-csv` now honours `sensors.platforms` and the configured date range.
  An ingested archive was previously analysed in full regardless of the
  platform list, so an offline run could silently include sensors the config
  excluded and disagree with the equivalent live fetch.
- A `headline_platform_group` absent from `sensors.platforms` is rejected at
  config load. The combination filtered every figure to an empty frame and
  produced no figures at all, silently.
- Titles now mark intra-group pooling too: a `VIIRS` headline over both S-NPP
  and NOAA-20 reads "S-NPP + NOAA-20 pooled". A group pools every satellite
  listed under it, so two VIIRS platforms is still a sum and the simple plots
  had no other way to say so.
- The `BOTH`-without-MODIS error now names the setting to use instead, and
  explains that a group already pools the satellites within it.
- Pooled-platform runs (`headline_platform_group: BOTH`) now carry
  "MODIS + VIIRS pooled" in the figure title, not only in the caption. The
  simple figures have no caption, so the title is the only text that survives
  every presentation mode.
- A cropland mask that fails to classify more than 20% of detections now raises
  instead of silently dropping them. A total mask failure - no network route to
  the tiles, an SSL-intercepting proxy - previously produced a successful run
  with zero detections.

### Fixed
- **Figures 05 (pentad) and 06 (cumulative) were wrong in 1.0.2.** An
  over-broad string replacement in the 1.0.2 pandas 3 fix passed a whole
  DataFrame to `ax.plot` instead of the `fires` column, drawing one line per
  *column* rather than one per year: 21 lines and 21 legend entries instead of
  7, plus two spurious flat lines. **Anyone who rendered figures with 1.0.2
  should re-render.** No other figure and no data output was affected.
- Empty subtitles and captions now reclaim their layout band instead of
  leaving white space.

### Testing
- Artist-count regression tests assert that line figures draw exactly one
  series per season year and bar figures one bar per group, which is what
  catches a whole-frame argument silently drawing extra series.

## [1.0.3] - 2026-08-18

### Security
- `config.resolved.yaml` is written into every output directory and previously
  echoed `firms.map_key` verbatim. It is now redacted. Anyone who set the key
  in a config rather than the environment should **rotate it** and check
  existing output directories before publishing them.

## [1.0.2] - 2026-08-18

### Fixed
- Figures failed with `TypeError: Invalid value '0' for dtype 'str'` when a
  platform, season or region had no rows at all in a given year. `reindex`
  filled a text column with `0`; it now reindexes only the numeric series.
- `version` mixed `2.0` (Standard Processing) with `'2.0NRT'` (Near Real-Time),
  breaking the parquet write. It is now always text.

### Added
- Persistent land-cover sample cache keyed by coordinate. Sampling 400k
  detections over `/vsicurl` takes roughly 25 minutes; a re-run now takes
  seconds. Keyed by mask source, so changing the mask invalidates it. Disable
  with `mask.cache_samples: false`.
- GDAL tuning for remote raster reads (`GDAL_DISABLE_READDIR_ON_OPEN` and
  friends), which avoids re-listing the bucket on every point sample.

## [1.0.1] - 2026-08-18

### Fixed
- Parquet writes failed with `ArrowInvalid` because `confidence` held `'h'`/`'n'`
  for VIIRS and integers for MODIS in one column. It is now always text, with
  typed `confidence_class` and `confidence_value` columns beside it.
- Outputs are written CSV-first with a cast-to-text parquet fallback, so a
  serialisation fault can no longer discard a run that cost an hour of API
  calls and mask sampling.
- `-v` was only accepted before the subcommand; it now works in either position.

### Changed
- `firms.max_day_range` defaults to 5. FIRMS documents a 1–10 day range but
  enforces 1–5. The client reads the real ceiling from the rejection message,
  adopts it for the rest of the run, and re-splits the affected block, so a
  future change on the API side does not stop a run.

## [1.0.0] - 2026-08-14

Initial release.

- Configurable extraction of NASA FIRMS active fire detections for regions
  defined by GeoPackage boundaries.
- Season windows as MM-DD anchors, including windows that wrap the new year,
  with season-year attribution and overlap rejection.
- Season windows drive the request schedule, so out-of-season dates are never
  requested.
- Cropland masking via ESA WorldCover, Dynamic World, or a precomputed raster,
  with centroid or footprint-fraction sampling.
- Automatic Standard Processing / Near Real-Time routing from
  `/api/data_availability`.
- Figure suite of 18 per-region and 3 cross-region figures, with captions
  derived from the config so method text cannot drift from the filter applied.
- QC exports: filter funnel, platform mix by year, sensor availability, record
  extent, land-cover composition, season definition.
- CLI and MCP server interfaces.
