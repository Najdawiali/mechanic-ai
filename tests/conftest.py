"""Shared fixtures. Tests that need PostgreSQL get a throw-away database with the migrations applied.

Where the server comes from, first that works:
  1. TEST_DATABASE_URL, if set
  2. DATABASE_URL from the settings (the Docker Compose database)
  3. a temporary local server, when the optional package `pixeltable-pgserver` is installed
With none of these the database tests are skipped, and the offline tests still run.
Your own data is never touched: each run creates a new database and drops it at the end.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

REPO = Path(__file__).resolve().parents[1]


def _reachable(url: str) -> bool:
    try:
        with psycopg.connect(url, connect_timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


def _with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, parts.fragment))


@pytest.fixture(scope="session")
def _server_url(tmp_path_factory) -> str:
    from app.config import get_settings

    for url in (os.environ.get("TEST_DATABASE_URL"), get_settings().database_url):
        if url and _reachable(url):
            yield url
            return
    try:
        import pixeltable_pgserver as pgserver
    except ImportError:
        pytest.skip("no PostgreSQL available: start it with `docker compose up -d`")
    server = pgserver.get_server(tmp_path_factory.mktemp("pg"))
    try:
        yield server.get_uri()
    finally:
        server.cleanup()


@pytest.fixture(scope="session")
def database_url(_server_url: str) -> str:
    """A new, empty database with every migration applied."""
    from alembic import command
    from alembic.config import Config

    name = f"mechanic_ai_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(_server_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = _with_database(_server_url, name)
    try:
        cfg = Config(str(REPO / "alembic.ini"))
        cfg.set_main_option("script_location", str(REPO / "migrations"))
        cfg.attributes["database_url"] = url
        cfg.attributes["configure_logging"] = False
        command.upgrade(cfg, "head")
        yield url
    finally:
        with psycopg.connect(_server_url, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def conn(database_url: str):
    """A connection to the test database, with the knowledge base emptied first."""
    from app.db import connect

    with connect(database_url) as connection:
        connection.execute("TRUNCATE kb.record RESTART IDENTITY CASCADE")
        connection.commit()
        yield connection
