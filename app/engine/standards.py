"""General standard values the assistant may give when there is no verified record (decided by Ali, 2026-11-24).

Why: in the comparison with venorexai, our general answers refused even values that are the same on almost every
car (NET-006: the mechanic measured 120 ohm between OBD pins 6 and 14; the answer could not say that a healthy
high-speed CAN bus reads about 60 ohm there). The model still never writes a value of its own: only the entries
below, given to it as facts when the question is about their topic, and the checker allows their numbers only then.
Each is shown as "general standard, confirm in the service information for this car".

EVERY ENTRY IS A DRAFT until the client's expert (Nasoh) approves it: set approved=True per entry. Drafts are used
only where drafts are served (evaluation, KB_SERVE_DRAFTS=true), like knowledge-base records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_CAN_WORDS = (r"\bcan[\s-]?(?:bus|h|l|high|low|lines?|wires?|network|communication)\b|\bu0\d{3}\b|obd.?port|\bdlc\b|"
              r"pins?\s*6\b|pins?\s*14\b|\bnetwork\b|"
              r"كان ?باص|الشبكة|شبكة|بن ?6|بن ?14|نيتورك")
_DLC_WORDS = r"obd.?(?:ii|2)?\s*(?:port|connector|socket)|\bdlc\b|diagnostic\s+(?:port|connector)|pins?\s*(?:4|5|6|14|16)\b|منفذ|سوكيت ?الفحص|بن ?(?:4|5|6|14|16)"
_BATTERY_WORDS = (r"\b12\s*v\b|12.?volt|\b1[0-5]\.\d+\s*v(?:olts?)?\b|alternator|charging\s+system|battery\s+light|"
                  r"(?<!hybrid )(?<!traction )(?<!hv )\bbattery\b|"
                  r"بطارية|بطاريه|دينمو|دينامو|الدينمو|الشحن|بیٹری|الٹرنیٹر")
_LIN_WORDS = r"\blin\b|lin.?bus|لين ?باص"


@dataclass(frozen=True)
class Standard:
    key: str
    topic: str
    text: str  # English; the model writes it in the mechanic's language, numbers unchanged
    source: str
    triggers: str  # words in the mechanic's messages that make the entry relevant
    codes: str = r"(?!)"  # fault codes that make it relevant (regex on the code)
    approved: bool = False  # Nasoh's approval

    def relevant(self, text: str, codes: list[str]) -> bool:
        return bool(re.search(self.triggers, text, re.I)) or any(re.fullmatch(self.codes, c.upper()) for c in codes)


STANDARDS: tuple[Standard, ...] = (
    Standard("can_termination", "high-speed CAN bus",
             "A healthy high-speed CAN bus measures about 60 ohm between CAN-H and CAN-L (for example OBD port pins 6 and "
             "14) with the battery disconnected, because its two 120 ohm terminating resistors are in parallel. A measured "
             "120 ohm is NOT normal: it means one terminator or one branch of the bus is open. Near 0 ohm means CAN-H and "
             "CAN-L are shorted together.", "ISO 11898-2", _CAN_WORDS, r"U0\d{3}"),
    Standard("can_voltages", "high-speed CAN bus",
             "With the bus idle (recessive), CAN-H and CAN-L each sit at about 2.5 V to ground; when a module transmits "
             "(dominant), CAN-H rises to about 3.5 V and CAN-L falls to about 1.5 V. Use an oscilloscope to see the signal.",
             "ISO 11898-2", _CAN_WORDS, r"U0\d{3}"),
    Standard("dlc_pins", "OBD-II diagnostic port",
             "OBD-II diagnostic port pins: 4 chassis ground, 5 signal ground, 6 CAN-H, 14 CAN-L, 16 battery positive "
             "(7 is the K-line on cars that use it).", "SAE J1962", _DLC_WORDS + "|" + _CAN_WORDS),
    Standard("battery_rest", "12 V lead-acid battery",
             "A fully charged 12 V lead-acid battery reads about 12.6 V at rest (engine off for some hours, no load); "
             "about 12.4 V is roughly three quarters charged, and below about 12.0 V it is deeply discharged.",
             "general lead-acid battery practice", _BATTERY_WORDS, r"P05[6-9]\w|P062\w|P065B"),
    Standard("charging_voltage", "charging system",
             "With the engine running and the charging system working, most cars hold about 13.5 to 14.7 V at the battery. "
             "Cars with smart (engine-computer controlled) charging may hold a lower voltage at times by design, so a single "
             "low reading on those cars must be checked against the service information before blaming the alternator.",
             "general charging-system practice", _BATTERY_WORDS, r"P05[6-9]\w|P062\w|P065B"),
    Standard("lin_levels", "LIN bus",
             "A LIN bus is a single wire: idle (recessive) it sits near battery voltage, and it is pulled near 0 V while a "
             "module transmits; one module is the master.", "ISO 17987 (LIN)", _LIN_WORDS),
)


def relevant(text: str, codes: list[str], *, drafts: bool) -> list[Standard]:
    return [s for s in STANDARDS if (s.approved or drafts) and s.relevant(text, codes)]


def as_facts(entries: list[Standard]) -> dict[str, str]:
    return {f"{s.topic} ({s.source})": s.text for s in entries}
