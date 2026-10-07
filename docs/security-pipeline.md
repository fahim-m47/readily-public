# Security & review pipeline

Change a tool choice here by PR, with the reason.

## Where these run

GitHub Actions runs `ci.yml`, `gitleaks.yml` and `zizmor.yml` on every PR.
The same deterministic gates run from a contributor's machine: `bun run
verify` for the macOS-answerable lanes and `bun run verify:linux` for the
three that need a Linux dependency tree, against a Linux machine over ssh.
Those scripts follow `.github/workflows/` as their specification and read
`LICENSE_ALLOW`/`LICENSE_IGNORE` out of `ci.yml` rather than restating the
licence policy. See CONTRIBUTING.md for how to run them and how to set the
Linux machine up.

## Layers

| Layer | Tool | Where |
|---|---|---|
| Actions workflow security | zizmor | `.github/workflows/zizmor.yml` |
| Secret leaks | gitleaks | `.github/workflows/gitleaks.yml` |
| Dependency freshness (action SHA pins, cargo, bun, engine uv) | Dependabot (7-day cooldown) | `.github/dependabot.yml` |
| Merge policy (as code) | Rulesets JSON | `.github/rulesets/` — imported into the repository's rulesets for `main` and `v*` tags |
| Disclosure path | SECURITY.md | repo root |

All actions are pinned to commit SHAs; Dependabot proposes pin bumps as PRs.

## The check matrix

One `ci.yml`, run by Actions and by the verify scripts (see above). **No workflow-level `paths:` filters on required checks** — a
skipped required check never reports and deadlocks the merge. Instead: a
detect-changes first job (`dorny/paths-filter`), and jobs whose paths didn't
change report success without doing work.

- **`ts`** (ubuntu): `bun install --frozen-lockfile`, typecheck, lint, vitest.
- **`engine`** (ubuntu): `uv sync --locked`, ruff check + format check,
  pytest. Unit tests must not import `mlx` (structure the Engine so
  hash-verification, chunking, catalog logic test without it; ubuntu has no
  MLX wheel, so the job enforces the convention by construction). The macOS
  smoke job's Apple Silicon leg is the only Apple Silicon lane and does not
  run the Engine, so the MLX paths are exercised locally; its Intel leg runs
  this suite against the Intel tree's older onnxruntime. **No model
  downloads or inference in this lane**; the Linux smoke job is the one
  lane that has them.
- **`shell`** (ubuntu + webkit2gtk): `cargo fmt --check`,
  `clippy -D warnings`, `cargo test`, cargo-deny (advisories + licenses +
  bans + sources; subsumes cargo-audit).
- **`licenses`**: license gates on lockfile changes — cargo-deny (Rust),
  pip-licenses over the uv-synced Engine production environment,
  license-checker over the Bun-installed production graph.
  `licenses-engine-macos` runs pip-licenses over both Mac Engine trees,
  Apple Silicon and Intel.
- **macOS app-build smoke** (`smoke` in `ci.yml`; an Apple Silicon and an
  Intel runner): `bun tauri build --no-sign`, then
  `scripts/check-bundle.sh` confirms the `.app` carries `uv`, the
  Engine sources and the Catalog Manifest — the only lane compiling the
  macOS dependency tree users actually run. The Intel leg also runs the
  Engine's tests. Runs on every PR and push to `main`; not a required
  check.
- **Linux app-build smoke** (`smoke-linux` in `ci.yml`; ubuntu):
  `bun tauri build --no-sign` bundles the `.deb` and `.rpm`,
  `scripts/check-bundle.sh` checks the installed `.deb` the same way, and
  `scripts/smoke-narration.sh` provisions the bundled Engine with its own
  `uv` and lockfile, downloads `kitten-tts:15m`, and narrates one Block to
  a PulseAudio null sink. The only lane that fetches a model or runs
  inference, so Hugging Face can fail it; not a required check.
- **`semgrep`**: the custom trust-boundary rules in `.semgrep/`:
  (1) no network egress outside `engine/src/readily_engine/download/` —
  the client libraries by import, with `import socket` allowed only in
  `server/serve.py` (the one loopback bind) and `download/`, plus a
  tripwire on the common outbound socket calls (`create_connection`,
  `connect`, `connect_ex`, `sendto`) when the receiver originates from
  `socket.socket()`;
  (2) no model load outside `loading/`, which loads only store-promoted
  (hash-verified) paths and hash-verified bundled data (the pronunciation
  fallback, VibeVoice's tokenizer); (3) no pickle-family loaders anywhere in the Engine
  (`pickle`, `torch.load`, `joblib`, `np.load(allow_pickle=True)`);
  the sole dev-tool exception is `engine/tools/export_pronunciation.py`,
  which verifies the exact NRC checkpoint SHA-256 before deserializing it
  in a separate development environment. Neither that tool nor Torch ships
  in the Engine. The runtime consumes only the exported ONNX and JSON;
  (4) no `dangerouslySetInnerHTML` in the frontend; (5) no production opener
  plugin import outside `src/shell/browser.ts`; (6) no process spawn
  except fixed-argv `/usr/bin/tmutil` backup exclusions and
  `/usr/bin/afconvert` FLAC encoding. Focused rule fixtures prove that local
  APIs such as `sqlite3.connect()` remain allowed while real socket egress
  and arbitrary subprocesses remain blocked; the production scan excludes
  only those malicious fixtures after their dedicated rule test. The gate
  runs unconditionally — a source path-filtered skip would silently waive a
  security boundary.
- Hygiene: workflow-level `permissions: contents: read`; concurrency-cancel
  superseded runs; Bun postinstall scripts stay disabled — no
  `trustedDependencies` without an ADR.

## Merge & branch policy

- Everything lands on `main` via PR — squash-merge only, linear history,
  branches auto-deleted. Repo settings enforce squash-only.
- **Rulesets as code** in `.github/rulesets/`, imported into the repository's rulesets:
  - `main.json` — PR required, 0 required approvals, required status checks
    (`detect-changes`, `ts`, `engine`, `semgrep`, `shell`, `licenses`,
    `gitleaks`), force-push/deletion blocked, **no bypass actors**. The
    emergency path is toggling the ruleset off — deliberate friction plus an
    audit-log entry, not a standing hole.
  - `release-tags.json` — `v*` tags immutable once created (a tampered
    release tag is code execution on users' machines).
- **Commit signing**: not required by the ruleset.

## Review policy

- **Every PR:** the required checks above, then a maintainer review before
  squash-merge. The ruleset requires no approvals, so the review is policy
  rather than a GitHub gate. Every finding is dispositioned in the PR
  (fixed, or dismissed with a reason) before merge.
- **Fork PRs:** workflows run on `pull_request` and hold no secrets. Never
  `pull_request_target` with PR-code checkout.
- **Trust-boundary PRs:** say so in the description and get a deeper
  security review — a line-by-line read against the threat model — before
  merge. What counts as a trust boundary — and the path globs that mark the
  tier — is defined by the review-triggers table in `docs/threat-model.md`,
  the canonical list.

## Licensing policy

- App is Apache-2.0 (ADR 0017). Inbound = outbound; no CLA, no DCO.
- **Permissive-only** for anything linked or imported into shipped
  processes — shell Rust crates, webview npm packages, Engine Python
  packages, and crates linked into the bundled uv binary. The enforceable
  allow lists are the canon: `src-tauri/deny.toml`, the `licenses:js` gate
  in `package.json`, the pip-licenses gate in `ci.yml`, and
  `scripts/uv-about.toml` with
  `scripts/uv-third-party-notices/license-exceptions.toml`. Prose never
  restates them (it drifts); every addition goes through an ADR (0005 is the
  running record). Copyleft is default-deny; exceptions go through the
  relevant scoped exceptions list plus an ADR, or an ADR alone for a
  library a wheel bundles, which no gate sees (ADR 0016). GPL tools as
  separate processes are legally mere aggregation but remain last-resort +
  ADR.
- Enforced at **PR time** (`licenses` job), not at release — a
  release-time audit catches violations months late.
- **Model weights carry their own licence rule**, and nothing above
  enforces it: weights are not linked into any process, so they pass every
  dependency gate unlooked-at. The bar is not "permissive". It is "may
  Readily hand these to a reader for ordinary personal use": redistributable,
  and not scoped non-commercial or research-only. Licences that bind the
  reader (Llama, OpenRAIL-M) are admitted, and the app carries the
  obligations that come with handing them on: the licence text travels with
  the weights, the sheet shows it, and the reader accepts every licence
  with one tick at first launch. The
  allow list is the canon, in code: `LICENCE_OBLIGATIONS` in
  `engine/src/readily_engine/catalog/licence_table.py`, checked on the manifest
  schema so the curation script refuses a draft before it downloads and CI
  refuses a hand-edited entry when it parses the committed manifest.
  Additions go through an ADR (0009 is the running record; 0008 is the
  history).
- `THIRD-PARTY-NOTICES` at the repo root names every package in the four
  shipped trees with the licence text it ships, or the MIT text for a
  package that names MIT and ships none; Python packages' NOTICE files are
  kept. `scripts/third-party-notices.sh` writes it (cargo-about reports for
  the shell and uv, pip-licenses, and license-checker); the `notices` check
  in `bun run verify` regenerates it and fails on a diff, so a dependency
  bump cannot leave it stale, and validates the vendored uv report against
  `scripts/uv-about.toml`, the scoped exceptions (for the licences
  `src-tauri/deny.toml` admits only per crate), and the hash in
  `scripts/uv-third-party-notices/report.hash`, which covers the report
  itself and the inputs it was generated from. That hash makes the crate
  list tamper-evident rather than tamper-proof: the check pastes the report
  in rather than regenerating it, so nothing under the gate re-derives the
  inventory and a fresh hash can be recorded by hand. What it buys is that
  the list can no longer change without a `report.hash` diff for review to
  question, and the honest way to produce one is
  `scripts/uv-third-party-notices/refresh.sh`, which rebuilds the report
  from a clean checkout of the pinned uv tag; question a `report.hash`
  change that arrives without a rebuilt report. The DMG carries the result
  in `Resources`. Model licences are
  curation-enforced in the Catalog Manifest (ADR 0003, ADR 0009).
- Privacy-claim wording: the app *downloads* (models, uv-provisioned Python —
  checksummed by uv); it never *uploads*. The Semgrep no-egress rule
  allowlists exactly the download module.

## Later

- CodeQL code scanning and `dependency-review-action` on PRs
- Require signed commits in the main ruleset
- OpenSSF Best Practices badge (passing tier) + Scorecard action
- `actions/attest-build-provenance`, SBOM. Developer ID signing,
  notarization and the DMG's checksum already happen locally in
  `scripts/release-candidate.sh`; what is missing is a build anyone
  else can verify independently.
