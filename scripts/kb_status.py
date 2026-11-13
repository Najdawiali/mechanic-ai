"""What is in the knowledge base: records by kind and status, files and jobs.  Run:  python scripts/kb_status.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect  # noqa: E402


def main() -> int:
    with connect() as conn:
        print("Records")
        for row in conn.execute("SELECT record_type, status, count(*) AS n FROM kb.record WHERE withdrawn_at IS NULL "
                                "GROUP BY 1, 2 ORDER BY 1, 2").fetchall():
            print(f"  {row['record_type']:<12} {row['status']:<9} {row['n']}")
        units = conn.execute("SELECT count(*) AS n, count(embedding) AS e FROM kb.search_unit").fetchone()
        pins = conn.execute("SELECT count(*) AS n FROM kb.diagram_pin").fetchone()["n"]
        print(f"Search pieces: {units['n']} ({units['e']} with a vector)   Diagram pins: {pins}")
        print("Files and jobs")
        for row in conn.execute("SELECT COALESCE(f.kind, 'unknown kind') AS kind, j.status, count(*) AS n FROM kb.ingest_job j "
                                "JOIN kb.source_file f ON f.id = j.source_file_id GROUP BY 1, 2 ORDER BY 1, 2").fetchall():
            print(f"  {row['kind']:<20} {row['status']:<10} {row['n']}")
        failed = conn.execute("SELECT f.original_name, j.error FROM kb.ingest_job j JOIN kb.source_file f ON f.id = j.source_file_id "
                              "WHERE j.status = 'failed' AND j.id = (SELECT max(id) FROM kb.ingest_job x WHERE x.source_file_id = f.id) "
                              "ORDER BY 1").fetchall()
        for row in failed:
            print(f"  FAILED  {row['original_name'][:60]}: {(row['error'] or '')[:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
