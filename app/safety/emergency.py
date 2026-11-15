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
    ("vehicle", r"\b(?:burn\w*|burnt|scorch\w*)\s+(?:smell|odou?r|plastic|wires?|insulation)\b"),
    ("vehicle", r"\bsmell\w*\s+(?:of\s+|like\s+)?(?:burn\w*|burnt|plastic|scorch\w*)\b"),
    ("vehicle", r"\bspark(?:s|ing)\b(?!\s*plugs?)|\barcing\b"),
    ("vehicle", r"\b(?:swollen|swelling|bulging|puffed|puffy)\b.{0,30}\bbatter|\bbatter\w*\b.{0,30}\b(?:swollen|swelling|bulging|puffed|puffy)\b"),
    ("vehicle", r"\bbatter\w*\b.{0,30}\b(?:leak\w*|hiss\w*|venting)\b|\b(?:electrolyte|coolant)\s+leak\w*\s+(?:from|out of)\s+(?:the\s+)?(?:hv\s+|high.voltage\s+)?batter"),
    ("vehicle", r"\bthermal\s+runaway\b|\bmelt(?:ed|ing)\b"),
    ("person", r"\b(?:got|get|was|been|getting)\s+(?:an?\s+)?(?:electric\s+)?shock(?:ed)?\b|\belectrocut\w*\b"),
    # Arabic
    ("vehicle", _ar("دخان", "دخنة", "دخنه", "يدخن", "بدخن", "بتدخن", "تدخن", "دخّان")),
    ("vehicle", _ar("شياط", "حريق", "نار", "ولعت", "ولّعت", "اشتعلت", "اشتعال", "بتولع", "مولعة", "مولعه")),
    ("vehicle", "(?:ريحة|ريحه|رائحة|رائحه)\\s+(?:\\S+\\s+)?(?:محروق|محروقة|شياط|حريق|احتراق|بلاستيك)"),
    ("vehicle", _ar("شرار", "شرارة", "شراره", "شرر")),
    ("vehicle", _BATTERY_AR + ".{0,25}(?:انتفخت|منفوخة|منفوخه|منتفخة|منتفخه|نافخة|نافخه|بتسرب|بتسرّب|تسرب|بتنقط|بتنفس|بتصفر)"
                "|(?:انتفاخ|نفخ|تسريب|تسرب)\\s+(?:في\\s+|من\\s+)?" + _BATTERY_AR),
    ("vehicle", _WIRE_AR + "\\s+(?:\\S+\\s+)?(?:ذاب|ذايب|ساح|سايح|ساحت|ذابت|محروق|محروقة|محروقه|محترق)"),
    ("person", _ar("كهربني", "كهربه", "كهربته", "كهربتني", "صعقة", "صعقه", "انصعق", "صعقته", "مكهرب")),
    # Urdu
    ("vehicle", "(?<!\\w)(?:دھواں|دھوئیں|دھویں)(?!\\w)"),
    ("vehicle", "(?<!\\w)آگ(?!\\w)|جلنے\\s+کی\\s+(?:بو|بدبو)|جلی\\s+ہوئی\\s+بو"),
    ("vehicle", "(?<!\\w)(?:چنگاری|چنگاریاں)(?!\\w)"),
    ("vehicle", "بیٹری.{0,25}(?:پھول|لیک|رس)|(?:پھولی|پھولی ہوئی)\\s+بیٹری"),
    ("vehicle", "(?:تار|تاریں)\\s+(?:\\S+\\s+)?(?:پگھل|جل)"),
    ("person", "کرنٹ\\s+لگ|جھٹکا\\s+لگ"),
]]

_NEGATION_BEFORE = re.compile(
    r"(?:^|\s)(?:no|not|without|never|isn'?t|wasn'?t|there'?s no|"
    r"لا|ما|مافي|ما في|مش|مو|بدون|بلا|ليس|لايوجد|لا يوجد|ولا|"
    r"کوئی نہیں|بغیر)\s+(?:\S+\s+)?$", re.I)
_NEGATION_AFTER = re.compile(r"^\s*(?:\S+\s+)?(?:نہیں|نہ)(?!\w)")


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
