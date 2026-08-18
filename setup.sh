#!/usr/bin/env bash
# Bootstrap firepipe: virtual environment, dependencies, verification.
#
#   bash firepipe/setup.sh            # installs into ../.venv, with extras
#   bash firepipe/setup.sh --minimal  # skip MCP and Earth Engine
#   bash firepipe/setup.sh --venv ./v # choose the venv location
#
# Safe to re-run: an existing venv is reused, not destroyed.

set -euo pipefail

PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$PKG_DIR")"
VENV="$PROJECT_DIR/.venv"
EXTRAS="[all]"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --minimal) EXTRAS=""; shift ;;
    --venv)    VENV="$2"; shift 2 ;;
    -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# --- python ----------------------------------------------------------------

say "Checking Python"
PY="$(command -v python3 || true)"
[[ -n "$PY" ]] || die "python3 not found. Install Python 3.10 or newer."

"$PY" - <<'EOF' || exit 1
import sys
if sys.version_info < (3, 10):
    sys.exit(f"Python 3.10+ required, found {sys.version.split()[0]}")
print(f"Python {sys.version.split()[0]} at {sys.executable}")
EOF

# --- venv ------------------------------------------------------------------

if [[ -d "$VENV" ]]; then
  say "Reusing existing environment at $VENV"
else
  say "Creating environment at $VENV"
  "$PY" -m venv "$VENV" || die "venv creation failed. On Debian/Ubuntu: sudo apt install python3-venv"
fi

VPY="$VENV/bin/python"
[[ -x "$VPY" ]] || VPY="$VENV/Scripts/python.exe"   # Windows / Git Bash
[[ -x "$VPY" ]] || die "No interpreter inside $VENV"

# --- install ---------------------------------------------------------------

say "Upgrading pip"
"$VPY" -m pip install --quiet --upgrade pip

say "Installing firepipe${EXTRAS} (a few minutes on first run)"
"$VPY" -m pip install --editable "${PKG_DIR}${EXTRAS}" \
  || die "Install failed. Full log: rerun without --quiet, or see $PKG_DIR/README.md"

# --- verify ----------------------------------------------------------------

say "Verifying imports"
"$VPY" - <<'EOF' || exit 1
import firepipe, geopandas, rasterio, matplotlib, pandas
print(f"  firepipe   {firepipe.__version__}")
print(f"  geopandas  {geopandas.__version__}")
print(f"  rasterio   {rasterio.__version__}")
print(f"  pandas     {pandas.__version__}")
try:
    import asyncio
    from firepipe.mcp_server import mcp
    print(f"  MCP server {len(asyncio.run(mcp.list_tools()))} tools")
except SystemExit:
    print("  MCP server not installed (--minimal)")
EOF

say "Running tests"
"$VPY" -m pytest "$PKG_DIR" -q || die "Tests failed. Do not proceed; report the output."

# --- next steps ------------------------------------------------------------

BIN="$(dirname "$VPY")"
cat <<EOF

$(printf '\033[1mReady.\033[0m')

Activate the environment:
    source $BIN/activate

Set your FIRMS key (free: https://firms.modaps.eosdis.nasa.gov/api/map_key/):
    export FIRMS_MAP_KEY=your_key_here

Then, from $PROJECT_DIR:
    firepipe layers gpkg/uttar_pradesh_admin.gpkg      # confirm layer names
    cp $PKG_DIR/configs/igp_2019_2026.yaml ./my.yaml   # edit the gpkg paths
    firepipe plan -c my.yaml                           # API cost, no downloads
    firepipe run  -c my.yaml                           # fetch, mask, plot

Full walkthrough: $PKG_DIR/SETUP.md
EOF
