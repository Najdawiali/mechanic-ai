"""The extractors: one per kind of source file. Each turns a file into draft records.

An extractor is registered here when it is built. A file whose kind has no extractor yet fails
its job with a clear message, and can be processed again later.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.kb.index import KbRecord
from app.kb.store import ExtractedBy


@dataclass(frozen=True)
class SourceDocument:
    file_id: int
    storage_key: str
    original_name: str
    kind: str | None
    declared: dict  # what the uploader said: make, model, year, system
    data: bytes


@dataclass(frozen=True)
class Extracted:
    record: KbRecord
    extracted_by: ExtractedBy  # "code" or "model"


Extractor = Callable[[SourceDocument], list[Extracted]]
Classifier = Callable[[SourceDocument], str | None]

EXTRACTORS: dict[str, Extractor] = {}
