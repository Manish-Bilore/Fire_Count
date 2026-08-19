# firepipe

A configurable pipeline for NASA FIRMS active-fire analysis over administrative
regions. It consolidates the earlier one-off R scripts into a single system that
takes a config file and produces analysis-ready detections, QC tables and a
publication figure suite.

What it does:

1. **Extracts** FIRMS detections for any set of regions defined by GeoPackage
   boundaries, requesting only the dates that fall inside your season windows.
2. **Masks** to cropland using precomputed rasters, ESA WorldCover, or Dynamic
   World.
3. **Splits** by arbitrary season windows, including ones that wrap the new year.
4. **Renders** figures whose captions are derived from the config, so method text
   cannot drift away from the filter that produced the data.

Everything is driven by one YAML file. Nothing analytical is hard-coded.

---

## Install

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[all]"            # or: pip install -r requirements.txt
```

`[all]` pulls in the MCP SDK and the Earth Engine client. Use plain
`pip install -e .` if you only need the CLI with WorldCover or precomputed masks.

Get a free FIRMS MAP_KEY from <https://firms.modaps.eosdis.nasa.gov/api/map_key/>
and export it:

```bash
export FIRMS_MAP_KEY=your_key_here
```

---

## Quick start

```bash
firepipe layers ./gpkg/uttar_pradesh_admin.gpkg   # what layers/fields exist?
firepipe init -o config.yaml                      # starter config
firepipe key -c config.yaml                       # MAP_KEY works? quota left?
firepipe plan -c config.yaml                      # how many API calls will this cost?
firepipe run  -c config.yaml --density            # fetch, filter, mask, plot
firepipe plot -c config.yaml                      # re-render figures only
```

`configs/igp_2019_2026.yaml` is a ready-made config for Uttar Pradesh, Punjab,
Haryana and Madhya Pradesh, 2019–2026, with Rabi = 1 Mar – 30 Jun and
Kharif = 1 Oct – 31 Dec.

Always run `plan` before `run`. It reports the exact API workload without
downloading anything:

```
API workload
                           bbox  in_season_days  blocks_per_platform  platforms  api_requests
73.7798,20.9707,84.7347,32.6116            1712                  184          3           552

Total API requests: 552 (bbox efficiency 0.46, FIRMS allows 5,000 per 10 minutes)
```

Four states, eight years, three platforms: 552 requests. This is possible because
the season windows drive the request schedule — off-season days are never
requested at all.

---

## Configuration

### Regions

Any GeoPackage with a boundary layer and, optionally, an admin subdivision layer.

```yaml
regions:
  - name: Uttar Pradesh
    gpkg: ./gpkg/uttar_pradesh_admin.gpkg
    boundary_layer: state_boundary
    admin_layer: district_boundary
    admin_field: DISTRICT
    parent_field: STATE_UT
    where: "DISTRICT in ['AGRA', 'ALIGARH']"   # optional subset
```

Layers are reprojected to WGS 84 on load. Survey of India products are sometimes
issued on an Everest 1830 datum; the loader warns when it sees one, because
reprojecting without a datum transformation leaves offsets on the order of a
VIIRS pixel — enough to move detections between districts.

### Seasons

Windows are MM-DD anchors. `start` later than `end` means the window wraps into
the following year, and the *season year* is always the year the window opened —
so a January detection in a Nov→Feb window is counted with the previous
November.

```yaml
seasons:
  drop_unassigned: true     # false keeps off-season detections, labelled 'Off-season'
  windows:
    - name: Rabi
      start: "03-01"
      end: "06-30"
      label: Rabi (wheat residue)
    - name: Kharif
      start: "10-01"
      end: "12-31"
```

Overlapping windows are rejected at load time rather than silently double
counting. Months in no window are excluded, and every caption says so.

### Sensors

```yaml
sensors:
  platforms: [VIIRS_SNPP, VIIRS_NOAA20, MODIS]
  processing: auto          # SP where archived, NRT for the recent tail
  viirs_confidence: [h, n]
  modis_confidence_min: 30  # parity with VIIRS h+n
  headline_platform_group: VIIRS
