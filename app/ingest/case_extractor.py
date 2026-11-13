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
- Copy fault codes character for character, exactly as written in the report. "code" is a code
  number or an alert name only (P0010, TAS_a211_noRideHeightCalib). If the report gives only the
  wording of a fault, put the wording in "text_on_tool" and use null for "code".
- A model is a model name only. If the report does not name the model, use null. Words such as
  "Hybrid" or "EV" are not a model.
- "powertrain" only if the report states it in words; do not work it out from the model name.
- "scan_tools" are the diagnostic tools the report names by name (Autel, Tesla Toolbox, FORScan).
  A general word such as "scan tool" is not a name.
- "cars" has one entry for each car that was actually repaired, with its model and its year. If the
  report does not give a car's year, its year is null. If the report does not say which single car
  was repaired but names the model or models it is about (for example "Model 3 / Model Y"), give
  one entry per named model with year null. A wider range that the technician only says the fix
  also applies to goes into "occurrences" as a sentence, never into "cars".
- "findings" are the checks that were done and what each showed, one per item.
- "parts_not_replaced" are parts that were checked or suspected and found good.
- "outcome": "confirmed_fixed" if the report says the fault was gone after the repair and that was
  verified; "fixed_not_verified" if a repair was done with no verification reported; "expected" if
  the fix is the technician's expectation; "not_fixed" if the fault remained.
- "sensitive" is true if the report describes work inside a high-voltage battery or any step that
  is dangerous without special training.
- "specific_car" is false if the report is general advice and describes no particular car.

