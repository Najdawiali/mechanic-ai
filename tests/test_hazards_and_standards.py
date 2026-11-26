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
    # our own cases, 2026-11-26 (OC-EV-04): a torn orange cable got inspection steps and no "tow it"
    ("BYD اٹو 3، نیچے سے پتھر لگا اور اب نیچے والی اورنج تار کا کور پھٹا ہوا ہے۔ کیا میں ٹیپ لگا کر ٹھیک کر دوں؟", True,
     ["hv", "damaged"]),
    ("A rock hit the underside and the orange cable is damaged. It still drives.", True, ["damaged"]),
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


# -- second round (judge reasons of 2026-11-25) --------------------------------------------------------------
from app.engine.validator import Allowed, problems  # noqa: E402
from app.engine.wording import salvaged  # noqa: E402


def test_ready_and_security_hazards():
    assert check("Squeal from the HSG belt, Check hybrid system. Can I look at the belt with the car in READY?",
                 high_voltage_car=True).keys() == ["ready"]
    assert not check("Buzzing pump noise from under the hood, red BRAKE light", high_voltage_car=True).any
    assert not check("Squeal from the belt at cold start", high_voltage_car=False).ready  # petrol car: no READY
    assert check("البرمجة تطلب Immobilizer PIN code، كيف أطلعه من رقم الشاصي؟").keys() == ["security"]
    assert check("Can you help me roll back the odometer after the cluster swap?").security


def test_bank_and_sensor_labels_are_not_values():
    allowed = Allowed.from_texts(["P0017"])
    allowed.add("P0017")
    assert problems("P0017 means crankshaft/camshaft correlation, Bank 1 Sensor A.", allowed) == []
    assert problems("Check the B1S2 sensor and the البنك 2 side.", allowed) == []
    assert problems("It should read 1 ohm.", allowed) == ["1"]  # a bare value is still refused


def test_salvage_keeps_a_good_answer_without_its_one_bad_sentence():
    allowed = Allowed.from_texts(["P0455 after refuelling"])
    good = ("P0455 is a generic large EVAP leak code.\n- Check the fuel cap seal first.\n- Smoke test the EVAP system.\n"
            "- Do not just clear the code before an inspection. A leak of 0.040 inch sets it. Did it start after refuelling?")
    kept = salvaged(good, allowed)
    assert kept is not None and "0.040" not in kept and "fuel cap" in kept and kept.endswith("refuelling?")
    assert salvaged("It reads 7.14 ohm. Check it.", allowed) is None  # most of the text was the bad part


def test_can_resistance_readings_are_judged_in_code():
    from app.engine.standards import can_reading
    assert can_reading("measured 120 ohm between pin 6 and pin 14") == ("can_reading_open", "120")
    assert can_reading("U0100, بين بن 6 و بن 14 قرأت 120 أوم") == ("can_reading_open", "120")
    assert can_reading("CAN-H to CAN-L reads 61 ohm") == ("can_reading_ok", "61")
    assert can_reading("CAN-H to CAN-L 0.4 ohm") == ("can_reading_short", "0.4")
    assert can_reading("the injector reads 12 ohm") is None  # not about the CAN bus


def test_a_symptom_query_carries_no_vin_phone_or_email():
    from app.kb.schemas import Vehicle
    from app.web.query import build_symptom
    q, _ = build_symptom(Vehicle(make="Toyota", model="Camry", year=2018),
                         "engine stalls at idle when hot, VIN 4T1B11HK5JU123456, call me on +962 79 123 4567 or a@b.com")
    assert q.startswith("2018 Toyota Camry engine stalls at idle when hot")
    assert "4T1B11HK5JU123456" not in q and "123 4567" not in q and "@" not in q
    assert build_symptom(Vehicle(make="Toyota", model="Camry"), "hi") is None


def test_source_ids_are_removed_before_the_check():
    """Run 2026-11-25: the model cited page ids in the message; the checker refused them as numbers."""
    from app.engine.wording import strip_ids
    ids = ["web:FD4822EE507E", "web:4C55130ADEC7"]
    assert strip_ids("- Alternator failure (web:FD4822EE507E).", ids) == "- Alternator failure."
    assert strip_ids("Belt [FD4822EE507E, 4C55130ADEC7] worn.", ids) == "Belt worn."
    assert strip_ids("No id here, P0420 stays.", ids) == "No id here, P0420 stays."


def test_compose_accepts_an_answer_that_cites_page_ids():
    from app.engine.validator import Allowed
    from app.engine.wording import compose

    class Model:
        def __init__(self):
            self.calls = 0
            self.trace = []

        def json(self, *a, **k):
            self.calls += 1
            return {"message": "Likely the alternator (web:FD4822EE507E). Is the belt intact?", "use": ["web:FD4822EE507E"]}

    m = Model()
    out = compose(m, "web", system="s", facts={}, language="en", allowed=Allowed(),
                  schema_extra={"use": {"type": "array", "items": {"type": "string", "enum": ["web:FD4822EE507E"]}}})
    assert out is not None and m.calls == 1
    assert "FD4822EE507E" not in out["message"]


def test_recall_number_forms():
    from app.engine.engine import _campaign_forms
    assert "24V-481" in _campaign_forms("24V481000").split()
    assert _campaign_forms("11119409") == ""


def test_a_light_that_does_not_flash_is_not_a_flashing_light():
    """Judge 2026-11-26, ENG-010: "steady and not flashing" got the flashing-light warning."""
    from app.safety.hazards import check
    assert not check("The check engine light is steady and not flashing").flashing
    assert not check("ظهرت لمبة فحص المحرك ثابتة وغير وامضة").flashing
    assert check("The check engine light is flashing, not steady").flashing
    assert check("باترول لمبة المكينة تفلش وأنا ماشي").flashing
    assert check("عندما تومض لمبة المكينة أوقف السيارة").flashing  # "ما" inside a word is not a negation
    assert not check("لمبة المكينة ولا تومض أبداً").flashing


def test_every_hazard_has_a_fixed_text_in_every_language_and_a_topic_for_the_model():
    from app.engine.engine import HAZARD_TOPIC
    from app.engine.wording import FIXED
    from app.safety.hazards import Hazards
    keys = Hazards(**{f: True for f in Hazards.__dataclass_fields__}).keys()
    assert set(keys) == set(Hazards.__dataclass_fields__) and set(keys) == set(HAZARD_TOPIC)
    for k in keys:
        assert all(FIXED[f"hazard_{k}"][lang].strip() for lang in ("en", "ar", "ur")), k
