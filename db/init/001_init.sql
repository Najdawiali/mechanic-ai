-- Runs once, the first time the database volume is created.
CREATE EXTENSION IF NOT EXISTS vector;

-- The AI service keeps its own schemas, separate from the backend's tables (handoff 7.2).
CREATE SCHEMA IF NOT EXISTS kb;        -- knowledge base: vehicles, procedures, cases, diagrams
CREATE SCHEMA IF NOT EXISTS ai_state;  -- per-session working state, checkpoints, traces
