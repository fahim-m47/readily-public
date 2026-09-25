#!/usr/bin/env bash
# Prints one hash over the vendored uv report and everything it is generated
# from: the template, the cargo-about config, the refresh script, and this
# list. refresh.sh records it in report.hash; scripts/third-party-notices.sh
# recomputes it, so editing the report or any of its inputs fails the notices
# check until the hash is recorded again.
#
# The report is in the list because nothing else re-derives its crate
# inventory: the notices check pastes the report in verbatim instead of
# regenerating it, and validate.py rules on the licences the report names,
# not on the crates it leaves out. A dropped crate is therefore evident
# rather than impossible: recording a fresh hash is one command, but the
# crate list can no longer change without a report.hash diff for review to
# question. Settling it under the check itself would mean rebuilding the
# report from the pinned uv tag every time, which is refresh.sh's job.
set -euo pipefail

cd "$(dirname "$0")/../.."
shasum -a 256 \
  scripts/uv-third-party-notices/report.txt \
  src-tauri/about.hbs \
  scripts/uv-about.toml \
  scripts/uv-third-party-notices/refresh.sh \
  scripts/uv-third-party-notices/report-hash.sh \
  | shasum -a 256 | cut -d ' ' -f 1