```

`processing: auto` queries `/api/data_availability` and routes each date block to
the dataset that actually covers it, so a 2019–2026 request transparently spans
Standard Processing and the NRT tail.

`headline_platform_group` decides which platform drives the main figures. VIIRS
at 375 m and MODIS at 1 km do not produce comparable counts, so MODIS is kept as
an independent cross-check rather than pooled. Setting `BOTH` pools them anyway
and every caption relabels itself to say the counts are not footprint-comparable.

### Cropland mask

```yaml
mask:
  kind: esa_worldcover      # none | esa_worldcover | dynamic_world | precomputed_raster
  esa_version: v200         # v200/2021 or v100/2020
  esa_remote: true          # read tiles over /vsicurl, nothing to download
  crop_classes: [40]        # WorldCover 40 = cropland; Dynamic World 4 = crops
  sampling: point           # or 'fraction'
  min_crop_fraction: 0.5
```

| Backend | When to use it |
|---|---|
| `precomputed_raster` | You already have a mask and want exact reproducibility |
| `esa_worldcover` | Default. 10 m, no auth, streams from public S3 |
| `dynamic_world` | The mask should be contemporaneous with the fires |
| `none` | Masking happened upstream |

**Dynamic World** builds one composite per season-year, so a 2025 detection is
tested against 2025 land cover. That answers the standard objection to a fixed
2021 epoch, at the cost of an Earth Engine dependency:

```bash
pip install earthengine-api && earthengine authenticate
```

**Point vs fraction sampling.** `point` tests the 10 m pixel under the detection
centroid. `fraction` requires at least `min_crop_fraction` of pixels within the
sensor footprint (187.5 m for VIIRS, 500 m for MODIS) to be cropland. Point
sampling is simpler and is the default; fraction sampling is the defensible
answer when a reviewer asks about mixed pixels. Use local tiles
(`esa_remote: false`) for fraction sampling — it issues many small reads.

---

## Outputs

```
output/
├── config.resolved.yaml         # exactly what ran, including defaults
├── manifest.json                # provenance: fingerprint, counts, funnel, settings
├── data/
│   ├── detections.parquet       # analysis-ready, one row per detection
│   ├── detections.csv
│   └── detections_<region>.csv
├── qc/
│   ├── qc_funnel.csv            # rows removed at every stage
│   ├── qc_season_definition.csv
│   ├── qc_platform_by_year.csv  # platform mix drift contaminates pooled trends
│   ├── qc_sensor_by_year.csv    # a sensor entering mid-record inflates counts
│   ├── qc_record_extent.csv     # exposes partial years
│   ├── qc_land_cover.csv
│   └── qc_confidence_composition.csv
└── figs/
    ├── <region>/                # 18 figures per region
    └── _comparison/             # cross-region figures
