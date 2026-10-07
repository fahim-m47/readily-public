#!/usr/bin/env bash
# Writes THIRD-PARTY-NOTICES: the licence of every package that ships inside
# a Readily build, with its text, in a fixed order.
#
#   scripts/third-party-notices.sh          # regenerate the file
#   scripts/third-party-notices.sh --check  # fail if the file is stale
#
# Four sections: the Rust shell (cargo-about), the Engine's locked Python
# environment (pip-licenses, in the production-only venv the licence gate
# also uses), the uv binary and its statically linked crates (a vendored
# cargo-about report for the pin in scripts/prepare-bundle.sh), and the
# frontend's runtime packages (license-checker). A package that names a
# licence but ships no text for it gets the text from scripts/licence-texts,
# or fails the run. The committed file is what every build carries in its
# resources; `bun run verify` runs the check, so a dependency bump cannot
# leave it stale. Every build ships this one file, so each section is the
# union of the Apple Silicon Mac, Intel Mac and Linux trees. Generated on
# Apple Silicon, whose tree (MLX's wheels start at macOS 14) uv will not
# install from anywhere else; the other two install for their platforms from
# here.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ $(uname -s) != Darwin || $(uname -m) != arm64 ]]; then
  echo "the notices start from the Apple Silicon tree; generate them on one" >&2
  exit 1
fi
for tool in cargo cargo-about python3 shasum uv; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed" >&2; exit 1; }
done
[[ -x node_modules/.bin/license-checker ]] || { echo "run bun install first" >&2; exit 1; }

target=THIRD-PARTY-NOTICES
check=no
case "$#:${1:-}" in
  0:) ;;
  1:--check) check=yes ;;
  *) echo "usage: $0 [--check]" >&2; exit 2 ;;
esac

