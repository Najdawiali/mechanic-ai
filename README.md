# mechanic-ai

The AI service for the mechanic diagnostic assistant. AI part only: the app, admin
dashboard, auth and billing belong to the client's software team and talk to this
service over REST.

## Layout

```
app/
  api/       FastAPI routes (only /health so far)
  engine/    state machine, step logic, numeric checks      (Milestone 3)
  llm/       gateway.py = model routing behind roles
  kb/        DB models, exact + hybrid search               (Milestone 2)
  ingest/    PDF / vision extraction, validation            (Milestone 2)
  lang/      language detection, query rewrite, glossary    (Milestone 3)
  safety/    emergency triggers, answer-policy validator    (Milestone 3)
eval/
  smoke/     day 1-2 model smoke tests
tests/       offline tests (no key, no cost)
db/init/     SQL that runs when the database is first created
data/        local sample files, git-ignored
```

## Setup (day 1)

```bash
git init
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
cp .env.example .env                 # Windows: copy .env.example .env
# open .env and paste the DeepInfra key after DEEPINFRA_API_KEY=
```

## Checks, in this order

```bash
pytest -q                            # 1. offline: code is healthy (no key used)
docker compose up -d                 # 2. Postgres 16 + pgvector
python scripts/check_db.py           #    expect: PASS ... pgvector ... schemas = ['ai_state', 'kb']
python -m eval.smoke.run             # 3. real calls to DeepInfra (a few US cents at most)
```

Step 3 prints a table and writes `eval/smoke/results/smoke-<time>.md`, which holds the
Arabic and Urdu replies for a native reader to judge.

Useful options:

```bash
python -m eval.smoke.run --repeat 5
python -m eval.smoke.run --only vision_pins,tool_call_ar
python -m eval.smoke.run --models google/gemma-4-31B-it-turbo
python -m eval.smoke.run --real-image data/p0010_page2.png   # a real procedure page, judged by eye
```

## Rules

- The key lives only in `.env`. Never in code, prompts, git or chat.
- Client files (procedure PDFs, cases, scan reports) stay in `data/`, which is git-ignored.
- Code asks the gateway for a role (`reasoning`, `fast`, `vision`), never for a model name.
  Changing model or provider is a change to `.env`.
