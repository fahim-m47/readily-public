"""Misaki's number hook is local and permissive, not the LGPL distribution."""

from pathlib import Path

import pytest

import num2words

LOCKFILE = Path(__file__).resolve().parents[1] / "uv.lock"


def test_number_words_compat_is_shipped_by_readily():
    module = Path(num2words.__file__).resolve()

    assert module.parts[-3:] == ("src", "num2words", "__init__.py")


def test_the_lgpl_distribution_is_absent_from_what_ci_installs():
    """The lockfile, not the interpreter, is the license boundary: `uv sync
    --locked` installs exactly what it names, so a resolver that ever pulled
    the LGPL num2words in behind Misaki would show up here (ADR 0007)."""
    packages = [
        line.removeprefix("name = ").strip('"\n')
        for line in LOCKFILE.read_text().splitlines()
        if line.startswith("name = ")
    ]

    assert "misaki" in packages, "the lockfile should still name Misaki itself"
    assert "num2words" not in packages


@pytest.mark.parametrize(
    ("value", "mode", "expected"),
    [
        (0, "cardinal", "zero"),
        (42, "cardinal", "forty-two"),
        (2026, "cardinal", "two thousand and twenty-six"),
        (21, "ordinal", "twenty-first"),
        (-3, "cardinal", "minus three"),
        # Misaki reads any bare 4-digit token as a year (misaki/en.py).
        (1984, "year", "nineteen eighty-four"),
        (1900, "year", "nineteen hundred"),
        (1907, "year", "nineteen oh-seven"),
        (2000, "year", "two thousand"),
        (2007, "year", "two thousand and seven"),
        (2026, "year", "twenty twenty-six"),
    ],
)
def test_number_words_covers_the_modes_misaki_calls(value, mode, expected):
    assert num2words.num2words(value, to=mode) == expected
