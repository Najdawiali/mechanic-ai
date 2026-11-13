"""Put local files into storage and queue them for processing.

  python -m app.ingest.add --kind licensed_diagram "C:/path/to/diagrams"
  python -m app.ingest.add --kind client_case_file report.pdf --make Tesla --model "Model 3"

This is the local way in. On the platform, the backend uploads the file to the shared bucket
and calls the API with its storage key; both paths end in the same register step.
"""

from __future__ import annotations

import argparse
import mimetypes
from pathlib import Path

from app.db import connect
from app.ingest import jobs
from app.kb.keys import norm_key
from app.storage import get_store

SUFFIXES = {".pdf", ".txt", ".png", ".jpg", ".jpeg"}


def storage_key_for(kind: str, path: Path) -> str:
    """Stable for a given file, so adding it again finds it, and different for the same file
    name under another car: the last five folder names are part of the key
    (year / make / model / engine / diagrams)."""
    folders = [norm_key(part) or "_" for part in path.resolve().parent.parts[-5:]]
    return "/".join(["kb", kind, *folders, path.name])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", type=Path, help="files or folders")
    parser.add_argument("--kind", required=True, choices=["licensed_procedure", "licensed_diagram", "client_case_file", "expert_transcript"])
    parser.add_argument("--again", action="store_true", help="process the files again even if they have not changed")
    for field in ("make", "model", "system"):
        parser.add_argument(f"--{field}")
    parser.add_argument("--year", type=int)
    args = parser.parse_args()

    files = sorted(
        f for p in args.paths for f in ([p] if p.is_file() else p.rglob("*")) if f.is_file() and f.suffix.lower() in SUFFIXES
    )
    if not files:
        print("no PDF, text or image files found")
        return 1
    declared = {k: v for k in ("make", "model", "year", "system") if (v := getattr(args, k))}
    store = get_store()
    store.ensure_bucket()
    with connect() as conn:
        conn.autocommit = True
        for path in files:
            key = storage_key_for(args.kind, path)
            store.put(key, path.read_bytes(), content_type=mimetypes.guess_type(path.name)[0])
            done = jobs.register_file(conn, store, key, original_name=path.name, kind=args.kind, declared=declared, again=args.again)
            print(f"{done.action:12} job {done.job_id}  {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
