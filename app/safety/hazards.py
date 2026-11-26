"""Hazards that need a fixed safety text before any general advice, decided in code (not by the model).

Comparison run 2026-11-24 (venorexai's 100 cases, judged with his rubric): 7 of our 16 critical failures were
safety gaps on these topics. The general answer told a mechanic how to clean an orange high-voltage connector,
suggested drying a flooded EV's high-voltage parts, answered "can I test the orange cables myself?" with a
wiring question, and described probing airbag circuits with a multimeter. These checks run on every message
and put a fixed text first; the model is also told, so it never writes a hands-on step for these parts.

  hv          hands-on work on high-voltage parts (orange cables, HV battery, inverter, service plug)
  isolation   an isolation (insulation) fault warning on a high-voltage car: do not charge or drive it
  srs         airbags, pretensioners, clock spring: never probe, disconnect the battery and wait first
  hid         xenon / HID headlamp ballasts: very high voltage at start-up
  flashing    a flashing check-engine light: severe misfire, do not keep driving under load
  ready       work near belts or pulleys on a hybrid: the engine can start by itself while the car is READY
  security    immobilizer PIN / security codes / mileage: never read, calculate or bypass them
              (comparison run 2026-11-25: HYB-012 endorsed a belt check in READY, ABS-006 described reading a PIN)

THE TEXTS ARE DRAFTS (wording.FIXED "hazard_*"), like the emergency texts: the client's expert must approve them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

TEXTS_APPROVED = False  # the client's expert approves the hazard_* texts in wording.FIXED

_L = "\\u0621-\\u064A\\u0671-\\u06D3"  # Arabic and Urdu letters (not punctuation such as the Arabic comma)
_AR = f"(?<![{_L}])(?:و|ف|ب|ل)?(?:ال)?"  # an Arabic word with its common prefixes
_NL = f"(?![{_L}])"  # end of an Arabic word


def _ar(*words: str) -> str:
    return _AR + "(?:" + "|".join(words) + ")" + _NL


_HV_OBJECT = re.compile(
    r"orange\s+(?:hv\s+)?(?:cable|wire|connector|lead|harness)s?|high.?voltage|\bhv\b|hybrid\s+battery|traction\s+battery|"
    r"\binverter\b|service\s+(?:plug|disconnect)|isolation|insulation\s+(?:fault|resistance)|"
    + _AR + r"(?:كيبل|كيابل|كابل|كابلات|سلك|اسلاك|أسلاك)\s+(?:ال)?برتقالي(?:ة|ه)?" + "|"
    + _ar("برتقالي", "برتقالية", "برتقاليه") + "|"
    + _AR + r"(?:جهد|فولت)\s+(?:ال)?عالي" + "|هاي ?فولت|"
    + _ar("انفرتر", "إنفرتر", "إنفيرتر", "انفيرتر", "عزل") + "|"
    + r"ہائی ?وولٹ|نارنجی|انورٹر", re.I)
_HANDS_ON = re.compile(
    r"\b(?:probe|probing|measure|measuring|test(?:ing)?|multi-?meter|disconnect|unplug|remove|open(?:ing)?|"
    r"clean(?:ing)?|dry(?:ing)?|spray|touch|cut|myself|diy)\b|"
    + _ar("أفحص", "افحص", "فحص", "أقيس", "اقيس", "قياس", "ملتيميتر", "ملتي ميتر", "أفك", "افك", "فك", "أفكه", "افكه",
          "أنظف", "انظف", "نظف", "أنظفه", "انظفه", "تنظيف", "بخاخ", "أفتح", "افتح", "فتح", "أجفف", "اجفف", "تجفيف",
          "ألمس", "المس", "بنفسي")
    + r"|ملٹی ?میٹر|کھول|صاف|اتار|خود", re.I)
_ISOLATION = re.compile(r"isolation|insulation\s+(?:fault|resistance)|" + _ar("عزل") + r"|آئسولیشن|انسولیشن", re.I)
# P0AA6 = hybrid/EV battery voltage isolation fault (SAE J2012 generic code)
ISOLATION_CODES = frozenset({"P0AA6"})
_SRS = re.compile(
    r"airbag|air\s+bag|\bsrs\b|pretensioner|clock\s*spring|squib|"
    + _ar("ايرباق", "ايرباغ", "إيرباق", "ايرباج", "الايرباق", "الوسائد", "شداد")
    + r"|وسادة\s+هوائية|وسائد\s+هوائية|ساعة\s+الدركسون|ساعة\s+الدركسيون|ایئر ?بیگ|ائیر ?بیگ", re.I)
_HID = re.compile(r"\bhid\b|xenon|ballast|" + _ar("زينون", "زنون", "بالاست", "بلاست") + r"|زینون|بیلسٹ", re.I)
_READY_WORK = re.compile(r"\b(?:drive|serpentine|accessory|hsg)?\s*belts?\b|pulley|tensioner|\bhsg\b|"
                         + _ar("حزام", "الحزام", "سير", "السير", "بكرة", "البكرة", "بكرات") + r"|بیلٹ|پلی", re.I)
_SECURITY = re.compile(r"immobili[sz]er|\bpin\s*code|security\s+code|key\s+(?:programming|coding)|odometer|mileage\s+"
                       r"(?:correction|change|rollback)|clock(?:ing)?\s+the\s+(?:car|mileage)|"
                       + _ar("إيموبلايزر", "ايموبلايزر", "الإيموبلايزر", "الايموبلايزر", "ايموبلايزر")
                       + r"|كود\s+(?:ال)?(?:حماية|أمان|امان|PIN)|رمز\s+(?:ال)?(?:حماية|PIN)|برمجة\s+(?:ال)?مفتاح|عداد\s+(?:ال)?(?:مسافة|كيلو)|"
                       r"(?:تصفير|ترجيع|تعديل)\s+(?:ال)?عداد|امو بلائزر|اموبلائزر", re.I)
_FLASH = (r"(?:flash\w*|blink\w*|ترمش|يرمش|تومض|يومض|وامض\w*|تفلش|يفلش|تولع\s+وتطفي|يولع\s+ويطفي|ٹمٹما\w*|جھپک\w*|"
          r"فلیش\w*)")
# "steady, not flashing" names the light but says it does NOT flash (judge 2026-11-26, ENG-010): removed before the check
_NOT_FLASHING = re.compile(r"(?:\bnot\b|n't|\bnon-?|\bnever\b|\bwithout\b|\bno\b|"
                           f"(?<![{_L}])و?(?:غير|ليست|ليس|لا|مو|مش|ما|نہیں|نہ)(?![{_L}]))"
                           r"\s*" + _FLASH, re.I)
_ENGINE_LIGHT = (r"(?:check.?engine|engine\s+light|\bcel\b|\bmil\b|لمبة\s+(?:ال)?(?:مكينة|مكينه|محرك|ماكينة)|"
                 r"لمبة\s+(?:ال)?check|انجن\s+لائٹ|چیک\s+انجن)")
_FLASHING = re.compile(_FLASH + r".{0,60}" + _ENGINE_LIGHT + "|" + _ENGINE_LIGHT + r".{0,60}" + _FLASH
                       + r"|\blight\s+(?:was\s+)?flashing", re.I | re.S)


@dataclass(frozen=True)
class Hazards:
    hv: bool = False  # hands-on high-voltage work is asked about
    isolation: bool = False
    srs: bool = False
    hid: bool = False
    flashing: bool = False
    ready: bool = False
    security: bool = False

    @property
    def any(self) -> bool:
        return self.hv or self.isolation or self.srs or self.hid or self.flashing or self.ready or self.security

    @property
    def hands_on(self) -> bool:
        """A question about doing dangerous work: answered with safety first, never sent to the wiring path."""
        return self.hv or self.srs or self.hid

    def keys(self) -> list[str]:
        return [k for k in ("hv", "isolation", "srs", "hid", "flashing", "ready", "security") if getattr(self, k)]


def check(text: str, *, high_voltage_car: bool = False, codes: list[str] | tuple[str, ...] = ()) -> Hazards:
    hv_object = bool(_HV_OBJECT.search(text))
    isolation = (bool(_ISOLATION.search(text)) and (high_voltage_car or hv_object)) or \
        bool(ISOLATION_CODES & {c.upper() for c in codes})
    return Hazards(hv=hv_object and (bool(_HANDS_ON.search(text)) or isolation), isolation=isolation,
                   srs=bool(_SRS.search(text)), hid=bool(_HID.search(text)),
                   flashing=bool(_FLASHING.search(_NOT_FLASHING.sub(" ", text))),
                   ready=high_voltage_car and bool(_READY_WORK.search(text)), security=bool(_SECURITY.search(text)))
