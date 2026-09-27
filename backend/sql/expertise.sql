-- Temporal expertise graph (spec §5, §7). Idempotent: applied after schema.sql on a fresh
-- database, and on its own to an existing one (python -m scripts.migrate).

-- Canonical skill/problem nodes. Small vocabulary (~60-120 at demo scale).
CREATE TABLE IF NOT EXISTS skills (
  id          TEXT PRIMARY KEY,          -- 'sk_kafka_consumer_lag'
  name        TEXT NOT NULL UNIQUE,      -- 'kafka consumer lag' (lowercase, 1-4 words)
  kind        TEXT NOT NULL CHECK (kind IN ('problem','domain','tool','practice')),
  description TEXT NOT NULL,             -- one line
  embedding   vector(768)                -- hybrid only
);

-- Raw label -> canonical skill. Makes canonicalization incremental and cacheable.
CREATE TABLE IF NOT EXISTS skill_labels (
  raw_label TEXT PRIMARY KEY,            -- lowercase as emitted by P12
  skill_id  TEXT NOT NULL REFERENCES skills(id)
);

-- Graph edges person -> skill, one row per piece of evidence. Append-only.
CREATE TABLE IF NOT EXISTS expertise_events (
  time            TIMESTAMPTZ NOT NULL,  -- time of the underlying work, not ingestion time
  person_id       TEXT NOT NULL REFERENCES people(id),
  skill_id        TEXT NOT NULL REFERENCES skills(id),
  evidence_kind   TEXT NOT NULL CHECK (evidence_kind IN
                  ('built','solved','organized','reviewed','discussed','helped','learned')),
  weight          REAL NOT NULL,         -- base weight by evidence_kind (§7.1)
  confidence      REAL NOT NULL,         -- 0..1 from P12 (1.0 for feedback evidence)
  source          TEXT NOT NULL CHECK (source IN ('activity','feedback','seed_feedback')),
  source_event_id BIGINT,                -- activity_events.id when source='activity'
  connection_id   BIGINT,                -- when source in ('feedback','seed_feedback')
  snippet         TEXT NOT NULL          -- <= 120 chars, paraphrased
);
SELECT create_hypertable('expertise_events', by_range('time'), if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS expertise_events_person_skill ON expertise_events (person_id, skill_id, time DESC);
CREATE INDEX IF NOT EXISTS expertise_events_skill ON expertise_events (skill_id, time DESC);

-- Materialized current state of the graph. Recomputed by expertise/decay.py.
CREATE TABLE IF NOT EXISTS person_skill_state (
  person_id      TEXT NOT NULL REFERENCES people(id),
  skill_id       TEXT NOT NULL REFERENCES skills(id),
  strength       REAL NOT NULL,          -- decayed evidence sum at computed_at
  level          REAL NOT NULL,          -- 1 - exp(-strength / STRENGTH_SCALE), 0..1
  strength_prev  REAL NOT NULL,          -- same formula evaluated at computed_at - 30 days
  trend          TEXT NOT NULL CHECK (trend IN ('new','rising','steady','fading')),
  first_seen     TIMESTAMPTZ NOT NULL,
  last_seen      TIMESTAMPTZ NOT NULL,
  evidence_count INT NOT NULL,
  best_snippet   TEXT NOT NULL,          -- snippet of the highest-contribution evidence
  computed_at    TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (person_id, skill_id)
);

-- Skill <-> skill edges (related skills). Stored once per unordered pair.
CREATE TABLE IF NOT EXISTS skill_edges (
  skill_a  TEXT NOT NULL REFERENCES skills(id),
  skill_b  TEXT NOT NULL REFERENCES skills(id),
  weight   REAL NOT NULL,                -- 0..1
  cooccur  INT NOT NULL DEFAULT 0,
  origin   TEXT NOT NULL CHECK (origin IN ('cooccur','muse','both')),
  PRIMARY KEY (skill_a, skill_b),
  CHECK (skill_a < skill_b)
);

-- Labels for the scoring blend and the learned model. One row per rating.
CREATE TABLE IF NOT EXISTS match_feedback (
  time          TIMESTAMPTZ NOT NULL DEFAULT now(),
  connection_id BIGINT NOT NULL,
  requester_id  TEXT NOT NULL,
  helper_id     TEXT NOT NULL,
  rater_id      TEXT NOT NULL,
  helpful       BOOLEAN NOT NULL,
  skill_ids     TEXT[] NOT NULL DEFAULT '{}'   -- task.required_skills (or pair shared_skills)
);
SELECT create_hypertable('match_feedback', by_range('time'), if_not_exists => TRUE);

-- Columns the temporal graph adds to existing tables (defaults keep current code working).
ALTER TABLE activity_events ADD COLUMN IF NOT EXISTS expertise_extracted BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE tasks           ADD COLUMN IF NOT EXISTS required_skills JSONB NOT NULL DEFAULT '[]';
ALTER TABLE similarities    ADD COLUMN IF NOT EXISTS temporal_score REAL NOT NULL DEFAULT 0;
ALTER TABLE similarities    ADD COLUMN IF NOT EXISTS shared_skills TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE connections     ADD COLUMN IF NOT EXISTS feedback_applied BOOLEAN NOT NULL DEFAULT false;

-- Hybrid mode (spec §8.6): Contriever vectors with StreamingDiskANN indexes (pgvectorscale), so
-- nearest-neighbour retrieval stays fast from 40 people to a whole company. Harmless in muse mode.
CREATE EXTENSION IF NOT EXISTS vectorscale;
CREATE INDEX IF NOT EXISTS people_summary_embedding_diskann ON people USING diskann (summary_embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS activity_events_embedding_diskann ON activity_events USING diskann (embedding vector_cosine_ops);
