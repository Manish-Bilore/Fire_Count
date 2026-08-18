# Local setup guide

Everything below was run end to end in a clean virtual environment before being
written down. Commands are for Linux; Windows differences are noted where they
matter.

---

## 1. Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.10+ | 3.12 tested. `python3 --version` |
| ~2 GB disk | Dependencies ~800 MB, plus outputs and the FIRMS cache |
| FIRMS MAP_KEY | Free, one form: <https://firms.modaps.eosdis.nasa.gov/api/map_key/> |

**No system GDAL needed.** `geopandas`, `rasterio` and `pyogrio` ship binary
wheels with GDAL bundled. Do not `apt install gdal-bin` first — a system GDAL of
a different version is the most common source of import errors here.

Optional, only if you want the Dynamic World mask: a Google Cloud project with
the Earth Engine API enabled.

---

## 2. Directory layout

Create one project directory. Suggested, alongside your existing archive:

```
/media/mb/HDD/Burnt_area/firepipe_project/
├── firepipe/                 # the package (unzipped from firepipe.zip)
│   ├── pyproject.toml
│   ├── requirements.txt
│   ├── README.md
│   ├── SETUP.md
│   ├── setup.sh
│   ├── configs/
│   │   └── igp_2019_2026.yaml
│   ├── src/firepipe/
│   └── tests/
├── gpkg/                     # your boundary files go here
│   ├── uttar_pradesh_admin.gpkg
│   ├── punjab_admin.gpkg
│   ├── haryana_admin.gpkg
│   └── madhya_pradesh_admin.gpkg
├── .venv/                    # created in step 3
└── output/                   # created by the first run
```

The config paths are relative to wherever you run `firepipe` from, so pick one
working directory and stay in it. I use the project root in every example below.

---

## 3. Install

### Option A — the bootstrap script

```bash
cd /media/mb/HDD/Burnt_area/firepipe_project
unzip firepipe.zip
bash firepipe/setup.sh
```

It creates `.venv`, installs everything, runs the test suite and prints the next
step. Takes 2–4 minutes, mostly downloading wheels.

### Option B — by hand

```bash
cd /media/mb/HDD/Burnt_area/firepipe_project
unzip firepipe.zip

python3 -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate

pip install --upgrade pip
pip install -e "./firepipe[all]"       # brackets need the quotes in zsh
```

`[all]` adds the MCP SDK and Earth Engine client. If you want neither:

```bash
pip install -e ./firepipe
```

### Verify

```bash
python -m pytest firepipe -q          # expect: 22 passed
firepipe --help
```

If `firepipe` is not found, the venv is not active. Either `source
.venv/bin/activate` or call it by path: `.venv/bin/firepipe`.

---

## 4. Boundary files

Copy your GeoPackages into `gpkg/`. Confirm the layer and field names before
touching the config:

```bash
firepipe layers gpkg/uttar_pradesh_admin.gpkg
```

```
            layer     geometry  features       crs   fields
   state_boundary MultiPolygon         1 EPSG:4326   STATE
district_boundary MultiPolygon        75 EPSG:4326   OBJECTID, STATE_LGD, DISTRICT, STATE_UT, ...
```

Your seven files all use `state_boundary` / `district_boundary` with a
`DISTRICT` field, which is what the shipped config already assumes. If the CRS
column shows an Everest 1830 or Kalianpur datum instead of EPSG:4326, the loader
will warn on every run — see troubleshooting.

---

## 5. FIRMS MAP_KEY

Register, then put the key in your shell profile so it survives new terminals:

```bash
echo 'export FIRMS_MAP_KEY=your_key_here' >> ~/.bashrc
source ~/.bashrc
```

Check it:

```bash
firepipe key
```

```
field                  value
current_transactions      12
transaction_limit       5000
```

The key can also go in the config under `firms.map_key`, but then it ends up in
`config.resolved.yaml` in every output directory. The environment variable is
the better habit.

---

## 6. Configure

Start from the four-state config:

```bash
cp firepipe/configs/igp_2019_2026.yaml ./up_punjab_haryana_mp.yaml
```

It already encodes your specification — Rabi 1 Mar – 30 Jun, Kharif 1 Oct –
31 Dec, 2019–2026, four states. The only edits needed are paths:

```yaml
out_dir: ./output/igp

regions:
  - name: Uttar Pradesh
    gpkg: ./gpkg/uttar_pradesh_admin.gpkg
  - name: Punjab
    gpkg: ./gpkg/punjab_admin.gpkg
  - name: Haryana
    gpkg: ./gpkg/haryana_admin.gpkg
  - name: Madhya Pradesh
    gpkg: ./gpkg/madhya_pradesh_admin.gpkg
```

Confirm the pipeline read it the way you intended:

```bash
firepipe plan -c up_punjab_haryana_mp.yaml
```

