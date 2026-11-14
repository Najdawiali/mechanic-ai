"""Normalised keys for exact matching.

Vehicles and fault codes are matched exactly, never by similarity. People write the same thing
in different ways (`ID.4` / `id4`, `TAS_a211` / `tas a211`), so both sides are reduced to a key
before they are compared. Two different codes never share a key: `P0010` and `P0011` stay apart,
and so do `P0C4400` and `P0C4411`.
"""

from __future__ import annotations

import re

_NOT_ALNUM = re.compile(r"[^A-Z0-9]+")

# A Tesla alert name: module, then a letter and three digits, then an optional description.
#   TAS_a211_noRideHeightCalib -> module TAS, id a211
# The description varies between tools and files, so two alerts are the same alert when the
# module and the id agree.
_TESLA_ALERT = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*)[\s_\-]+([A-Za-z]\d{3})(?=$|[\s_\-])")


def norm_key(text: str | None) -> str | None:
    """Upper case, letters and digits only. `Qin Plus DM-i` -> `QINPLUSDMI`. Empty -> None."""
    if text is None:
        return None
    key = _NOT_ALNUM.sub("", text.upper())
    return key or None


def code_key(code: str) -> str:
    """The whole code as a key. Raises on a code with no letters or digits."""
    key = norm_key(code)
    if key is None:
        raise ValueError(f"not a fault code: {code!r}")
    return key


def code_stem(code: str) -> str:
    """What lookups compare.

    For a Tesla alert name it is module + id (`TASA211`), with or without the description.
    For every other code it is the whole key: nothing is shortened, so a 7-character code
    (`P0C4400`) never matches its 5-character base (`P0C44`) or a sibling (`P0C4411`).
    """
    alert = _TESLA_ALERT.match(code)
    if alert:
        return (alert.group(1) + alert.group(2)).upper()
    return code_key(code)
