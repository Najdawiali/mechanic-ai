"""The extractors, one per kind of source file.

An extractor is added here when it is built. A file whose kind has no extractor yet fails its
job with a clear message, and can be processed again later (`python -m app.ingest.add --again`).
"""

from __future__ import annotations

from app.ingest.case_extractor import extract_case
from app.ingest.diagram_extractor import extract_diagram
from app.ingest.extractors import Extractor
from app.ingest.procedure_extractor import extract_procedure

EXTRACTORS: dict[str, Extractor] = {
    "licensed_diagram": extract_diagram,      # code only
    "licensed_procedure": extract_procedure,  # calls the vision and reasoning models
    "client_case_file": extract_case,         # calls the reasoning model
}
