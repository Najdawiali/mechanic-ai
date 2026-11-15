from app.engine.validator import Allowed, problems

FACTS = Allowed.from_texts([
    "Step 1 of 4. VTC oil control solenoid valve A connector (2P) pin 1 to pin 2. Normal: 7.14 - 7.90 ohm at room temperature.",
    "Fuse 5-5 40A at UNDER-HOOD FUSE/RELAY BOX pin A23; ground G020; connector C224 pin 8; code P0010",
])


def test_facts_from_the_sources_pass_in_any_language_and_in_value_form():
    assert problems("Measure between pin 1 and pin 2 of the valve connector (2P): 7.14 to 7.9 ohm.", FACTS) == []
    assert problems("قِس بين البن 1 والبن 2. القراءة الطبيعية 7.14 إلى 7.90 أوم. الكود P0010.", FACTS) == []
    assert problems("Check fuse 5-5 (40A), connector C224 pin 8 and ground G020.", FACTS) == []


def test_a_number_pin_or_code_with_no_source_is_caught():
    assert problems("It should read about 12.5 V at pin 64.", FACTS) == ["12.5", "64"]
    assert problems("This is often P0011 as well.", FACTS) == ["P0011"]
    assert problems("Look at ground G021.", FACTS) == ["G021"]


def test_numbers_are_never_translated():
    assert problems("القراءة ٧٫١٤ أوم", FACTS) == ["Arabic-Indic digits: numbers must be written with Latin digits"]


def test_list_numbering_is_layout_and_a_long_code_allows_its_short_form():
    allowed = Allowed.from_texts(["VCFRONT_a490_coolantPumpsNotIdentified"])
    assert problems("First checks:\n1. Check the fuse.\n2) Check the ground.\n(3) Check the connector.", allowed) == []
    assert problems("VCFRONT_a490 means the pumps were not identified.", allowed) == []
    assert problems("Check pin 12.", allowed) == ["12"]
