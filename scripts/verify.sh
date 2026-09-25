#!/usr/bin/env bash
# Runs the deterministic gates from .github/workflows/ci.yml against the
# working tree, choosing lanes the same way the `detect-changes` job does.
#
#   bun run verify           # lanes whose files changed vs origin/main
#   bun run verify --all     # every lane
#   bun run verify --smoke   # add the macOS app build (slow, macOS only)
#   RANGE=a..b bun run verify
#
# This and `scripts/verify-linux.sh` run the ci.yml lanes before a PR opens;
# ci.yml is the spec they follow and the source of the licence policy. This
# half runs one platform against a dirty tree with warm caches, so it cannot
# see a clean-room lockfile resolution; the lanes needing a Linux dependency
# tree are the other half's. See CONTRIBUTING.md.
set -uo pipefail

cd "$(dirname "$0")/.."

failed=()
skipped=()   # CI gates this machine could not attempt
run() { # run <lane> <command...>
  local lane=$1; shift
  printf '\n\033[1m── %s\033[0m\n' "$lane"
  if "$@"; then return 0; fi
  failed+=("$lane")
}

changed() { # changed <pattern...> — does any changed path match?
  local f
  for f in "$@"; do
    grep -qE "$f" <<<"$files" && return 0
  done
  return 1
}

all=no
smoke=no
for arg in "$@"; do
  case $arg in
    --all)   all=yes ;;
    --smoke) smoke=yes ;;
    *) echo "unknown option: $arg (expected --all or --smoke)" >&2; exit 2 ;;
  esac
done

if [[ $all == yes ]]; then
  files='ALL'
else
  base=${RANGE:-$(git merge-base HEAD origin/main 2>/dev/null)}
  head=${HEAD_REV:-HEAD}
  if [[ -z $base ]]; then
    echo "No origin/main to diff against; running every lane." >&2
    files='ALL'
  else
    # -z plus a NUL-splitting read: porcelain quotes paths with spaces and
    # writes renames as "old -> new", either of which defeats an anchored
    # pattern and silently drops a lane.
    files=$(git diff -z --name-only "$base" "$head" | tr '\0' '\n')
    # The working tree counts only when it is what is about to be pushed; a
    # gate that reads a tree unrelated to $head reports on the wrong code.
    if [[ $(git rev-parse "$head") == $(git rev-parse HEAD) ]]; then
      dirty=$( { git diff -z --name-only HEAD; \
                 git ls-files -z --others --exclude-standard; } | tr '\0' '\n' )
      files=$(printf '%s\n%s\n' "$files" "$dirty")
    fi
    files=$(sort -u <<<"$files")
  fi
fi
[[ $files == 'ALL' ]] && changed() { return 0; }

# Filters mirror ci.yml's `detect-changes`. Keep them in step.
FRONTEND='^src/|^index\.html$|^package\.json$|^bun\.lock$|^bunfig\.toml$|^vite\.config\.ts$|^tsconfig.*\.json$|^eslint\.config\.js$'
ENGINE='^engine/|^catalog/'
SHELL_='^src-tauri/|^scripts/prepare-bundle\.sh$'
DEPS='^package\.json$|^bun\.lock$|^bunfig\.toml$|^engine/pyproject\.toml$|^engine/uv\.lock$|^src-tauri/Cargo\.(toml|lock)$|^src-tauri/deny\.toml$|^src-tauri/about\.(toml|hbs)$|^THIRD-PARTY-NOTICES$|^scripts/third-party-notices\.sh$|^scripts/uv-about\.toml$|^scripts/uv-third-party-notices/|^scripts/licence-texts/|^scripts/prepare-bundle\.sh$'
WORKFLOWS='^\.github/(workflows|actions)/'

# ci.yml adds itself to every lane's filter, so a pipeline edit exercises all
# of them. Same rule here.
if changed "$WORKFLOWS"; then changed() { return 0; }; fi

if changed "$FRONTEND"; then
  run 'ts: install'   bun install --frozen-lockfile
  run 'ts: typecheck' bun run typecheck
  run 'ts: lint'      bun run lint
  run 'ts: test'      bun run test
fi

if changed "$ENGINE"; then
  run 'engine: lockfile' uv sync --locked --project engine
  run 'engine: lint'     uv run --project engine ruff check engine
  run 'engine: format'   uv run --project engine ruff format --check engine
  run 'engine: test'     uv run --project engine pytest engine
fi

if changed "$SHELL_"; then
  # tauri-build's build script reads both of these, so without them every
  # cargo invocation below fails before it compiles anything. ci.yml creates
  # the same stand-ins; the real `uv` and frontend are the smoke lane's job.
  mkdir -p src-tauri/resources dist && touch src-tauri/resources/uv
  run 'shell: format' cargo fmt --check --manifest-path src-tauri/Cargo.toml
  run 'shell: clippy' cargo clippy --locked --all-targets --manifest-path src-tauri/Cargo.toml -- -D warnings
  run 'shell: test'   cargo test --locked --manifest-path src-tauri/Cargo.toml
  # `custom-protocol` is the cfg the shipped app compiles under, and Tauri
  # derives `cfg(dev)` from its absence. Without this pass the tests that
  # only exist in a bundled build are never compiled, here or by clippy.
  run 'shell: test (bundled)' \
    cargo test --locked --manifest-path src-tauri/Cargo.toml --features tauri/custom-protocol
  if command -v cargo-deny >/dev/null; then
    run 'shell: cargo-deny' cargo deny --manifest-path src-tauri/Cargo.toml check
  else
    skipped+=('shell: cargo-deny (cargo-deny not installed)')
  fi
