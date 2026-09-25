"""MIT compatibility surface for the number modes Misaki calls.

The third-party ``num2words`` distribution is LGPL and cannot be imported by
Readily. Misaki imports one function from that package to expand numeric Source
tokens before its own lexicon handles them; this module provides that narrow
surface without admitting the dependency.
"""

from decimal import Decimal

_SMALL = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_TENS = (
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
)
_SCALES = (
    (10**18, "quintillion"),
    (10**15, "quadrillion"),
    (10**12, "trillion"),
    (10**9, "billion"),
    (10**6, "million"),
    (10**3, "thousand"),
)
_ORDINALS = {
    "one": "first",
    "two": "second",
    "three": "third",
    "four": "fourth",
    "five": "fifth",
    "six": "sixth",
    "seven": "seventh",
    "eight": "eighth",
    "nine": "ninth",
    "ten": "tenth",
    "eleven": "eleventh",
    "twelve": "twelfth",
}


def _under_thousand(value: int) -> str:
    if value < 20:
        return _SMALL[value]
    if value < 100:
        tens, remainder = divmod(value, 10)
        return _TENS[tens] + (f"-{_SMALL[remainder]}" if remainder else "")
    hundreds, remainder = divmod(value, 100)
    words = f"{_SMALL[hundreds]} hundred"
    return f"{words} and {_under_thousand(remainder)}" if remainder else words


def _cardinal(value: int) -> str:
    if value < 0:
        return f"minus {_cardinal(-value)}"
    if value < 1000:
        return _under_thousand(value)
    for scale, name in _SCALES:
        if value >= scale:
            high, remainder = divmod(value, scale)
            words = f"{_cardinal(high)} {name}"
            if not remainder:
                return words
            joiner = " and " if remainder < 100 else " "
            return f"{words}{joiner}{_cardinal(remainder)}"
    raise ValueError("number is too large to narrate")


def _ordinal(value: int) -> str:
    cardinal = _cardinal(value)
    separator = "-" if "-" in cardinal.rsplit(" ", 1)[-1] else " "
    head, _, tail = cardinal.rpartition(separator)
    if tail in _ORDINALS:
        ending = _ORDINALS[tail]
    elif tail.endswith("y"):
        ending = f"{tail[:-1]}ieth"
    else:
        ending = f"{tail}th"
    return f"{head}{separator if head else ''}{ending}"


def _year(value: int) -> str:
    """Read a year in century pairs, matching num2words' English rules:
    1984 → "nineteen eighty-four", 1907 → "nineteen oh-seven", while round
    thousands and anything outside 4-ish digits fall back to the cardinal."""
    high, low = divmod(abs(value), 100)
    if high == 0 or (high % 10 == 0 and low < 10) or high >= 100:
        return _cardinal(value)
    if low == 0:
        return f"{_cardinal(high)} hundred"
    if low < 10:
        return f"{_cardinal(high)} oh-{_cardinal(low)}"
    return f"{_cardinal(high)} {_cardinal(low)}"


def num2words(number: int | float | Decimal, to: str = "cardinal") -> str:
    """Expand a number for Misaki's cardinal, ordinal, and year paths."""
    if to not in {"cardinal", "ordinal", "year"}:
        raise NotImplementedError(f"unsupported number mode: {to}")
    if isinstance(number, float) and not number.is_integer():
        integer, _, fractional = format(number, "f").rstrip("0").partition(".")
        words = _cardinal(int(integer))
        return f"{words} point {' '.join(_SMALL[int(digit)] for digit in fractional)}"
    value = int(number)
    if to == "ordinal":
        return _ordinal(value)
    return _year(value) if to == "year" else _cardinal(value)
