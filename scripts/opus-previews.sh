#!/usr/bin/env bash
# Builds the frontend for a Linux bundle, with its Voice Previews as Opus.
#
#   bash scripts/opus-previews.sh
#
# Run by the Linux `beforeBuildCommand` (src-tauri/tauri.linux.conf.json) in
# place of a plain `bun run build`. The committed previews are AAC in .m4a,
# which WKWebView plays and WebKitGTK does not without a GStreamer decoder
# most Linux desktops leave out; Ogg Opus plays with the base plugins the
# .deb and .rpm depend on. Transcoding at build time, rather than committing a second
# set, keeps one set of clips to curate. ffmpeg is a build tool only: none of
# it ships.
set -euo pipefail

cd "$(dirname "$0")/.."

command -v ffmpeg >/dev/null || {
  echo "ffmpeg (with libopus) is needed to build the Linux previews." >&2
  exit 1
}

# The page asks for .ogg only when this is set (src/shell/catalog.ts), so
# the flag and the files it names come from the same build.
VITE_PREVIEW_FORMAT=ogg bun run build

# Bit-exact, so the same clip transcodes to the same bytes on every build.
# The .m4a goes once its .ogg is written: nothing in this build asks for it.
count=0
while IFS= read -r -d '' clip; do
  ffmpeg -nostdin -loglevel error -i "$clip" -vn -map_metadata -1 \
    -c:a libopus -b:a 64k -fflags +bitexact -flags:a +bitexact \
    "${clip%.m4a}.ogg"
  rm "$clip"
  count=$((count + 1))
done < <(find dist/previews -name '*.m4a' -print0)
echo "Transcoded $count Voice Previews to Opus."
