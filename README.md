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

## Knowledge and test data (days 2-4)

```
app/kb/schemas.py        record types: Procedure, DiagramPage, Case, ExpertNote
eval/scenario_schema.py  one evaluation scenario, judged with and without its case in the KB
seed/cases/              solved cases drafted from the client's own material
seed/expert_notes/       the expert's rules of thumb (each claim marked: expert said it / AI added it)
eval/scenarios/          scripted mechanic conversations with expected behaviour
scripts/inventory.py     facts about the data folders listed in data/sources.txt
```

Every file in `seed/` and `eval/scenarios/` is a **draft** until the client's expert approves it;
the `review.open_questions` list in each file is what to ask him. `pytest` validates all of them.

```bash
python scripts/inventory.py          # file counts, duplicates, PDF text layers; safe to paste
python -m eval.smoke.run --no-standard --real-kind scan_report --real-image eval/scenarios/assets/byd-qin-plus-2024-pdc-report.png --expect P2B5313,P2B5411
```

## Evaluation (Milestone 1)

```
app/engine/readings.py         a reading compared with a normal range, in code (units, OL, Arabic and Urdu digits)
app/engine/procedure_walk.py   next step or conclusion for a procedure, as pure functions
eval/golden/procedures/        hand-verified reference records
eval/scenarios/                the 30 scenarios: 5 procedure branches, 4 language and number variants,
                               12 solved cases, 4 wiring-diagram questions, 2 scan-report readings,
                               2 with no data in the knowledge base, 1 emergency
eval/scenario_schema.py        per-turn checks a program can score, plus behaviour a reviewer checks
```

`pytest` walks every P0010 scenario through the reference record and fails if a pin, a value or a
conclusion in a scenario disagrees with it.

Each solved case is judged twice: with the case in the knowledge base (the assistant must offer it as
a hint, with its source) and with the case held out (the assistant must not invent the fix). The
diagram answers expected by the four diagram scenarios were read by eye from the rendered pages; they
are compared with the extracted records in Milestone 2.

## Knowledge base (Milestone 2)

The knowledge base lives in PostgreSQL. Tables are created by migrations, never by hand.

```bash
uv pip install -e ".[dev]"     # picks up the new packages (alembic, sqlalchemy)
docker compose up -d           # PostgreSQL with pgvector
alembic upgrade head           # create or update the tables
python scripts/load_seed.py    # load the records kept in this repository, as drafts
pytest -q
```

`python scripts/check_storage.py` checks file storage the same way `scripts/check_db.py` checks the
database. Locally, storage is the `storage` service in `docker-compose.yml`, a stand-in for the S3
bucket shared with the platform; the values in `.env.example` match it.

With the database running, `pytest` also runs the database tests, in a throw-away database it
creates and drops itself. Without it those tests are skipped and the rest still run.

```
migrations/versions/     one file per change to the tables
app/kb/schemas.py        the record types (the single definition of what a record is)
app/kb/keys.py           normalised keys, so vehicles and fault codes match exactly
app/kb/index.py          what is copied out of a record into the lookup tables
app/kb/store.py          save, approve, exact lookups, keyword search
```

### Adding files

A file goes into storage, is registered, and a background worker turns it into draft records.

```bash
python -m app.ingest.add --kind licensed_diagram "C:/path/to/diagrams" --make Acura --model Integra --year 2025
python -m app.ingest.worker --once     # process what is waiting, then exit
python -m app.ingest.worker            # or: keep running and pick up new files as they arrive
```

```
app/storage.py            the S3 bucket: read, write, short-lived download links
app/ingest/jobs.py        registering files, and the job queue (a PostgreSQL table)
app/ingest/extractors.py  one extractor per kind of file, registered here as each is built
app/ingest/worker.py      takes jobs from the queue and runs the extractor
app/ingest/add.py         the local way to add files
```

- The same file added twice is processed once. Changed content becomes a new version.
- A job that fails for a passing reason is tried again after 30 seconds, then 2 minutes, then 8.
  One that cannot pass is marked failed at once, with the reason, and can be queued again.
- Withdrawing a file stops everything extracted from it being served.

### Wiring diagrams

`app/ingest/diagrams.py` reads a diagram PDF from its drawing: wires, pins, connectors, junctions,
version braces and page joins. No model is used. It can list a component's pins with their wire
colours and follow a wire from a pin to everything it reaches, across pages.

```bash
python scripts/score_diagrams.py "C:/path/to/diagrams"
```

Added as a `licensed_diagram` file, a diagram becomes one record with every pin and where its wire
goes, plus an image of each page. After `alembic upgrade head`:

```bash
python -m app.ingest.add --kind licensed_diagram "C:/path/to/diagrams"
python -m app.ingest.worker --once
python scripts/show_pin.py Acura Integra 2025 "VSA MODULATOR-CONTROL UNIT" 31
python scripts/show_pin.py Acura Integra 2025 --find "wheel speed sensor"
```

Each diagram gets a verdict from checks that need no answer key: `traced` (pins and routes can be
used), `partial` (routes that hit a problem are marked incomplete) or `show_only` (the file is not
drawn the way the reader understands: it is stored and shown, and no pin is taken from it).

The scoring script prints, per diagram, how many wires were read, the colour check (the colour a wire is
drawn in against the colour printed beside it), every wire end the reader could not attach, and
how many hand-verified routes in `eval/golden/diagrams/` it reproduces.

Rules the store enforces, each covered by tests:

- **Only approved records are served.** Drafts are visible only when the caller asks for them
  (development and evaluation).
- **A record is used only for its own vehicle and its own fault code.** `P0010` never returns
  `P0011`; a 7-character code never matches its 5-character base; a procedure needs the same
  make, model, year and engine.
- **An approved record is never changed in place.** New content becomes a new draft version, and
  the approved version keeps being served until the new one is approved.
- **Search finds a small piece and returns the whole record** (one symptom, one finding or one
  claim leads to the full case or expert note). Text the chat assistant added and the expert has
  not confirmed is not searched.

## Rules

- Only the client's **own** knowledge (their cases, their expert's notes) may be committed under `seed/`.
  Source files from licensed manuals (procedure and diagram PDFs) stay in `data/` and in storage, never in git.
- `eval/golden/` holds hand-verified reference records (one so far: the P0010 procedure). They are the
  answer key the extraction and the scenarios are checked against, so they are versioned with the code.

- The key lives only in `.env`. Never in code, prompts, git or chat.
- Client files (procedure PDFs, cases, scan reports) stay in `data/`, which is git-ignored.
- Code asks the gateway for a role (`reasoning`, `fast`, `vision`), never for a model name.
  Changing model or provider is a change to `.env`.
