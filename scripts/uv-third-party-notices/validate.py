#!/usr/bin/env python3
"""Check a cargo-about report for uv against the licence policy.

Every licence in the report must be accepted by the cargo-about config, the
crates under a scoped licence must match license-exceptions.toml exactly,
and the report must describe the pinned uv version. A scoped licence is one
src-tauri/deny.toml admits only per crate: the shell's ruling decides which
licences need a named-crate exception, so this file cannot loosen it. Any
other licence the uv config accepts beyond the shell's allow list must be
named in `admitted`, so a blanket admission is always an explicit edit.
"""

from __future__ import annotations

import argparse
import difflib
import re
from pathlib import Path
from typing import NamedTuple

import tomllib

SECTION_SEPARATOR = "-" * 70
PACKAGE_LINE = re.compile(r"^  ([A-Za-z0-9_.+-]+) (\S+)$")
LICENCE_HEADING = re.compile(r".+ \(([^()]*)\)")


class Package(NamedTuple):
    licence: str
    name: str
    version: str


def parse_report(path: Path) -> list[Package]:
    sections = path.read_text(encoding="utf-8").split(f"\n{SECTION_SEPARATOR}\n")
    if len(sections) < 2 or sections[-1]:
        raise SystemExit(f"{path} is not a separator-delimited cargo-about report")

    packages: list[Package] = []
    for section_number, section in enumerate(sections[:-1], start=1):
        lines = section.removeprefix("\n").splitlines()
        heading = LICENCE_HEADING.fullmatch(lines[0]) if lines else None
        if heading is None:
            raise SystemExit(f"{path}: malformed heading in section {section_number}")
        licence = heading.group(1)
        package_index = 1
        package_count = 0
        while package_index < len(lines):
            package = PACKAGE_LINE.fullmatch(lines[package_index])
            if package is None:
                break
            packages.append(Package(licence, *package.groups()))
            package_count += 1
            package_index += 1
        if package_count == 0 or package_index == len(lines) or lines[package_index]:
            raise SystemExit(
                f"{path}: malformed package list in section {section_number}"
            )
    return packages


class ShellPolicy(NamedTuple):
    allowed: set[str]
    scoped: set[str]


def parse_deny(path: Path) -> ShellPolicy:
    with path.open("rb") as deny_file:
        licenses = tomllib.load(deny_file).get("licenses")
    allowed = licenses.get("allow") if isinstance(licenses, dict) else None
    entries = licenses.get("exceptions") if isinstance(licenses, dict) else None
    if not is_string_list(allowed) or not isinstance(entries, list):
        raise SystemExit(
            f"{path}: expected `licenses.allow` and `licenses.exceptions` lists"
        )
    scoped: set[str] = set()
    for entry in entries:
        allow = entry.get("allow") if isinstance(entry, dict) else None
        if not is_string_list(allow):
            raise SystemExit(f"{path}: each licence exception needs an `allow` list")
        scoped.update(allow)
    if not scoped:
        raise SystemExit(f"{path} scopes no licence per crate")
    return ShellPolicy(set(allowed), scoped)


class UvExceptions(NamedTuple):
    admitted: set[str]
    crates: set[tuple[str, str]]


def parse_exceptions(path: Path, scoped: set[str]) -> UvExceptions:
    with path.open("rb") as exceptions_file:
        document = tomllib.load(exceptions_file)
    if set(document) != {"admitted", "exceptions"}:
        raise SystemExit(f"{path}: expected exactly `admitted` and `exceptions`")
    admitted = document["admitted"]
    entries = document["exceptions"]
    if not is_string_list(admitted) or not isinstance(entries, list):
        raise SystemExit(f"{path}: `admitted` and `exceptions` must be lists")
    crates: set[tuple[str, str]] = set()
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        allow = entry.get("allow") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not is_string_list(allow):
            raise SystemExit(f"{path}: each exception needs `name` and an `allow` list")
        for licence in allow:
            if licence not in scoped:
                raise SystemExit(
                    f"{path}: {licence} is not scoped per crate in deny.toml"
                )
            if (licence, name) in crates:
                raise SystemExit(f"{path}: duplicate exception: {licence} {name}")
            crates.add((licence, name))
    return UvExceptions(set(admitted), crates)


def is_string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("config", type=Path)
    parser.add_argument("exceptions", type=Path)
    parser.add_argument("deny", type=Path)
    parser.add_argument("expected_uv_version")
    args = parser.parse_args()

    packages = parse_report(args.report)
    with args.config.open("rb") as config_file:
        accepted = set(tomllib.load(config_file)["accepted"])

    reported_licences = {package.licence for package in packages}
    rejected = sorted(reported_licences - accepted)
    if rejected:
        raise SystemExit(
            f"{args.report} contains licences absent from {args.config}: "
            + ", ".join(rejected)
        )

    shell = parse_deny(args.deny)
    exceptions = parse_exceptions(args.exceptions, shell.scoped)
    # Everything the uv config accepts beyond the shell's allow list is
    # either scoped per crate or an admission ADR 0013 rules on by name;
    # dropping a licence from deny.toml's exceptions cannot quietly widen it.
    unruled = sorted(accepted - shell.allowed - shell.scoped - exceptions.admitted)
    if unruled:
        raise SystemExit(
            f"{args.config} accepts licences the shell policy neither allows nor "
            f"scopes and {args.exceptions} does not admit: " + ", ".join(unruled)
        )
    expected_exceptions = exceptions.crates
    actual_exceptions = {
        (package.licence, package.name)
        for package in packages
        if package.licence in shell.scoped
    }
    if actual_exceptions != expected_exceptions:
        expected_lines = [
            f"{licence} {package}\n" for licence, package in sorted(expected_exceptions)
        ]
        actual_lines = [
            f"{licence} {package}\n" for licence, package in sorted(actual_exceptions)
        ]
        print(
            "".join(
                difflib.unified_diff(
                    expected_lines,
                    actual_lines,
                    fromfile=str(args.exceptions),
                    tofile=str(args.report),
                )
            ),
            end="",
        )
        raise SystemExit(
            "uv's scoped licence exceptions changed: make a policy ruling "
            f"before updating {args.exceptions}"
        )

    uv_versions = {package.version for package in packages if package.name == "uv"}
    if uv_versions != {args.expected_uv_version}:
        found = ", ".join(sorted(uv_versions)) or "none"
        raise SystemExit(
            f"{args.report} describes uv {found}; expected {args.expected_uv_version}"
        )


if __name__ == "__main__":
    main()
