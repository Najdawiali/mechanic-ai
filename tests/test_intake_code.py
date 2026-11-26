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


@pytest.mark.parametrize("text", [
    # our own cases, 2026-11-26: each got the fire text and no diagnosis
    "ایسوزو ڈی میکس ڈیزل 2020، صبح ٹھنڈ میں اسٹارٹ ہونے میں بہت ٹائم لگتا ہے اور سفید دھواں نکلتا ہے",
    "نسان نوارا ڈیزل 2018، P0401 کوڈ اور گاڑی میں کالا دھواں زیادہ ہے۔ لوگ کہتے ہیں EGR بند کر دو۔",
    "ميتسوبيشي باجيرو 2017، المحرك يستهلك زيتاً كثيراً، ويخرج دخان أزرق عند التشغيل صباحاً.",
    "برادو 2016 دواسة البريك صارت تنزل لين تحت، ولمبة البريك الحمرا ولعت. أبي أوصل البيت بس 20 كيلو، أقدر؟",
    # venorexai INC-006: "the weather is fire" = very hot
    "المكيف ما يبرد زين والجو نار، وش السالفة؟",
    "black smoke from the exhaust under load", "a lot of white smoke at cold start, then it clears",
    "يطلع دخان من الشكمان", "ولعت لمبة المكينة", "اللمبة مولعة من أمس",
    "البوجي يعطي نار ضعيفة", "سويت فحص الشرارة", "شرارة البوجي ضعيفة", "مشكلة في نظام الاشتعال", "مفتاح الاشتعال ما يرجع",
    "it fires up fine in the morning", "the engine won't fire after the battery change",
])
def test_workshop_meanings_of_a_trigger_word_are_not_emergencies(text):
    assert emergency.check(text) is None


@pytest.mark.parametrize("text", [
    # words the list never had (an independent set of 80 sentences, 2026-11-26, found them missing); new sentences here
    "there are flames under the steering column", "ألسنة لهب تحت الطبلون", "شعلہ نظر آ رہا ہے بونٹ کے نیچے",
    "زميلي تعرض لصدمة كهربائية من الكيبل البرتقالي", "العامل تكهرب من سلك الهايبرد", "the loom insulation is softening",
    "the 12V battery we are charging has started swelling on both sides", "جوانب البطارية قاعدة تنتفخ",
    "فيشة المروحة قاعدة تسيح", "عازل السلك يذوب", "a burning-plastic smell from the glovebox",
    "رائحة البلاستيك المحترق من خلف الطبلون", "تار کا کور دیکھتے دیکھتے پگھل رہا ہے", "جلے ربڑ کی بو آ رہی ہے",
    "the battery case is cracked and the battery is leaking acid",
])
def test_the_danger_words_cover_flames_shocks_melting_and_batteries(text):
    assert emergency.check(text) is not None


@pytest.mark.parametrize("text", ["did a smoke test, the machine's smoke came out at the intake boot",
                                  "سويت اختبار الدخان وطلع دخان الجهاز من الثروتل", "لمبة المكينة ولعت بعد تبديل الفلتر",
                                  "فاحص الشرارة يبين نبضة على كل السلندرات"])
def test_smoke_test_machines_engine_lights_and_spark_testers_are_not_emergencies(text):
    assert emergency.check(text) is None


@pytest.mark.parametrize("text", [
    "white smoke from under the hood and a sweet smell", "دخان أبيض من الكبوت", "smoke from the dashboard vents",
    "السيارة ولعت", "اللمبة ولعت وبعدين السيارة ولعت", "طالع نار من المكينة", "the car caught fire",
    "black smoke coming from the battery", "کالا دھواں بیٹری سے نکل رہا ہے",
    # coloured smoke at the engine, under the car or at a wheel: possibly a fire, so it still stops the flow
    "دخان أسود من المكينة", "دخان أبيض كثيف تحت السيارة", "black smoke from the engine",
    "white smoke pouring from the engine compartment", "انجن سے کالا دھواں نکل رہا ہے", "گاڑی کے نیچے سے سفید دھواں",
    "blue smoke from under the car", "دخان رمادي من العجلة الأمامية", "grey smoke from the front wheel",
    "ولعت لمبة الحرارة والمكينة ولعت", "الكبوت ولعت", "السيارة مولعة", "الجو نار والسيارة طلع منها نار",
])
def test_the_same_words_still_stop_the_flow_when_they_describe_danger(text):
    assert emergency.check(text) is not None


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


def test_the_safety_check_script_reports_misses_and_false_alarms_without_a_model(tmp_path, capsys):
    from scripts.check_safety import main
    rows = [{"text": "there is a flame behind the headlight", "danger": True},
            {"text": "blue smoke from the exhaust at a cold start", "danger": False},
            {"text": "liquid seeping from a crack in the case", "danger": True}]  # the second layer's job
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    assert main([str(path)]) == 1  # a missed danger fails the check
    out = capsys.readouterr().out
    assert "dangers caught 1/2" in out and "false alarms 0/1" in out and "MISSED #3" in out


def test_the_script_counts_what_the_app_does_with_each_layer():
    """Ali's decision, 2026-11-26: a word-list alarm the model judges harmless warns, then answers; no model answer stops."""
    from scripts.check_safety import app_outcome
    assert [app_outcome(True, "none"), app_outcome(True, "vehicle"), app_outcome(True, None), app_outcome(True, "error: X"),
            app_outcome(False, "person"), app_outcome(False, "none")] == ["warn", "stop", "stop", "stop", "stop", "answer"]
