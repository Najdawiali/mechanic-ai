"""Putting a reply into the mechanic's language, with every number checked.

Two kinds of text:

  translate()  a text rendered in code from records (a step, a conclusion, a pin list). The model
               only translates it. The result must contain every number, pin and code of the
               English text and nothing more; otherwise it is asked again once, and then the
               English text is sent with a one-line note in the mechanic's language.
  compose()    a reply the model writes from evidence (a case hint, general guidance). The result
               must pass the validator: nothing specific without a source. Asked again once with
               the problems listed; then the caller's fallback (written in code) is used.

Fixed short texts (the "no verified information" reply, notes, labels) are written here in the
three languages and need no model. Arabic and Urdu wording is ours: a native reader should check it.
"""

from __future__ import annotations

import json
import re

from app.engine.model import ModelClient, ModelError, reject
from app.engine.validator import Allowed, problems, tokens
from app.lang.script import Language

LANGUAGE_NAME = {"en": "English", "ar": "Arabic (as spoken in Jordan; clear, not formal), in Arabic script",
                 "ur": "Urdu, written in Urdu script (never Roman Urdu in Latin letters)"}
_ARABIC_SCRIPT = re.compile("[\u0600-\u06ff]")
_LATIN = re.compile("[A-Za-z]")
_SIGNS = "<>≤≥"

