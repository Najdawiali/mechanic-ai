"""A solved-case report (text or PDF) -> one Case record.

The reports are free text written from a technician's notes, so a model reads them. As with the
procedures, code then checks the result against the report itself, with no answer key: every
fault code, the make, each model and each year in the record must be written in the report.
What the report does not say stays empty: the year of the car is never guessed.

The record is always stored as a draft. Findings for the reviewer are written into it:
"CHECK FAILED" for something in the record that the report does not back up, "CHECK" for
something in the report that the record may have missed.
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from app.ingest.extractors import Extracted, SourceDocument
from app.ingest.jobs import PermanentError
from app.ingest.pdf_text import read_pdf_text
from app.ingest.procedure_extractor import _json
from app.kb.keys import code_stem, norm_key
from app.kb.schemas import Case, Review, Source

OBD_CODE = re.compile(r"\b[PCBU][0-9A-F]{4}(?:[0-9A-F]{2})?\b")
ALERT_NAME = re.compile(r"\b[A-Z][A-Za-z0-9]{1,9}_[aAwW]\d{3}(?:_[A-Za-z0-9_]+)?\b")  # TAS_a211_noRideHeightCalib

WRITE_PROMPT = """You convert a workshop's report of one solved repair into JSON. The report is given below.

Rules:
- Use only what the report says. Never add knowledge of your own. If the report does not give
  something, use null or an empty list. Never guess a year, a model or a fault code.
- Copy fault codes character for character, exactly as written in the report.
- "models", "year_from" and "year_to" describe the car or cars that were actually repaired. A wider
  range that the technician says the fix applies to goes into "occurrences" as a sentence, not
  into the years. If the report does not give the year of the repaired car, both years are null.
- "findings" are the checks that were done and what each showed, one per item.
- "parts_not_replaced" are parts that were checked or suspected and found good.
- "outcome": "confirmed_fixed" if the report says the fault was gone after the repair and that was
  verified; "fixed_not_verified" if a repair was done with no verification reported; "expected" if
  the fix is the technician's expectation; "not_fixed" if the fault remained.
- "sensitive" is true if the report describes work inside a high-voltage battery or any step that
  is dangerous without special training.
- "specific_car" is false if the report is general advice and describes no particular car.
- "missing" lists which of these the report does not give: "model", "year", "fault code", "scan tool", "outcome".

Answer with JSON only, in this shape:
{"make": "...", "models": ["..."], "year_from": number or null, "year_to": number or null,
 "powertrain": "ICE" | "Hybrid" | "PHEV" | "EV" | null, "engine": string or null, "system": string or null,
 "modules": ["..."],
 "fault_codes": [{"code": "...", "text_on_tool": string or null, "tool": string or null, "module": string or null, "tool_note": string or null}],
 "symptoms": ["..."], "findings": ["..."], "diagnosis": "...", "root_cause": string or null,
 "repair_steps": ["..."], "parts_replaced": [{"part": "...", "part_source": "OEM" | "used" | "aftermarket" | "unknown"}],
 "parts_not_replaced": ["..."], "outcome": "...", "occurrences": ["..."], "sensitive": false,
 "technician": string or null, "date": "YYYY-MM-DD" or null,
 "specific_car": true, "missing": ["..."]}"""


def report_text(document: SourceDocument) -> tuple[str, int]:
    """The report's text, and how many images it carries (screenshots are not read here)."""
    if document.original_name.lower().endswith(".pdf") or document.data.startswith(b"%PDF"):
        try:
            pdf = read_pdf_text(document.data)
        except Exception as exc:  # noqa: BLE001
            raise PermanentError(f"the file could not be read as a PDF: {exc}") from None
        return re.sub(r"\[image \d+\]", "[screenshot]", pdf.text), len(pdf.images)
    for encoding in ("utf-8-sig", "utf-16", "cp1256"):
        try:
            return document.data.decode(encoding), 0
        except UnicodeDecodeError:
            continue
    raise PermanentError("the file is neither a PDF nor readable text")


