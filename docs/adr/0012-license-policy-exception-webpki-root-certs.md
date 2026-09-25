# License-policy ruling for the updater's TLS stack

Status: accepted (2026-09-11) · Builds on [ADR 0005](0005-license-policy-exceptions-tauri-tree.md)

Over-the-air updates make Readily's first outbound HTTPS request of
its own, and `tauri-plugin-updater` brings a TLS stack with it. Built with
`default-features = false, features = ["rustls-tls"]`, that stack resolves
through `reqwest` → `rustls-platform-verifier` → `webpki-root-certs`, whose
licence is CDLA-Permissive-2.0. The permissive-only policy
(`docs/security-pipeline.md`) does not name it, so cargo-deny rejects it and
the ruling has to be made here.

## The decision

**CDLA-Permissive-2.0 is excepted for exactly `webpki-root-certs`.**

The Community Data License Agreement – Permissive 2.0 is not copyleft: it
grants use and redistribution of the data with no share-alike obligation and
no condition beyond keeping the disclaimer intact. The share-alike sibling is
CDLA-Sharing, which this is not. On that reading it clears the bar the policy
actually sets, and ADR 0005's first ruling — a permissive, obligation-free
licence belongs with MIT and BSD — would put it in `[licenses] allow`.

It goes in `[licenses.exceptions]` instead, scoped to the one crate. CDLA is
a licence for *data*, written for datasets, and the crate is one: a generated
copy of Mozilla's CA root store. Allowing the licence outright would also
admit a future crate shipping some other dataset under it, and a dataset is a
thing worth a fresh look rather than a blanket yes. Scoping it keeps
CDLA-Permissive-2.0 default-deny for everything else.

## What was weighed against it

Dropping the crate means dropping rustls: `native-tls` would use
Security.framework on macOS and pull nothing under CDLA. It would also pull
OpenSSL on the Linux lane, trade a memory-safe TLS implementation for a C
one, and re-enable the `system-proxy` machinery this build deliberately
removed. A narrowly scoped licence exception is the smaller change.

## Consequences

- `src-tauri/deny.toml` carries the exception and names this ADR; it stays
  limited to `webpki-root-certs`.
- Any other crate arriving under CDLA-Permissive-2.0 fails the `licenses`
  lane until a new ruling lands here.
- The root bundle is a fallback on macOS — `rustls-platform-verifier` uses
  the system trust store there — but it is compiled in, so the licence
  applies to what ships.
