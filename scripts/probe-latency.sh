#!/usr/bin/env bash
# Measures the supervisor's health probe while an MLX model loads.
#
# The supervisor restarts a ready Engine after MISSES_BEFORE_RESTART probes
# in a row take longer than PROBE_TIMEOUT (src-tauri/src/engine/supervisor.rs).
# A model load that held the GIL inside a native call for that long would
# get a working Engine restarted. This script starts an Engine by hand,
# probes /health the way the supervisor does, ten times a second instead
# of once every two, asks for a Narration with an MLX model so the load
# happens, and fails when any probe misses the supervisor's deadline, when
# the Narration does not reach `playing`, or when the load was over too
# fast to have been probed.
#
#   scripts/probe-latency.sh [model]      default qwen3-tts:0.6b
#
# The model must already be installed in this Mac's Readily data folder.
# The Engine runs on a scratch folder holding a clone of that one model
# (an APFS clone: no bytes copied), so the run leaves no Narration in your
# History and touches nothing under the real folder. The Engine is new, so
# the model is loaded from scratch; the page cache is warm, because the
# Engine hashes every installed model's files before it answers its first
# probe, and that is the condition the app loads under too. The Narration
# plays a few words out loud before it is stopped.
set -euo pipefail
cd "$(dirname "$0")/.."

model=${1:-qwen3-tts:0.6b}
probe_timeout=2           # PROBE_TIMEOUT in supervisor.rs, in seconds
probe_every=0.1           # seconds between probes; the supervisor waits 2
start_deadline=120        # seconds for the Engine to answer its first probe
load_deadline=600         # seconds for the Narration to leave `preparing`
least_probes=10           # a load probed fewer times than this was no load

real="$HOME/Library/Application Support/Readily/models/${model%%:*}/${model#*:}"
if [[ ! -d $real ]]; then
  echo "$model is not installed: $real is missing" >&2
  exit 1
fi
scratch=$(mktemp -d)
data=$scratch/data

# Stopped last to first: uvicorn waits for the event stream to close
# before it exits, so the Engine must outlive the curl that is reading it.
# Registered before the clone so an interrupted or failed copy leaves no
# gigabytes behind.
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

mkdir -p "$data/models/${model%%:*}"
cp -Rc "$real" "$data/models/${model%%:*}/"

port=$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')
token=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
base=http://127.0.0.1:$port
auth="Authorization: Bearer $token"
# Every request is to the loopback Engine and none may leave the machine:
# a shell with `http_proxy` set would otherwise send the token, and the
# measurement, to the proxy.
curl=(curl --noproxy '*')

# Seconds since the epoch to the millisecond. `date +%s` is too coarse:
# a load over in under a second would put the probes before, during and
# after it on the same tick.
now() {
  perl -MTime::HiRes=time -e 'printf "%.3f\n", time'
}

READILY_DATA_DIR=$data READILY_ENGINE_PORT=$port READILY_ENGINE_TOKEN=$token \
  uv run --project engine readily-engine </dev/null >"$scratch/engine.log" 2>&1 &
pids+=($!)

# One probe, as the supervisor makes it: connect, request, answer, each
# under the deadline. Prints the status and the round trip in seconds;
# a probe that misses prints 000.
probe() {
  "${curl[@]}" -sS -o /dev/null --connect-timeout "$probe_timeout" -m "$probe_timeout" \
    -w '%{http_code} %{time_total}' -H "$auth" "$base/health" 2>/dev/null || true
}

give_up() {
  echo "$1" >&2
  tail -n 20 "$scratch/engine.log" >&2
  exit 1
}

deadline=$(($(date +%s) + start_deadline))
until [[ $(probe) == 200* ]]; do
  (($(date +%s) < deadline)) || give_up "the Engine did not answer within ${start_deadline}s:"
  sleep 0.2
done

probes=$scratch/probes.log
( while :; do printf '%s %s\n' "$(now)" "$(probe)"; sleep "$probe_every"; done ) >"$probes" &
prober=$!
pids+=($prober)
"${curl[@]}" -sN -H "$auth" "$base/v1/events" >"$scratch/events.log" 2>/dev/null &
pids+=($!)

started=$(now)
accepted=$("${curl[@]}" -sS -H "$auth" -H 'Content-Type: application/json' \
  -d "{\"model\":\"$model\",\"input\":\"Ready, Zyntrix.\"}" "$base/v1/audio/speech")
narration=$(printf '%s' "$accepted" | grep -oE '"narrationId":"[^"]+"' || true)
[[ -n $narration ]] || give_up "the Narration was not accepted: $accepted"

# The load is over once this Narration's phase leaves `preparing`: the
# first Block was synthesized, or the attempt failed.
settled() {
  grep -F "$narration" "$scratch/events.log" | grep -qvE '"phase":"preparing"'
}
deadline=$(($(date +%s) + load_deadline))
until settled; do
  (($(date +%s) < deadline)) || give_up "the Narration was still preparing after ${load_deadline}s:"
  sleep 0.5
done
loaded=$(now)
phase=$(grep -F "$narration" "$scratch/events.log" | grep -oE '"phase":"[a-z]+"' | tail -n 1)
"${curl[@]}" -sS -o /dev/null -X POST -H "$auth" "$base/v1/audio/stop"
sleep 0.5
kill "$prober" 2>/dev/null || true
wait "$prober" 2>/dev/null || true

echo "model $model, phase after $(awk -v a="$started" -v b="$loaded" 'BEGIN { printf "%.1f", b - a }')s: ${phase:-none}"
[[ $phase == '"phase":"playing"' ]] || give_up "the load did not end in playing, so nothing was measured:"

echo "probes while the Engine was ready, load included:"
awk -v limit="$probe_timeout" -v started="$started" -v loaded="$loaded" -v least="$least_probes" '
  BEGIN { n = 0; max = 0; sum = 0; during = 0; during_max = 0; missed = 0 }
  { n++; if ($3 > max) max = $3; sum += $3 }
  $1 >= started && $1 <= loaded { during++; if ($3 > during_max) during_max = $3 }
  $2 != 200 || $3 > limit { missed++ }
  $3 > limit / 4 { print "  over " limit / 4 "s: " $0 | "sort" }
  END {
    close("sort")
    if (n == 0) { print "  no probe was taken"; exit 1 }
    printf "  %d probes, longest %.3fs, mean %.3fs\n", n, max, sum / n
    printf "  %d during the load, longest %.3fs\n", during, during_max
    printf "  %d missed the %ss deadline\n", missed, limit
    if (during < least) { printf "  fewer than %d probes fell inside the load: too short to measure\n", least; exit 1 }
    exit missed > 0
  }' "$probes"
