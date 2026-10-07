#!/usr/bin/env bash
# Narrates one Block with the Engine a built bundle carries, given the
# environment the shell gives it: the bundled uv provisions from the bundled
# lockfile, the Engine runs from the bundled sources, and the smallest
# instant model is downloaded and heard to the end of one sentence. Nothing
# supervises it, so it listens on a port picked here rather than announcing
# its own.
#
#   scripts/smoke-narration.sh <resources>
#
# <resources> is the bundle's resource folder: `/usr/lib/Readily` once a
# .deb or .rpm is installed (ci.yml's `smoke-linux` job), or a Mac bundle's
# `Contents/Resources`. The environment, the data folder and the model go to
# a scratch folder that is removed afterwards. It reaches the network twice,
# for the first-run sync and the model, and plays the sentence out loud: CI
# gives it a null audio sink.
set -euo pipefail

resources=${1:?usage: $0 <bundle resources folder>}
model=kitten-tts:15m # 24 MB and no Support Model: the least there is to fetch
start_deadline=300   # seconds for the first-run sync and the first probe
download_deadline=600
narration_deadline=120

test -x "$resources/uv" && test -d "$resources/engine" ||
  { echo "no uv and engine under $resources — build the bundle first" >&2; exit 1; }
resources=$(cd "$resources" && pwd)
scratch=$(mktemp -d)

# Stopped last to first: uvicorn waits for the event streams to close before
# it exits, so the Engine must outlive the curls reading them.
pids=()
cleanup() {
  local i
  for ((i = ${#pids[@]} - 1; i >= 0; i--)); do
    kill "${pids[i]}" 2>/dev/null || true
    wait "${pids[i]}" 2>/dev/null || true
  done
  rm -rf "$scratch"
}
trap cleanup EXIT

give_up() {
  echo "$1" >&2
  tail -n 40 "$scratch/engine.log" >&2 || true
  exit 1
}

port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
token=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
base=http://127.0.0.1:$port
auth="Authorization: Bearer $token"
# Loopback only: a shell with `http_proxy` set would send the token to it.
curl=(curl --noproxy '*' -sS)

# What `uv_command` in src-tauri/src/engine/launch.rs hands uv: the
# `INHERITED` variables and nothing else, the environment and bytecode
# outside the bundle, and the project named on argv from inside it.
launch=(env -i PATH="$PATH" HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}"
  ${XDG_RUNTIME_DIR:+XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR"}
  UV_PROJECT_ENVIRONMENT="$scratch/environment"
  PYTHONPYCACHEPREFIX="$scratch/bytecode")
uv=("$resources/uv" --project "$resources/engine")

echo "provisioning the Engine from the bundle"
(cd "$resources/engine" && "${launch[@]}" "${uv[@]}" sync --locked --no-dev --quiet) ||
  give_up "the bundled lockfile did not provision"

(cd "$resources/engine" &&
  exec "${launch[@]}" READILY_DATA_DIR="$scratch/data" READILY_ENGINE_PORT="$port" \
    READILY_ENGINE_TOKEN="$token" "${uv[@]}" run --no-sync readily-engine) \
  </dev/null >"$scratch/engine.log" 2>&1 &
pids+=($!)

deadline=$((SECONDS + start_deadline))
until "${curl[@]}" -o /dev/null -f -H "$auth" "$base/health" 2>/dev/null; do
  ((SECONDS < deadline)) || give_up "the Engine did not answer within ${start_deadline}s:"
  sleep 1
done

# Waits until the stream in $1 has a line naming $2 whose phase is one of
# $3 (an extended regex), within $4 seconds, and prints that phase.
await() {
  local deadline=$((SECONDS + $4)) phase
  while :; do
    phase=$(grep -F "$2" "$1" | grep -oE "\"phase\":\"($3)\"" | tail -n 1 || true)
    [[ -z $phase ]] || { echo "$phase"; return; }
    ((SECONDS < deadline)) || return 1
    sleep 0.5
  done
}

"${curl[@]}" -N -H "$auth" "$base/v1/models/events" >"$scratch/downloads.log" 2>/dev/null &
pids+=($!)
"${curl[@]}" -N -H "$auth" "$base/v1/events" >"$scratch/events.log" 2>/dev/null &
pids+=($!)

echo "downloading $model"
"${curl[@]}" -f -o /dev/null -X POST -H "$auth" "$base/v1/models/$model/download" ||
  give_up "the download was not accepted"
phase=$(await "$scratch/downloads.log" "\"modelId\":\"$model\"" 'installed|failed' "$download_deadline") ||
  give_up "$model was not installed within ${download_deadline}s:"
[[ $phase == '"phase":"installed"' ]] ||
  give_up "the download failed: $(grep -F '"failed"' "$scratch/downloads.log" | tail -n 1)"

echo "narrating one Block"
accepted=$("${curl[@]}" -H "$auth" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$model\",\"input\":\"Readily reads this sentence aloud.\"}" \
  "$base/v1/audio/speech")
narration=$(printf '%s' "$accepted" | grep -oE '"narrationId":"[^"]+"' || true)
[[ -n $narration ]] || give_up "the Narration was not accepted: $accepted"
# `finished` fires once the output device has drained the last sample, so
# it says the sentence was synthesized and played, not only queued.
phase=$(await "$scratch/events.log" "$narration" 'finished|failed' "$narration_deadline") ||
  give_up "the Narration did not finish within ${narration_deadline}s:"
[[ $phase == '"phase":"finished"' ]] ||
  give_up "the Narration failed: $(grep -F "$narration" "$scratch/events.log" | tail -n 1)"

echo "the bundled Engine narrated one Block with $model"
