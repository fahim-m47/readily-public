# License-policy rulings for the uv runtime

Status: accepted (2026-09-14) · Builds on [ADR 0005](0005-license-policy-exceptions-tauri-tree.md) and [ADR 0012](0012-license-policy-exception-webpki-root-certs.md)

Readily bundles a pinned uv binary to provision the Python Engine on first
run. Auditing the crates statically linked into uv 0.11.17 found four crates
under MPL-2.0 and one copy of `webpki-root-certs` under
CDLA-Permissive-2.0. The latter already has a ruling in ADR 0012; the MPL
crates need the same explicit decision that the shell and Engine trees use.

## The decision

**MPL-2.0 is excepted for exactly `astral-pubgrub`,
`astral-version-ranges`, `option-ext`, and `priority-queue` in the bundled uv
binary.**

The first three are published under MPL-2.0. `priority-queue` is offered
under `LGPL-3.0-or-later OR MPL-2.0`; Readily uses the MPL option and does not
accept its LGPL option. All four are unmodified transitive dependencies of
the uv binary. MPL-2.0 is file-scoped weak copyleft, so its obligations apply
to modifications of those files rather than Readily as a whole.

Replacing these crates means replacing or forking the pinned uv runtime.
That is disproportionate to the limited obligation, especially because the
same licence already has scoped exceptions in ADRs 0005 and 0007.

ADR 0012's exception for `webpki-root-certs` also applies to the copy linked
into uv. It remains scoped to that crate rather than admitting
CDLA-Permissive-2.0 globally.

uv's tree also carries crates under MIT-0 and CC0-1.0, which neither Rust
allow list (`src-tauri/deny.toml`, `src-tauri/about.toml`) admits. Both are
permissive public-domain-style grants that the Engine's pip-licenses gate in
`ci.yml` already accepts, so they join the uv allow list in
`scripts/uv-about.toml` rather than the scoped exceptions.

## Consequences

- `scripts/uv-third-party-notices/license-exceptions.toml` is the enforcement
  point for the vendored uv report. The notices generator rejects any new
  crate under MPL-2.0 or CDLA-Permissive-2.0 until that list and the relevant
  policy ruling change together. Which licences are scoped per crate comes
  from `src-tauri/deny.toml`, and any other licence the uv config accepts
  beyond the shell's allow list must be named in that file's `admitted`
  list, so neither ruling can be loosened by deleting lines.
- MPL-2.0 and CDLA-Permissive-2.0 remain default-deny for every other crate in
  uv's dependency tree.
- The generated notices carry all five licence texts and identify the crates
  they cover.
