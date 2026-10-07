#!/usr/bin/env bash
# Regenerates scripts/uv-third-party-notices/report.txt, the cargo-about
# report for the uv binary pinned in scripts/prepare-bundle.sh, then
# THIRD-PARTY-NOTICES, which carries it. Run it after a uv pin change or an
# edit to scripts/uv-about.toml, src-tauri/about.hbs, or the cargo-about
# invocation below.
#
#   scripts/uv-third-party-notices/refresh.sh              # clone the uv tag
#   scripts/uv-third-party-notices/refresh.sh /path/to/uv  # reuse a clean checkout
#
# The report comes from a checkout of the exact uv tag, with the feature set
# and targets of uv's own release builds for every platform Readily ships
# on, and is validated against scripts/uv-about.toml, license-exceptions.toml
# and the shell's deny.toml before it replaces the committed one. A changed
# exception set stops the run there: make the ADR ruling first, then update
# license-exceptions.toml. The adjacent report.hash records a hash of the
# report, the template, the config and this script (see report-hash.sh), so
# an edit to the report or to any of them fails the notices check until the
# hash is recorded again; refreshing here records it over a report rebuilt
# from source. Needs cargo-about 0.9.2 (the version below) on Apple
# Silicon.
set -euo pipefail

cd "$(dirname "$0")/../.."
repo=$PWD

cargo_about_version=0.9.2
if [[ $(uname -s) != Darwin || $(uname -m) != arm64 ]]; then
  echo "the notices start from the Apple Silicon tree; generate them on one" >&2
  exit 1
fi
for tool in cargo cargo-about git perl python3 shasum uv; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed" >&2; exit 1; }
done
[[ -x node_modules/.bin/license-checker ]] || { echo "run bun install first" >&2; exit 1; }
actual_cargo_about_version=$(cargo-about --version)
if [[ $actual_cargo_about_version != "cargo-about $cargo_about_version" ]]; then
  echo "cargo-about $cargo_about_version is required; found $actual_cargo_about_version" >&2
  exit 1
fi

uv_version=$(sed -n 's/^UV_VERSION=//p' scripts/prepare-bundle.sh)
[[ -n $uv_version ]] || { echo "UV_VERSION is not set in scripts/prepare-bundle.sh" >&2; exit 1; }

notices_dir=scripts/uv-third-party-notices
report=$notices_dir/report.txt
candidate=$(mktemp "$notices_dir/.report.XXXXXX")
backup=$(mktemp -d)
scratch=
promoted=no

# Once the candidate is promoted, ending before THIRD-PARTY-NOTICES is
# regenerated, by failure or by a signal, puts back whichever of the report
# and its hash existed before and removes the rest, so the tree is never
# half refreshed. An EXIT trap sees $? as 0 after a signal, so the flag
# alone decides; set -e is off here so a failed restore still reports.
cleanup() {
  local status=$?
  set +e
  if [[ $promoted == yes ]]; then
    rm -f "$report" "$notices_dir/report.hash"
    if cp -R "$backup"/. "$notices_dir"/; then
      echo "THIRD-PARTY-NOTICES was not regenerated; $notices_dir is as it was" >&2
    else
      echo "THIRD-PARTY-NOTICES was not regenerated and $notices_dir could not be restored; the previous files are in $backup" >&2
      backup=
    fi
    [[ $status -ne 0 ]] || status=1
  fi
  rm -f "$candidate"
  [[ -z $backup ]] || rm -rf "$backup"
  [[ -z $scratch ]] || rm -rf "$scratch"
  exit "$status"
}
trap cleanup EXIT

case "$#:${1:-}" in
  0:) ;;
  1:?*) uv_checkout=$(cd "$1" && pwd) ;;
  *) echo "usage: $0 [uv-checkout]" >&2; exit 2 ;;
esac
if [[ -z ${uv_checkout:-} ]]; then
  scratch=$(mktemp -d)
  uv_checkout=$scratch/uv
  git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$uv_version" https://github.com/astral-sh/uv.git "$uv_checkout"
fi

expected_commit=$(git -C "$uv_checkout" rev-parse "$uv_version^{commit}")
actual_commit=$(git -C "$uv_checkout" rev-parse HEAD)
if [[ $actual_commit != "$expected_commit" ]]; then
  echo "$uv_checkout is at $actual_commit; expected uv tag $uv_version ($expected_commit)" >&2
  exit 1
fi
if [[ -n $(git -C "$uv_checkout" status --porcelain --untracked-files=no) ]]; then
  echo "$uv_checkout has tracked changes; use a clean checkout of uv $uv_version" >&2
  exit 1
fi

(
  cd "$uv_checkout"
  cargo about generate --fail --locked --features self-update \
    --target aarch64-apple-darwin --target x86_64-apple-darwin \
    --target x86_64-unknown-linux-gnu \
    --manifest-path crates/uv/Cargo.toml \
    -c "$repo/scripts/uv-about.toml" \
    -o "$repo/$candidate" \
    "$repo/src-tauri/about.hbs"
)
# Licence texts are copied verbatim, and some upstream files carry CRLF or
# trailing whitespace that would churn every diff of the committed report.
perl -pi -e 's/\r$//; s/[ \t]+$//' "$candidate"
uv run --quiet --no-project --python 3.12 python "$notices_dir/validate.py" \
  "$candidate" scripts/uv-about.toml "$notices_dir/license-exceptions.toml" \
  src-tauri/deny.toml "$uv_version"

for previous in "$report" "$notices_dir/report.hash"; do
  [[ ! -f $previous ]] || cp "$previous" "$backup"/
done
chmod 644 "$candidate" # mktemp made it 0600
promoted=yes
mv "$candidate" "$report"
"$notices_dir/report-hash.sh" > "$notices_dir/report.hash"
scripts/third-party-notices.sh
promoted=no
echo "wrote $report and THIRD-PARTY-NOTICES"
