CREATE EXTENSION IF NOT EXISTS timescaledb;
CREATE EXTENSION IF NOT EXISTS vector;   -- used only in hybrid mode; harmless otherwise

CREATE TABLE orgs (
  id   TEXT PRIMARY KEY,                 -- 'org_eng'
  name TEXT NOT NULL
);

CREATE TABLE teams (
  id          TEXT PRIMARY KEY,          -- 't_payments'
  org_id      TEXT NOT NULL REFERENCES orgs(id),
  name        TEXT NOT NULL,
  lead_id     TEXT,                      -- FK added below
  summary     TEXT,
  focus_areas TEXT[] NOT NULL DEFAULT '{}'
);

CREATE TABLE people (
  id                TEXT PRIMARY KEY,    -- 'p_001'
  team_id           TEXT NOT NULL REFERENCES teams(id),
  manager_id        TEXT REFERENCES people(id),
  name              TEXT NOT NULL,
  title             TEXT NOT NULL,
  email             TEXT UNIQUE NOT NULL,
  slack_user_id     TEXT,                -- real Slack ID only for demo personas
  is_engineer       BOOLEAN NOT NULL DEFAULT false,
  is_manager        BOOLEAN NOT NULL DEFAULT false,
  available         BOOLEAN NOT NULL DEFAULT true,
  summary           TEXT,
  focus_areas       TEXT[] NOT NULL DEFAULT '{}',
  summary_embedding vector(768)          -- hybrid only
);
ALTER TABLE teams ADD CONSTRAINT teams_lead_fk FOREIGN KEY (lead_id) REFERENCES people(id);

-- Work history. One row per meaningful unit (a PR, a review, a Slack post, an email).
CREATE TABLE activity_events (
  id          BIGINT GENERATED ALWAYS AS IDENTITY,
  time        TIMESTAMPTZ NOT NULL,
  person_id   TEXT NOT NULL REFERENCES people(id),
  direction   TEXT NOT NULL CHECK (direction IN ('did','received')),
  source      TEXT NOT NULL CHECK (source IN ('github','slack','email','calendar')),
  kind        TEXT NOT NULL,             -- pr | review | slack_message | email | calendar_invite
  external_id TEXT NOT NULL,
  title       TEXT,
  text        TEXT NOT NULL,             -- compact text (<= 800 chars) used for profiles/matching
  metadata    JSONB NOT NULL DEFAULT '{}',
  embedding   vector(768),               -- hybrid only
  PRIMARY KEY (id, time)
);
SELECT create_hypertable('activity_events', by_range('time'));
CREATE UNIQUE INDEX activity_events_dedupe ON activity_events (source, external_id, time);
CREATE INDEX activity_events_person ON activity_events (person_id, time DESC);

CREATE TABLE tasks (
  id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  person_id        TEXT NOT NULL REFERENCES people(id),
  source_event_id  BIGINT,               -- activity_events.id (no FK: hypertable composite PK)
  source           TEXT NOT NULL,
  summary          TEXT NOT NULL,
  task_type        TEXT,
  status           TEXT NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open','notified','connected','dismissed')),
  best_match_score REAL,
  created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  embedding        vector(768)           -- hybrid only
);

-- Person-pair edges (graph). Stored once per unordered pair.
CREATE TABLE similarities (
  person_a       TEXT NOT NULL REFERENCES people(id),
  person_b       TEXT NOT NULL REFERENCES people(id),
  semantic_score REAL NOT NULL,          -- 0..1 (Muse score / 100)
  dir_overlap    REAL NOT NULL DEFAULT 0,-- Jaccard of GitHub directories, 0..1
  score          REAL NOT NULL,          -- final, see §8.3
  rank_a         SMALLINT,               -- rank of b in a's list (1 = best); NULL if absent
  rank_b         SMALLINT,               -- rank of a in b's list
  reason         TEXT NOT NULL,          -- one line, from Muse
  shared_dirs    TEXT[] NOT NULL DEFAULT '{}',
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (person_a, person_b),
  CHECK (person_a < person_b)
);

CREATE TABLE team_overlaps (
  team_a        TEXT NOT NULL REFERENCES teams(id),
  team_b        TEXT NOT NULL REFERENCES teams(id),
  score         REAL NOT NULL,           -- 0..1
  shared_topics TEXT[] NOT NULL DEFAULT '{}',
  summary       TEXT NOT NULL,
  lead_brief    TEXT,                    -- markdown, generated for pairs >= BRIDGE_MIN_SCORE
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (team_a, team_b),
  CHECK (team_a < team_b)
);

CREATE TABLE connections (
  id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  requester_id          TEXT NOT NULL REFERENCES people(id),   -- employee A
  helper_id             TEXT NOT NULL REFERENCES people(id),   -- employee B
  task_id               BIGINT REFERENCES tasks(id),
  origin                TEXT NOT NULL CHECK (origin IN ('auto','manager_nudge','lead_intro')),
  initiated_by          TEXT REFERENCES people(id),            -- manager for nudges/intros
  reason                TEXT NOT NULL,
  match_score           REAL,
  status                TEXT NOT NULL CHECK (status IN
                        ('suggested','requested','accepted','active','declined','expired')),
  slack_channel_id      TEXT,
  message_count         INT NOT NULL DEFAULT 0,
  helpful_requester     BOOLEAN,
  helpful_helper        BOOLEAN,
  feedback_requested_at TIMESTAMPTZ,
  is_seed               BOOLEAN NOT NULL DEFAULT false,        -- for /api/dev/reset-demo
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
  accepted_at           TIMESTAMPTZ,
  active_at             TIMESTAMPTZ,
  closed_at             TIMESTAMPTZ,
  request_note          TEXT,                                   -- what the requester wrote to the helper
  request_links         TEXT[] NOT NULL DEFAULT '{}'            -- docs the requester attached
);
CREATE INDEX connections_requester ON connections (requester_id);
CREATE INDEX connections_helper    ON connections (helper_id);

-- Every status change, every chat message (count only), every feedback answer.
CREATE TABLE connection_events (
  time          TIMESTAMPTZ NOT NULL DEFAULT now(),
  connection_id BIGINT NOT NULL,
  event         TEXT NOT NULL CHECK (event IN
                ('suggested','requested','accepted','active','declined','expired','message','feedback')),
  person_id     TEXT,
  value         BOOLEAN                  -- feedback: helpful yes/no
);
SELECT create_hypertable('connection_events', by_range('time'));

-- Powers the synopsis trend chart and headline stats.
CREATE MATERIALIZED VIEW connection_daily
WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
SELECT time_bucket('1 day', time) AS day, event, count(*) AS n
FROM connection_events
GROUP BY day, event;

SELECT add_continuous_aggregate_policy('connection_daily',
  start_offset => INTERVAL '90 days', end_offset => INTERVAL '1 hour',
  schedule_interval => INTERVAL '5 minutes');

-- Intentional meetings: one row per booked meeting. Notes live in a Slack canvas; only its id is stored.
CREATE TABLE meetings (
  id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  connection_id   BIGINT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
  parent_id       BIGINT REFERENCES meetings(id) ON DELETE CASCADE,  -- set for a follow-up
  start_at        TIMESTAMPTZ NOT NULL,
  event_link      TEXT,
  meet_link       TEXT,
  canvas_id       TEXT,
  canvas_url      TEXT,
  deliverable     TEXT,                                               -- title of the accepted deliverable
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Manager-editable settings from the dashboard (one JSON document under key 'prefs').
CREATE TABLE app_settings (
  key        TEXT PRIMARY KEY,
  value      JSONB NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
