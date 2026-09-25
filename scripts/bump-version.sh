#!/usr/bin/env bash
# Sets the app version everywhere it is written, in one move.
#
#   scripts/bump-version.sh 0.1.1
#
# Rewrites package.json, src-tauri/tauri.conf.json, src-tauri/Cargo.toml and
# engine/pyproject.toml, then lets cargo and uv rewrite their lock files to
# match, and prints the diff. Nothing is committed: the change is reviewed
# and committed like any other. Refuses a version that is not newer than the
# newest one already written, and refuses a tree with uncommitted tracked
# changes, so the bump is one commit on its own and nothing else rides along
# with it. Anything that fails after the first file is rewritten puts every
# file back the way it was.
#
# The four files are read one by one rather than through check-version.sh,
# so this is also the way out when that check fails: a tree where the files
# disagree is exactly the one a bump has to be able to repair.
set -euo pipefail

cd "$(dirname "$0")/.."

new=${1:-}
if [[ -z $new ]]; then
  echo "usage: scripts/bump-version.sh <MAJOR.MINOR.PATCH>" >&2
  exit 2
fi
if ! [[ $new =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "not a MAJOR.MINOR.PATCH version: $new" >&2
  exit 2
fi

# Each file's version line, anchored at both ends, with VERSION where the
# number goes. The first such line in the file is the app's: [package] and
# [project] come first in the two TOML files, and engine/pyproject.toml
# carries a second `version =` lower down, in a dependency-metadata table,
# which is why "first" is the rule here and in check-version.sh rather than
# "only".
files=(package.json src-tauri/tauri.conf.json src-tauri/Cargo.toml engine/pyproject.toml)
patterns=('  "version": "VERSION",' '  "version": "VERSION",' 'version = "VERSION"' 'version = "VERSION"')
locks=(src-tauri/Cargo.lock engine/uv.lock)

# The version a file carries, and a refusal when the file has no such line:
# it would be left alone by the rewrite below, and a bump that leaves one
# file behind is what check-version.sh exists to catch. Every file is read
# before any is written.
version_in() { # version_in <file> <pattern>
  local file=$1 pattern=$2 found
  found=$(sed -n "s|^${pattern//VERSION/\\([0-9.]*\\)}\$|\\1|p" "$file" | head -n 1)
  if [[ -z $found ]]; then
    echo "no version line in $file" >&2
    exit 1
  fi
  echo "$found"
}

versions=()
for i in "${!files[@]}"; do
  versions+=("$(version_in "${files[$i]}" "${patterns[$i]}")")
done
newest=$(printf '%s\n' "${versions[@]}" | sort -V | tail -n 1)

if [[ $new == "$newest" ]]; then
  echo "the version is already $newest" >&2
  exit 1
fi
if [[ $(printf '%s\n%s\n' "$newest" "$new" | sort -V | tail -n 1) != "$new" ]]; then
  echo "$new is not newer than the current version $newest" >&2
  exit 1
fi

# Tracked changes only: an untracked scratch file is not something that
# would ride along in the commit.
if [[ -n $(git status --porcelain --untracked-files=no) ]]; then
  echo "the tree has uncommitted changes; commit or stash them so the bump is its own commit" >&2
  exit 1
fi

# From here on a failure leaves the tree as it was found, rather than with
# some of the files bumped and the lever refusing to run again.
restore() {
  local status=$?
  if [[ $status != 0 ]]; then
    git checkout --quiet -- "${files[@]}" "${locks[@]}"
    rm -f -- "$bumped"
    echo "the bump did not finish; every file is back as it was." >&2
  fi
}
bumped=
trap restore EXIT

for i in "${!files[@]}"; do
  file=${files[$i]} old=${patterns[$i]//VERSION/${versions[$i]}}
  # The first line equal to the old one, compared whole: no regex, so
  # nothing in the line is syntax. A temp file rather than `-i`, which
  # needs a suffix argument on BSD sed and takes an optional one on GNU.
  bumped=$(mktemp "$file.XXXXXX")
  awk -v old="$old" -v new="${old//${versions[$i]}/$new}" \
    '!done && $0 == old { $0 = new; done = 1 } { print }' "$file" > "$bumped"
  mv "$bumped" "$file"
done

# The lock files name the root package's version too. `cargo update` with a
# workspace scope rewrites that line alone; `uv lock` re-resolves, so the
# diff is checked afterwards: a lock that changed in any line other than a
# version line is dependency drift, which is its own commit, not this one.
cargo update --quiet --offline --workspace --manifest-path src-tauri/Cargo.toml
uv lock --quiet --project engine
drift=$(git --no-pager diff -U0 -- "${locks[@]}" | grep '^[-+]' | grep -v '^[-+][-+]' | grep -v '^[-+]version = "' || true)
if [[ -n $drift ]]; then
  echo "the lock files changed beyond the version line; lock that on its own first:" >&2
  echo "$drift" >&2
  exit 1
fi

test "$(scripts/check-version.sh)" == "$new"

git --no-pager diff --stat
echo
echo "version: $newest -> $new. Review the diff and commit it on its own."
