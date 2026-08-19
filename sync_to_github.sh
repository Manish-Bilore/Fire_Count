#!/usr/bin/env bash
# Sync firepipe and the config files to github.com/Manish-Bilore/Fire_Count
#
#   bash sync_to_github.sh            # dry run: shows every change, touches nothing
#   bash sync_to_github.sh --apply    # stage the changes, still does not push
#
# Pushing is left to you, deliberately. The script stops after staging so you
# can read `git status` before anything leaves the machine.
#
# What it does:
#   1. deletes the stray "{src" directory from a shell typo
#   2. installs the root .gitignore
#   3. UNTRACKS gpkg/ and the output directories, which are already committed
#      (files stay on disk; only git stops following them)
#   4. reports what would be added
#
# Run from the project root: /media/mb/HDD/Fire_Count_version_2

set -euo pipefail

APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m    %s\033[0m\n' "$*"; }
run()  {
  if [[ $APPLY -eq 1 ]]; then
    eval "$@"
  else
    printf '    would run: %s\n' "$*"
  fi
}

[[ -d .git ]] || { echo "Not a git repository. cd to the project root first."; exit 1; }
[[ -d firepipe ]] || { echo "No firepipe/ directory here. Wrong directory?"; exit 1; }

if [[ $APPLY -eq 0 ]]; then
  say "DRY RUN - nothing will be changed. Re-run with --apply to act."
fi

# -- 1. secret scan, before anything else ---------------------------------

say "Scanning tracked files for anything shaped like a MAP_KEY"
HITS=$(git grep -InE '[0-9a-f]{32}' -- '*.yaml' '*.yml' '*.py' '*.md' '*.R' 2>/dev/null || true)
if [[ -n "$HITS" ]]; then
  warn "POSSIBLE KEY IN TRACKED FILES:"
  echo "$HITS"
  warn "Rotate it at https://firms.modaps.eosdis.nasa.gov/api/map_key/ before pushing."
  warn "Note it may already be in the pushed history - see PUBLISHING.md section 7."
  [[ $APPLY -eq 1 ]] && { echo "Stopping. Resolve this first."; exit 1; }
else
  echo "    clean"
fi

say "Checking my.yaml specifically (it is tracked in the remote)"
if [[ -f my.yaml ]] && grep -q 'map_key' my.yaml; then
  grep -n 'map_key' my.yaml
  warn "Confirm this is not a real key."
else
  echo "    no map_key entry"
fi

# -- 2. the brace-expansion accident --------------------------------------

if [[ -d 'firepipe/{src' ]]; then
  say "Removing the stray 'firepipe/{src' directory"
  run "rm -rf 'firepipe/{src'"
else
  say "No stray '{src' directory - already clean"
fi

# -- 3. gitignore ----------------------------------------------------------

say "Installing the root .gitignore"
if [[ -f gitignore_root ]]; then
  run "cp gitignore_root .gitignore"
elif [[ -f .gitignore ]]; then
  echo "    .gitignore already present; leaving it alone"
else
  warn "gitignore_root not found here. Copy it in, then re-run."
fi

# -- 4. untrack what should never have been committed ---------------------

say "Untracking large or licensed material already in the repository"
warn "Files stay on disk. This only stops git following them from now on."

for path in gpkg output outputs output_version_01 UP_Fire_Count_output \
            output_platforms_VIIRS_SNPP_VIIRS_NOAA20_MODIS archive; do
  if git ls-files --error-unmatch "$path" >/dev/null 2>&1 || \
     [[ -n "$(git ls-files "$path" 2>/dev/null)" ]]; then
    n=$(git ls-files "$path" | wc -l)
    echo "    $path: $n tracked file(s)"
    run "git rm -r --cached --quiet '$path'"
  fi
done

for pat in '*.zip' '*.parquet'; do
  n=$(git ls-files "$pat" | wc -l)
  if [[ "$n" -gt 0 ]]; then
    echo "    $pat: $n tracked file(s)"
    run "git rm --cached --quiet \$(git ls-files '$pat')"
  fi
done

# -- 5. stage --------------------------------------------------------------

say "Staging the code and configs"
run "git add -A"

if [[ $APPLY -eq 1 ]]; then
  say "Staged. Review before committing:"
  git status --short | head -40
  echo
  cat <<'EOF'
    Read that list. It should contain firepipe source, configs, docs and the
    root .yaml files - and no .gpkg, .parquet, .png or detections*.csv.

    Then:
        git commit -m "firepipe 1.1.0: simple plot mode, platform configs, API fixes"
        git push

    If the untracking looks wrong, undo everything with:
        git reset
EOF
else
  say "Dry run complete. Re-run with --apply to make these changes."
fi
