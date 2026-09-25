"""The Readily Engine.

The local service that downloads, verifies, and runs Voice Models to produce
Narrations (ADR 0001). The UI never runs a model itself; it asks the Engine.

The subpackage layout is a security boundary, not a style choice — the threat
model's review triggers and the Semgrep rules in `.semgrep/` target these
paths (docs/threat-model.md):

- `download/` — the ONLY package that may touch the network.
- `store/`    — verify + promote; owns the models directory.
- `loading/`  — the ONLY package that may call model-loading APIs.
- `server/`   — the localhost HTTP API: bind, auth, routes.
"""
