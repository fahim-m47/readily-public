# License-policy rulings for the Tauri dependency tree

Status: accepted (2026-08-25) · Builds on [ADR 0001](0001-tauri-shell-python-engine.md)

The permissive-only policy (docs/security-pipeline.md) names MIT / Apache-2.0
/ BSD / ISC / Zlib and makes copyleft default-deny, with exceptions requiring
an ADR plus a `deny.toml` exceptions entry. The first real dependency tree —
Tauri v2, accepted by ADR 0001 — forces two rulings.

## The decisions

1. **Unicode-3.0 joins the permissive allow list.** The ICU4X crates
   (`icu_*`, `idna` internals) that every modern Rust HTTP/URL stack pulls
   are Unicode-3.0 — an OSI-approved permissive license with no copyleft
   obligations. It belongs in the same family as MIT/BSD and goes in
   `[licenses] allow`, not the exceptions list.
2. **MPL-2.0 is excepted for exactly five crates inside Tauri's webview
   stack**: `cssparser`, `cssparser-macros`, `dtoa-short`, `selectors`
   (wry/webview CSS machinery) and `option-ext` (via `dirs`). MPL-2.0 is
   file-scoped weak copyleft: obligations attach only to modifying those
   files, which we do not do, and it is unavoidable in every Tauri v2 app.
   The exception is scoped per-crate in `deny.toml` — MPL-2.0 stays
   default-deny for anything new.

## Consequences

- `src-tauri/deny.toml` is the enforcement point; its exceptions list must
  reference this ADR and stay limited to the crates above.
- A future dependency bringing MPL-2.0 (or any other copyleft) fails CI
  until a new ruling lands here.
- Transitive *unmaintained* advisories (Tauri's GTK3 bindings on Linux,
  `unic-*`, `proc-macro-error`) are configured to warn-only via
  `unmaintained = "workspace"`: they are Tauri's to fix, tracked upstream,
  and not actionable here. Direct dependencies going unmaintained still fail.
