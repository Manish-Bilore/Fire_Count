# Publishing to GitHub

The repository root is the `firepipe/` directory. Your boundary files, configs
with local paths, and `output/` stay outside it and are gitignored, so nothing
licensed or regenerable ends up in version control.

---

## 1. Check for secrets first

Do this before `git init`, not after. Once a key is in a commit it is in the
history, and removing it means rewriting history on every clone.

```bash
cd /media/mb/HDD/Fire_count_version_2

# Does any file contain something shaped like a MAP_KEY (32 hex characters)?
grep -rInE '[0-9a-f]{32}' --include='*.yaml' --include='*.yml' \
     --include='*.py' --include='*.md' firepipe/ *.yaml

# And specifically:
grep -rn 'map_key' firepipe/ *.yaml
```

Expect no output beyond documentation mentions. If a real key appears anywhere:
**rotate it** at <https://firms.modaps.eosdis.nasa.gov/api/map_key/> before
continuing.

Also check the resolved configs already on disk. Versions before 1.0.3 wrote
the key verbatim:

```bash
grep -rn 'map_key' output/*/config.resolved.yaml
```

From 1.0.3 onward this reads `<redacted: set FIRMS_MAP_KEY in the environment>`.

---

## 2. Decide what to publish

| Directory | Publish? | Why |
|---|---|---|
| `firepipe/` | Yes | The code, tests, configs and docs |
| `gpkg/` | **No** | Survey of India boundaries are licensed; check terms before redistributing anything derived from them |
| `output/` | No | Regenerable from a config plus a MAP_KEY; hundreds of MB; stale figures get cited as current |
| `my.yaml`, root configs | No | Contain machine-specific absolute paths |

`.gitignore` already enforces all of this. Verified: a dry run with `output/`,
`gpkg/` and a key-bearing `config.local.yaml` present staged 24 source files and
excluded every one of them.

If you want the figures visible on GitHub, commit a handful deliberately into a
`docs/` directory rather than un-ignoring `output/`.

---

## 3. Initialise

```bash
cd /media/mb/HDD/Fire_count_version_2/firepipe

# Put your name in the licence
sed -i 's/<YOUR NAME>/Your Name/' LICENSE

git init
git add -A
git status              # read this list before committing
```

Confirm the list contains no `.gpkg`, no `output/`, no `.parquet`, and no
`detections*.csv`. Then:

```bash
git commit -m "firepipe 1.0.3: NASA FIRMS agricultural fire pipeline

Configurable extraction of FIRMS active fire detections over administrative
regions, with cropland masking, day-anchored season windows, and a figure
suite whose captions derive from the config."
```

---

## 4. Create the remote

With the [GitHub CLI](https://cli.github.com):

```bash
gh repo create firepipe --public --source=. --remote=origin \
  --description "NASA FIRMS agricultural fire analysis pipeline: FIRMS extraction, cropland masking, seasonal analysis and publication figures"
git push -u origin main
```

Or by hand: create an empty repository on github.com (no README, no .gitignore,
no licence — you already have all three), then:

```bash
git branch -M main
git remote add origin git@github.com:<username>/firepipe.git
git push -u origin main
```

---

## 5. Confirm CI passes

`.github/workflows/tests.yml` runs on every push:

- the test suite on Python 3.10 through 3.13
- a CLI smoke test
- `ruff` for syntax and undefined names
- a scan that fails the build if a 32-hex-character string is ever committed

All four gates were run locally before this was written, and all four pass. The
suite is entirely offline — FIRMS responses are mocked and rasters synthesised —
so CI needs no MAP_KEY and no network.

---

## 6. Worth adding once it is up

**Repository topics**: `remote-sensing`, `nasa-firms`, `viirs`, `modis`,
`crop-residue-burning`, `agriculture`, `india`, `geospatial`.

**A citation file.** If this supports a publication, `CITATION.cff` lets GitHub
render a "Cite this repository" button:

```yaml
cff-version: 1.2.0
title: firepipe
message: If you use this software, please cite it as below.
type: software
authors:
  - family-names: <surname>
    given-names: <given name>
    orcid: https://orcid.org/0000-0000-0000-0000
repository-code: https://github.com/<username>/firepipe
license: MIT
version: 1.0.3
date-released: 2026-08-18
```

**A Zenodo DOI.** Enabling the Zenodo GitHub integration and cutting a release
mints a DOI, which reviewers increasingly expect for analysis code.

**Data provenance in the README.** State which boundary files the analysis used
and where they came from, even though they are not distributed. Someone
reproducing the work needs to know that `DISTRICT` came from a specific Survey
of India vintage, since district boundaries in Uttar Pradesh have changed over
the analysis period.

---

## 7. If a key does leak

Rotating the key is the fix that matters — do that first, immediately. Purging
the history is secondary and only worth it for a private repository that never
went public.

```bash
# 1. Rotate at firms.modaps.eosdis.nasa.gov/api/map_key/  <- do this first
# 2. Then, optionally, purge:
pip install git-filter-repo
git filter-repo --replace-text <(echo 'OLD_KEY_HERE==>REDACTED')
git push --force
```

Anyone who cloned in the interim still has the old key in their copy, which is
why rotation is the part that actually protects you.
