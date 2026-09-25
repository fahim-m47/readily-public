# Security Policy

Readily is a local-first app: narration happens entirely on your machine, and
"nothing leaves your machine" is a security property we intend to be held to.
Readily downloads — voice models, its Python runtime, and its own updates —
and never uploads. The update check is the one request it makes without being
asked, a GET of `https://readily-updates.vercel.app/latest.json`;
`docs/threat-model.md` records exactly what that request discloses.

## Reporting a vulnerability

Please report vulnerabilities privately with GitHub's private vulnerability
reporting on this repository: Security tab, Report a vulnerability. Do not
open a public issue for security reports.

You can expect an acknowledgement within 7 days. Once a fix ships, reports and
credits are disclosed in the advisory.

## Scope of particular interest

- Model download & verification (catalog integrity, hash checks, archive extraction)
- Anything that causes network egress outside the model-download path
- Link/file ingestion (SSRF, parser exploits) — v1 surface
- Update mechanism and release artifact integrity. Updates are signed with a
  minisign key whose public half is compiled into every build; a release
  archive that does not verify against it is refused before anything is
  unpacked, and nothing installs without the reader agreeing first. Bypassing
  either of those is in scope. So is anything that lets the webview name the
  host, headers or version of the update request: the page holds no updater
  capability, and the check, verification, install and restart all happen in
  the Rust supervisor.