fi

if changed "$DEPS"; then
  run 'licenses: js' bun run licenses:js
  if command -v cargo-deny >/dev/null; then
    run 'licenses: rust' cargo deny --manifest-path src-tauri/Cargo.toml check licenses
  else
    skipped+=('licenses: rust (cargo-deny not installed)')
  fi
  # The committed file is what the DMG ships; a dependency bump that did not
  # regenerate it would ship stale notices. Apple Silicon only, because the
  # Engine's shipped tree is resolved for it.
  if [[ $(uname -s) == Darwin && $(uname -m) == arm64 ]] && command -v cargo-about >/dev/null; then
    run 'notices' scripts/third-party-notices.sh --check
  else
    skipped+=('notices (Apple Silicon with cargo-about only)')
  fi
  # ci.yml is the single source of the policy: duplicating the allow-list here
  # would let the local gate enforce a rule CI does not.
  allow=$(sed -n 's/^  LICENSE_ALLOW: "\(.*\)"$/\1/p' .github/workflows/ci.yml)
  ignore=$(sed -n 's/^  LICENSE_IGNORE: "\(.*\)"$/\1/p' .github/workflows/ci.yml)
  if [[ -n $allow && -n $ignore ]]; then
    # A production-only tree in its own venv, so this lane neither prunes
    # the dev venv every other lane (and any other terminal) is using nor
    # has to put it back afterwards.
    run 'licenses: engine' bash -c '
      UV_PROJECT_ENVIRONMENT=.venv-licenses uv sync --locked --no-dev --project engine &&
      uvx pip-licenses@5.5.5 --python engine/.venv-licenses/bin/python \
        --ignore-packages '"$ignore"' --allow-only "'"$allow"'"'
  else
    skipped+=('licenses: engine (could not read the policy out of ci.yml)')
  fi
fi

# The macOS app build. This machine is the hardware that lane wants, so here
# it is opt-in only for being a full release build.
if [[ $smoke == yes ]]; then
  if [[ $(uname -s) == Darwin ]]; then
    run 'smoke: install' bun install --frozen-lockfile
    run 'smoke: build'   bun tauri build --no-sign
    run 'smoke: bundle'  scripts/check-macos-bundle.sh
  else
    skipped+=('smoke app build (macOS only)')
  fi
else
  skipped+=('smoke app build (pass --smoke)')
fi

# The version is written in four files and the bump script keeps them equal;
# this is the check that nothing edited one by hand. Cheap, so unconditional.
run 'version' scripts/check-version.sh

# gitleaks scans history, so unlike every other lane it does not see the
# working tree: an uncommitted secret passes here and fails CI.
if command -v gitleaks >/dev/null; then
  run 'gitleaks' gitleaks git --no-banner
else
  skipped+=('gitleaks (not installed)')
fi

# The trust-boundary rules run unconditionally in CI, so they do here too.
# Rule self-tests first: a rule that silently stopped matching is a gate that
# reports green for a rule nobody wrote.
export SEMGREP_ENABLE_VERSION_CHECK=0
run 'semgrep: rule tests' uvx semgrep@1.174.0 test \
  --config .semgrep/engine-no-egress.yml engine/tests/semgrep/engine-no-egress.py
run 'semgrep: rule tests (server)' uvx semgrep@1.174.0 test \
  --config .semgrep/engine-server-no-outbound-socket.yml engine/tests/semgrep/server/serve.py
run 'semgrep: scan' uvx semgrep@1.174.0 scan --config .semgrep \
  --exclude engine/tests/semgrep/engine-no-egress.py \
  --exclude engine/tests/semgrep/server/serve.py \
  --error --metrics=off

if changed "$WORKFLOWS"; then
  if command -v uvx >/dev/null; then
    run 'zizmor' uvx zizmor@1.10.0 .github/workflows
  else
    skipped+=('zizmor (uvx not installed)')
  fi
fi

printf '\n'
for s in ${skipped+"${skipped[@]}"}; do
  printf '\033[33mnot attempted:\033[0m %s\n' "$s"
done
# Always named, never implied: these gates are required in CI and this run
# cannot answer them. See CONTRIBUTING.md.
printf '\033[33mnot attempted:\033[0m %s\n' \
  'the Ubuntu engine and licence trees and the WebKitGTK shell build — run `bun run verify:linux`'
printf '\033[33mnot attempted:\033[0m %s\n' \
  'a clean-room --locked/--frozen-lockfile resolution'
# On macOS the licence lane above *is* licenses-engine-macos: it resolves the
# same darwin tree. Anywhere else that job has no local stand-in.
[[ $(uname -s) == Darwin ]] || printf '\033[33mnot attempted:\033[0m %s\n' \
  'licenses-engine-macos (needs macOS)'

if (( ${#failed[@]} )); then
  printf '\n\033[31mFAILED:\033[0m %s\n' "${failed[*]}"
  exit 1
fi
printf '\n\033[32mSelected lanes passed.\033[0m Only the ones this run attempted.\n'
