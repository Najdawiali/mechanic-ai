"""Hazards found in code, and the general standard values (comparison run 2026-11-24). No database, no model."""

import pytest

from app.engine.engine import code_kind, code_numbers
from app.engine.standards import STANDARDS, relevant
from app.safety.hazards import check


@pytest.mark.parametrize("text,hv_car,expected", [
    ("2022 Kona Electric, isolation fault, I have a multimeter, can I test the orange cables myself?", True, ["hv", "isolation"]),
    ("كونا كهربائية، تحذير عن عزل الجهد العالي، هل أقدر أفحص الكيابل البرتقالية بنفسي؟", True, ["hv", "isolation"]),
    ("نزلت تحت وشفت الكيبل البرتقالي عليه طين، أبي أفكه وأنظفه ببخاخ كونتاكت", True, ["hv"]),
    ("ظهر تحذير عن عطل في نظام الجهد العالي/العزل، هل أفتح الأغطية؟", True, ["hv", "isolation"]),
    ("Can I open the hybrid battery and swap the bad cells myself?", True, ["hv"]),
    ("SRS light after I removed the seat, can I check the airbag connector with a multimeter?", False, ["srs"]),
    ("الزينون صار يرمش، أبي أفحص الـ ballast بالملتيميتر", False, ["hid"]),
    ("Codes P0300 and P0302, once the light was flashing, can I drive?", False, ["flashing"]),
    ("السيارة ترجف ولمبة المكينة تولع وتطفي (تومض)", False, ["flashing"]),
])
def test_hazards_are_found_in_three_languages(text, hv_car, expected):
    assert check(text, high_voltage_car=hv_car).keys() == expected


@pytest.mark.parametrize("text,hv_car", [
    ("2015 Camry, P0171, can I clean the MAF sensor myself?", False),
    ("Battery drains overnight, how do I test the parasitic draw?", False),
    ("Can bus codes U0100 after a jump start", False),
    ("Prius A/C blows warm, the compressor clutch does not engage", True),
    ("smoke test found a leak at the intake", False),
])
def test_ordinary_questions_raise_no_hazard(text, hv_car):
    assert not check(text, high_voltage_car=hv_car).any


def test_an_isolation_code_is_a_hazard_without_the_word():
    assert check("Check Hybrid System after the A/C job", high_voltage_car=True, codes=["P0AA6"]).isolation


def test_code_kinds_follow_sae_j2012():
    assert [code_kind(c) for c in ("P0011", "P2463", "P0A93", "U0100", "P1456", "P3000", "P3400", "B1342", "C0035",
                                   "TAS_a211")] == ["generic", "generic", "generic", "generic", "manufacturer-specific",
                                                    "manufacturer-specific", "generic", "manufacturer-specific",
                                                    "generic", "manufacturer-specific"]


def test_numbers_named_by_a_code_are_allowed():
    assert code_numbers("P0304") == {4.0} and code_numbers("P0312") == {12.0} and code_numbers("P0300") == set()
    assert code_numbers("P0174") == {2.0} and code_numbers("P0011") == set()


def test_standard_values_only_on_their_topic_and_drafts_only_where_drafts_are_served():
    keys = [s.key for s in relevant("measured 120 ohm between pin 6 and pin 14 on the OBD port", [], drafts=True)]
    assert {"can_termination", "dlc_pins"} <= set(keys)
    assert relevant("can I drive it to Dubai?", ["P0300"], drafts=True) == []  # "can" is not the CAN bus
    assert [s.key for s in relevant("", ["U0100"], drafts=True)][:2] == ["can_termination", "can_voltages"]
    assert relevant("battery light and 12.2 V running", [], drafts=False) == []  # nothing approved yet
    assert not any(s.approved for s in STANDARDS)  # Nasoh approves each entry
