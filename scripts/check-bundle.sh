#!/usr/bin/env bash
# Checks what a built bundle actually carries. `bun tauri build` proves the
# tree compiles; this answers the question a compile cannot — that the bundle
# coming out still holds `uv`, the Engine sources, the Catalog and every
# licence and reference clip, byte for byte.
#
#   scripts/check-bundle.sh [resources]
#
# [resources] is the bundle's resource folder, relative to the repository:
# by default the Readily.app a Mac build leaves, or `/usr/lib/Readily` once
# a .deb or .rpm is installed. Shared by ci.yml's `smoke` and `smoke-linux` jobs and
# `scripts/verify.sh --smoke`, so they cannot drift into checking different
# things.
set -euo pipefail

cd "$(dirname "$0")/.."
app=${1:-src-tauri/target/release/bundle/macos/Readily.app/Contents/Resources}
test -d "$app" || { echo "no bundle at $app — build it first"; exit 1; }

# A bundle that builds but ships no `uv` is an app that opens once and can never
# provision. Named one by one rather than as a listing, so the check fails on
# the file that went missing.
for resource in uv engine/pyproject.toml engine/uv.lock \
  engine/.python-version engine/src/readily_engine/main.py \
  catalog/manifest.json; do
  test -e "$app/$resource" || { echo "missing: $resource"; exit 1; }
done
test -x "$app/uv"

# The store hands a reader `catalog/licenses/<id>.txt` as the terms the weights
# come under (ADR 0009 §3). A text the bundle dropped or altered would ship a
# model with the wrong licence, so the bundled directory has to be the source
# one exactly.
diff -r catalog/licenses "$app/catalog/licenses"
pronunciation=engine/src/readily_engine/loading/data/pronunciation
diff -r "$pronunciation" "$app/$pronunciation"
vibevoice_tokenizer=engine/src/readily_engine/loading/vibevoice/tokenizer
diff -r "$vibevoice_tokenizer" "$app/$vibevoice_tokenizer"

# The expressive Tier conditions every Block on the clip the manifest names for
# its Voice. A clip the bundle dropped is an Engine that cannot start;
# one it altered is a different voice.
diff -r catalog/references "$app/catalog/references"

# The licences of everything in the bundle travel with it. A copy the bundle
# dropped or that fell behind the committed one is a download whose notices
# are not the notices.
diff THIRD-PARTY-NOTICES "$app/THIRD-PARTY-NOTICES"

# `.pyc` in Resources is a file no reviewer read, that the `.py` beside it did
# not necessarily produce, and that CPython prefers when the header matches.
# `prepare-bundle.sh` prunes it at build time and `PYTHONPYCACHEPREFIX` keeps
# the running app from putting it back; this is what says both still hold.
found=$(find "$app/engine" -name '*.pyc' -o -name __pycache__)
test -z "$found" || { echo "bytecode in the bundle:"; echo "$found"; exit 1; }

echo "bundle carries what a first run needs, with licences and clips intact"
