# Readily is licensed under Apache-2.0

Status: accepted (2026-10-05)

Readily was MIT. MIT says nothing about patents or the project's name, and
an app built on ML models benefits from both of the things Apache-2.0 adds:

- **A patent grant (§3).** Every contributor licenses any patents their
  contribution needs, and loses that licence if they sue over patents in
  Readily.
- **A trademark clause (§6).** The licence grants no right to the "Readily"
  name, so a fork can't ship under it.

The public mirror has no GitHub Releases yet, so switching now means every
published binary ships under one licence. Every commit so far is the
maintainer's, apart from Dependabot's version bumps, so no other contributor
has to agree.

## The decisions

1. **Readily's own code is Apache-2.0.** `LICENSE` carries the text under
   the existing copyright line; `src-tauri/Cargo.toml` and
   `engine/pyproject.toml` declare it; README and CONTRIBUTING name it.
   Inbound = outbound stays the rule, and Apache-2.0 §5 already makes it
   the default for any contribution submitted without other terms, so no
   CLA or DCO is needed.

2. **A `NOTICE` file exists, naming the project and its copyright.** §4(d)
   binds redistributors to carry it only if one exists. Two lines cost
   nothing and keep the attribution on every fork and repackaging; it ships
   beside `LICENSE` in the repository, not inside the app bundle, which
   carries no copy of Readily's own licence either.

## Stays as it is

- **Vendored code keeps its own licence.** The Supertonic loader
  (`engine/src/readily_engine/loading/supertonic/__init__.py`) carries
  Supertone's MIT notice. Apache-2.0 code can include MIT code with its
  notice intact.
- **The dependency allowlists are unchanged.** `LICENSE_ALLOW` in `ci.yml`,
  `licenses:js`, `src-tauri/deny.toml` and `scripts/uv-about.toml` judge
  dependencies, not Readily itself.
- **The copyleft policy (ADR 0006, ADR 0007, ADR 0016) is unchanged.** The
  switch does not change what may be linked or imported.
- **`THIRD-PARTY-NOTICES` is unchanged.** Readily is already left out of its
  own notices (`src-tauri/about.toml` ignores private crates, and
  `scripts/third-party-notices.sh` ignores `readily-engine`); regenerating
  it after the switch produced no diff.
- **Snapshots already on the public mirror stay MIT** for anyone who copied
  them; the licence of a published copy cannot be withdrawn. The next
  `scripts/public-export.sh` snapshot carries Apache-2.0.

## Consequences

- A licence change is hard to undo once a release is out. This ADR is the
  record that it was made before the first one.
- A contributor who wants other terms for a contribution has to say so when
  submitting it (§5); otherwise it is Apache-2.0.