def _fixed(document: SourceDocument, case_id: str = "check") -> dict:
    return {"case_id": case_id, "source": Source(kind="client_case_file", file=document.original_name, licensed=False)}


def write_case(gateway, text: str, document: SourceDocument, *, attempts: int = 2) -> dict:
    messages = [{"role": "system", "content": WRITE_PROMPT}, {"role": "user", "content": text}]
    problem = "no attempt made"
    for _ in range(attempts):
        reply = gateway.chat(messages, role="reasoning", json_mode=True, max_tokens=4000).text
        try:
            data = _json(reply)
            Case(**{k: v for k, v in data.items() if k not in ("specific_car", "missing")}, **_fixed(document))
            return data
        except (ValueError, json.JSONDecodeError, ValidationError) as exc:
            problem = str(exc)[:1500]
            messages = messages[:2] + [{"role": "assistant", "content": reply},
                                       {"role": "user", "content": f"That answer was not accepted: {problem}\nSend the corrected JSON only."}]
    raise RuntimeError(f"the model did not produce a valid case: {problem}")


def check_case(case: Case, text: str) -> list[str]:
    """What the report does not back up ("CHECK FAILED") and what the record may have missed ("CHECK")."""
    notes: list[str] = []
    flat = norm_key(text) or ""
    for fault in case.fault_codes:
        if (norm_key(fault.code) or "?") not in flat:
            notes.append(f"CHECK FAILED: the fault code {fault.code} is not written in the report")
    if (norm_key(case.make) or "?") not in flat:
        notes.append(f"CHECK FAILED: the make {case.make!r} is not written in the report")
    for model in case.models:
        if (norm_key(model) or "?") not in flat:
            notes.append(f"CHECK FAILED: the model {model!r} is not written in the report")
    for year in {case.year_from, case.year_to} - {None}:
        if str(year) not in text:
            notes.append(f"CHECK FAILED: the year {year} is not written in the report")
    if case.technician and (norm_key(case.technician) or "?") not in flat:
        notes.append(f"CHECK FAILED: the technician name {case.technician!r} is not written in the report")
    recorded = {code_stem(fault.code) for fault in case.fault_codes}
    for found in dict.fromkeys(OBD_CODE.findall(text) + ALERT_NAME.findall(text)):
        if code_stem(found) not in recorded:
            notes.append(f"CHECK: the report mentions {found}, which is not among the record's fault codes")
    return notes


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def extract_case(document: SourceDocument, gateway=None) -> list[Extracted]:
    text, images = report_text(document)
    if len(text.strip()) < 100:
        raise PermanentError("the report has almost no text")
    if gateway is None:
        from app.llm.gateway import LLMGateway

        gateway = LLMGateway()
    data = write_case(gateway, text, document)
    specific, missing = data.pop("specific_car", True), data.pop("missing", []) or []

    car = "-".join(filter(None, [_slug(data["make"]), _slug(" ".join(data.get("models") or [])[:40]),
                                 str(data.get("year_from") or "year-unknown")]))
    codes = "-".join(_slug(fault["code"]) for fault in data.get("fault_codes") or [])[:60]
    case_id = f"case:{car}:{codes or _slug(document.original_name.rsplit('.', 1)[0])[:60]}"
    case = Case(**data, **_fixed(document, case_id))

    questions = check_case(case, text)
    if not specific:
        questions.append("The report describes no particular car: it may belong with the expert notes, not the cases.")
    if missing:
        questions.append("The report does not give: " + ", ".join(str(m) for m in missing) + ".")
    if images:
        questions.append(f"The file has {images} screenshot(s) that were not read. Compare the codes and the vehicle on them with the record.")
    if case.sensitive:
        questions.append("This case describes dangerous work (for example inside a high-voltage battery). "
                         "The owner must approve in writing before mechanics see it.")
    return [Extracted(case.model_copy(update={"review": Review(open_questions=questions)}), "model")]
