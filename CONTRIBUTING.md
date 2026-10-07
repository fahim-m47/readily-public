# Contributing

Everything below is how every change lands, including the maintainer's own.

## How changes land

- Everything merges to `main` via PR. No direct pushes.
- **Squash-merge only**, linear history. The PR title becomes the commit
  title, so titles explain *why the change matters* in plain language, not
  what files moved.
- PR descriptions open with the problem, then the solution. Branches are
  deleted on merge.

## The gates

Deterministic checks (see `docs/security-pipeline.md` for the full matrix):
TypeScript (typecheck/lint/test), Python Engine (ruff/pytest — unit tests
never import `mlx`, never download models), Rust shell (fmt/clippy/test/
cargo-deny), license gates on lockfile changes, plus gitleaks and zizmor.
All of them must pass before a PR merges.

GitHub Actions runs them on every PR: `.github/workflows/ci.yml` holds the
matrix and is the single source of the licence policy, with `gitleaks.yml`
and `zizmor.yml` beside it. `.github/rulesets/main.json` names the checks a
PR must pass: `detect-changes`, `ts`, `engine`, `semgrep`, `shell`,
`licenses` and `gitleaks`. The same lanes run locally, so you can see a
verdict before you push. Two app-build smoke jobs run beside them and are
not required: `smoke` on macOS, and `smoke-linux`, the one lane that
downloads a Voice Model, to narrate one Block with the Engine inside the
`.deb` it builds.

### Running the gates locally

`bun run verify` runs the lanes above that can run on a dev machine, picking
them from what changed the same way CI's `detect-changes` job does. `bun run
verify --all` forces every lane; `bun run verify --smoke` adds the macOS app
build. `bun run hooks:install` points `core.hooksPath` at `.githooks/`, which
runs the same script on `git push` (bypass once with `--no-verify`).

It closes by naming every gate it did not attempt — an uninstalled
`gitleaks` or `cargo-deny`, and the lanes below — so a pass is never read as
covering more than it ran.

Three of the gates need a Linux dependency tree, so `bun run verify:linux`
rsyncs your working tree to a Linux machine and runs them there: the `engine`
lane, the Linux `licenses` tree and the WebKitGTK `shell` build. `verify.sh`
covers the rest on your Mac. Between them, run both and every gate has
answered.

The first two Linux lanes are not stand-ins for anything: `mlx-audio` is
pinned behind a `sys_platform == 'darwin'` marker, so any Linux resolves the
identical set the `ubuntu-latest` matrix names. The `shell` lane is the one
place a different distro shows: it builds against that machine's WebKitGTK
rather than Ubuntu's.

On macOS the `licenses: engine` lanes *are* `licenses-engine-macos`,
resolving the same two darwin trees, and `--smoke` is your Mac's leg of the
`smoke` job: both run `scripts/check-bundle.sh`, so the two cannot
check different things.

What neither script can see: a clean-room `--locked`/`--frozen-lockfile`
resolution. Both also read your working tree, uncommitted files and all,
rather than the committed one — so run them with a clean `git status`.

#### Setting up the Linux machine

Any Linux machine you can ssh into with key auth will do — a spare box on
your LAN, a VM, a cloud instance. Set `READILY_LINUX_HOST=user@host` in your
shell profile (`verify:linux` refuses to run without it) and optionally
`READILY_LINUX_DIR` for the checkout path there. It needs `rsync` and:

```sh
# Fedora. Debian/Ubuntu names differ (libwebkit2gtk-4.1-dev, and so on).
sudo dnf install -y webkit2gtk4.1-devel openssl-devel \
  libappindicator-gtk3-devel librsvg2-devel file
