"""Which language a message is written in, decided in code from its letters.

Arabic and Urdu share a script. Urdu uses letters that Arabic does not (ٹ ڈ ڑ ں ے ہ ھ ی ک) and its
own digits (۰-۹), so one of those is enough to call a message Urdu. Arabic is a message whose
letters are mostly Arabic script; mechanics mix in English car words and codes, so the test is a
share of the letters, not "any Latin letter means English". A message with no letters at all
("0.4", "٧٫٥") says nothing: the caller keeps the session's language. Nor does a message whose only Latin
words are units or a yes/no ("7.5 ohm", "OL", "ok"): an Arabic session stays Arabic when the reading is typed
with an English unit (Ali's demo, 2026-11-18).
"""

from __future__ import annotations

import re
from typing import Literal

Language = Literal["en", "ar", "ur"]

_URDU = re.compile("[ٹڈڑںےۓہۃھیک۰-۹]")
_ARABIC_LETTER = re.compile("[ء-يٮ-ۓۺ-ۿ]")
_LATIN_LETTER = re.compile("[A-Za-z]")
ARABIC_SHARE = 0.3
# Latin words that say nothing about the mechanic's language: units of a reading, and short answers
_NEUTRAL = re.compile(r"(?i)\b(?:k|m|meg|mega|milli|kilo)?(?:ohms?|volts?|amps?)\b|\b(?:v|mv|a|ma|ol|hz|ok|okay|yes|no)\b")


def detect(text: str) -> Language | None:
    text = _NEUTRAL.sub(" ", text)
    arabic = len(_ARABIC_LETTER.findall(text))
    latin = len(_LATIN_LETTER.findall(text))
    if arabic + latin == 0:
        return None
    if arabic / (arabic + latin) < ARABIC_SHARE:
        return "en"
    return "ur" if _URDU.search(text) else "ar"