render_engine=$(cat <<'PY'
import json, os, sys

# pip-licenses reports the classifier when a wheel ships no licence file.
# Only these two are supplied from scripts/licence-texts; any other name
# without a text stops the run, so a new package cannot slip in unread.
SUPPLIED = {"Apache Software License": "Apache-2.0", "MIT License": "MIT"}

def supplied(name):
    with open(os.path.join("scripts/licence-texts", name), encoding="utf-8") as f:
        return f.read().rstrip()

# One pip-licenses report per shipped tree, each package once per version
# and text any of them carries: the Intel tree pins an older onnxruntime, and
# one numpy version's wheels name different paths for the libraries they
# bundle on Linux and on a Mac.
packages = {}
for path in sys.argv[1:]:
    with open(path, encoding="utf-8") as f:
        for p in json.load(f):
            packages[p["Name"], p["Version"], p["LicenseText"], p["NoticeText"]] = p

for _, p in sorted(packages.items()):
    print(f"{p['Name']} {p['Version']} ({p['License']})")
    print()
    text = p["LicenseText"]
    if text == "UNKNOWN":
        spdx = SUPPLIED.get(p["License"])
        if spdx is None:
            sys.exit(f"{p['Name']} ships no licence file and names {p['License']}: add a case for it")
        print("The package ships no licence file; its authors name the licence above, and the text follows.")
        print()
        text = supplied(spdx)
    print(text.rstrip())
    if p["NoticeText"] != "UNKNOWN":
        print()
        print("NOTICE:")
        print(p["NoticeText"].rstrip())
    print()
    print("-" * 70)
PY
)
render_frontend=$(cat <<'PY'
import glob, json, os, re, sys

# For a package that names MIT but ships no licence file: the MIT text
# itself, so the notice the licence asks for is in the document. The
# copyright line comes from the package's SPDX file when it has one.
with open("scripts/licence-texts/MIT", encoding="utf-8") as f:
    MIT = f.read().rstrip()

def read(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read().rstrip()

def licence_files(folder):
    # Licence texts, by name. An SPDX file is metadata about the licence,
    # not the licence, and a README is neither.
    found = []
    for path in sorted(glob.glob(os.path.join(folder, "*"))):
        name = os.path.basename(path).upper()
        if name.startswith(("LICENSE", "LICENCE", "COPYING")) and not name.endswith(".SPDX"):
            found.append(path)
    return found

def copyright_line(folder):
    spdx = os.path.join(folder, "LICENSE.spdx")
    if os.path.exists(spdx):
        match = re.search(r"^PackageCopyrightText:\s*(.+)$", read(spdx), re.M)
        if match:
            return f"Copyright (c) {match.group(1).strip()}"
    return None

for name, p in sorted(json.load(sys.stdin).items()):
    licences = p["licenses"]
    print(f"{name} ({licences})")
    if p.get("repository"):
        print(p["repository"])
    print()
    files = licence_files(p["path"])
    if files:
        for path in files:
            print(read(path))
            print()
    elif "MIT" in licences.split():
        print("The package ships no licence file; Readily uses it under the MIT option.")
        print()
        holder = copyright_line(p["path"])
        if holder:
            print(holder)
            print()
        print(MIT)
        print()
    else:
        sys.exit(f"{name} ships no licence file and is not MIT: add a case for it")
    print("-" * 70)
PY
)

rule=----------------------------------------------------------------------
uv_version=$(sed -n 's/^UV_VERSION=//p' scripts/prepare-bundle.sh)
[[ -n $uv_version ]] || { echo "UV_VERSION is not set in scripts/prepare-bundle.sh" >&2; exit 1; }
uv_notices=scripts/uv-third-party-notices/report.txt
[[ -f $uv_notices ]] || {
  echo "$uv_notices is missing: run scripts/uv-third-party-notices/refresh.sh" >&2
  exit 1
}
# refresh.sh records a hash of the report and what it generated the report
# from; an edited report, template, config or refresh script therefore fails
# here until the report is redone. The report is pasted in verbatim below,
# so this hash is what stands between a hand-edited crate list and the
# shipped notices.
uv_hash=$(scripts/uv-third-party-notices/report-hash.sh)
[[ $uv_hash == "$(cat scripts/uv-third-party-notices/report.hash 2>/dev/null)" ]] || {
  echo "$uv_notices does not match its recorded hash: run scripts/uv-third-party-notices/refresh.sh" >&2
  exit 1
}
uv run --quiet --no-project --python 3.12 python \
  scripts/uv-third-party-notices/validate.py \
  "$uv_notices" scripts/uv-about.toml \
  scripts/uv-third-party-notices/license-exceptions.toml src-tauri/deny.toml "$uv_version"
# Always written to a temporary file first: a generator that failed halfway
# must not leave a truncated THIRD-PARTY-NOTICES in the tree.
out=$(mktemp)
engine_reports=$(mktemp -d)
trap 'rm -rf "$out" "$engine_reports"' EXIT

{
  cat <<'HEADER'
THIRD-PARTY NOTICES

Readily is built from the open-source packages below, each under the
licence its authors chose. Their texts follow, in four sections: the Rust
shell, the Python Engine, the uv runtime that installs the Engine, and the
frontend. Generated by scripts/third-party-notices.sh; do not edit by hand.

Model weights are not listed here. Each Voice's licence is shown in the
app before it is downloaded and travels with the weights.

HEADER

  echo "$rule"
  echo "RUST SHELL"
  echo "$rule"
  echo
  (cd src-tauri && cargo about generate --offline -c about.toml about.hbs)

  echo "$rule"
  echo "PYTHON ENGINE"
  echo "$rule"
  echo
  UV_PROJECT_ENVIRONMENT=.venv-licenses uv sync -q --locked --no-dev --project engine
  UV_PROJECT_ENVIRONMENT=.venv-licenses-x86_64 uv sync -q --locked --no-dev --project engine \
    --python-platform x86_64-apple-darwin
  UV_PROJECT_ENVIRONMENT=.venv-licenses-linux uv sync -q --locked --no-dev --project engine \
    --python-platform x86_64-unknown-linux-gnu
  for venv in .venv-licenses .venv-licenses-x86_64 .venv-licenses-linux; do
    uvx pip-licenses@5.5.5 --python "engine/$venv/bin/python" \
      --with-license-file --with-notice-file --no-license-path \
      --format=json --ignore-packages readily-engine \
      --output-file "$engine_reports/${venv#.}.json" >/dev/null
  done
  python3 -c "$render_engine" "$engine_reports"/*.json

  echo
  echo "$rule"
  echo "UV RUNTIME CRATES"
  echo "$rule"
  echo
  echo "uv $uv_version and its statically linked Rust dependencies"
  echo "https://github.com/astral-sh/uv/tree/$uv_version"
  echo
  cat "$uv_notices"
  echo "$rule"
  echo "FRONTEND"
  echo "$rule"
  echo
  node_modules/.bin/license-checker --production --excludePrivatePackages --json \
    | python3 -c "$render_frontend"
} > "$out"

if [[ $check == no ]]; then
  chmod 644 "$out" # mktemp made it 0600
  mv "$out" "$target"
  exit 0
fi
if [[ ! -f $target ]]; then
  echo "$target is missing: run scripts/third-party-notices.sh and commit the result" >&2
  exit 1
fi
if cmp -s "$target" "$out"; then
  echo "$target is current"
else
  # The whole diff of an eleven-thousand-line file would bury every other
  # verify lane; the first screen says which package moved.
  diff -u "$target" "$out" | head -n 40 || true
  echo "$target is stale: run scripts/third-party-notices.sh and commit the result" >&2
  exit 1
fi
