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

_TOKEN = re.compile(r"[A-Za-z_]*\d[\w.\-/]*")
_EASTERN_DIGIT = re.compile("[٠-٩۰-۹]")
_NUMBER = re.compile(r"^\d+(?:\.\d+)?$")
# words that carry a digit but say nothing specific about the car
_HARMLESS = {"1st", "2nd", "3rd", "4th", "12v", "12-volt", "24/7"}


def _clean(token: str) -> str:
    return token.strip(".-/_").upper()


def tokens(text: str) -> list[str]:
    return [t for t in (_clean(m) for m in _TOKEN.findall(normalise_digits(text))) if t]


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
            for part in re.split(r"[-/]", token):  # "5-5" allows "5-5"; "7.14-7.90" allows each number
                if _NUMBER.match(part):
                    self.numbers.add(float(part))
            if _NUMBER.match(token):
                self.numbers.add(float(token))

    def has(self, token: str) -> bool:
        if token in self.words or token.lower() in _HARMLESS:
            return True
        if _NUMBER.match(token):
            return float(token) in self.numbers
        parts = [p for p in re.split(r"[-/]", token) if p]
        return len(parts) > 1 and all(self.has(p) for p in parts)


def problems(message: str, allowed: Allowed) -> list[str]:
    """The tokens in `message` that have no source. Empty = the message passes."""
    found = []
    if _EASTERN_DIGIT.search(message):
        found.append("Arabic-Indic digits: numbers must be written with Latin digits")
    for token in tokens(message):
        if not allowed.has(token) and token not in found:
            found.append(token)
    return found