```

The funnel is the audit trail:

```
stage                  n_records  removed  retained_pct  note
firms_raw                 335110        0        100.00  detections returned by the API
within_admin_boundary     335109        1        100.00  clipped to region polygons
confidence_filter         294161    40948         87.78  VIIRS in ['h','n']; MODIS >= 30
season_window             270167    23994         91.84  Rabi = 1 Mar to 30 Jun; ...
cropland_mask             198432    71735         73.45  ESA WorldCover v200 (2021), class [40]
```

### Figures

Per region: `01` yearly counts · `02` season × year · `03` season share ·
`04`/`04b` monthly panels and month × year heatmap · `05` 5-day bins per season ·
`06` cumulative curves per season · `07` FRP load · `08` MODIS vs VIIRS
cross-check · `09`/`10` season-isolated deck figures · `11` top districts ·
`12` district × year heatmap · `13` district choropleth (counts and per-1,000 km²).

Cross-region: `20` totals by state · `21` normalised trend index · `22` seasonal
balance grid.

Render a subset with `plots.figures: ["01", "08", "20"]`.

Cumulative curves are plotted against days-since-window-opening rather than day
of year, so they contain no flat gaps over excluded months. A final year whose
record stops before the season window closes is flagged automatically:

> NOTE: the 2025 record ends 30 Nov 2025, before the season window closes —
> partial year, do not read as a decline

---

## Caption integrity

Every caption line is derived from the config objects. Change
`viirs_confidence` from `[h, n]` to `[h]` and the subtitle changes from
"high + nominal confidence" to "high confidence" without touching a string. The
test suite asserts this directly — `test_changing_a_window_changes_the_caption`
exists to catch the regression where figure text quietly describes a filter that
is no longer in force.

---

## Methodological choices

Deliberately kept simple, with consequences documented rather than hidden:

- **Confidence filtering only.** No source-type filter, no persistent thermal
  anomaly mask. Non-vegetation thermal sources such as brick kilns survive the
  cropland mask and are a documented background signal.
- **No cross-platform deduplication.** MODIS and VIIRS are reported as parallel
  series, never pooled into a headline count. `qc_platform_by_year.csv` exists so
  platform-mix drift is visible.
- **Counts are detections, not fires.** One fire observed on four overpasses is
  four detections. Every caption says so.
- **`fetch_ranges` drives everything.** The season windows that define the
  analysis also define which dates are requested, so the API cost scales with
  the analysis rather than the calendar.

---

## MCP server

The pipeline is also exposed as an MCP server for use from an agent client:

```bash
firepipe serve -c config.yaml
```

```json
{
  "mcpServers": {
    "firepipe": {
      "command": "/path/to/.venv/bin/firepipe",
      "args": ["serve", "-c", "/path/to/config.yaml"],
      "env": { "FIRMS_MAP_KEY": "your_key_here" }
    }
  }
}
```

| Tool | Purpose |
|---|---|
| `firms_write_template_config` | Write a starter config |
| `firms_describe_config` | Regions, seasons, sensors, mask, date span |
| `firms_list_gpkg_layers` | Layers, fields and feature counts in a GeoPackage |
| `firms_check_map_key` | MAP_KEY validity and transactions used |
| `firms_data_availability` | Date coverage per FIRMS dataset |
| `firms_plan_extraction` | Dry run: API cost, bbox, in-season days |
| `firms_run_pipeline` | Full extraction, optionally rendering figures |
| `firms_render_figures` | Re-render without re-fetching |
| `firms_summarise_detections` | Aggregate by region, season, district, platform |
| `firms_get_qc_report` | Funnel, platform mix, record extent, season definition |
| `firms_detection_density` | Detections per 1,000 km² by admin unit |

Only `firms_run_pipeline`, `firms_render_figures` and
`firms_write_template_config` write anything; the rest are read-only and marked
as such. Tables are truncated with an explicit `truncated` flag and a pointer to
the file on disk rather than flooding the context window.

Built against MCP SDK 2.0 (`mcp.server.mcpserver.MCPServer`), with a fallback
import for SDK 1.x FastMCP.

---

## Working from an existing archive

Any previously downloaded FIRMS export can be pushed through the identical
filtering, masking and plotting path:

```bash
firepipe run -c config.yaml --from-csv agricultural_fires.csv
```

Legacy short source labels (`MODIS`, `SUOMI`, `VIIRS_J1`) are mapped onto dataset
ids automatically. This is also how you reproduce an older analysis under new
season definitions without spending any API quota.

---

## Python API

```python
from firepipe import Config, Pipeline, render

cfg = Config.from_yaml("configs/igp_2019_2026.yaml")
cfg.sensors.headline_platform_group = "MODIS"   # sensitivity test

pipe = Pipeline(cfg)
df = pipe.run()
render(cfg, df, regions=pipe.regions)

print(pipe.funnel.to_frame())
```

`cfg.fingerprint` is a stable hash of the analytical settings, ignoring
`out_dir`, so two runs can be compared for methodological equivalence.

---

## Tests

```bash
pytest -q
```

22 tests covering season wrap logic, season-year attribution, request scheduling
and chunking, config validation, MODIS/VIIRS schema normalisation, legacy label
mapping, bbox arithmetic, and caption derivation.

---

## Rate limits and caching

The MAP_KEY allowance is 5,000 transactions per rolling 10-minute window, and a
multi-day request can count as more than one transaction. The client budgets
conservatively, backs off on 429, and caches every retrieved block to
`~/.cache/firepipe/firms`. An interrupted run resumes without re-spending quota,
and re-running with different season or confidence settings against the same
dates costs nothing.
