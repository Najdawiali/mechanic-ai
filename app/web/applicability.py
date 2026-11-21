"""Does a web page apply to this car and this code? Decided in code, before the model sees the page.

Each result is marked:
    fits   code and model named; the year named or inside a closed range; no other engine named
    check  probably about this car, but something is not confirmed (no year, an open year range
           such as "2016~", an engine-specific page when the car's engine is not known)
    drop   neither the make nor the model named, other years only, another engine, the code not named, a blocked site

Probe findings this guards against (web-search-story-review.md): a bulletin for the 2016- Sonata
Hybrid offered for a 2020 car; a Toyota bulletin for the 2GR-FKS V6 only; a page date read as a model year.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlparse

from app.kb.schemas import Vehicle

Applies = Literal["fits", "check", "drop"]

_RANGE = re.compile(r"\b((?:19|20)\d\d)\s*(?:[-–~]|to)\s*((?:19|20)?\d\d)?")
_YEAR = re.compile(r"\b(?:19|20)\d\d\b")
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
# Dates are when a page was written, not which cars it is for: "January 16, 2020", "16 Jan 2020",
# "2020-01-16", "1/16/2020", "© 2020".
_DATE = re.compile(
    rf"\b{_MONTH}\s+(?:\d{{1,2}}(?:st|nd|rd|th)?,?\s+)?(?:19|20)\d\d\b"
    rf"|\b\d{{1,2}}\s+{_MONTH}\s+(?:19|20)\d\d\b"
    r"|\b(?:19|20)\d\d-\d\d-\d\d\b|\b\d{1,2}/\d{1,2}/(?:19|20)\d\d\b|(?:©|\(c\)|copyright)\s*(?:19|20)\d\d",
    re.IGNORECASE,
)
# A page that says which engine it is for: "equipped w/2GR-FKS", "engine code: L15CA", "with the G4NS engine".
_ENGINE_SAID = re.compile(r"(?:equipped\s+(?:with|w/)\s*|engine\s+code[:\s]+|engine[:\s]+)([A-Z0-9]{2,6}(?:-[A-Z0-9]{2,4})?)"
                          r"|\b([A-Z0-9]{2,6}(?:-[A-Z0-9]{2,4})?)\s+engine\b")
_OTHER_MAKES = {  # another name for the same make
    "volkswagen": ("vw",), "mercedes-benz": ("mercedes", "benz"), "chevrolet": ("chevy",),
}
_GENERIC_MODEL_WORDS = {"model", "series", "class", "type"}


@dataclass
class Mark:
    applies: Applies
    reasons: list[str] = field(default_factory=list)


def year_fit(text: str, year: int | None) -> str:
    """'match': the year itself or a closed range that holds it (2016-2019, 2023–25, 2016 to 2019);
    'open range': only an open range holds it ("2016~", "2016-"): possibly another generation;
    'other years only': years are named and none fits; 'not stated': no year at all."""
    if year is None:
        return "not stated"
    text = _DATE.sub(" ", text)
    years = {int(y) for y in _YEAR.findall(text)}
    open_hit = False
    for start, end in _RANGE.findall(text):
        low = int(start)
        if not end:
            open_hit = open_hit or year >= low
            continue
        high = int(end) if len(end) == 4 else int(start[:2] + end)
        if low <= year <= high:
            return "match"
    if year in years:
        return "match"
    if open_hit:
        return "open range"
    return "other years only" if years else "not stated"


def model_words(model: str) -> list[str]:
    """How a page names this model: 'Sonata Hybrid' -> ['sonata'], 'Model Y' -> ['model y'], 'ID.4' -> ['id.4', 'id4']."""
    words = model.lower().split()
    if not words:
        return []
    name = " ".join(words[:2]) if words[0] in _GENERIC_MODEL_WORDS and len(words) > 1 else words[0]
    plain = re.sub(r"[.\-]", "", name)
    return list(dict.fromkeys([name, plain]))


def _named(word: str, text: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])", text) is not None


def make_named(make: str, text: str, domain: str) -> bool:
    make = make.lower()
    names = (make, *_OTHER_MAKES.get(make, ()))
    return any(_named(n, text) for n in names) or any(n.replace("-", "") in domain for n in names)


def code_named(code: str, text: str) -> bool:
    """P0C4400 is named by 'P0C44' too (the last two characters are the failure type); PCS_a027 by 'PCS a027'."""
    flat = re.sub(r"[\s_\-]", "", text.lower())
    code = re.sub(r"[\s_\-]", "", code.lower())
    return code in flat or (len(code) == 7 and code[:5] in flat)


_NOT_ENGINE = re.compile(r"^(?:[VIWH]\d{1,2}|\d+(?:\.\d+)?L|[PBCU]\d[0-9A-F]{3}(?:[0-9A-F]{2})?)$")


def engines_said(text: str) -> set[str]:
    """Engine codes a page says it is for. An engine code has letters and digits ("2GR-FKS", "L15CA"); "CHECK
    ENGINE", "V6 engine", "1.5L engine" and fault codes are not engine codes."""
    said = set()
    for a, b in _ENGINE_SAID.findall(text):
        name = (a or b).upper()
        if re.search(r"\d", name) and re.search(r"[A-Z]", name) and len(name) >= 3 and not _NOT_ENGINE.match(name):
            said.add(name)
    return said


def judge(vehicle: Vehicle, code: str, url: str, title: str, text: str, blocked: tuple[str, ...]) -> Mark:
    domain = (urlparse(url).hostname or "").lower().removeprefix("www.")
    if any(domain == d or domain.endswith("." + d) for d in blocked):
        return Mark("drop", ["blocked site"])
    whole = f"{title} {text}"
    low = whole.lower()
    if not code_named(code, whole):
        return Mark("drop", [f"{code} is not named"])
    make = make_named(vehicle.make, low, domain)
    model = any(_named(w, low) for w in model_words(vehicle.model))
    if not make and not model:  # bulletins often name the model only ("2018-2022 Camry"): either one is enough to keep it
        return Mark("drop", [f"neither {vehicle.make} nor {vehicle.model} is named"])
    reasons: list[str] = []
    if not model:
        reasons.append(f"{vehicle.model} is not named")
    year = year_fit(whole, vehicle.year)
    if year == "other years only":
        return Mark("drop", ["for other model years"])
    if year == "open range":
        reasons.append("open year range: may be another generation")
    elif year == "not stated":
        reasons.append("model year not stated" if vehicle.year else "the car's model year is not known")
    said = engines_said(whole)
    if said:
        mine = re.sub(r"[\s\-]", "", (vehicle.engine_code or "").upper())
        if mine and not any(mine.startswith(re.sub(r"-", "", e)) or re.sub(r"-", "", e).startswith(mine) for e in said):
            return Mark("drop", [f"for engine {', '.join(sorted(said))}"])
        if not mine:
            reasons.append(f"engine-specific ({', '.join(sorted(said))}); the car's engine is not known")
    return Mark("check" if reasons else "fits", reasons)


_MARKUP = re.compile(r"!\[[^\]]*\]\([^)]*\)|\[([^\]]*)\]\([^)]*\)|https?://\S+|<[^>]+>|[#*_`|>]{1,}")


def clean_excerpt(text: str, limit: int = 700) -> str:
    """Page text as plain words: no links, markup or tables, length capped. It is data for the model, never instructions."""
    text = _MARKUP.sub(lambda m: m.group(1) or " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " ..."
