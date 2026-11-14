"""Load the records kept in the repository into the knowledge base.

  seed/cases/           the client's solved cases
  seed/expert_notes/    the expert's notes
  eval/golden/          hand-verified reference records

Run after `alembic upgrade head`:   python scripts/load_seed.py
Safe to run again: a record whose content has not changed is left alone.
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from app.db import connect  # noqa: E402
from app.kb.schemas import Case, ExpertNote, Procedure  # noqa: E402
from app.kb.store import KnowledgeStore  # noqa: E402

FOLDERS = [
    ("seed/cases", Case, "model"),  # drafted from the case reports with a model, then checked by hand
    ("seed/expert_notes", ExpertNote, "model"),
    ("eval/golden/procedures", Procedure, "hand"),
]


def load_all(store: KnowledgeStore) -> list[tuple[str, str]]:
    done = []
    for folder, model, extracted_by in FOLDERS:
        for path in sorted((REPO / folder).glob("*.json")):
            record = model(**json.loads(path.read_text(encoding="utf-8")))
            saved = store.save(record, extracted_by=extracted_by)
            done.append((f"{folder}/{path.name}", f"{saved.action} (version {saved.version}, {record.review.status})"))
    return done


def main() -> int:
    with connect() as conn:
        for name, result in load_all(KnowledgeStore(conn)):
            print(f"{result:40} {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