FIXED = {
    "english_follows": {
        "en": "",
        "ar": "النص التالي من المصدر بالإنجليزية كما هو:",
        "ur": "درج ذیل متن ماخذ سے انگریزی میں ہے:",
    },
    "no_verified": {
        "en": "There is no verified information for this on this car in the knowledge base, so I can't give a pin number, "
              "wire colour or value for it. Take it from the manufacturer's service information or wiring diagram for this car, "
              "or ask the workshop's expert.",
        "ar": "لا توجد معلومات موثقة عن هذا لهذه السيارة في قاعدة البيانات، لذلك لا أستطيع إعطاء رقم بن أو لون سلك أو قيمة. "
              "خذها من معلومات الصيانة أو مخطط الأسلاك الخاص بالشركة المصنعة لهذه السيارة، أو اسأل خبير الورشة.",
        "ur": "اس گاڑی کے لیے نالج بیس میں اس بارے میں کوئی تصدیق شدہ معلومات نہیں ہیں، اس لیے میں پن نمبر، تار کا رنگ یا ویلیو "
              "نہیں دے سکتا۔ یہ اس گاڑی کی مینوفیکچرر سروس معلومات یا وائرنگ ڈایاگرام سے لیں، یا ورکشاپ کے ماہر سے پوچھیں۔",
    },
    "general_header": {
        "en": "{codes}: no verified data for this car in the knowledge base. What follows is general knowledge, not verified for this car:",
        "ar": "{codes}: لا توجد بيانات موثقة لهذه السيارة في قاعدة البيانات. ما يلي معلومات عامة غير موثقة لهذه السيارة:",
        "ur": "{codes}: اس گاڑی کے لیے نالج بیس میں کوئی تصدیق شدہ ڈیٹا نہیں ہے۔ درج ذیل عمومی معلومات ہیں، اس گاڑی کے لیے تصدیق شدہ نہیں:",
    },
    "general_header_no_code": {
        "en": "No verified data for this on this car in the knowledge base. What follows is general knowledge, not verified for this car:",
        "ar": "لا توجد بيانات موثقة عن هذا لهذه السيارة في قاعدة البيانات. ما يلي معلومات عامة غير موثقة لهذه السيارة:",
        "ur": "اس گاڑی کے لیے نالج بیس میں اس بارے میں کوئی تصدیق شدہ ڈیٹا نہیں ہے۔ درج ذیل عمومی معلومات ہیں، اس گاڑی کے لیے تصدیق شدہ نہیں:",
    },
    "general_fallback": {
        "en": "Start with the basics for this fault: battery voltage, the fuses and grounds of the system, and the connectors of the parts involved. "
              "Tell me what you find, or give me the fault code if there is one.",
        "ar": "ابدأ بالأساسيات لهذا العطل: فولتية البطارية، وفيوزات النظام والأرضي، وكنكتورات القطع المعنية. "
              "أخبرني بما تجده، أو أعطني كود العطل إن وجد.",
        "ur": "اس خرابی کے لیے بنیادی چیزوں سے شروع کریں: بیٹری وولٹیج، سسٹم کے فیوز اور گراؤنڈ، اور متعلقہ پرزوں کے کنیکٹر۔ "
              "مجھے بتائیں کہ کیا ملا، یا اگر کوئی فالٹ کوڈ ہے تو وہ دیں۔",
    },
    "fix_noted": {
        "en": "Noted: {fix}. You report the fault as fixed. Close the session to save this repair for review as a case.",
        "ar": "تم التسجيل: {fix}. حسب كلامك العطل انحل. أغلق الجلسة ليتم حفظ هذا الإصلاح كحالة للمراجعة.",
        "ur": "نوٹ کر لیا: {fix}۔ آپ کے مطابق خرابی ٹھیک ہو گئی ہے۔ سیشن بند کریں تاکہ یہ مرمت جائزے کے لیے کیس کے طور پر محفوظ ہو جائے۔",
    },
    "need_reading": {
        "en": "For this step I need the meter reading, with its unit.",
        "ar": "لهذه الخطوة أحتاج قراءة الملتيميتر مع الوحدة.",
        "ur": "اس قدم کے لیے مجھے میٹر کی ریڈنگ اس کی یونٹ کے ساتھ چاہیے۔",
    },
    "which_diagram": {
        "en": "Which system or component do you need the wiring diagram for?",
        "ar": "مخطط أي نظام أو أي قطعة بدك؟",
        "ur": "آپ کو کس سسٹم یا پرزے کا وائرنگ ڈایاگرام چاہیے؟",
    },
    "diagram_sent": {
        "en": "Here are the wiring diagram pages: {pages}.",
        "ar": "هذه صفحات مخطط الأسلاك: {pages}.",
        "ur": "یہ وائرنگ ڈایاگرام کے صفحات ہیں: {pages}۔",
    },
    "ask_year": {
        "en": "Which model year is the car? The procedure for {code} exists for: {years}.",
        "ar": "شو سنة صنع السيارة؟ الإجراء للكود {code} موجود للسنوات: {years}.",
        "ur": "گاڑی کس ماڈل سال کی ہے؟ کوڈ {code} کا طریقہ ان سالوں کے لیے موجود ہے: {years}۔",
    },
    "ask_engine": {
        "en": "Is the engine {engine}? The procedure for {code} is for that engine only.",
        "ar": "هل المحرك {engine}؟ الإجراء للكود {code} خاص بهذا المحرك فقط.",
        "ur": "کیا انجن {engine} ہے؟ کوڈ {code} کا طریقہ صرف اسی انجن کے لیے ہے۔",
    },
    "ask_codes": {
        "en": "What fault code does the scan tool show, or what does the car do?",
        "ar": "شو كود العطل اللي بطلع على جهاز الفحص، أو شو المشكلة اللي بتعملها السيارة؟",
        "ur": "سکینر پر کون سا فالٹ کوڈ آ رہا ہے، یا گاڑی میں کیا مسئلہ ہے؟",
    },
    "report_read": {
        "en": "I read these codes from the report. Check that they match the screen:",
        "ar": "قرأت هذه الأكواد من التقرير. تأكد أنها مطابقة للشاشة:",
        "ur": "میں نے رپورٹ سے یہ کوڈ پڑھے ہیں۔ تصدیق کریں کہ یہ سکرین سے ملتے ہیں:",
    },
    "report_vehicle": {
        "en": "The report names the vehicle as: {vehicle}.",
        "ar": "التقرير يذكر السيارة: {vehicle}.",
        "ur": "رپورٹ میں گاڑی کا نام: {vehicle}۔",
    },
    "report_unreadable": {
        "en": "I could not read any fault code from the attachment. Type the codes, or send a clearer photo.",
        "ar": "ما قدرت أقرأ أي كود من المرفق. اكتب الأكواد أو ابعث صورة أوضح.",
        "ur": "میں منسلکہ فائل سے کوئی کوڈ نہیں پڑھ سکا۔ کوڈ لکھ دیں یا زیادہ واضح تصویر بھیجیں۔",
    },
    "which_part": {
        "en": "Which part do you mean? Write its name as printed on the wiring diagram, or describe where it is.",
        "ar": "أي قطعة تقصد؟ اكتب اسمها مثل ما هو مكتوب على مخطط الأسلاك، أو احكيلي وين مكانها.",
        "ur": "آپ کس پرزے کی بات کر رہے ہیں؟ اس کا نام ویسے لکھیں جیسے وائرنگ ڈایاگرام پر لکھا ہے، یا بتائیں کہ وہ کہاں ہے۔",
    },
    "no_check": {
        "en": "I can't offer a check for this wire: {why}",
        "ar": "ما بقدر أعطيك فحص لهذا السلك: {why}",
        "ur": "میں اس تار کے لیے کوئی چیک نہیں دے سکتا: {why}",
    },
}


def fixed(name: str, language: Language, **values: str) -> str:
    return FIXED[name][language].format(**values)


def _numbers_kept(source: str, result: str) -> bool:
    """Every specific token of the English text is still in the translation, and every comparison sign."""
    have = Allowed.from_texts([result])
    if any(source.count(sign) > result.count(sign) for sign in _SIGNS):
        return False
    return all(have.has(t) for t in tokens(source))