```
Season definition
season  label                   start_md end_md  wraps_year  n_days_common_year  months_touched
  Rabi  Rabi (wheat residue)       03-01  06-30       False                 122  Mar,Apr,May,Jun
Kharif  Kharif (paddy residue)     10-01  12-31       False                  92  Oct,Nov,Dec

Excluded from all figures (in no season window): Jan, Feb, Jul, Aug, Sep

API workload
                           bbox  in_season_days  blocks_per_platform  platforms  api_requests
73.7798,20.9707,84.7347,32.6116            1712                  184          3           552

Total API requests: 552 (bbox efficiency 0.46, FIRMS allows 5,000 per 10 minutes)
```

Read the exclusion line carefully — it is the single most consequential
consequence of your season definitions, and it is stated before you spend any
quota.

---

## 7. First run: offline, against your existing archive

Do this before any live call. It exercises the whole path — clipping, confidence
filter, seasons, figures — at zero API cost, and lets you compare against the R
output you already trust.

```bash
firepipe run -c up_punjab_haryana_mp.yaml \
  --from-csv /media/mb/HDD/Burnt_area/archive/20260805_UP_Presentation/agricultural_fires.csv
```

Expected, since that archive is UP-only:

```
Uttar Pradesh: 113099 of 196875 detections fall outside the boundary - dropped
Punjab: ... - dropped        # 0 retained, correct
```

Check the funnel:

```
stage                  n_records  removed  retained_pct
ingested_from_csv         335110        0        100.00
within_admin_boundary     335109        1        100.00
confidence_filter         294161    40948         87.78
season_window             270167    23994         91.84
cropland_mask             270167        0        100.00
```

Note the archive is already cropland-masked, so the mask stage is a no-op here.
For live runs it will not be.

---

## 8. First live run

Start small — one state, one season, one year — to confirm the API path before
committing to 552 requests:

```bash
cp up_punjab_haryana_mp.yaml smoke.yaml
# in smoke.yaml: keep only Uttar Pradesh, set
#   start_date: 2024-10-01
#   end_date:   2024-12-31
#   out_dir:    ./output/smoke

firepipe plan -c smoke.yaml     # should say ~30 requests
firepipe run  -c smoke.yaml
```

Then the full extraction:

```bash
firepipe run -c up_punjab_haryana_mp.yaml --density -v
```

Roughly 30–60 minutes, dominated by network and the WorldCover mask sampling.
Every retrieved block is cached to `~/.cache/firepipe/firms`, so if it dies
halfway, re-running resumes without re-spending quota. Changing season or
confidence settings afterwards and re-running costs nothing.

---

## 9. Reading the output

```
output/igp/
├── manifest.json              # start here: settings, counts, funnel, fingerprint
├── config.resolved.yaml       # exactly what ran, defaults included
├── data/detections.parquet    # one row per detection
├── qc/qc_funnel.csv           # what each stage removed
├── qc/qc_platform_by_year.csv # platform mix drift
├── qc/qc_record_extent.csv    # partial years
└── figs/
    ├── uttar_pradesh/         # 18 figures
    ├── punjab/
    ├── haryana/
    ├── madhya_pradesh/
    └── _comparison/           # 3 cross-region figures
```

Before reading any trend, open `qc_platform_by_year.csv` and
`qc_record_extent.csv`. Either can invalidate a headline.

Re-render figures without re-fetching:

```bash
firepipe plot -c up_punjab_haryana_mp.yaml
```

---

## 10. Optional: Dynamic World mask

```bash
pip install earthengine-api
earthengine authenticate
```

Then in the config:

```yaml
mask:
  kind: dynamic_world
  dw_project: your-gcp-project-id
  crop_classes: [4]       # set automatically if you leave 40
  dw_reducer: mode
  sampling: point         # fraction is not supported for Dynamic World
```

Slower and quota-bound, but the mask is contemporaneous with the fires rather
than fixed at a 2021 epoch.

---

## 11. Optional: MCP server

```bash
firepipe serve -c /full/path/to/up_punjab_haryana_mp.yaml
```

For Claude Desktop, edit
`~/.config/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "firepipe": {
      "command": "/media/mb/HDD/Burnt_area/firepipe_project/.venv/bin/firepipe",
      "args": ["serve", "-c", "/media/mb/HDD/Burnt_area/firepipe_project/up_punjab_haryana_mp.yaml"],
      "env": { "FIRMS_MAP_KEY": "your_key_here" }
    }
  }
}
```

Use absolute paths throughout — the client does not inherit your shell.

---

## 12. File inventory

### Package source, `firepipe/src/firepipe/`

| File | Lines | What it does |
|---|---:|---|
| `config.py` | 378 | Pydantic models for every analytical parameter. Validation, YAML load/save, `fingerprint` provenance hash, the starter config template. |
| `seasons.py` | 261 | `SeasonCalendar`: window membership, wrap-around, season-year attribution, overlap rejection, fetch scheduling, chunking, caption text, QC table. |
| `firms.py` | 451 | API client: 10-day chunking, disk cache, retries, rolling rate limiter, SP/NRT routing via `data_availability`, MODIS/VIIRS schema normalisation, `ingest_csv` for existing archives, `BBox`. |
| `aoi.py` | 237 | GeoPackage loading, CRS harmonisation and Everest-datum warning, geometry repair, spatial join to admin units, per-district area, bbox union and efficiency. |
| `masks.py` | 399 | `NoMask`, `ESAWorldCoverMask`, `DynamicWorldMask`, `PrecomputedRasterMask`. Point and footprint-fraction sampling, tile resolution, class dictionaries. |
| `pipeline.py` | 353 | Orchestration and the `Funnel`. Fetch → clip → confidence → seasons → mask → write. QC tables, manifest, detection density. |
| `plots.py` | 1193 | `Captions` (config-derived method text), `FigureWriter`, `FigureSuite` with 18 per-region and 3 cross-region figures. |
| `cli.py` | 208 | `init`, `layers`, `key`, `availability`, `plan`, `run`, `plot`, `serve`. |
| `mcp_server.py` | 408 | 11 MCP tools with read-only/destructive annotations and truncated table responses. |
| `__init__.py` | 49 | Public API. |
| `__main__.py` | 3 | `python -m firepipe`. |

