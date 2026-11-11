"""Check that Postgres is up and pgvector works.  Run:  python scripts/check_db.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402

from app.config import get_settings  # noqa: E402


def main() -> int:
    url = get_settings().database_url
    try:
        with psycopg.connect(url, connect_timeout=5) as conn:
            pg = conn.execute("SHOW server_version").fetchone()[0]
            row = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
            if row is None:
                print(f"FAIL  Postgres {pg} is up, but the 'vector' extension is not installed.")
                return 1
            # A real vector operation, not just a version lookup.
            dist = conn.execute("SELECT '[1,2,3]'::vector <-> '[1,2,4]'::vector").fetchone()[0]
            schemas = [
                r[0]
                for r in conn.execute(
                    "SELECT schema_name FROM information_schema.schemata "
                    "WHERE schema_name IN ('kb', 'ai_state') ORDER BY 1"
                )
            ]
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  cannot use the database: {type(exc).__name__}: {exc}")
        return 1

    print(f"PASS  Postgres {pg}, pgvector {row[0]}, test distance = {dist}, schemas = {schemas}")
    return 0 if schemas == ["ai_state", "kb"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
