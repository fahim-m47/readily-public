#!/usr/bin/env bash
# Makes the working tree ready for `tauri build` to bundle it.
#
#   bash scripts/prepare-bundle.sh
#
# Run by `beforeBuildCommand`, so a plain `bun tauri build` is enough. Both
# steps below are about what ends up inside the .app, which is why they are
# here rather than in the frontend build.
set -euo pipefail

cd "$(dirname "$0")/.."

# ADR 0001 §6 ships no Python: first run provisions the Engine with this
# binary, so it is the first link in the chain that ends at the interpreter
# and every wheel (threat model B5). Whatever lands here is handed the launch
# token and runs as the reader, which is why it is pinned twice — the release
# archive by hash, and the binary inside it by hash — rather than taken from
# whatever the build machine happens to have on PATH.
#
# Bumping uv means changing all three constants together, from the checksums
# astral-sh publishes beside the release. Dependabot does not watch this; it
# is a reviewed change like any other pin.
UV_VERSION=0.11.17
UV_ARCHIVE_SHA256=2a162f6b90ff3691a2f9cae1622e066a3ce592e110f66670cdcc841324b28226
UV_BINARY_SHA256=94fee727893cd7a6020fb9167c859f4ac6d8efa03c711dd2cd3b604984c577d7

# Apple Silicon only, which is not this script being narrow: ADR 0001's
# consequences put Intel Macs out of scope because MLX has no x86_64 wheel.
UV_TRIPLE=aarch64-apple-darwin

# Checked rather than assumed. Installing an arm64 `uv` into an x86_64 build
# produces a bundle that passes every content check and then dies on a
# reader's machine with "Bad CPU type in executable" — a failure a long way
# from its cause. Better to refuse here.
if [[ $(uname -s) != Darwin || $(uname -m) != arm64 ]]; then
  echo "Readily builds on Apple Silicon macOS only (ADR 0001)." >&2
  exit 1
fi

# Two copies of one binary. `pristine` is the bytes astral-sh published,
# which is what the hash pins; `destination` is what the bundle ships, and a
# release build signs it (below), which changes the bytes. Keeping them apart
# is what lets the pin stay checkable after signing.
UV_PRISTINE=src-tauri/resources/uv.pristine
UV_DESTINATION=src-tauri/resources/uv

fetch_uv() {
  # Idempotent by the same hash the download is checked against, so a repeat
  # build — and `beforeBuildCommand` runs this on every one — costs a hash of
  # a 47MB file rather than a download.
  if [[ -f $UV_PRISTINE ]] &&
    shasum -a 256 -c --status <<<"$UV_BINARY_SHA256  $UV_PRISTINE"; then
    echo "uv $UV_VERSION already in place."
    return
  fi

  local archive=uv-$UV_TRIPLE.tar.gz
  local url=https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$archive
  local scratch
  scratch=$(mktemp -d)
  trap 'rm -rf "$scratch"' RETURN

  echo "Fetching uv $UV_VERSION for ${UV_TRIPLE}..."
  # `--proto '=https'` because `--location` follows whatever it is told to,
  # and a redirect to http would hand the bytes to anyone on the path. The
  # hash below makes a substitution useless either way; this makes it
  # unavailable rather than merely futile.
  curl --fail --silent --show-error --location --proto '=https' \
    --output "$scratch/$archive" "$url"

  # Before unpacking, not after: tar is the thing being handed the bytes, so
  # a hash checked afterwards has already trusted them.
  shasum -a 256 -c --status <<<"$UV_ARCHIVE_SHA256  $scratch/$archive" || {
    echo "uv $UV_VERSION archive does not match its pinned hash." >&2
    return 1
  }
  tar -xzf "$scratch/$archive" -C "$scratch"

  # The archive hash bounds what came off the network; this one says the file
  # taken out of it is the binary the pin names, which is the fact the .app
  # depends on.
  local binary=$scratch/uv-$UV_TRIPLE/uv
  shasum -a 256 -c --status <<<"$UV_BINARY_SHA256  $binary" || {
    echo "uv $UV_VERSION binary does not match its pinned hash." >&2
    return 1
  }

  mkdir -p "$(dirname "$UV_PRISTINE")"
  install -m 644 "$binary" "$UV_PRISTINE"
  echo "uv $UV_VERSION installed at $UV_PRISTINE."
}

# `tauri build` signs the shell and anything under `MacOS/` and `Frameworks/`,
# and nothing under `Resources/` — where `uv` lives. Notarization rejects a
# bundle carrying an executable that is not Developer ID-signed with the
# hardened runtime, so the release path signs `uv` here, before the bundler
# copies it in; the signature travels with the file. `APPLE_SIGNING_IDENTITY`
# is the same variable the bundler reads, so one setting signs both, and a
# build without it stays the unsigned local build it always was.
#
# Always re-copied from the pristine file, rather than checked, because the
# signed bytes have no pin to check against and a copy plus a signature costs
# about a second.
stage_uv() {
  install -m 755 "$UV_PRISTINE" "$UV_DESTINATION"
  if [[ -n ${APPLE_SIGNING_IDENTITY:-} ]]; then
    codesign --force --sign "$APPLE_SIGNING_IDENTITY" \
      --options runtime --timestamp "$UV_DESTINATION"
    echo "uv signed as $APPLE_SIGNING_IDENTITY."
  fi
}

# `tauri.conf.json` bundles `engine/src/` as a directory, which means it
# bundles whatever is sitting in it — including the `__pycache__` a local
# test run leaves behind. Shipping that bytecode would put a file in the
# bundle that no reviewer read and that the shipped `.py` beside it did not
# produce; if the copy happens to preserve mtimes, CPython accepts it and
# runs it. Cheaper to delete than to reason about.
prune_bytecode() {
  find engine/src -type d -name __pycache__ -prune -exec rm -rf {} +
}

fetch_uv
stage_uv
prune_bytecode
