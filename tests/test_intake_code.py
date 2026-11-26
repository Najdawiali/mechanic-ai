"""What intake decides in code, with no model: language, workshop words, emergencies."""

import json
from pathlib import Path

import pytest

from app.lang.glossary import find_terms
from app.lang.script import detect
from app.safety import emergency

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(("text", "language"), [
    ("أكورا انتيجرا 2025 محرك 1.5 تيربو، طالع عندي كود P0010", "ar"),
    ("عندي byd qin plus 2024 dmi hybrid فيها هاي الاعطال شو اعمل", "ar"),
    ("تسلا موديل Y 2022 الشحن بوقف شوي وبرجع، وطالع PCS_A027_SCHG", "ar"),
    ("گاڑی 2025 اکیورا انٹیگرا 1.5 ٹربو ہے، سکینر پر P0010 کوڈ آ رہا ہے۔", "ur"),
    ("ریڈنگ ۱۵ اوہم آئی ہے۔", "ur"),
    ("٧.٥ اوم", "ar"),
    ("I'm getting fifteen ohms", "en"),
    ("VW ID.4 2021. Codes P0C4400, P0CE300 and B148754.", "en"),
    ("0.4", None), ("٧٫٥", None),
    ("7.5 ohm", None), ("0.3 Ohms", None), ("12.4 V", None), ("OL", None), ("ok", None),  # units and yes/no: keep the session's
    ("12.4 volts on the fuse", "en"),
])
def test_language_is_read_from_the_letters(text, language):
    assert detect(text) == language


def test_workshop_words_are_found_with_their_prefixes_and_longest_first():
    found = {t.written: t.english for t in find_terms("غيرنا البواجي والدبرياج وفحصنا رقم الشاصي")}
    assert found == {"البواجي": "spark plugs", "والدبرياج": "clutch", "رقم الشاصي": "VIN"}


@pytest.mark.parametrize(("text", "kind"), [
    ("هيونداي سوناتا هايبرد 2020 بعد حادث عليها P199000، وهسا في دخان وريحة شياط طالعة من جهة بطارية الهاي فولتج", "vehicle"),
    ("there is smoke coming from under the rear seat", "vehicle"),
    ("I can smell burning plastic near the fuse box", "vehicle"),
    ("the HV battery is swollen", "vehicle"),
    ("sparks at the 12V terminal when I connect it", "vehicle"),
    ("the harness melted at the firewall", "vehicle"),
    ("البطارية منفوخة", "vehicle"), ("في ريحة احتراق من تحت الطبلون", "vehicle"), ("الشريط ساح عند الكمبيوتر", "vehicle"), ("طالع شرار من الفيوز", "vehicle"),
    ("my colleague got a shock from the orange cable", "person"), ("زميلي كهربه الكابل", "person"),
    ("بیٹری سے دھواں نکل رہا ہے", "vehicle"), ("جلنے کی بو آ رہی ہے", "vehicle"), ("مکینک کو کرنٹ لگ گیا", "person"),
])
def test_danger_now_is_an_emergency(text, kind):
    found = emergency.check(text)
    assert found is not None and found.kind == kind


@pytest.mark.parametrize("text", [
    "no smoke, no smell, just the warning light", "ما في دخان ولا ريحة", "لا يوجد دخان", "دھواں نہیں ہے",
    "replaced the spark plugs and coils", "did a smoke test on the EVAP system", "P0300 random misfire",
    "the fuse was blown, replaced it", "غيرنا البواجي", "ما تم تغيير مضخة المحرك الاحتراق الداخلي", "go ahead with the next step", "آگے کیا کرنا ہے؟",
])
def test_workshop_talk_and_negations_are_not_emergencies(text):
    assert emergency.check(text) is None


def test_only_the_emergency_scenario_trips_the_check_among_all_scenario_messages():
    fired = []
    for path in sorted((REPO / "eval/scenarios").glob("*.json")):
        scenario = json.loads(path.read_text(encoding="utf-8"))
        for turn in scenario["turns"]:
            if emergency.check(turn["mechanic"]):
                fired.append(scenario["group"])
    assert fired == ["emergency"]


def test_the_safety_texts_exist_in_every_language_carry_the_number_and_are_marked_as_drafts():
    for kind in ("vehicle", "person"):
        for language in ("en", "ar", "ur"):
            for hv in (True, False):
                assert "911" in emergency.text(kind, language, high_voltage=hv, number="911")
    assert emergency.TEXTS_APPROVED is False
