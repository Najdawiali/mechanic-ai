"""The search query, built in code from the vehicle and one fault code: nothing else goes to the provider.

Search providers keep what they are sent (Exa's terms give it a perpetual licence to queries and
results), so a query never carries the mechanic's words, a VIN, a name, a phone number or any
customer data. Make, model and engine are cut to letters, digits and a few signs; the code must
look like a fault code or an alert name.
"""

from __future__ import annotations

import re

from app.kb.schemas import Vehicle

_CODE = re.compile(r"^(?:[PBCU][0-9][0-9A-F]{3}(?:[0-9A-F]{2})?|[A-Z][A-Z0-9]{1,9}_[AWF]\d{3})$", re.IGNORECASE)
_UNSAFE = re.compile(r"[^\w .+\-/]")


def _safe(text: str | None, limit: int = 40) -> str:
    return re.sub(r"\s+", " ", _UNSAFE.sub(" ", text or "")).strip()[:limit]


def valid_code(code: str) -> bool:
    return bool(_CODE.match(code.strip()))


def build(vehicle: Vehicle, code: str) -> tuple[str, str] | None:
    """(query, what the highlights should be about), or None when the code does not look like a fault code."""
    code = code.strip()
    if not valid_code(code):
        return None
    parts = [str(vehicle.year) if vehicle.year else "", _safe(vehicle.make), _safe(vehicle.model), _safe(vehicle.engine_code, 20), code]
    query = " ".join(p for p in parts if p) + " meaning causes diagnosis"
    return query, f"{code} meaning causes test"


_VIN = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b", re.IGNORECASE)
_PHONE = re.compile(r"\+?\d[\d\s\-]{6,}\d")
_EMAIL = re.compile(r"\S+@\S+")


def build_symptom(vehicle: Vehicle, symptom: str) -> tuple[str, str] | None:
    """(query, highlight) for a question with no fault code: the car and the symptom in English, cleaned of anything
    that could identify a person or a car (VIN, phone numbers, emails), cut to a short query. None when nothing is left."""
    text = _EMAIL.sub(" ", _PHONE.sub(" ", _VIN.sub(" ", symptom or "")))
    text = _safe(text, 140)
    if len(text) < 8:
        return None
    parts = [str(vehicle.year) if vehicle.year else "", _safe(vehicle.make), _safe(vehicle.model), _safe(vehicle.engine_code, 20), text]
    return " ".join(p for p in parts if p) + " causes diagnosis", f"{text} causes diagnosis"