curl -LsSf https://astral.sh/uv/install.sh | sh          # engine + licences
curl -LsSf https://sh.rustup.rs | sh                     # shell
rustup component add rustfmt clippy
curl -fsSL https://bun.sh/install | bash
```

Nothing else is required — `webkit2gtk4.1-devel` pulls in gtk3, libsoup3 and
javascriptcoregtk. The script checks these before it runs a lane, so a box
that is not set up says so instead of failing as a compile error.

`verify:linux` compiles the shell and never bundles. Building the Linux app
on that machine also takes `ffmpeg` with libopus (the README's
[Building](README.md#building) has the Debian/Ubuntu line), and hearing it
takes PortAudio and GStreamer's base and good plugins. A release build
(`scripts/linux-candidate.sh`, [docs/release.md](docs/release.md)) opens
the packages it made with `rpm2cpio` and `cpio`, on Fedora from the
`rpm` and `cpio` packages, and writes the dnf metadata with `createrepo_c`,
which it fetches through `uvx` at a pinned version, so nothing else goes
on the box. `bun tauri dev`
serves the committed AAC `.m4a` previews rather than the Opus a bundle
carries, so previews in dev also need GStreamer's libav plugin
(`gstreamer1.0-libav` on Debian/Ubuntu). `scripts/smoke-narration.sh
<resources>` runs `smoke-linux`'s narration against any built bundle's
resource folder, on Linux or a Mac.

## Review

Every PR gets a maintainer review before it is squash-merged. The ruleset
requires no approvals, so this is policy rather than a GitHub gate. Every
review finding is **dispositioned in the PR before merge**: fixed, or
dismissed with a stated reason in a comment.

## Trust-boundary PRs

PRs touching catalog/model download & verification, archive extraction,
URL/file ingestion, IPC/capabilities/entitlements, the updater, CI workflows,
or any new network call site get a deeper security review — a line-by-line
read against the threat model — before merge. Say so in the PR description
if your change qualifies. The threat model
(`docs/threat-model.md`) defines the boundaries.

## Licensing

- The app is Apache-2.0 (ADR 0017). Inbound = outbound: contributions are
  accepted under Apache-2.0, which is what its §5 says of any contribution
  without other terms. No CLA, no DCO.
- Anything linked or imported into shipped processes must carry a
  permissive license. The enforceable allow lists live in
  `src-tauri/deny.toml` (Rust), the `licenses:js` gate in `package.json`
  (JS), the pip-licenses gate in `ci.yml` (Python), and
  `scripts/uv-about.toml` plus
  `scripts/uv-third-party-notices/license-exceptions.toml` (the bundled uv
  binary, which also inherits `deny.toml`'s per-crate scoping); additions
  require an ADR. Copyleft is default-deny. CI enforces
  this on lockfile or uv pin changes — check the transitive tree *before*
  adding a dependency.
- A change to a shipped dependency also changes `THIRD-PARTY-NOTICES`, the
  licence texts every build ships, which covers the Apple Silicon, Intel
  and Linux builds alike. Run `scripts/third-party-notices.sh` on an Apple Silicon
  Mac. For a uv pin or policy change, run
  `scripts/uv-third-party-notices/refresh.sh` instead; it refuses to run
  under any `cargo-about` but the version it pins. Commit the result; the
  `notices` check in `bun run verify` fails while the file is stale.
- A wheel may bundle libraries the gates never see, because they judge the
  package's declared licence. Check what a new dependency's Linux and macOS
  wheels carry before adding it. numpy's Linux wheel carries `libgfortran`
  under GPL with the GCC runtime exception, which permits linking, and
  `libquadmath` under LGPL, which ADR 0016 admits for that wheel alone. A
  bundled runtime under such an exception is allowed; any other copyleft
  library a wheel bundles needs a ruling of its own. Its text stays in the
  notices as its authors wrote it.
- Model weights are not linked into anything, so they meet a different
  rule: the Catalog may carry any weights Readily can hand a reader for
  ordinary personal use, and the app carries the licence's obligations with
  them (ADR 0009). Non-commercial and research-only scopes stay out. Record
  the licence on the *weights*, not the repository's tag.
