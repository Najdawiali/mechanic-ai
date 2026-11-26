"""The answer validator (handoff section 6): no specific fact without a source.

A reply written by the model is checked before it is returned. Every "specific" token in it must be
found in what the model was given: the facts and evidence of this reply, the vehicle, and the
mechanic's own messages. A specific token is anything that carries a digit: a value (7.14), a pin
(64, A49), a fault code (P0010), a connector (C224), a ground (G020), a fuse (5-5), a year.

    allowed = Allowed.from_texts(["Step 1: pin 1 to pin 2, 7.14 to 7.90 ohm"])
    problems("Measure between pin 1 and pin 2; it should read 7.14 to 7.9 ohm.", allowed)  ->  []
    problems("It should read about 12 V.", allowed)                                      ->  ["12"]

Numbers are compared by value, so 7.9 matches 7.90. Arabic-Indic digits in a reply are a problem in
themselves: numbers are never translated (handoff 7.1).

What this cannot see: a wrong fact with no digit in it ("the red wire"). Wire colours and component
names in replies about wiring come from code, not from the model (see app/engine/wiring.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.engine.readings import normalise_digits

_TOKEN = re.compile(r"[A-Za-z_]*[.\-]?\d[\w.\-/]*")
_EASTERN_DIGIT = re.compile("[٠-٩۰-۹]")
_NUMBER = re.compile(r"^\d+(?:\.\d+)?$")
# words that carry a digit but say nothing specific about the car
_HARMLESS = {"1st", "2nd", "3rd", "4th", "12v", "12-volt", "24/7"}
# a value with its unit glued on, as tokens() gives it: 12V, 0.5MV, 40A, 120OHM, 2.5KPA, 30PSI, 25NM, 50HZ, 10MS, 5K, 20%
_VALUE_WITH_UNIT = re.compile(r"^\d+(?:\.\d+)?(?:V|MV|KV|A|MA|OHMS?|KOHMS?|Ω|KΩ|KPA|PA|BAR|PSI|NM|HZ|KHZ|MS|S|K|KW|W|C|F|°C|°F|RPM|MM|CM|L|%)$")


def _clean(token: str) -> str:
    return token.strip(".-/_").upper()


# A list number at the start of a line, plain or in markdown: "1. ", "2) ", "(3) ", "**4.** ", "### 5. ", "- 6: "
_ENUMERATOR = re.compile(r"(?m)^\s*(?:[-*•#>]+\s*)?(?:\*\*|__)?\(?\d{1,2}[.)\-:](?:\*\*|__)?\s+")


def tokens(text: str) -> list[str]:
    """The specific tokens of a text. List numbering at the start of a line is layout, not a fact
    (second full run: '1. 2. 3.' in general guidance was refused as numbers without a source)."""
    return [t for t in (_clean(m) for m in _TOKEN.findall(_ENUMERATOR.sub(" ", normalise_digits(text)))) if t]


@dataclass
class Allowed:
    words: set[str] = field(default_factory=set)
    numbers: set[float] = field(default_factory=set)

    @classmethod
    def from_texts(cls, texts) -> Allowed:
        allowed = cls()
        for text in texts:
            allowed.add(text)
        return allowed

    def add(self, text: str | None) -> None:
        for token in tokens(text or ""):
            self.words.add(token)
            self.words.add(re.sub(r"[.\-]", "", token))  # ID.4 also allows ID4
            parts = token.split("_")  # VCFRONT_A490_COOLANTPUMPSNOTIDENTIFIED also allows VCFRONT_A490
            for i in range(2, len(parts)):
                self.words.add("_".join(parts[:i]))
            for part in re.split(r"[-/]", token):  # "5-5" allows "5-5"; "7.14-7.90" allows each number
                if _NUMBER.match(part):
                    self.numbers.add(float(part))
            if _NUMBER.match(token):
                self.numbers.add(float(token))

    def add_identifiers(self, text: str | None) -> None:
        """Only the names in an untrusted text (a web page): fault codes, bulletin numbers, engine codes, model
        names like ID.4. Never its values: "7.14", "12V", "40A", "2.5kPa" from the web are not allowed (design 2.7)."""
        for token in tokens(text or ""):
            if re.search(r"[A-Z]", token) and not _VALUE_WITH_UNIT.match(token):
                self.words.add(token)
                self.words.add(re.sub(r"[.\-]", "", token))

    def has(self, token: str) -> bool:
        if token in self.words or re.sub(r"[.\-]", "", token) in self.words or token.lower() in _HARMLESS:
            return True
        if _NUMBER.match(token):
            return float(token) in self.numbers
        parts = [p for p in re.split(r"[-/]", token) if p]
        return len(parts) > 1 and all(self.has(p) for p in parts)


# Positions named by generic code meanings ("Bank 1 Sensor 2", "B1S2"): labels, not values. Without this the checker
# refused correct generic meanings of P0017, P0008, P0420 ... and the answer fell back to a fixed text
# (comparison run 2026-11-25, ENG-016).
_POSITION = re.compile(r"\b(?:bank|sensor)\s*[1-3]\b|\bB[12]\s*S[1-3]\b|(?:ال)?بنك\s*(?:رقم\s*)?[12](?!\d)|"
                       r"(?:ال)?حساس\s*رقم\s*[1-3](?!\d)", re.I)


# A reference to one of the message's own numbered checks ("go to 2", "step 3", "انتقل إلى الخطوة 2", "مرحلہ 2"): layout,
# allowed only when the message numbers its checks that far (answer layout, 2026-11-26). "cylinder 2" stays a value.
_LINE_NUMBER = re.compile(r"(?m)^\s*(?:[-*•#>]+\s*)?(?:\*\*|__)?\(?(\d{1,2})[.)\-:](?:\*\*|__)?\s+")
_STEP_REF = re.compile(r"(?:\b(?:step|check|go\s+to|move\s+to|continue\s+(?:with|to)|skip\s+to|back\s+to|see)\s+"
                       r"(?:step\s+|check\s+)?|(?:انتقل|ارجع|عد|اذهب)\s+(?:إلى|الى|ل)\s*(?:ال)?(?:خطوة|فحص)?\s*(?:رقم\s*)?"
                       r"|(?:ال)?(?:خطوة|فحص)\s*(?:رقم\s*)?|(?:مرحلہ|اسٹیپ|قدم|چیک)\s*(?:نمبر\s*)?)(\d{1,2})(?!\d)", re.I)


def _without_step_references(message: str) -> str:
    numbered = {int(n) for n in _LINE_NUMBER.findall(message)}
    return _STEP_REF.sub(lambda m: " " if int(m.group(1)) in numbered else m.group(0), message)


def problems(message: str, allowed: Allowed) -> list[str]:
    """The tokens in `message` that have no source. Empty = the message passes."""
    message = _without_step_references(_POSITION.sub(" ", message))
    found = []
    if _EASTERN_DIGIT.search(message):
        found.append("Arabic-Indic digits: numbers must be written with Latin digits")
    for token in tokens(message):
        if not allowed.has(token) and token not in found:
            found.append(token)
    return found
