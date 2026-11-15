"""Reading fault codes from an attached scan report: a photo or screenshot, or a PDF.

The model reads the picture (or the PDF's text) into modules and codes. Code then keeps only what
looks like a fault code, drops modules that show no code, and the reply lists the codes back to
the mechanic to confirm (first real photo test: both models read the codes exactly, but made
character slips in the free text, so codes are checked by pattern and descriptions are never
trusted character for character).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

import pymupdf

from app.engine.intake import codes_in
from app.engine.model import ModelClient, ModelError
from app.llm.gateway import image_part_from_bytes


@dataclass
class Attachment:
    name: str
    content_type: str
    data: bytes


@dataclass
class ReportReading:
    modules: list[tuple[str, list[str]]] = field(default_factory=list)  # (module, codes)
    vehicle: str | None = None

    @property
    def codes(self) -> list[str]:
        return list(dict.fromkeys(code for _, codes in self.modules for code in codes))


SYSTEM = """You read a car scan-tool report (a photo, a screenshot, or the text of a PDF) and return JSON.
List each control module that shows at least one fault code, with its codes exactly as printed.
Do not list a module that shows no code, or whose codes you cannot see. Never guess a character you cannot read.
vehicle: the vehicle name or model as printed on the report, or null."""

SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["vehicle", "modules"],
    "properties": {
        "vehicle": {"type": ["string", "null"]},
        "modules": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["module", "codes"],
            "properties": {"module": {"type": "string"}, "codes": {"type": "array", "items": {"type": "string"}}}}},
    },
}


def _pdf_text(data: bytes) -> str:
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        return "\n".join(page.get_text() for page in pdf)[:12000]


def read(model: ModelClient, attachments: list[Attachment]) -> ReportReading:
    reading = ReportReading()
    for attachment in attachments:
        if attachment.content_type == "application/pdf":
            text = _pdf_text(attachment.data)
            user, role = f"Report text:\n{text}", "fast"
        elif attachment.content_type.startswith("image/"):
            user, role = [image_part_from_bytes(attachment.data, attachment.content_type),
                          {"type": "text", "text": "Read this scan report."}], "vision"
        else:
            continue
        try:
            out = model.json("report", role=role, system=SYSTEM, user=user, schema=SCHEMA, max_tokens=1200)
        except ModelError:
            continue
        if isinstance(out.get("vehicle"), str) and out["vehicle"].strip() and not reading.vehicle:
            reading.vehicle = out["vehicle"].strip()
        for item in out.get("modules") or []:
            if not isinstance(item, dict) or not isinstance(item.get("module"), str):
                continue
            codes = [c for raw in item.get("codes") or [] if isinstance(raw, str) for c in codes_in(raw)]
            if codes:
                reading.modules.append((item["module"].strip(), codes))
    return reading


def decode(item: dict) -> Attachment:
    """An attachment as the API receives it: {"name", "content_type", "data_base64"}."""
    return Attachment(item.get("name") or "attachment", item.get("content_type") or "application/octet-stream",
                      base64.b64decode(item["data_base64"]))
