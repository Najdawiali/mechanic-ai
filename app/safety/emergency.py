"""The emergency check. It runs first on every message, in code, before any model or lookup.

A message that describes danger now (fire, smoke, a burning smell, sparks, a swollen or leaking
battery, melted wiring, a person who got an electric shock) stops the troubleshooting flow and gets
a fixed safety text, never a model-written one.

Two ways to get this wrong, and which one we accept:
  * missing a real emergency: not acceptable, so the triggers are broad;
  * stopping on a false alarm: acceptable. The mechanic reads the safety text and writes again;
    the next message is checked on its own and, with no trigger in it, the flow carries on.
A trigger preceded by a negation ("no smoke", "ما في دخان", "دھواں نہیں") does not fire, and some
workshop phrases that contain a trigger word are not emergencies ("spark plug", "smoke test",
"محرك الاحتراق الداخلي" = internal combustion engine: "احتراق" counts only as "a smell of burning").

Workshop meanings of a trigger word (`_workshop_meaning`; our own test cases, 2026-11-26: 4 of the 6 critical
failures were these false alarms, each answered with the fire text and no diagnosis):
  * exhaust smoke told by its colour or by the exhaust ("سفید دھواں" at a cold start, "دخان أزرق", "black smoke from
    the exhaust") is a symptom to diagnose, unless the same words place it at the hood, the dashboard, the wiring,
    the battery or a charger: those stay emergencies;
  * "ولعت" / "مولعة" right next to a light means the light came on ("ولمبة البريك الحمرا ولعت");
  * "الجو نار" is very hot weather; "البوجي يعطي نار" and "شرارة البوجي" are the ignition spark; "نظام الاشتعال",
    "مفتاح الاشتعال" are the ignition system and switch; "fire it up", "won't fire" are starting the engine.

A SECOND LAYER catches what the word list misses: the intake model, which reads every message anyway, also answers
"is something dangerous happening now?" (`Intake.danger`, app/engine/intake.py); a yes gets the same fixed text.
An independent set of 80 sentences written by another AI (2026-11-26) showed why: the word list alone missed 20 of
40 real dangers ("a flame", "لهب", "شعلہ", "صدمة كهربائية", "تكهرب", insulation "softening and dripping"...). The word
list stays first: it needs no model, so it also works when the model is down.

THE SAFETY TEXTS ARE DRAFTS. The client's expert must approve them (handoff, section 7.1). Until he
does, `TEXTS_APPROVED` is False and every emergency reply says so in its warnings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.lang.script import Language

Kind = Literal["vehicle", "person"]
TEXTS_APPROVED = False

_AR = "(?<!\\w)(?:و|ف|ب|ل|ك)?(?:ال)?"
_END = "(?!\\w)"


def _ar(*words: str) -> str:
    return _AR + "(?:" + "|".join(words) + ")" + _END


_BATTERY_AR = "(?:بطارية|بطاريه|البطارية|البطاريه|بطاريات)"
_WIRE_AR = "(?:سلك|شريط|ضفيرة|ضفيره|كيبل|كابل|اسلاك|شرايط)"

# (kind, pattern). English is matched without regard to case.
TRIGGERS: list[tuple[Kind, re.Pattern[str]]] = [(kind, re.compile(p, re.I)) for kind, p in [
    # English
    ("vehicle", r"\bsmok(?:e|es|ing|y)\b(?!\s*(?:test|machine|generator))"),
    ("vehicle", r"\b(?:fire|flames?|on fire|caught fire|burst into flames?)\b"),
    ("vehicle", r"\b(?:burn\w*|burnt|scorch\w*)[\s-]+(?:smell|odou?r|plastic|wires?|insulation)\b"),
    ("vehicle", r"\bsmell\w*\s+(?:of\s+|like\s+)?(?:burn\w*|burnt|plastic|scorch\w*)\b"),
    ("vehicle", r"\bspark(?:s|ing)\b(?!\s*plugs?)|\barcing\b"),
    ("vehicle", r"\b(?:swollen|swelling|bulging|puffed|puffy)\b.{0,60}\bbatter|\bbatter\w*\b.{0,60}\b(?:swollen|swelling|bulging|puffed|puffy)\b"),
    ("vehicle", r"\bbatter\w*\b.{0,30}\b(?:leak\w*|hiss\w*|venting)\b|\b(?:electrolyte|coolant)\s+leak\w*\s+(?:from|out of)\s+(?:the\s+)?(?:hv\s+|high.voltage\s+)?batter"),
    ("vehicle", r"\bthermal\s+runaway\b|\bmelt(?:ed|ing)\b|\b(?:insulation|wire|wiring|loom|harness|connector|plug)\b.{0,40}"
                r"\b(?:soften\w*|dripp\w*)\b"),
    ("person", r"\b(?:got|get|was|been|getting)\s+(?:an?\s+)?(?:electric\s+)?shock(?:ed)?\b|\belectrocut\w*\b"),
    # Arabic
    ("vehicle", _ar("دخان", "دخنة", "دخنه", "يدخن", "بدخن", "بتدخن", "تدخن", "دخّان")),
    ("vehicle", _ar("شياط", "حريق", "نار", "ولعت", "ولّعت", "اشتعلت", "اشتعال", "بتولع", "مولعة", "مولعه", "لهب", "لهبا", "لهبًا",
                    "ألسنة")),
    ("vehicle", "(?:ريحة|ريحه|رائحة|رائحه)\\s+(?:\\S+\\s+)?(?:ال)?(?:محروق|محروقة|محترق|محترقة|شياط|حريق|احتراق|بلاستيك)"),
    ("vehicle", _ar("شرار", "شرارة", "شراره", "شرر")),
    ("vehicle", _BATTERY_AR + ".{0,60}(?:انتفخت|تنتفخ|ينتفخ|منفوخة|منفوخه|منتفخة|منتفخه|نافخة|نافخه|بتسرب|بتسرّب|تسرب|بتنقط|بتنفس|بتصفر)"
                "|(?:انتفاخ|نفخ|تسريب|تسرب)\\s+(?:في\\s+|من\\s+)?" + _BATTERY_AR),
    ("vehicle", "(?:" + _WIRE_AR[3:-1] + "|عازل|فيشة|فيشه|جلدة|موصل)\\s+(?:\\S+\\s+){0,3}(?:قاعد\\s+|قاعدة\\s+)?"
                "(?:ذاب|ذايب|ساح|سايح|ساحت|ذابت|يذوب|تذوب|يسيح|تسيح|محروق|محروقة|محروقه|محترق)"),
    ("person", _ar("كهربني", "كهربه", "كهربته", "كهربتني", "صعقة", "صعقه", "انصعق", "صعقته", "مكهرب", "تكهرب", "تكهربت",
                   "انكهرب", "انكهربت") + "|صدمة\\s+كهربائية|صدمه\\s+كهربائيه"),
    # Urdu
    ("vehicle", "(?<!\\w)(?:دھواں|دھوئیں|دھویں)(?!\\w)"),
    ("vehicle", "(?<!\\w)(?:آگ|شعلہ|شعلے)(?!\\w)|جلنے\\s+کی\\s+(?:بو|بدبو)|جلی\\s+ہوئی\\s+بو|جلے\\s+(?:\\S+\\s+)?کی\\s+بو"),
    ("vehicle", "(?<!\\w)(?:چنگاری|چنگاریاں)(?!\\w)"),
    ("vehicle", "بیٹری.{0,60}(?:پھول|لیک|رس)|(?:پھولی|پھولی ہوئی)\\s+بیٹری"),
    ("vehicle", "(?:تار|تاریں)\\s+(?:\\S+\\s+){0,4}(?:پگھل|جل)"),
    ("person", "کرنٹ\\s+لگ|جھٹکا\\s+لگ"),
]]

_NEGATION_BEFORE = re.compile(
    r"(?:^|\s)(?:no|not|without|never|isn'?t|wasn'?t|there'?s no|"
    r"لا|ما|مافي|ما في|مش|مو|بدون|بلا|ليس|لايوجد|لا يوجد|ولا|"
    r"کوئی نہیں|بغیر)\s+(?:\S+\s+)?$", re.I)
_NEGATION_AFTER = re.compile(r"^\s*(?:\S+\s+)?(?:نہیں|نہ)(?!\w)")


# -- workshop meanings of trigger words (see the module docstring) ------------------------------------------
_W = "(?:و|ف|ب|ل|ك)?(?:ال)?"  # common Arabic prefixes
_SMOKE_WORD = re.compile(r"smok|دخان|دخن|دخّان|دھواں|دھوئیں|دھویں", re.I)
_COLOUR = re.compile(
    r"\b(?:white|blue|black|gr[ae]y|bluish|blackish|whitish)\b|"
    r"(?<!\w)(?:أبيض|ابيض|بيضاء|بيضا|أزرق|ازرق|زرقاء|زرقا|أسود|اسود|سوداء|سودا|رمادي|رصاصي)(?!\w)|"
    r"(?<!\w)(?:سفید|نیلا|نیلے|نیلی|کالا|کالے|کالی|سیاہ|سرمئی)(?!\w)", re.I)
_EXHAUST = re.compile(
    r"\bexhaust\b|tail\s*pipe|muffler|silencer|"
    f"(?<!\\w){_W}(?:شكمان|إشكمان|اشكمان|اكزوز|إكزوز|عادم)(?!\\w)|"
    r"(?<!\w)(?:ایگزاسٹ|اگزاسٹ|سائلنسر|سلنسر)(?!\w)", re.I)
# Smoke placed at the engine, under the car, at a wheel or brake, or in the cabin stays an emergency, coloured or not
# (black smoke from the engine compartment can be a fire): "from the engine", "من المكينة", "تحت السيارة", "انجن سے".
_DANGER_PLACE = re.compile(
    r"\b(?:hood|bonnet|engine\s+(?:bay|compartment|room)|dash\w*|cabin|interior|vents?|wir\w*|harness|batter\w*|"
    r"fuse\s*box|charg\w*|under\s+the\s+(?:car|vehicle|truck|seat|dash\w*)|"
    r"from\s+(?:the\s+|under\s+the\s+)?(?:engine|motor|front|wheels?|tyres?|tires?|brakes?|steering))\b|"
    r"(?:من|تحت|عند|جنب|داخل|فوق)\s+(?:ال)?(?:مكينة|مكينه|ماكينة|ماكينه|محرك|سيارة|سياره|عجلة|عجله|عجل|كفر|فرامل|بريك|"
    r"دركسون|مقود)(?!\w)|"
    r"(?<!\w)(?:انجن|بونٹ|گاڑی|ٹائر|پہیے|پہیہ|بریک)\s+(?:کے\s+)?(?:سے|نیچے|اندر)(?!\w)|"
    f"(?<!\\w){_W}(?:كبوت|كبّوت|بونيت|طبلون|تابلوه|مقصورة|فتحات|سلك|اسلاك|أسلاك|ضفيرة|شريط|شرايط|بطارية|بطاريه|"
    f"فيوز|فيوزات|شاحن|مكيف|كمبيوتر)(?!\\w)|"
    r"(?<!\w)(?:بونٹ|ڈیش|تار|تاریں|وائرنگ|بیٹری|فیوز|چارجر|کیبن)(?!\w)", re.I)
_LIT_WORD = re.compile(r"ولعت|ولّعت|بتولع|مولعة|مولعه|اشتعلت")
_LIGHT = re.compile(f"^{_W}(?:لمبة|لمبه|لمبات|ضوء|أضواء|اضواء|إشارة|اشارة|مؤشر|علامة|رسالة|شاشة|عداد)$")
_VEHICLE_NOUN = re.compile(f"^{_W}(?:سيارة|سياره|سيارتي|كبوت|مكينة|مكينه|محرك|ماكينة|ماكينه|بطارية|بطاريه|سلك|"
                           f"شريط|أسلاك|اسلاك|ضفيرة|كيبل|كابل|فيوز)$")
_FIRE_WORD = re.compile(r"نار$")
_HOT_WEATHER = re.compile(f"^{_W}(?:جو|حر|طقس|دنيا|شمس|حرارة|حراره|صيف)$")
_GIVES = re.compile(r"^(?:يعطي|تعطي|يعطيني|تعطيني|يجيب|تجيب|يولد|تولد)$")
_IGNITION_PART = re.compile(f"(?<!\\w){_W}(?:بوجي|بواجي|بوجيه|كويل|كويلات|بلاتين|شمعات|شمعة|موزع)(?!\\w)")
_IGNITION_WORD = re.compile(r"اشتعال")
_IGNITION_BEFORE = re.compile(f"^{_W}(?:نظام|توقيت|ملف|ملفات|كويل|كويلات|دائرة|مفتاح|سويتش|شمعات|شمعة|ضعف|تقديم|"
                              f"تأخير|زاوية|بوبينة)$")
_SPARK_AR = re.compile(r"شرار|شرر")
_SPARK_TEST = re.compile(f"^{_W}(?:فحص|فاحص|اختبار|قوة|ضعف|جهاز|تستر|تيستر)$")
# smoke from a smoke-test machine: "اختبار الدخان", "جهاز الدخان", "دخان الجهاز", "مشین کا دھواں", "smoke machine"
_SMOKE_TOOL = re.compile(f"^{_W}(?:اختبار|فحص|جهاز|مشين|مشین|ٹیسٹ|سموك|اسموک)$|^(?:test|machine(?:'s)?|tester)$", re.I)
_FIRE_EN = re.compile(r"^fire$", re.I)


def _words(text: str) -> list[str]:
    return re.findall(r"[^\s.,،؛;:!?؟()]+", text)


def _workshop_meaning(text: str, match: re.Match[str]) -> bool:
    """True when the trigger word, in its words around it, is ordinary workshop talk and not danger."""
    word, start, end = match.group(0), match.start(), match.end()
    near = text[max(0, start - 60): end + 60]
    before, after = _words(text[:start])[-3:], _words(text[end:])[:2]
    if _SMOKE_WORD.search(word):
        if any(_SMOKE_TOOL.match(w) for w in before[-2:]) or (after and _SMOKE_TOOL.match(after[0])) or \
                (len(before) >= 2 and before[-1] == "کا" and _SMOKE_TOOL.match(before[-2])):
            return not _DANGER_PLACE.search(near)
        coloured = _COLOUR.search(text[max(0, start - 25): end + 25])
        return bool(coloured or _EXHAUST.search(near)) and not _DANGER_PLACE.search(near)
    if _LIT_WORD.search(word):
        for i, w in enumerate(before):  # the light, then the verb, with no other car part named in between;
            # the word right after the light names it ("لمبة المكينة" = the engine light)
            if _LIGHT.match(w) and not any(_VEHICLE_NOUN.match(x) for x in before[i + 2:]):
                return True
        return bool(after) and bool(_LIGHT.match(after[0]) or (len(after) > 1 and _LIGHT.match(after[1])
                                                                 and not _VEHICLE_NOUN.match(after[0])))
    if _FIRE_WORD.search(word):
        if before and _HOT_WEATHER.match(before[-1]):
            return True
        return bool(before) and bool(_GIVES.match(before[-1])) and bool(_IGNITION_PART.search(near))
    if _IGNITION_WORD.search(word):
        return bool(before) and bool(_IGNITION_BEFORE.match(before[-1]))
    if _SPARK_AR.search(word):
        return bool(after and _IGNITION_PART.match(after[0])) or bool(before and _SPARK_TEST.match(before[-1]))
    if _FIRE_EN.match(word):
        return bool(re.match(r"\s*(?:it\s+|her\s+)?up\b", text[end:end + 12], re.I)) or \
            bool(re.search(r"(?:won'?t|wont|doesn'?t|didn'?t|does\s+not|did\s+not|will\s+not|can'?t)\s+$", text[max(0, start - 15):start], re.I))
    return False


@dataclass(frozen=True)
class Emergency:
    kind: Kind
    matched: str  # the words that fired, for the trace


def check(text: str) -> Emergency | None:
    """The first danger described in the message, or None."""
    for kind, pattern in TRIGGERS:
        for match in pattern.finditer(text):
            before, after = text[:match.start()], text[match.end():]
            if _NEGATION_BEFORE.search(before[-40:]) or _NEGATION_AFTER.match(after[:30]):
                continue
            if _workshop_meaning(text, match):
                continue
            return Emergency(kind, match.group(0))
    return None


# -- the fixed texts (DRAFT: to be approved by the client's expert) --------------------------------
def text(kind: Kind, language: Language, *, high_voltage: bool, number: str) -> str:
    if kind == "person":
        return {
            "en": (f"Call {number} now. If the person is still touching the vehicle or a cable, do not touch them. "
                   "Switch the power off only if you can do it without touching them. Keep everyone away from the vehicle."),
            "ar": (f"اتصل على {number} فوراً. إذا كان الشخص ما زال ملامساً للسيارة أو لكابل فلا تلمسه. "
                   "افصل الكهرباء فقط إذا استطعت ذلك دون لمسه. أبعد الجميع عن السيارة."),
            "ur": (f"ابھی {number} پر کال کریں۔ اگر وہ شخص ابھی بھی گاڑی یا کسی تار کو چھو رہا ہے تو اسے ہاتھ نہ لگائیں۔ "
                   "بجلی صرف اسی صورت میں بند کریں جب آپ اسے چھوئے بغیر ایسا کر سکیں۔ سب کو گاڑی سے دور رکھیں۔"),
        }[language]
    if high_voltage:
        return {
            "en": (f"Stop work now and move everyone away from the vehicle. If there is fire or smoke, call {number} and tell them "
                   "it is a high-voltage (hybrid or electric) vehicle. Do not touch orange high-voltage cables or parts, and do not "
                   "open or try to cool the high-voltage battery yourself. Continue the diagnosis only after a person qualified for "
                   "high-voltage work has made the vehicle safe."),
            "ar": (f"أوقف العمل الآن وأبعد الجميع عن السيارة. إذا كان هناك نار أو دخان اتصل على {number} وأخبرهم أنها سيارة "
                   "هاي فولتج (هايبرد أو كهربائية). لا تلمس الكوابل أو القطع البرتقالية، ولا تفتح بطارية الهاي فولتج ولا تحاول تبريدها "
                   "بنفسك. لا تكمل التشخيص إلا بعد أن يجعل فنيٌّ مؤهل لأعمال الهاي فولتج السيارة آمنة."),
            "ur": (f"ابھی کام روک دیں اور سب کو گاڑی سے دور کر دیں۔ اگر آگ یا دھواں ہے تو {number} پر کال کریں اور بتائیں کہ یہ "
                   "ہائی وولٹیج (ہائبرڈ یا الیکٹرک) گاڑی ہے۔ نارنجی ہائی وولٹیج تاروں یا پرزوں کو ہاتھ نہ لگائیں، اور ہائی وولٹیج بیٹری "
                   "کو خود نہ کھولیں اور نہ ٹھنڈا کرنے کی کوشش کریں۔ تشخیص تب ہی جاری رکھیں جب ہائی وولٹیج کام کا اہل شخص گاڑی کو محفوظ کر دے۔"),
        }[language]
    return {
        "en": (f"Stop work now and move everyone away from the vehicle. If there is fire or smoke, call {number}. "
               "Continue the diagnosis only once the vehicle is safe and the cause of the smoke, smell or sparks is known."),
        "ar": (f"أوقف العمل الآن وأبعد الجميع عن السيارة. إذا كان هناك نار أو دخان اتصل على {number}. "
               "لا تكمل التشخيص إلا بعد أن تصبح السيارة آمنة ويُعرف سبب الدخان أو الرائحة أو الشرر."),
        "ur": (f"ابھی کام روک دیں اور سب کو گاڑی سے دور کر دیں۔ اگر آگ یا دھواں ہے تو {number} پر کال کریں۔ "
               "تشخیص تب ہی جاری رکھیں جب گاڑی محفوظ ہو اور دھوئیں، بو یا چنگاریوں کی وجہ معلوم ہو۔"),
    }[language]


NOT_APPROVED_WARNING = "Safety text not yet approved by the client's expert (draft)."
