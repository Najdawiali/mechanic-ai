"""Which public records belong to the mechanic's car: make group, model words, model year.

The imported NHTSA records name a make GROUP, not always the brand: the source import put sibling brands
under one name (an Acura is stored as Honda, a Lexus as Toyota, a GMC as GM). So the car's make is mapped to
the same group before a lookup, and the model then tells the cars apart. Makes outside the list are their own
group; NHTSA holds US-market makes only, so most Chinese makes simply have no records.
"""

from __future__ import annotations

import re
from typing import Literal

# The grouping used by the imported snapshot (venorexai app/rag/nhtsa.py, read 2026-11-24): brand -> group.
MAKE_GROUP = {
    "TOYOTA": "TOYOTA", "LEXUS": "TOYOTA", "NISSAN": "NISSAN", "INFINITI": "NISSAN", "HYUNDAI": "HYUNDAI",
    "GENESIS": "HYUNDAI", "KIA": "KIA", "FORD": "FORD", "LINCOLN": "FORD", "CHEVROLET": "CHEVROLET",
    "GMC": "GM", "CADILLAC": "GM", "BUICK": "GM", "GM": "GM", "HONDA": "HONDA", "ACURA": "HONDA",
    "MITSUBISHI": "MITSUBISHI", "MAZDA": "MAZDA", "JEEP": "JEEP", "DODGE": "JEEP", "CHRYSLER": "JEEP", "RAM": "JEEP",
    "BMW": "BMW", "MERCEDES-BENZ": "MERCEDES-BENZ", "MERCEDES": "MERCEDES-BENZ", "VOLKSWAGEN": "VOLKSWAGEN",
    "VW": "VOLKSWAGEN", "AUDI": "AUDI", "LAND ROVER": "LAND ROVER", "PORSCHE": "PORSCHE", "TESLA": "TESLA",
    "SUBARU": "SUBARU", "VOLVO": "VOLVO",
}

Applies = Literal["fits", "check"]


def make_key(make: str | None) -> str | None:
    words = re.sub(r"\s+", " ", (make or "").strip().upper())
    return MAKE_GROUP.get(words, words) or None


def model_key(model: str | None) -> str | None:
    """'ID.4' -> 'ID 4'; 'CS75 Plus' -> 'CS75 PLUS'; '(...)' notes dropped. Same rule for stored and asked models."""
    m = re.sub(r"\s*\(.*?\)", "", model or "").upper()
    m = re.sub(r"[^A-Z0-9 ]+", " ", m)
    return re.sub(r"\s+", " ", m).strip() or None


def model_applies(car: str, record: str) -> Applies | None:
    """Same model -> fits. One is the other plus words ('CAMRY' / 'CAMRY HYBRID') -> check: it may be another
    version of the car. Anything else -> None (another car: never offered)."""
    if car == record:
        return "fits"
    if record.startswith(car + " ") or car.startswith(record + " "):
        return "check"
    return None


def year_applies(year: int | None, year_from: int | None, year_to: int | None) -> Applies | None:
    if year is None or year_from is None:
        return "check"  # the car's year or the record's years are not known
    return "fits" if year_from <= year <= (year_to or year_from) else None


def applies(car_model: str, year: int | None, record_model: str, year_from: int | None, year_to: int | None) -> Applies | None:
    m, y = model_applies(car_model, record_model), year_applies(year, year_from, year_to)
    if m is None or y is None:
        return None
    return "fits" if m == y == "fits" else "check"
