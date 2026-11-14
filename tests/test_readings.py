"""Readings are compared with normal ranges in code. These are the rules a diagnosis rests on."""

import pytest

from app.engine.readings import Reading, ReadingError, compare, normalise_digits, parse_reading
from app.kb.schemas import NormalRange

VALVE = NormalRange(op="between", min=7.14, max=7.90, unit="ohm")
CONTINUITY = NormalRange(op="lt", max=1, unit="ohm")
NO_SHORT = NormalRange(op="gt", min=1, unit="Mohm")


@pytest.mark.parametrize("reading, normal, verdict", [
    (Reading(15, "ohm"), VALVE, "abnormal"),
    (Reading(7.5, "ohm"), VALVE, "normal"),
    (Reading(7.14, "ohm"), VALVE, "normal"),        # the limits themselves are normal
    (Reading(7.90, "ohm"), VALVE, "normal"),
    (Reading(7.91, "ohm"), VALVE, "abnormal"),
    (Reading(0.0075, "kohm"), VALVE, "normal"),     # same value in another unit
    (Reading(7.5, None), VALVE, "normal"),          # a bare number is in the unit the step asked for
    (Reading(0.3, "ohm"), CONTINUITY, "normal"),
    (Reading(1, "ohm"), CONTINUITY, "abnormal"),    # "less than 1" excludes 1
    (Reading(50, "ohm"), CONTINUITY, "abnormal"),
    (Reading(200, "ohm"), NO_SHORT, "abnormal"),    # 200 ohm is far below 1 Mohm
    (Reading(5, "megaohm"), NO_SHORT, "normal"),
    (Reading(1.2, "M\u03a9"), NO_SHORT, "normal"),
    (Reading(1500, "kohm"), NO_SHORT, "normal"),
    (Reading(over_limit=True), NO_SHORT, "normal"),     # OL = above the meter's range
    (Reading(over_limit=True), CONTINUITY, "abnormal"),  # OL where continuity is expected = open wire
])
def test_compare(reading, normal, verdict):
    assert compare(reading, normal) == verdict


def test_an_ambiguous_unit_is_a_question_not_a_guess():
    with pytest.raises(ReadingError) as err:
        compare(Reading(5, "mohm"), NO_SHORT)  # milli or mega? a factor of a billion apart
    assert "milliohm or megaohm" in err.value.question


def test_wrong_quantity_and_unknown_unit_are_refused():
    with pytest.raises(ReadingError, match="expected resistance, got voltage"):
        compare(Reading(12, "V"), VALVE)
    with pytest.raises(ReadingError, match="unknown unit"):
        compare(Reading(12, "bananas"), VALVE)
    with pytest.raises(ReadingError, match="OL on a non-resistance"):
        compare(Reading(over_limit=True), NormalRange(op="between", min=11, max=14, unit="V"))


@pytest.mark.parametrize("text, expected", [
    ("15 ohm", Reading(15.0, "ohm")),
    ("15ohm", Reading(15.0, "ohm")),
    ("\u0661\u0665 \u0623\u0648\u0645", Reading(15.0, "\u0623\u0648\u0645")),             # Arabic-Indic digits
    ("\u06f1\u06f5 \u0627\u0648\u06c1\u0645", Reading(15.0, "\u0627\u0648\u06c1\u0645")),  # Urdu digits
    ("\u0667.\u0665 \u0627\u0648\u0645", Reading(7.5, "\u0627\u0648\u0645")),             # unit without the hamza
    ("7\u066b5", Reading(7.5, None)),                                                    # Arabic decimal separator
    ("0.3", Reading(0.3, None)),
    ("OL", Reading(over_limit=True)),
    ("o.l.", Reading(over_limit=True)),
])
def test_parse_reading(text, expected):
    assert parse_reading(text) == expected


def test_parsed_arabic_and_urdu_readings_compare_like_english_ones():
    for text in ("15 ohm", "\u0661\u0665 \u0623\u0648\u0645", "\u06f1\u06f5 \u0627\u0648\u06c1\u0645"):
        assert compare(parse_reading(text), VALVE) == "abnormal"


def test_sentences_are_left_to_the_intake_model():
    assert parse_reading("I'm getting fifteen ohms") is None
    assert parse_reading("it reads about 15 ohm I think") is None


def test_digits_are_normalised_one_way_only():
    assert normalise_digits("\u0661\u0665 and \u06f1\u06f5") == "15 and 15"
    assert normalise_digits("P0010 pin 64") == "P0010 pin 64"
