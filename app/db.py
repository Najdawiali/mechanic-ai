"""Database connections."""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from app.config import get_settings


def connect(url: str | None = None) -> psycopg.Connection:
    """A connection whose rows are dicts. The caller closes it (use it as a context manager)."""
    return psycopg.connect(url or get_settings().database_url, row_factory=dict_row, connect_timeout=10)
