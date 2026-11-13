"""The extractors, one per kind of source file.

An extractor is added here when it is built. A file whose kind has no extractor yet fails its
job with a clear message, and can be processed again later (`python -m app.ingest.add --again`).
"""

from __future__ import annotations

from app.ingest.diagram_extractor import extract_diagram
from app.ingest.extractors import Extractor

EXTRACTORS: dict[str, Extractor] = {
    "licensed_diagram": extract_diagram,
}
