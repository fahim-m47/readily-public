#!/usr/bin/env bash
# Runs the CI lanes that only Linux can answer, on a Linux machine over ssh.
# Set READILY_LINUX_HOST=user@host (required); see CONTRIBUTING.md.
#
#   bun run verify:linux            # engine + licenses + shell
#   bun run verify:linux engine     # one lane
#   READILY_LINUX_HOST=me@box bun run verify:linux
#
# `scripts/verify.sh` covers everything that is platform-independent plus the
# macOS lanes. Three CI jobs it cannot answer need a Linux dependency tree:
#
#   engine    mlx-audio's `sys_platform == 'darwin'` marker means Linux
#             resolves the exact set ubuntu-latest gets — not a stand-in.
#   licenses  same reason: the production Python tree differs by platform,
#             which is why CI runs `licenses` and `licenses-engine-macos`.
#   shell     the WebKitGTK build. macOS compiles a different tauri backend.
#
# It copies the working tree, not a commit, matching verify.sh's semantics.
set -uo pipefail

cd "$(dirname "$0")/.."

HOST=${READILY_LINUX_HOST:-}
if [[ -z $HOST ]]; then
  echo "READILY_LINUX_HOST is unset. Point it at a Linux machine with ssh key auth (user@host); see CONTRIBUTING.md." >&2
  exit 2
fi
DIR=${READILY_LINUX_DIR:-readily-verify}
lanes=("$@")
(( ${#lanes[@]} )) || lanes=(engine licenses shell)

failed=()
remote() { ssh -o BatchMode=yes "$HOST" "export PATH=\$HOME/.cargo/bin:\$HOME/.local/bin:\$HOME/.bun/bin:\$PATH; cd $DIR && $1"; }
run() { # run <lane> <remote command>
  printf '\n\033[1m── %s\033[0m (on %s)\n' "$1" "$HOST"
  remote "$2" || failed+=("$1")
}

if ! ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" true 2>/dev/null; then
  echo "Cannot reach $HOST over ssh with key auth. Set READILY_LINUX_HOST." >&2
  exit 2
fi

# --delete so a file deleted here cannot pass by lingering there. The build
# outputs are excluded rather than synced: they are this platform's, and the
# remote keeps its own between runs so a second pass is not a cold build.
printf '\033[1m── copying the working tree to %s:%s\033[0m\n' "$HOST" "$DIR"
rsync -a --delete --exclude .git --exclude node_modules --exclude dist \
  --exclude src-tauri/target --exclude 'engine/.venv*' --exclude '*.pyc' \
  ./ "$HOST:$DIR/" || { echo "rsync failed" >&2; exit 2; }

for lane in "${lanes[@]}"; do
  case $lane in
  engine)
    run 'engine: lockfile' 'uv sync --locked --project engine'
    run 'engine: lint'     'uv run --project engine ruff check engine'
    run 'engine: format'   'uv run --project engine ruff format --check engine'
    run 'engine: test'     'uv run --project engine pytest engine'
    ;;
  licenses)
    # ci.yml stays the single source of the policy, exactly as verify.sh
    # reads it rather than restating an allow-list a gate could drift from.
    allow=$(sed -n 's/^  LICENSE_ALLOW: "\(.*\)"$/\1/p' .github/workflows/ci.yml)
    ignore=$(sed -n 's/^  LICENSE_IGNORE: "\(.*\)"$/\1/p' .github/workflows/ci.yml)
    if [[ -n $allow && -n $ignore ]]; then
      # Its own venv, as in verify.sh, so the engine lane's tree is untouched.
      run 'licenses: engine (linux tree)' "UV_PROJECT_ENVIRONMENT=.venv-licenses uv sync --locked --no-dev --project engine && uvx pip-licenses@5.5.5 --python engine/.venv-licenses/bin/python --ignore-packages $ignore --allow-only '$allow'"
    else
      failed+=('licenses: engine (could not read the policy out of ci.yml)')
    fi
    ;;
  shell)
    # Ends the lane rather than recording a failure and carrying on, so an
    # unset-up box gets the one-line answer instead of that plus the compile
    # errors it would cause. The stand-ins are the ones ci.yml creates:
    # tauri-build's build script reads both, on every cargo invocation.
    printf '\n\033[1m── %s\033[0m (on %s)\n' 'shell: prerequisites' "$HOST"
    remote '
      pkg-config --exists webkit2gtk-4.1 || { echo "webkit2gtk4.1-devel and friends are not installed; see CONTRIBUTING.md"; exit 1; }
      cargo fmt --version >/dev/null 2>&1 && cargo clippy --version >/dev/null 2>&1 ||
        { echo "run: rustup component add rustfmt clippy"; exit 1; }
      mkdir -p src-tauri/resources dist && touch src-tauri/resources/uv' ||
      { failed+=('shell: prerequisites'); continue; }
    run 'shell: format' 'cargo fmt --check --manifest-path src-tauri/Cargo.toml'
    run 'shell: clippy' 'cargo clippy --locked --all-targets --manifest-path src-tauri/Cargo.toml -- -D warnings'
    run 'shell: test'   'cargo test --locked --manifest-path src-tauri/Cargo.toml'
    run 'shell: test (bundled)' 'cargo test --locked --manifest-path src-tauri/Cargo.toml --features tauri/custom-protocol'
    ;;
  *)
    echo "unknown lane: $lane (expected engine, licenses or shell)" >&2
    exit 2
    ;;
  esac
done

printf '\n'
if (( ${#failed[@]} )); then
  printf '\033[31mFAILED:\033[0m %s\n' "${failed[*]}"
  exit 1
fi
printf '\033[32mLinux lanes passed.\033[0m %s\n' \
  "$HOST is not ubuntu-latest: same dependency resolution, different distro packages."
