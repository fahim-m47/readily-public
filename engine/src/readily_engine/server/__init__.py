"""The localhost HTTP API the webview talks to (ADR 0001 §4).

Owns everything with a B3 face: the bind (`serve()` is the only place the
Engine listens, 127.0.0.1 only — enforced by the bind-only-in-server
Semgrep rule; the port is announced on stdout only after the socket is
held), token sourcing (environment, never argv), bearer-token auth
on every request, the webview-only Origin allowlist, the refusal of
non-HTTP protocols, and how long the listener lives — `lifetime` ends the
Engine when the supervisor's pipe closes, so no orphan keeps listening
after the app is gone (threat model B3).
"""