Answer with JSON only, in this shape:
{"make": "...", "cars": [{"model": string or null, "year": number or null}],
 "powertrain": "ICE" | "Hybrid" | "PHEV" | "EV" | null, "engine": string or null, "system": string or null,
 "modules": ["..."], "scan_tools": ["..."],
 "fault_codes": [{"code": string or null, "text_on_tool": string or null, "tool": string or null, "module": string or null, "tool_note": string or null}],
 "symptoms": ["..."], "findings": ["..."], "diagnosis": "...", "root_cause": string or null,
 "repair_steps": ["..."], "parts_replaced": [{"part": "...", "part_source": "OEM" | "used" | "aftermarket" | "unknown"}],
 "parts_not_replaced": ["..."], "outcome": "...", "occurrences": ["..."], "sensitive": false,
 "technician": string or null, "date": "YYYY-MM-DD" or null,
 "specific_car": true}"""


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


NOT_ENGLISH = "the report is not in English: only the English reports are used"


def _mostly_arabic(text: str) -> bool:
    letters = [ch for ch in text if ch.isalpha()]
    return bool(letters) and sum("\u0600" <= ch <= "\u06ff" for ch in letters) / len(letters) > 0.15


def _fixed(document: SourceDocument, case_id: str = "check") -> dict:
    return {"case_id": case_id, "source": Source(kind="client_case_file", file=document.original_name, licensed=False)}


def write_case(gateway, text: str, document: SourceDocument, *, attempts: int = 2) -> dict:
    messages = [{"role": "system", "content": WRITE_PROMPT}, {"role": "user", "content": text}]
    problem = "no attempt made"
    for _ in range(attempts):
        reply = gateway.chat(messages, role="reasoning", json_mode=True, max_tokens=4000).text
        try:
            data = _json(reply)
            tidy(data)
            Case(**{k: v for k, v in data.items() if k != "specific_car"}, **_fixed(document))
            return data
        except (ValueError, json.JSONDecodeError, ValidationError) as exc:
            problem = str(exc)[:1500]
            messages = messages[:2] + [{"role": "assistant", "content": reply},
                                       {"role": "user", "content": f"That answer was not accepted: {problem}\nSend the corrected JSON only."}]
    raise RuntimeError(f"the model did not produce a valid case: {problem}")


LOOKS_LIKE_A_CODE = re.compile(r"(?=.*\d)[A-Za-z0-9][A-Za-z0-9_.:\-]{2,49}")
NOT_A_MODEL = {"HYBRID", "PHEV", "EV", "BEV", "ELECTRIC", "PETROL", "GASOLINE", "DIESEL", "ALLMODELS", "UNKNOWN"}
NOT_A_TOOL_NAME = {"SCANTOOL", "SCANNER", "DIAGNOSTICTOOL", "DIAGNOSTICSCANNER", "KEYPROGRAMMINGTOOL", "TOOL"}
POWERTRAIN_WORDS = {"EV": r"\b(EV|BEV)\b|electric", "Hybrid": r"hybrid", "PHEV": r"plug-?in|\bPHEV\b",
                    "ICE": r"petrol|gasoline|diesel|combustion"}


def tidy(data: dict) -> None:
    """Corrections made in code, the same way every time, whatever the model's habits.

    A wording is not a code: "Voltage sampling abnormal" moves from `code` to `text_on_tool`.
    A powertrain word is not a model: "Hybrid" is dropped from `models`.
    """
    for fault in data.get("fault_codes") or []:
        code = fault.get("code")
        if isinstance(code, str) and not LOOKS_LIKE_A_CODE.fullmatch(code.strip()):
            fault["text_on_tool"], fault["code"] = fault.get("text_on_tool") or code.strip(), None
        if not fault.get("code"):  # a code written inside a wording ("TAS_A025_Crash Event") is still a code
            inside = ALERT_NAME.search(str(fault.get("text_on_tool") or "")) or OBD_CODE.search(str(fault.get("text_on_tool") or ""))
            if inside:
                fault["code"] = inside.group(0)

    data["scan_tools"] = [t for t in data.get("scan_tools") or [] if isinstance(t, str) and (norm_key(t) or "") not in NOT_A_TOOL_NAME | {""}]

    # The cars repaired, as model-and-year pairs; models and the year range are derived from them.
    cars = data.pop("cars", None)
    if cars is None:  # an answer in the older shape
        years = {data.get("year_from"), data.get("year_to")}
        cars = [{"model": m, "year": years.pop() if len(years) == 1 else None} for m in data.get("models") or []]
    kept = []
    for car in cars or []:
        model = car.get("model") if isinstance(car, dict) else None
        model = model if isinstance(model, str) and (norm_key(model) or "") not in NOT_A_MODEL | {""} else None
        year = car.get("year") if isinstance(car, dict) and isinstance(car.get("year"), int) else None
        if (model or year) and {"model": model, "year": year} not in kept:
            kept.append({"model": model, "year": year})
    data["repaired_cars"] = kept
    data["models"] = list(dict.fromkeys(car["model"] for car in kept if car["model"]))
    known_years = [car["year"] for car in kept if car["year"]]
    data["year_from"], data["year_to"] = (min(known_years), max(known_years)) if known_years else (None, None)


def check_case(case: Case, text: str) -> list[str]:
    """What the report does not back up ("CHECK FAILED") and what the record may have missed ("CHECK")."""
    notes: list[str] = []
    flat = norm_key(text) or ""
    for fault in case.fault_codes:
        if fault.code and (norm_key(fault.code) or "?") not in flat:
            notes.append(f"CHECK FAILED: the fault code {fault.code} is not written in the report")
        if fault.text_on_tool and (norm_key(fault.text_on_tool) or "?") not in flat:
            notes.append(f"CHECK FAILED: the fault wording {fault.text_on_tool!r} is not written in the report")
    for tool in case.scan_tools:
        if (norm_key(tool) or "?") not in flat:
            notes.append(f"CHECK FAILED: the scan tool {tool!r} is not written in the report")
    if case.powertrain and not re.search(POWERTRAIN_WORDS[case.powertrain.value], text, re.I):
        notes.append(f"CHECK FAILED: the report does not state the powertrain; the record says {case.powertrain.value}")
    if (norm_key(case.make) or "?") not in flat:
        notes.append(f"CHECK FAILED: the make {case.make!r} is not written in the report")
    for model in case.models:
        if (norm_key(model) or "?") not in flat:
            notes.append(f"CHECK FAILED: the model {model!r} is not written in the report")
    for year in {car.year for car in case.repaired_cars} | {case.year_from, case.year_to} - {None}:
        if year is None:
            continue
        if str(year) not in text:
            notes.append(f"CHECK FAILED: the year {year} is not written in the report")
    if case.technician and (norm_key(case.technician) or "?") not in flat:
        notes.append(f"CHECK FAILED: the technician name {case.technician!r} is not written in the report")
    recorded = {code_stem(fault.code) for fault in case.fault_codes if fault.code}
    for found in dict.fromkeys(OBD_CODE.findall(text) + ALERT_NAME.findall(text)):
        if code_stem(found) not in recorded:
            notes.append(f"CHECK: the report mentions {found}, which is not among the record's fault codes")
    return notes


def case_name(file_name: str) -> str:
    """A report's name without what differs between copies of it: the extension, a language
    suffix, a "(1)" added by a download."""
    stem = file_name.rsplit(".", 1)[0]
    stem = re.sub(r"\s*\(\d+\)\s*$", "", stem)
    stem = re.sub(r"[_\- ](EN|AR)$", "", stem, flags=re.I)
    return _slug(stem)[:100] or "unnamed"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def extract_case(document: SourceDocument, gateway=None) -> list[Extracted]:
    text, images = report_text(document)
    if len(text.strip()) < 100:
        raise PermanentError("the report has almost no text")
    if _mostly_arabic(text):
        raise PermanentError(NOT_ENGLISH)
    if gateway is None:
        from app.llm.gateway import LLMGateway

        gateway = LLMGateway()
    data = write_case(gateway, text, document)
    specific = data.pop("specific_car", True)

    # The record is named after its file, not after what the model read: reading the same report
    # again must update the same record, and the model's reading of the car varies between runs.
    case_id = f"case:{case_name(document.original_name)}"
    # Dangerous work is flagged by the model's reading or by the report's own words, whichever says so:
    # on two real runs the model's flag for the same report changed.
    data["sensitive"] = bool(data.get("sensitive")) or bool(re.search(r"high[- ]voltage|\bHV\b", text, re.I))
    case = Case(**data, **_fixed(document, case_id))

    questions = check_case(case, text)
    if not specific:
        questions.append("The report describes no particular car: it may belong with the expert notes, not the cases.")
    # What a complete report should carry, worked out from the record itself.
    missing = [name for name, absent in (
        ("model", not case.models), ("year", case.year_from is None and case.year_to is None),
        ("fault code", not any(fault.code for fault in case.fault_codes)), ("scan tool", not case.scan_tools),
    ) if absent]
    if missing:
        questions.append("The report does not give: " + ", ".join(missing) + ".")
    if images:
        questions.append(f"The file has {images} screenshot(s) that were not read. Compare the codes and the vehicle on them with the record.")
    if case.sensitive:
        questions.append("This case describes dangerous work (for example inside a high-voltage battery). "
                         "The owner must approve in writing before mechanics see it.")
    return [Extracted(case.model_copy(update={"review": Review(open_questions=questions)}), "model")]
