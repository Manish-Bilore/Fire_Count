#!/usr/bin/env python3
"""Isolate why a FIRMS /api/area request returns 400.

Standalone - imports nothing from firepipe, so it tests the API rather than the
pipeline. Costs roughly 10 transactions out of 5,000.

    export FIRMS_MAP_KEY=your_key_here
    python diagnose_firms.py

Each probe changes exactly one thing from the failing request. The first PASS
after a run of FAILs tells you which variable is responsible.
"""

from __future__ import annotations

import os
import sys
import time
from datetime import date, timedelta

import requests

BASE = "https://firms.modaps.eosdis.nasa.gov"

# The exact request that failed in the pipeline run.
FAIL_BBOX = "73.7798,20.9707,84.7347,32.6116"
FAIL_SOURCE = "VIIRS_SNPP_SP"
FAIL_DATE = "2019-03-01"
FAIL_DAYS = 10

SMALL_BBOX = "80.0,26.0,81.0,27.0"  # ~1 degree inside Uttar Pradesh
UA = "firepipe/1.0 (agricultural fire pipeline)"

KEY = os.environ.get("FIRMS_MAP_KEY", "").strip()
if not KEY:
    sys.exit("Set FIRMS_MAP_KEY first:  export FIRMS_MAP_KEY=your_key_here")

results: list[tuple[str, bool, str]] = []


