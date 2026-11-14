"""What an extractor is: a function that turns one source file into draft records.

The extractors themselves are listed in app/ingest/registry.py.
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
class Asset:
    """A file made from the source (a page image) that goes into storage beside the record."""

    key: str
    data: bytes
    content_type: str


@dataclass(frozen=True)
class Extracted:
    record: KbRecord
    extracted_by: ExtractedBy  # "code" or "model"
    assets: tuple[Asset, ...] = ()


Extractor = Callable[[SourceDocument], list[Extracted]]
Classifier = Callable[[SourceDocument], str | None]
