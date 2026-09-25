#!/usr/bin/env bash
# Prints the app version, and fails when the files that carry it disagree.
# The version lives in package.json, src-tauri/tauri.conf.json,
# src-tauri/Cargo.toml and engine/pyproject.toml, and the two lock files
# repeat it for the root package. Nothing else keeps them equal: this is the
# check, scripts/bump-version.sh is the lever, and `bun run verify` runs
# this on every pass.
set -euo pipefail

cd "$(dirname "$0")/.."

json_version() { python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["version"])' "$1"; }
toml_version() {
  # The first top-level `version = "..."` in the file. [package] and
  # [project] come first in these two files; engine/pyproject.toml carries
  # another such line lower down, in a dependency-metadata table.
  sed -n 's/^version = "\([^"]*\)"$/\1/p' "$1" | head -n 1
}
lock_version() { # lock_version <file> <package name>: the line after `name = "<package>"`
  awk -v name="name = \"$2\"" '$0 == name { getline; sub(/^version = "/, ""); sub(/"$/, ""); print; exit }' "$1"
}

files=(
  package.json
  src-tauri/tauri.conf.json
  src-tauri/Cargo.toml
  engine/pyproject.toml
  src-tauri/Cargo.lock
  engine/uv.lock
)
versions=(
  "$(json_version package.json)"
  "$(json_version src-tauri/tauri.conf.json)"
  "$(toml_version src-tauri/Cargo.toml)"
  "$(toml_version engine/pyproject.toml)"
  "$(lock_version src-tauri/Cargo.lock readily)"
  "$(lock_version engine/uv.lock readily-engine)"
)

version=${versions[0]}
for v in "${versions[@]}"; do
  if [[ $v != "$version" ]]; then
    echo "the version differs between the files that carry it:" >&2
    for i in "${!files[@]}"; do
      printf '  %-28s %s\n' "${files[$i]}" "${versions[$i]}" >&2
    done
    echo "run scripts/bump-version.sh <version> to set all of them." >&2
    exit 1
  fi
done

if ! [[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "the version is not MAJOR.MINOR.PATCH: $version" >&2
  exit 1
fi

echo "$version"