def probe(label: str, url: str, headers: dict | None = None) -> bool:
    """Fetch a URL and classify the outcome. Returns True on real data."""
    safe = url.replace(KEY, "<KEY>")
    try:
        r = requests.get(url, headers=headers or {}, timeout=120)
    except Exception as exc:
        results.append((label, False, f"network error: {exc}"))
        print(f"  FAIL  {label}\n        {safe}\n        network error: {exc}")
        return False

    body = r.text[:160].replace("\n", " ").strip()
    ok = r.status_code == 200 and r.text[:20].lower().startswith(
        ("latitude", "country_id", "data_id", "sensor")
    )
    # A 200 with "No data" is a valid answer about coverage, not a failure mode.
    empty = r.status_code == 200 and not ok

    if ok:
        rows = max(r.text.count("\n") - 1, 0)
        note = f"200, {rows} rows"
    elif empty:
        note = f"200 but no table: {body!r}"
    else:
        note = f"HTTP {r.status_code}: {body!r}"

    results.append((label, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}\n        {safe}\n        {note}")
    time.sleep(1)
    return ok


def area(source: str, bbox: str, days: int, day: str | None) -> str:
    tail = f"/{days}" + (f"/{day}" if day else "")
    return f"{BASE}/api/area/csv/{KEY}/{source}/{bbox}{tail}"


print("=" * 78)
print("FIRMS API diagnostic")
print("=" * 78)

# -- 1. account -------------------------------------------------------------
print("\n[1] MAP_KEY status")
try:
    r = requests.get(f"{BASE}/mapserver/mapkey_status/?MAP_KEY={KEY}", timeout=60)
    payload = r.json()
    if "error" in payload:
        sys.exit(f"  MAP_KEY rejected: {payload['error']}")
    for k, v in payload.items():
        print(f"        {k}: {v}")
except Exception as exc:
    sys.exit(f"  Could not reach FIRMS at all: {exc}")

# -- 2. archive coverage ----------------------------------------------------
print("\n[2] Archive coverage per dataset")
try:
    txt = requests.get(f"{BASE}/api/data_availability/csv/{KEY}/all", timeout=60).text
    for line in txt.strip().splitlines():
        print(f"        {line}")
except Exception as exc:
    print(f"        could not fetch: {exc}")

# -- 3. the probes ----------------------------------------------------------
recent = (date.today() - timedelta(days=10)).isoformat()

print("\n[3] Known-good control (from the NASA tutorial notebook)")
probe("NRT, small bbox, 1 day, no date", area("VIIRS_SNPP_NRT", SMALL_BBOX, 1, None))

print("\n[4] The exact failing request")
probe("SP, big bbox, 10 days, 2019-03-01", area(FAIL_SOURCE, FAIL_BBOX, FAIL_DAYS, FAIL_DATE))

print("\n[5] Change ONE variable at a time")
probe("  -> day_range 10 to 1", area(FAIL_SOURCE, FAIL_BBOX, 1, FAIL_DATE))
probe("  -> big bbox to small", area(FAIL_SOURCE, SMALL_BBOX, FAIL_DAYS, FAIL_DATE))
probe("  -> bbox to integer precision", area(FAIL_SOURCE, "73,20,85,33", FAIL_DAYS, FAIL_DATE))
probe("  -> SP to NRT (same old date)", area("VIIRS_SNPP_NRT", FAIL_BBOX, FAIL_DAYS, FAIL_DATE))
probe("  -> SNPP to MODIS SP", area("MODIS_SP", FAIL_BBOX, FAIL_DAYS, FAIL_DATE))
probe("  -> 2019 date to recent", area(FAIL_SOURCE, FAIL_BBOX, FAIL_DAYS, recent))

print("\n[6] Does the custom User-Agent matter?")
probe("  -> with firepipe User-Agent", area(FAIL_SOURCE, SMALL_BBOX, 1, FAIL_DATE), {"User-Agent": UA})

# -- 4. verdict -------------------------------------------------------------
print("\n" + "=" * 78)
print("VERDICT")
print("=" * 78)

by = dict((label.strip(), ok) for label, ok, _ in results)


def got(name: str) -> bool:
    return by.get(name, False)


if not got("NRT, small bbox, 1 day, no date"):
    print("""
  Nothing works, including the tutorial's own example. This is not a firepipe
  problem - it is the key, the network, or a FIRMS outage. Try the printed URL
  in a browser, and check https://forum.earthdata.nasa.gov for outages.""")
elif got("SP, big bbox, 10 days, 2019-03-01"):
    print("""
  The failing request succeeded this time. That points at a transient server
  fault rather than a malformed URL. Re-run the pipeline; cached blocks mean
  you resume rather than restart.""")
elif got("-> day_range 10 to 1"):
    print("""
  CAUSE: the 10-day range. The server rejects that span for this bbox and
  dataset, most likely because the response would be too large.

  FIX - in your config:
      firms:
        max_day_range: 1        # or try 3, 5

  This multiplies request count by up to 10x. Pair it with a per-region bbox
  to keep the response size down:
      firms:
        bbox_strategy: per_region""")
elif got("-> big bbox to small"):
    print("""
  CAUSE: the bounding box. The four-state union box is too large for this
  dataset, most likely because the response would be too large.

  FIX - in your config:
      firms:
        bbox_strategy: per_region
        max_day_range: 5

  Running one state at a time also resolves it.""")
elif got("-> bbox to integer precision"):
    print("""
  CAUSE: coordinate precision. The server dislikes 4-decimal coordinates.
  Report this - it is a firepipe bug in BBox.as_param() and I will round the
  query box outward to whole degrees.""")
elif got("-> SP to NRT (same old date)"):
    print("""
  CAUSE: the Standard Processing dataset, not the request shape.

  FIX - in your config:
      sensors:
        processing: NRT

  Check section [2] above for the real SP date range. NRT reaches back further
  than people expect, but confirm the coverage before relying on it.""")
elif got("-> 2019 date to recent"):
    print("""
  CAUSE: the 2019 date is outside this dataset's archive. Compare against the
  min_date/max_date in section [2] and set start_date accordingly, or switch
  to sensors.processing: NRT.""")
elif got("-> SNPP to MODIS SP"):
    print("""
  CAUSE: specific to VIIRS_SNPP_SP. Drop it and rely on NOAA-20 plus MODIS:
      sensors:
        platforms: [VIIRS_NOAA20, MODIS]""")
elif got("-> with firepipe User-Agent") is False and got("-> day_range 10 to 1") is False:
    print("""
  Every variant failed but the control passed. Send me the full output above -
  the pattern will identify it.""")
else:
    print("""
  No single variable explains it. Send me the full output above.""")

print()