### Project files

| File | Lines | Purpose |
|---|---:|---|
| `pyproject.toml` | 45 | Package metadata, pinned minimums, `firepipe` console script, extras. |
| `requirements.txt` | 12 | Flat dependency list for `pip install -r`. |
| `configs/igp_2019_2026.yaml` | 68 | Four states, 2019–2026, your Rabi/Kharif definitions, commented. |
| `tests/test_firepipe.py` | 266 | 22 tests: season wrap logic, season-year attribution, scheduling and chunking, config validation, schema normalisation, legacy labels, bbox arithmetic, caption derivation. |
| `README.md` | 358 | Reference: configuration options, outputs, methodology, MCP tools, Python API. |
| `SETUP.md` | — | This file. |
| `setup.sh` | — | Bootstrap: venv, install, tests. |

### Generated at runtime, not shipped

| Path | Contents |
|---|---|
| `~/.cache/firepipe/firms/` | Cached API blocks, `SOURCE_YYYYMMDD_hash.csv.gz`. Safe to delete; costs quota to rebuild. |
| `~/.cache/firepipe/masks/` | Downloaded WorldCover tiles, only if `esa_remote: false`. Large. |
| `output/<project>/` | Detections, QC, manifest, figures. |

---

## 13. Troubleshooting

**`ModuleNotFoundError: No module named 'geopandas'`**
The venv is not active, or you installed into system Python. `source
.venv/bin/activate`, then `pip list | grep geopandas`.

**`firepipe: command not found`**
Same cause. Use `.venv/bin/firepipe` or activate the venv.

**`FirmsError: No FIRMS MAP_KEY`**
`echo $FIRMS_MAP_KEY` is empty in this shell. Re-`source ~/.bashrc`, or pass
`--map-key`.

**`FIRMS transaction limit hit`**
5,000 per 10 minutes exceeded. Wait ten minutes and re-run — cached blocks are
reused, so you resume rather than restart. Lower `firms.requests_per_10min` if
something else is sharing the key.

**`AOIError: Layer 'state_boundary' not in ...`**
Run `firepipe layers <file>` and set `boundary_layer` / `admin_layer` to what it
reports.

**Warning: `... is on Everest 1830 (Everest-family datum)`**
The file is not on WGS 84. It is reprojected, but confirm a datum transformation
is being applied — without one you get a systematic offset of a few hundred
metres, comparable to a VIIRS pixel, which can move detections across district
boundaries. Check with `pyogrio.read_info(path)`.

**All detections dropped at `within_admin_boundary`**
Points and polygons are in different places. Confirm the GeoPackage covers your
region, and that longitude and latitude are not swapped in a `--from-csv` input.

**Mask stage removes far more than expected**
Check `qc/qc_land_cover.csv` for the class composition. If most detections land
on class 30 (grassland) or 50 (built-up) rather than 40, either the mask year is
poorly matched to the period or `sampling: point` is being defeated by mixed
pixels. Try `sampling: fraction`.

**WorldCover reads are slow**
Each `/vsicurl` read is an HTTP range request. For repeated runs or fraction
sampling, set `esa_remote: false` to download tiles once — but they are 1–2 GB
each.

**Figures render but look wrong after a config change**
Run `firepipe plot -c config.yaml` rather than reusing old files; captions are
regenerated from the config each time.

---

## 14. Relationship to the existing R script

`01_Fire_Count.R` still works and is not replaced by this. The mapping:

| R script | firepipe |
|---|---|
| `PLATFORM_MODE` | `sensors.headline_platform_group` |
| `CONF_CLASSES` | `sensors.viirs_confidence` |
| `CONF_NUM_MIN` | `sensors.modis_confidence_min` |
| `RABI_MONTHS` / `KHARIF_MONTHS` | `seasons.windows` — now day-precision, not whole months |
| `REGION_LABEL` | `regions[].name`, one per region |
| `cap()` caption builder | `plots.Captions` |
| `qc_season_definition.csv` | Same file, same purpose |
| Figures P1–P12 | Figures 01–10, plus 11–13 district and 20–22 cross-region |

The substantive difference is that seasons are day-anchored rather than
month-sets, so Rabi = 1 Mar – 30 Jun is now expressible exactly rather than
approximated as months 1–6. That will change your numbers relative to the R
output, and it should.
