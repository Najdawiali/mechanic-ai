"""Keys for exact matching: the same thing written differently matches; different things never do."""

import pytest

from app.kb.keys import code_key, code_stem, norm_key


@pytest.mark.parametrize(
    ("a", "b"),
    [("ID.4", "id4"), ("Qin Plus DM-i", "qin plus dmi"), ("Model 3", "MODEL3"), ("L15CA", "l15ca"), ("Sonata Hybrid", "sonata-hybrid")],
)
def test_the_same_name_written_differently_has_one_key(a, b):
    assert norm_key(a) == norm_key(b)


def test_empty_names_have_no_key():
    assert norm_key(None) is None and norm_key("  ") is None and norm_key("-") is None


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("TAS_a211_noRideHeightCalib", "tas a211 no ride height calib"),
        ("TAS_a211_noRideHeightCalib", "TAS_A211"),
        ("BMS_A123_SW_Internal_Isolation", "bms_a123"),
        ("PCS_A027_SCHG", "pcs-a027"),
        ("VCFRONT_a490_coolantPumpsNotIdentified", "VCFRONT_a490"),
        ("RCM2_A009", "rcm2 a009"),
        ("P0010", " p0010 "),
        ("P2B5313", "p2b5313"),
    ],
)
def test_the_same_code_written_differently_matches(a, b):
    assert code_stem(a) == code_stem(b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("P0010", "P0011"),
        ("P0C4400", "P0C4411"),  # same base code, different failure type: a different fault
        ("P0C4400", "P0C44"),  # a 7-character code never matches its 5-character base
        ("P199000", "P1990"),
        ("TAS_a211", "TAS_a212"),
        ("TAS_a211", "TAS_w211"),
        ("TAS_a211", "RCM_a211"),  # same number in another module
        ("GTW_W151_HCM3R", "GTW_w139"),
        ("C119F00", "B119F00"),
    ],
)
def test_different_codes_never_match(a, b):
    assert code_stem(a) != code_stem(b)


def test_a_code_is_never_shortened_unless_it_is_an_alert_name():
    assert code_stem("P0C4400") == code_key("P0C4400") == "P0C4400"
    assert code_stem("W03-9") == "W039"
    assert code_stem("TAS_a211_noRideHeightCalib") == "TASA211"


def test_text_without_letters_or_digits_is_not_a_code():
    with pytest.raises(ValueError):
        code_key(" - ")