def _in_script(result: str, language: Language) -> bool:
    """An Arabic or Urdu reply is written in Arabic script (part numbers and codes stay in Latin letters).
    First real run: the Urdu reply came back in Roman letters."""
    if language == "en":
        return True
    script, latin = len(_ARABIC_SCRIPT.findall(result)), len(_LATIN.findall(result))
    return script >= 0.25 * (script + latin)


TRANSLATE_SYSTEM = """You translate a message for a car mechanic into {language}.
Keep these exactly as written, in English letters and Latin digits: fault codes, pin numbers, connector and
component names, wire colours, values, units, tool names and menu paths. Every number stays a digit: "Step 1" is
"الخطوة 1", never a word such as "الأولى". Keep the signs < > ≤ ≥ in front of their values. Do not add, drop or explain anything.
Keep the line breaks. Return JSON: {{"message": "..."}}"""


def translate(model: ModelClient, english: str, language: Language, *, role: str = "fast") -> tuple[str, bool]:
    """(text, translated). For English the text is returned as it is."""
    if language == "en" or not english.strip():
        return english, False
    allowed = Allowed.from_texts([english])
    schema = {"type": "object", "additionalProperties": False, "required": ["message"],
              "properties": {"message": {"type": "string"}}}
    user = english
    for _ in range(2):
        try:
            out = model.json("translate", role=role, system=TRANSLATE_SYSTEM.format(language=LANGUAGE_NAME[language]),
                             user=user, schema=schema, max_tokens=1200)
        except ModelError:
            break
        text = out.get("message") if isinstance(out.get("message"), str) else ""
        reasons = ["empty"] if not text.strip() else [
            *([f"no source for {problems(text, allowed)}"] if problems(text, allowed) else []),
            *(["a number, pin, code or comparison sign of the English text is missing"] if not _numbers_kept(english, text) else []),
            *([f"not written in {language} script"] if not _in_script(text, language) else []),
        ]
        if not reasons:
            return text, True
        reject(model, "; ".join(reasons))
        user = (f"{english}\n\n(Your previous translation changed, dropped or added a number, pin, code or comparison sign, "
                f"or was not written in {LANGUAGE_NAME[language]}. Translate again.)")
    lead = fixed("english_follows", language)
    return f"{lead}\n{english}" if lead else english, False


# The model reports its own length ("120 words.", "120 words limit reached."): fifth run, 5 refused answers.
_BARE_NUMBER = re.compile(r"(?:(?<=[.!?:] )|^)\d+\.(?=\s|$)")  # a sentence that is only a number: "120."
_WORD_COUNT = re.compile(r"(?i)(?:\(\s*)?\b\d+\s+words?(?:\s+(?:limit\s+)?(?:reached|used|max(?:imum)?))?\s*(?:\)|\.)?")


def compose(model: ModelClient, purpose: str, *, system: str, facts: dict, language: Language, allowed: Allowed,
            schema_extra: dict | None = None, max_tokens: int = 900, forbidden: re.Pattern[str] | None = None,
            role: str = "reasoning") -> dict | None:
    """The model's reply as a dict with at least "message" (possibly empty), or None when it failed the
    validator twice or did not answer."""
    properties = {"message": {"type": "string"}, **(schema_extra or {})}
    schema = {"type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}
    full_system = (system + f"\n\nWrite the message in {LANGUAGE_NAME[language]}. Keep fault codes, part and component names, "
                   "tool names and units in English letters, and write every number with Latin digits. "
                   "Never write a number, pin, code, value or specification that is not in the facts or in the mechanic's words.")
    user = json.dumps(facts, ensure_ascii=False, default=str)
    for _ in range(2):
        try:
            out = model.json(purpose, role=role, system=full_system, user=user, schema=schema, max_tokens=max_tokens)
        except ModelError:
            return None
        message = out.get("message") if isinstance(out.get("message"), str) else ""
        message = _BARE_NUMBER.sub("", _WORD_COUNT.sub("", message))  # notes about the prompt's length, not content
        message = re.sub(r"[ \t]{2,}", " ", message).strip()
        out = {**out, "message": message}
        found = problems(message, allowed)
        if forbidden is not None:
            found += [f"must not mention {m.group(0)!r}" for m in forbidden.finditer(message)]
        if not found:  # an empty message is an answer too ("no record fits"): the caller decides
            return out
        reject(model, f"no source for {found}")
        user = json.dumps({**facts, "IMPORTANT": f"Your last message contained things with no source: {found}. "
                                                 "Write it again with none of them, and with no number, pin, code or value that is not in the facts or in "
                                                 "the mechanic's words; do not number a list."}, ensure_ascii=False, default=str)
    return None
