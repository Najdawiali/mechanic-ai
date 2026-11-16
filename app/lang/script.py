"""Which language a message is written in, decided in code from its letters.

Arabic and Urdu share a script. Urdu uses letters that Arabic does not (ٹ ڈ ڑ ں ے ہ ھ ی ک) and its
own digits (۰-۹), so one of those is enough to call a message Urdu. Arabic is a message whose
letters are mostly Arabic script; mechanics mix in English car words and codes, so the test is a
share of the letters, not "any Latin letter means English". A message with no letters at all
("0.4", "٧٫٥") says nothing: the caller keeps the session's language.
"""

from __future__ import annotations

import re
from typing import Literal

Language = Literal["en", "ar", "ur"]

_URDU = re.compile("[ٹڈڑںےۓہۃھیک۰-۹]")
_ARABIC_LETTER = re.compile("[ء-يٮ-ۓۺ-ۿ]")
_LATIN_LETTER = re.compile("[A-Za-z]")
ARABIC_SHARE = 0.3


def detect(text: str) -> Language | None:
    arabic = len(_ARABIC_LETTER.findall(text))
    latin = len(_LATIN_LETTER.findall(text))
    if arabic + latin == 0:
        return None
    if arabic / (arabic + latin) < ARABIC_SHARE:
        return "en"
    return "ur" if _URDU.search(text) else "ar"
