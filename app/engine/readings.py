"""A mechanic's reading, compared with a normal range. In code, never by the model.

    compare(Reading(15, "ohm"), NormalRange(op="between", min=7.14, max=7.90, unit="ohm"))  ->  "abnormal"
    compare(Reading(200, "ohm"), NormalRange(op="gt", min=1, unit="Mohm"))                   ->  "abnormal"
    compare(Reading(over_limit=True), NormalRange(op="gt", min=1, unit="Mohm"))              ->  "normal"

Rules:
- Units are converted before comparing (200 ohm against "more than 1 Mohm").
- "OL" on a meter means the value is above the meter's range. It is treated as infinitely
  high for resistance, and refused for any other quantity.
- An ambiguous unit is an error, not a guess. Lower-case "mohm" could be milli or mega, and
  the two differ by a factor of a billion, so the assistant must ask.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Literal

from app.kb.schemas import NormalRange

Verdict = Literal["normal", "abnormal"]


class ReadingError(ValueError):
    """The reading cannot be compared as given. `question` is what to ask the mechanic."""

    def __init__(self, message: str, question: str) -> None:
        super().__init__(message)
        self.question = question


# unit spelling -> (quantity, factor to the base unit). Case matters: "M" is mega, "m" is milli.
_OMEGA = ("\u03a9", "\u2126")  # Greek capital omega, and the dedicated ohm sign
_UNITS: dict[str, tuple[str, float]] = {
    "ohm": ("resistance", 1.0), "ohms": ("resistance", 1.0),
    "kohm": ("resistance", 1e3), "kiloohm": ("resistance", 1e3),
    "Mohm": ("resistance", 1e6), "megaohm": ("resistance", 1e6), "megohm": ("resistance", 1e6),
    "milliohm": ("resistance", 1e-3),
    "V": ("voltage", 1.0), "v": ("voltage", 1.0), "volt": ("voltage", 1.0), "volts": ("voltage", 1.0),
    "mV": ("voltage", 1e-3), "mv": ("voltage", 1e-3), "millivolt": ("voltage", 1e-3),
    "A": ("current", 1.0), "amp": ("current", 1.0), "amps": ("current", 1.0),
    "mA": ("current", 1e-3), "ma": ("current", 1e-3),
    # as mechanics write them in Arabic and Urdu
    "\u0623\u0648\u0645": ("resistance", 1.0),  # أوم
    "\u0627\u0648\u0645": ("resistance", 1.0),  # اوم (written without the hamza)
    "\u0627\u0648\u06c1\u0645": ("resistance", 1.0),  # اوہم (Urdu)
    "\u0641\u0648\u0644\u062a": ("voltage", 1.0),  # فولت
    "\u0648\u0648\u0644\u0679": ("voltage", 1.0),  # وولٹ (Urdu)
}
for _omega in _OMEGA:
    _UNITS[_omega] = ("resistance", 1.0)
    _UNITS["k" + _omega] = ("resistance", 1e3)
    _UNITS["M" + _omega] = ("resistance", 1e6)
    _UNITS["m" + _omega] = ("resistance", 1e-3)
_AMBIGUOUS = {"mohm", "mohms", "m"}

# Arabic-Indic (٠-٩) and Extended Arabic-Indic (۰-۹) digits, and the Arabic decimal separator.
_DIGITS = str.maketrans(
    "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669"
    "\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9\u066b",
    "01234567890123456789.")
_OVER_LIMIT = re.compile(r"^\s*(o\.?l\.?|over\s*(limit|range|load)|infinite|infinity|\u221e)\s*$", re.I)
_NUMBER_UNIT = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*([^\d\s][^\s]*(?:\s*[^\d\s]+)?)?\s*$")


@dataclass(frozen=True)
class Reading:
    value: float | None = None
    unit: str | None = None
    over_limit: bool = False  # the meter showed OL


def normalise_digits(text: str) -> str:
    """١٥ and ۱۵ become 15. Numbers are never translated the other way."""
    return text.translate(_DIGITS)


def _unit(unit: str) -> tuple[str, float]:
    key = unit.strip().replace(" ", "")
    if key in _UNITS:
        return _UNITS[key]
    if key.lower() in _AMBIGUOUS:
        raise ReadingError(f"ambiguous unit {unit!r}", "Is that milliohm or megaohm?")
    if key.lower() in _UNITS:  # "OHM", "Volt"; safe because the ambiguous spellings were handled above
        return _UNITS[key.lower()]
    raise ReadingError(f"unknown unit {unit!r}", "Which unit is that reading in?")


def parse_reading(text: str) -> Reading | None:
    """A bare reading such as "15 ohm", "١٥ أوم", "0.3", "OL". Returns None for anything else.

    Sentences ("I'm getting fifteen ohms") are the intake model's job; its structured answer
    is then checked with this same module.
    """
    text = normalise_digits(text)
    if _OVER_LIMIT.match(text):
        return Reading(over_limit=True)
    m = _NUMBER_UNIT.match(text)
    if not m:
        return None
    return Reading(value=float(m.group(1)), unit=(m.group(2) or None))


_ANY_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])|\d")
_NUMBER_THEN_WORD = re.compile(r"(?<![\w.])(-?\d+(?:\.\d+)?)\s*([^\d\s.,،؛;:!?()]+)")
_OL_WORD = re.compile(r"(?<!\w)OL(?!\w)")


def reading_in_sentence(text: str) -> Reading | None:
    """The reading in a sentence such as "طلعت القراءة ١٥ أوم" or "it reads 0.4 ohm now".

    Taken only when it cannot be misread: the message holds exactly one number, and a known unit
    follows it ("15 ohm"); or it holds no number and says OL in capitals. Anything else (two numbers,
    a number with no unit, a unit nobody uses) is left to the intake model, whose value must then be
    written in the message.
    """
    text = normalise_digits(text)
    numbers = _ANY_NUMBER.findall(text)
    if not numbers:
        return Reading(over_limit=True) if _OL_WORD.search(text) else None
    if len(numbers) != 1:
        return None
    m = _NUMBER_THEN_WORD.search(text)
    if not m:
        return None
    try:
        _unit(m.group(2))
    except ReadingError:
        return None
    return Reading(value=float(m.group(1)), unit=m.group(2))


def compare(reading: Reading, normal: NormalRange) -> Verdict:
    quantity, factor = _unit(normal.unit)
    if reading.over_limit:
        if quantity != "resistance":
            raise ReadingError("OL on a non-resistance check", "What value did the meter show?")
        value = math.inf
    else:
        if reading.value is None:
            raise ReadingError("no value", "What value did the meter show?")
        if reading.unit is None:
            value = reading.value * factor  # a bare number is taken in the unit the step asked for
        else:
            got_quantity, got_factor = _unit(reading.unit)
            if got_quantity != quantity:
                raise ReadingError(f"expected {quantity}, got {got_quantity}",
                                   f"This step measures {quantity}. What is the {quantity} reading?")
            value = reading.value * got_factor
    low = normal.min * factor if normal.min is not None else None
    high = normal.max * factor if normal.max is not None else None
    ok = {
        "between": lambda: low <= value <= high,
        "lt": lambda: value < high,
        "le": lambda: value <= high,
        "gt": lambda: value > low,
        "ge": lambda: value >= low,
    }[normal.op]()
    return "normal" if ok else "abnormal"
