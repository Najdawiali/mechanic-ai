"""Workshop words in Jordanian Arabic and Urdu, with their English names.

The fast model dropped slang words when it rewrote a message into English (smoke test, 0 of 3 for
"بواجي" and "دبرياج"). So the words are found here, in code, and handed to the model as hints next
to the message, and added to the search query. The message itself is never rewritten: a word list
can be wrong in context, the mechanic's own words cannot.

The list is a start. The client's expert should add the words his mechanics use.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# term as written -> English. Arabic terms match with the usual attached prefixes (و ف ب ل ال).
TERMS: dict[str, str] = {
    # Arabic (Levant workshop usage)
    "بواجي": "spark plugs", "بوجيه": "spark plug", "بوجيات": "spark plugs",
    "دبرياج": "clutch", "كلتش": "clutch",
    "رديتر": "radiator", "راديتر": "radiator",
    "كوشوك": "tyre", "كاوتش": "tyre", "كوشوكات": "tyres",
    "كمبروسر": "A/C compressor", "كمبريسور": "A/C compressor",
    "فريون": "refrigerant",
    "دينمو": "alternator", "دينامو": "alternator",
    "سلف": "starter motor", "مارش": "starter motor",
    "ضفيرة": "wiring harness", "ضفيره": "wiring harness", "شريط": "wire", "شرايط": "wires", "سلك": "wire", "اسلاك": "wires",
    "فيوز": "fuse", "فيوزات": "fuses", "ريلاي": "relay", "ريليه": "relay",
    "أرضي": "ground", "ارضي": "ground", "ارث": "ground", "ايرث": "ground",
    "سويتش": "ignition switch", "سويج": "ignition switch",
    "جير": "transmission", "قير": "transmission", "كمبيوتر": "control module",
    "حساس": "sensor", "حساسات": "sensors", "سنسر": "sensor",
    "بطارية": "battery", "بطاريه": "battery", "هاي فولتج": "high voltage", "هاي فولت": "high voltage",
    "شاصي": "chassis (VIN)", "رقم الشاصي": "VIN",
    "مكيف": "air conditioning", "مروحة": "fan", "مراوح": "fans", "مضخة": "pump", "مضخه": "pump", "طرمبة": "pump", "طرمبه": "pump",
    "ضو": "light", "ضوء": "light", "اضوية": "lights", "ابريك": "brake", "بريك": "brake", "فرامل": "brakes",
    "كوندنسر": "condenser", "ثرموستات": "thermostat", "بلف": "valve", "بلوف": "valves",
    "كنكتور": "connector", "سوكيت": "connector", "فيشة": "connector", "فيشه": "connector",
    "ملتيميتر": "multimeter", "افوميتر": "multimeter", "آفوميتر": "multimeter",
    # Urdu (workshop usage)
    "گاڑی": "car", "سکینر": "scanner", "ریڈنگ": "reading", "تار": "wire", "تاریں": "wires",
    "فیوز": "fuse", "بیٹری": "battery", "سینسر": "sensor", "کنیکٹر": "connector", "ارتھ": "ground",
}

_PREFIX = "(?:و|ف|ب|ل|ك)?(?:ال)?"


@dataclass(frozen=True)
class Term:
    written: str  # as found in the message
    english: str


def _pattern(term: str) -> re.Pattern[str]:
    return re.compile(f"(?<!\\w){_PREFIX}{re.escape(term)}(?!\\w)")


_COMPILED = sorted(((t, e, _pattern(t)) for t, e in TERMS.items()), key=lambda item: -len(item[0]))


def find_terms(text: str) -> list[Term]:
    """Each workshop word in the message once, longest first, so "رقم الشاصي" wins over "شاصي"."""
    found, taken = [], []
    for term, english, pattern in _COMPILED:
        for match in pattern.finditer(text):
            if any(match.start() < end and start < match.end() for start, end in taken):
                continue
            taken.append(match.span())
            found.append(Term(match.group(0), english))
            break
    return found
