# Musketeer — Technical Spec

HackGT 13 · Meta track ("Bringing people closer together with AI").
This document is the source of truth for implementation. Build only what is specified here. If something is ambiguous, pick the simplest option that satisfies the spec and leave a `// SPEC-QUESTION:` comment.

---

## 1. Product in one paragraph

Musketeer automatically connects employees who do similar work but don't know each other. When new work reaches someone (an email, a Slack message, a PR review request), AI detects it, finds colleagues who have done similar work, and connects them in Slack so they can talk. The product surface is a **manager dashboard**: a zoomable graph (orgs → teams → people) showing who could help whom, which connections happened, and which teams should be talking, plus a synopsis page with stats. Employees never open a new app; they only see Slack messages.

### Principles (do not violate)

- **The output is always a human conversation**, never a document or artifact to read.
- **Automatic**: employees never invoke the tool. New work triggers matching.
- **No migration**: employee interaction happens only in Slack.
- **All generative AI is Meta Muse Spark.** Embeddings (hybrid mode only) use a Meta FAIR open model. No Gemini, no OpenAI models.
- **Privacy**: store message *counts* for connection chats, never message content. Only ingest data the demo org opted into.
- **Demo reliability over completeness**: precompute everything that can be precomputed; cache every Muse response.

### Out of scope

Auth/login, real calendar integration, real GitHub ingestion (seed data only; ingestion interface is built), Jira, browser extensions, mobile layout, notifications outside Slack, storing chat content, any AI provider other than Meta.

---

## 2. Stack

| Layer | Choice |
|---|---|
| Backend | Python 3.11+, FastAPI, Uvicorn, Pydantic v2, psycopg 3 (async) + psycopg_pool, raw SQL (no ORM) |
| Database | Tiger Cloud (Tiger Data) **Hybrid** service: Postgres + TimescaleDB (+ pgvector for hybrid mode) |
| LLM | Meta Model API, Muse Spark, via the `openai` Python SDK pointed at `https://api.meta.ai/v1` |
| Embeddings (hybrid mode only) | `facebook/contriever-msmarco` via Hugging Face `transformers` + `torch` (CPU), 768 dims |
| Slack | Slack Bolt for Python, **Socket Mode** (no public URL) |
| Email | Gmail API (polling) |
| Frontend | React 18 + Vite + TypeScript, D3 v7 (zoomable circle packing + edge overlay), TanStack Query (polling), React Router, Recharts, Tailwind CSS |

---

## 3. Repo layout

```
bridge/
  backend/
    app/
      main.py                 # FastAPI app; starts Slack handler + background loops on startup
      config.py               # env settings (pydantic-settings)
      db.py                   # async pool, helpers
      schemas.py              # Pydantic models: NormalizedEvent, API response models
      ai/
        muse.py               # Muse client wrapper: json() with schema, retries, disk cache
        prompts/              # one .md file per prompt (see §7)
        embedder.py           # Contriever wrapper (hybrid only; lazy import)
      matching/
        base.py               # Matcher protocol + shared types
        muse_matcher.py       # MATCHER_MODE=muse
        hybrid_matcher.py     # MATCHER_MODE=hybrid
        scoring.py            # dir overlap, final score, task-candidate adjustments
      pipeline/
        profiles.py           # person + team summaries
        similarity.py         # person-pair edges
        team_overlap.py       # team bridges + lead briefs
        tasks.py              # detect task -> match -> maybe notify
      connectors/
        base.py               # Connector protocol -> NormalizedEvent
        github.py             # normalize GitHub-shaped PR/review payloads
        gmail.py              # Gmail poller
        slack_ingest.py       # Slack message -> NormalizedEvent (for task detection)
      bot/
        slack_bot.py          # Bolt app: notifications, buttons, group DMs, message counting
        messages.py           # Block Kit builders (templates)
        jobs.py               # feedback loop, simulated deliveries
      api/
        graph.py  people.py  pairs.py  bridges.py  search.py  synopsis.py  actions.py  dev.py
    scripts/
      seed_generate.py        # Muse generates fixtures from seed_spec.yaml
      seed_load.py            # loads fixtures into DB (incl. backdated history)
      run_pipeline.py         # profiles -> (embeddings) -> similarities -> team overlaps
      export_mocks.py         # dumps real API responses into frontend/src/mocks
    seed/
      seed_spec.yaml          # hand-written (see §9)
      fixtures/               # generated JSON, committed
    sql/schema.sql
    requirements.txt
    requirements-hybrid.txt   # torch, transformers, pgvector
    .env.example
  frontend/
    src/
      api/ (client.ts, types.ts)   mocks/ (*.json)
      views/ (GraphView.tsx, SynopsisView.tsx)
      graph/ (pack.ts, aggregate.ts, edges.ts, externalRing.ts, zoom.ts)
      components/ (TopBar, Toolbar, Breadcrumb, PersonPanel, PairPanel, BridgePanel, SearchBox, Toast, StatCard)
      theme.css
```

---

## 4. Configuration (`backend/.env.example`)

```
DATABASE_URL=postgres://...            # Tiger Cloud connection string
MODEL_API_KEY=...                      # Meta Model API key
MUSE_MODEL=muse-spark-1.3              # VERIFY with GET https://api.meta.ai/v1/models
MUSE_CACHE_DIR=.cache/muse
MATCHER_MODE=muse                      # muse | hybrid

SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...               # Socket Mode app-level token (connections:write)
ENABLE_SLACK=true
SIMULATE_UNMAPPED=true                 # people without slack_user_id get simulated delivery
SIMULATED_ACCEPT_SEC=8

ENABLE_GMAIL=true
GMAIL_POLL_SEC=5
GMAIL_CREDENTIALS=credentials.json     # OAuth desktop client
GMAIL_TOKEN=token.json
DEMO_EMAIL_FALLBACK_PERSON=p_031     # Anthony; confirm from seed_load output

DEFAULT_VIEWER_ID=p_029                # t_events lead (Anthony's manager); confirm from seed_load output
ENABLE_DEV_ROUTES=true

TOPK_STORE=8                           # matches stored per person
AUTO_NOTIFY_THRESHOLD=0.75             # task match score needed to auto-DM
MISSED_MIN_SCORE=0.70                  # potential edges counted as "missed opportunities"
BRIDGE_MIN_SCORE=0.70                  # team overlap needed to draw a bridge
AMBER_AFTER_MIN=2                      # demo value; open task w/o connection -> amber
FEEDBACK_AFTER_MIN=3                   # demo value; ask "was this helpful?"
```

Frontend: `VITE_USE_MOCKS=true|false`, `VITE_POLL_MS=3000`. Vite dev server proxies `/api` → `http://localhost:8000`.

Thresholds are starting values; tune after the first pipeline run so planted overlaps (§9) clear them and noise doesn't.

---

## 5. Data model (`backend/sql/schema.sql`)

One schema serves both matcher modes. Embedding columns are nullable and only filled in hybrid mode.

```sql
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
  closed_at             TIMESTAMPTZ
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
```

Notes:
- If continuous aggregates are unavailable on the free tier, replace `connection_daily` with a plain `GROUP BY time_bucket(...)` query in `synopsis.py`; data is small.
- No vector indexes are needed at demo scale (hundreds of rows); pgvector scans are fast enough.
- `hypertable` syntax `by_range` requires TimescaleDB ≥ 2.13 (Tiger Cloud is current).

### Derived states (computed in SQL at read time, never stored)

- **Edge state for a pair** (latest connection between them, either direction):
  `suggested|requested` → `pending`; `accepted|active` → `connected`; otherwise → `potential`.
- **needs_connection (amber dot)**: person has a task with status `open|notified`, `created_at < now() - AMBER_AFTER_MIN`, and no connection for that task in `accepted|active`.
- **Interacted before**: any `connections` row between the two people in any status.
- **Visible rank of a pair**: `LEAST(rank_a, rank_b)` ignoring NULLs.

---

## 6. Ingestion: common event format and connectors

Every connector converts raw data into `NormalizedEvent` and inserts it into `activity_events`. Nothing downstream knows which tool an event came from.

```python
class NormalizedEvent(BaseModel):
    source: Literal['github', 'slack', 'email', 'calendar']
    kind: str                      # pr | review | slack_message | email | calendar_invite
    external_id: str               # stable id from the source, used for dedupe
    time: datetime
    person_id: str                 # whose stream this belongs to
    direction: Literal['did', 'received']
    title: str | None
    text: str                      # compact, <= 800 chars
    metadata: dict
```

- `direction='did'`: work the person performed. Used for profiles and matching.
- `direction='received'`: something sent to the person. Used only for task detection.

### 6.1 GitHub (`connectors/github.py`)

Demo uses seed fixtures only, in GitHub API shape, so real webhook payloads (`pull_request`, `pull_request_review`) can reuse the same normalizer later.

Fixture shape:
```json
{"repo": "platform", "number": 412, "title": "...", "body": "...", "author": "p_004",
 "created_at": "...", "merged_at": "...", "files": ["shared/kafka-client/src/consumer.py"],
 "commits": [{"message": "fix(consumer): commit offsets after batch"}],
 "reviews": [{"reviewer": "p_009", "body": "...", "submitted_at": "..."}]}
```

Normalization rules:
- **One event per PR** (never per commit), `kind='pr'`, `direction='did'`, for the author.
- **One event per review**, `kind='review'`, `direction='did'`, for the reviewer. Text: `Reviewed PR #{n} "{title}" in {dirs}`.
- `metadata.commit_types`: counts of conventional-commit prefixes, regex `^(feat|fix|perf|refactor|test|docs|chore|build|ci)(\(.+\))?!?:`. Unmatched → `other`.
- `metadata.directories`: each file path trimmed to its first `min(3, depth-1)` segments, deduped (`services/payments/consumer/lag.py` → `services/payments/consumer`).
- `metadata.languages`: from file extensions (`.py` Python, `.ts/.tsx` TypeScript, `.go` Go, `.java` Java, `.sql` SQL, `.yaml/.yml` YAML, ...).
- `metadata`: also `repo`, `pr_number`, `files_changed`.
- Compact `text`: `PR: {title}. {first 200 chars of body} | dirs: {dirs} | commits: {3 fix, 1 test} | langs: {langs}`.
- Never store diffs or file contents.
- Review requests and issue assignments (future real ingestion) are `direction='received'` and go through task detection.

### 6.2 Gmail (`connectors/gmail.py`)

- OAuth desktop flow (`GMAIL_CREDENTIALS` → `GMAIL_TOKEN`), scope `gmail.modify`.
- Poll every `GMAIL_POLL_SEC` for `is:unread`. For each message: map recipient to a person via **plus addressing** (`demo+p_031@gmail.com` → `p_031`), falling back to `DEMO_EMAIL_FALLBACK_PERSON`.
- Insert as `kind='email'`, `direction='received'`, text = subject + first 600 chars of plain-text body. Run task detection (§8.4). Mark the message read.

### 6.3 Slack ingest (`connectors/slack_ingest.py`)

Messages in public channels the bot is in that mention or are addressed to a roster person → `direction='received'` for the mentioned person → task detection. Messages in connection group DMs are **not** ingested; they are only counted (§10).

---

## 7. AI layer

### 7.1 Muse client (`ai/muse.py`)

```python
client = OpenAI(base_url="https://api.meta.ai/v1", api_key=settings.MODEL_API_KEY)

async def muse_json(prompt_name: str, variables: dict, schema: type[BaseModel],
                    *, cache: bool = True, prefix: str | None = None) -> BaseModel: ...
```

- Uses Chat Completions with structured output (`response_format` = JSON schema derived from the Pydantic model). Validate the result with Pydantic; retry up to 2 times on validation failure.
- **Disk cache**: key = sha256(model + prompt name + rendered prompt). Always on for seed/pipeline; off for live icebreakers and search. Makes pipeline reruns free and deterministic.
- **Prompt caching**: when `prefix` is passed (the roster block), place it first and byte-identical across calls. Check Meta's prompt-caching docs for whether any explicit parameter is required.
- Reasoning cannot be set to `"none"` (the API rejects it). Use low effort for batch jobs if the API accepts it; otherwise omit the field.
- Batch jobs run with an `asyncio.Semaphore(4)`.
- Prompts live in `ai/prompts/{name}.md` with `{{variable}}` placeholders.

### 7.2 Shared scoring rubric (include verbatim in every ranking prompt)

```
Score 0-100 how useful it would be for these two people to talk about their work.
90-100: they have done nearly the same work or solved the same problem.
70-89: strongly overlapping domain; one could directly help the other.
50-69: related area; useful context but not direct help.
Below 50: omit.
Judge by the substance of the work, not shared words. Different wording for the same
problem (e.g. "consumer lag" vs "messages piling up in the queue") counts as the same.
Reasons must be one concrete sentence naming the shared work, <= 20 words.
```

### 7.3 Roster block

Used by Muse mode as the cached prefix. One line per person:
```
[p_017] Andy Shah | Senior University Recruiter | University Recruiting (Recruiting) | Summary: ... | Focus: campus events, hackathon sponsorship, ...
```
For engineers append `| Code: {top 5 directories} | Langs: {langs}`.

### 7.4 Prompts

All outputs are JSON matching the listed schema.

| # | Name | Input | Output |
|---|---|---|---|
| P1 | `detect_task` | event title/text; recipient name, title, team | `{is_new_task: bool, confidence: 0-1, summary: str (<=25 words), task_type: event\|project\|bug\|review\|request\|other}` |
| P2 | `person_profile` | person meta; up to 40 `did` events, newest first; GitHub aggregates (§8.2) | `{summary: str (<=60 words), focus_areas: [str] (<=6, lowercase, 1-3 words)}` |
| P3 | `team_profile` | team name; member summaries | `{summary: str (<=50 words), focus_areas: [str] (<=6)}` |
| P4 | `rank_for_person` | roster prefix; target person id | `{matches: [{person_id, score: 0-100, reason}]}` up to `TOPK_STORE`, excludes target |
| P5 | `rank_for_task` | roster prefix; task summary; requester id | `{candidates: [{person_id, score, reason}]}` up to 3, excludes requester |
| P6 | `rerank` (hybrid) | up to 15 candidate profiles; the target person, task, or query | same output shape as P4/P5/P7 depending on use |
| P7 | `search` | roster prefix; natural-language query | `{results: [{person_id, score, reason}]}` up to 5 |
| P8 | `team_overlaps` | all team profiles | `{pairs: [{team_a, team_b, score: 0-100, shared_topics: [str], summary: str (<=40 words)}]}` only pairs >= 50 |
| P9 | `lead_brief` | both team profiles; overlap summary | `{brief: str}` markdown <= 150 words: what each team does, current work, the overlap, 3 suggested topics |
| P10 | `icebreaker` | requester + helper profiles; task summary; match reason | `{message: str (<=90 words), questions: [str] (exactly 3)}` |
| P11 | `synopsis_summary` | stats JSON from `/api/synopsis` | `{summary: str (<=60 words)}` |

Slack notification and nudge texts are templates (§10), not LLM calls.

---

## 8. Matching

### 8.1 Matcher interface (`matching/base.py`)

Both modes implement the same interface, return the same types, and write the same tables. Switching modes = change `MATCHER_MODE` and rerun `run_pipeline.py`.

```python
class Match(BaseModel):
    person_id: str
    score: float        # 0..1, always calibrated by Muse (never a raw cosine)
    reason: str

class Matcher(Protocol):
    async def rank_for_person(self, person_id: str) -> list[Match]: ...   # up to TOPK_STORE
    async def rank_for_task(self, task_id: int) -> list[Match]: ...       # up to 3
    async def search(self, query: str) -> list[Match]: ...                # up to 5

def get_matcher() -> Matcher:  # returns MuseMatcher or HybridMatcher per MATCHER_MODE
```

Team overlaps use P8 in both modes (only 8 teams; no retrieval needed).

### 8.2 GitHub aggregates (SQL, both modes)

Per engineer over the last 60 days of `pr`/`review` events: top directories (by count), languages, commit-type mix. Feed into P2, the roster line, and the person panel. Directory set = all directories touched.

### 8.3 Final pair score (`matching/scoring.py`, both modes)

```
dir_overlap = |dirs_a ∩ dirs_b| / |dirs_a ∪ dirs_b|        (0 if either is not an engineer)
dir_bonus   = min(1, 2 * dir_overlap)
score       = min(1, semantic_score + 0.15 * dir_bonus)
shared_dirs = dirs_a ∩ dirs_b
```

Symmetrizing: if A lists B and B lists A, `semantic_score` = mean of the two; if only one side lists the pair, use that score. Store `rank_a`/`rank_b` from each side's list. Keep the reason from the higher-scoring side. If `shared_dirs` is non-empty and the reason doesn't mention code, append ` Both changed {first shared dir}.`

### 8.4 Task pipeline (`pipeline/tasks.py`, both modes)

1. Event with `direction='received'` arrives → P1 `detect_task`.
2. If `is_new_task && confidence >= 0.7`: insert `tasks` row (`open`). Otherwise stop.
3. `candidates = matcher.rank_for_task(task_id)`.
4. Adjust each candidate:
   ```
   adjusted = score
            + 0.10 if never interacted with requester
            - min(0.15, 0.05 * helper_requests_last_7_days)
   drop if not available or candidate == requester
   ```
5. Store `best_match_score`. If best `adjusted >= AUTO_NOTIFY_THRESHOLD`: create connection (`origin='auto'`, `status='suggested'`), send Slack suggestion to requester (§10), set task `notified`.
6. Otherwise the task stays `open` and becomes an amber dot after `AMBER_AFTER_MIN`. Managers can nudge from the dashboard.
7. If the helper declines: try the next candidate once if it clears the threshold; else task returns to `open`.

### 8.5 Mode A — Muse only (`MATCHER_MODE=muse`, default)

No embeddings. The roster (~40 lines, a few thousand words) fits easily in Muse Spark's ~1M-token context.

- `rank_for_person`: P4 with the roster prefix. Run once per person in `run_pipeline.py` (40 calls, cached).
- `rank_for_task`: P5 with the roster prefix (live, ~seconds).
- `search`: P7 with the roster prefix (live; frontend shows a loading state).
- Scores: `score / 100`.

Limitation to acknowledge in the pitch: sending the whole roster per call doesn't scale to a full company; Mode B is the scale path.

### 8.6 Mode B — Contriever retrieval + Muse rerank (`MATCHER_MODE=hybrid`)

Contriever narrows candidates cheaply; Muse makes the final judgment and writes reasons. **Contriever cosine scores are uncalibrated; never threshold on them.** All thresholds apply to Muse scores.

Embedder (`ai/embedder.py`):
- `facebook/contriever-msmarco` via `AutoTokenizer`/`AutoModel`, mean pooling over the attention mask, L2-normalize, `max_length=512`, batch 16, CPU. Load once at startup (lazy import so Mode A has no torch dependency).
- Embed: people (`summary + focus areas`) → `people.summary_embedding`; `did` activity events (`text`) → `activity_events.embedding`; tasks (`summary`); search queries (on the fly).
- `run_pipeline.py` embeds after profiles are generated.

Retrieval SQL:
```sql
-- person neighbors
SELECT b.id, 1 - (a.summary_embedding <=> b.summary_embedding) AS sim
FROM people a JOIN people b ON b.id <> a.id
WHERE a.id = %(pid)s
ORDER BY a.summary_embedding <=> b.summary_embedding
LIMIT 15;

-- task candidates: best recency-weighted event match per person
SELECT person_id,
       max((1 - (embedding <=> %(q)s)) *
           exp(-extract(epoch FROM now() - time) / 86400 / 45)) AS s
FROM activity_events
WHERE direction = 'did' AND embedding IS NOT NULL AND person_id <> %(requester)s
GROUP BY person_id
ORDER BY s DESC
LIMIT 15;

-- search: query embedding vs people.summary_embedding, LIMIT 15
```

Then P6 `rerank` over the 15 candidates' profiles → top `TOPK_STORE` / 3 / 5 with Muse scores and reasons.

---

## 9. Seed data and batch pipeline

### 9.1 `seed/seed_spec.yaml` (hand-written; names below are placeholders the generator may keep)

40 people, 8 teams, 3 orgs, 26 engineers.

```yaml
company: "Fictional Meta demo org"
history_days: 60
orgs:
  - id: org_eng
    name: Engineering
    teams:
      - {id: t_payments, name: Payments Infrastructure, size: 6, engineers: 6}
      - {id: t_messaging, name: Messaging Infra, size: 6, engineers: 6}
      - {id: t_ads, name: Ads Ranking, size: 6, engineers: 6}
      - {id: t_devplat, name: Developer Platform, size: 5, engineers: 5}
  - id: org_rec
    name: Recruiting
    teams:
      - {id: t_uni, name: University Recruiting, size: 5, engineers: 0}
      - {id: t_events, name: Events & Community Recruiting, size: 4, engineers: 0}
  - id: org_prod
    name: Product & Design
    teams:
      - {id: t_growth, name: Growth Product, size: 4, engineers: 1}
      - {id: t_design, name: Design Systems, size: 4, engineers: 2}

repos:
  payments:  [services/payments/api, services/payments/consumer, services/payments/ledger]
  messaging: [services/messaging/delivery, services/messaging/notifications, services/messaging/queue]
  ads:       [services/ads/ranking, services/ads/serving, services/ads/serving/throttle]
  platform:  [shared/kafka-client, shared/auth, platform/gateway/ratelimit, platform/ci]
  web:       [web/design-system, web/growth/funnels]

planted_overlaps:           # must surface as rank 1-2 matches; NO prior connections between these people
  - id: recruiting_mlh      # the recruiter story; also a team-level bridge t_uni <-> t_events
    people:
      - {team: t_uni, persona: "Andy Shah, senior recruiter; ran MLH hackathon booths at 3 events this year"}
      - {team: t_events, persona: "Anthony Novokshanov, recruiter, new to hackathons; receives the live demo email"}
    team_bridge: [t_uni, t_events]   # teams do overlapping campus/hackathon work; zero cross-team connections
  - id: kafka_lag
    people:
      - {team: t_payments, persona: "fixed consumer lag via shared/kafka-client batching"}
      - {team: t_messaging, persona: "debugging notifications backlog; also touches shared/kafka-client"}
  - id: rate_limit
    people:
      - {team: t_ads, persona: "built request throttling in services/ads/serving/throttle"}
      - {team: t_devplat, persona: "built gateway rate limiting in platform/gateway/ratelimit"}
  - id: amber_case          # cross-org; open task with no connection -> amber dot on viewer's report
    people:
      - {team: t_events, persona: "Sam Ortiz; open task (2 days old): build a candidate referral tracking dashboard"}
      - {team: t_growth, persona: "PM who built the growth funnel dashboard in web/growth/funnels"}

demo:
  viewer: {team: t_events, role: lead}   # DEFAULT_VIEWER_ID; Anthony and Sam report to this person
  email_recipient: Anthony Novokshanov
  slack_mapped: [viewer, Anthony Novokshanov, Andy Shah]   # real Slack accounts on camera

activity_per_person:
  engineer: {prs: [5, 10], reviews: [3, 6], slack_messages: [3, 5]}
  non_engineer: {slack_messages: [5, 8], emails: [4, 7]}

seed_history:                 # backdated over last 42 days, for synopsis trends
  connections: 25             # mostly within-org, ~8 cross-team, ~3 cross-org
  helpful_rate: 0.8
  statuses: {active: 18, declined: 3, expired: 2, accepted: 2}
```

### 9.2 `scripts/seed_generate.py`

1. Muse generates the roster (names, titles, emails, manager chain) from the spec. The script assigns ids (`p_001…`) deterministically in spec order. Team leads are managers; members report to their lead; leads report to an org head (the first lead listed per org).
2. For each person, Muse generates activity fixtures from a persona brief (team, title, planted-overlap instructions if any). Engineers get GitHub-shaped PR/review fixtures (§6.1) using that repo layout, plus Slack messages; others get Slack messages and sent emails.
3. Validate with Pydantic; assert each planted overlap's directories/keywords appear. Write to `seed/fixtures/*.json` and commit. Reruns reuse the committed fixtures unless `--regenerate`.

### 9.3 `scripts/seed_load.py`

Applies `schema.sql` (with `--reset`), loads orgs/teams/people, normalizes fixtures into `activity_events`, creates the amber-case task (`created_at = now() - 2 days`, status `open`), and inserts backdated `seed_history` connections plus matching `connection_events` (`is_seed = true`). Never create a connection between planted pairs. Sets `slack_user_id` for mapped personas from `seed/slack_map.json` (gitignored; `{person_name: slack_user_id}`).

### 9.4 `scripts/run_pipeline.py` (idempotent, all Muse calls cached)

1. `profiles.py`: P2 for every person, P3 for every team.
2. Hybrid mode only: embed people and `did` events.
3. `similarity.py`: `rank_for_person` for every person → symmetrize and score (§8.3) → upsert `similarities`.
4. `team_overlap.py`: P8 → upsert `team_overlaps` (score / 100); P9 lead brief for pairs ≥ `BRIDGE_MIN_SCORE`.
5. Print a report: each planted pair's rank and score, count of pairs ≥ `MISSED_MIN_SCORE`, bridges found. Planted pairs failing to rank 1–2 is a bug to fix (prompt or fixtures), not a threshold to lower silently.

---

## 10. Slack bot (`bot/`)

Bolt for Python, Socket Mode, started as a background task in FastAPI startup when `ENABLE_SLACK=true`.

**App config**: bot scopes `chat:write`, `im:write`, `mpim:write`, `im:history`, `mpim:history`, `users:read`; events `message.im`, `message.mpim`; interactivity on; app-level token with `connections:write`.

**Delivery rule**: people with `slack_user_id` get real Slack messages. Others, when `SIMULATE_UNMAPPED=true`, get simulated delivery: log it, and auto-accept after `SIMULATED_ACCEPT_SEC`; a simulated connection becomes `active` 10 s later with `message_count=4`. Actions return `delivered: "slack" | "simulated"`.

### Flows (Block Kit templates in `bot/messages.py`)

**Auto suggestion → requester** (`status=suggested`):
> New task spotted: *{task_summary}*. {helper_name} on {helper_team} {reason_lowercase_first}. Want to connect?
> [Connect] [Not now]

- Not now → `declined`; task `dismissed`.
- Connect → `requested`; send to helper:

**Request → helper**:
> {requester_name} ({requester_team}) is working on *{task_summary}*. You were suggested because {reason}. Up for a quick chat?
> [Sure] [Can't right now]

- Can't → `declined`; tell requester "{helper_first} can't right now. Looking for someone else." → §8.4 step 7.
- Sure → `conversations.open(users=[requester, helper])` (bot included) → post P10 icebreaker (message + 3 questions) and a [Grab 15 min] button → `accepted`, store `slack_channel_id`.

**Grab 15 min** (stub): posts "Suggested time: tomorrow 2:00–2:15 PM. Add it to your calendars?" No calendar API.

**Message counting**: `message.mpim` events in a channel matching a connection → `message_count += 1`, insert `connection_events(event='message', person_id)`. Store no content. When both people have sent ≥ 2 messages → `active`, set `active_at`.

**Feedback** (`bot/jobs.py`, loop every 30 s): `active` connections with `active_at < now() - FEEDBACK_AFTER_MIN` and `feedback_requested_at IS NULL` → DM both: "Was your chat with {other_name} helpful?" [Yes] [Not really] → set `helpful_*`, insert `connection_events(event='feedback', value)`.

**Manager nudge → employee** (`origin=manager_nudge`, `status=suggested`, `initiated_by=manager`):
> {manager_name} suggests reaching out to {target_name} on {target_team}. {reason}
> [Reach out] [Not now]

Reach out → continues at "Request → helper".

**Lead intro → lead A** (`origin=lead_intro`, requester = lead of team A, helper = lead of team B):
> {manager_name} wants to introduce you to {lead_b_name}, lead of {team_b}. {overlap_summary}
> [Connect] [Not now]

Continues at "Request → helper" (framed as a team intro). The group DM icebreaker also posts the P9 lead brief.

Every status change writes a `connection_events` row.

---

## 11. API contract

Base path `/api`. JSON only. Errors: `{"error": "message"}` with a 4xx/5xx status. `viewer_id` defaults to `DEFAULT_VIEWER_ID`. Mirror these as TypeScript types in `frontend/src/api/types.ts`.

At demo scale the whole graph (~40 people, ≤ 320 edges) ships in one response and **the frontend aggregates per zoom level**. (At company scale, aggregation would move server-side; out of scope.)

### `GET /api/graph?viewer_id=`
```json
{
  "viewer": {"id": "p_029", "name": "Dana Kim", "team_id": "t_events"},
  "orgs":   [{"id": "org_rec", "name": "Recruiting"}],
  "teams":  [{"id": "t_uni", "org_id": "org_rec", "name": "University Recruiting",
              "lead_id": "p_024", "summary": "..."}],
  "people": [{"id": "p_024", "team_id": "t_uni", "name": "Andy Shah", "title": "...",
              "is_lead": true, "is_viewer_report": false, "needs_connection": false,
              "open_task": null}],
  "edges":  [{"a": "p_024", "b": "p_031", "score": 0.91, "rank": 1,
              "state": "potential", "message_count": 0, "connection_id": null}],
  "bridges":[{"team_a": "t_events", "team_b": "t_uni", "score": 0.88, "has_connection": false}],
  "generated_at": "2026-09-27T14:02:11Z"
}
```
- `people[].open_task`: `{id, summary, created_at}` or `null` (latest `open|notified` task).
- `edges`: every `similarities` row, plus any connected pair lacking one (`score = match_score`, `rank = null`, always visible). `a < b`. `rank = LEAST(rank_a, rank_b)`. `state` per §5 derived states.
- `bridges`: `team_overlaps` with `score >= BRIDGE_MIN_SCORE`; `has_connection` = any `accepted|active` connection between members.

### `GET /api/people/{id}`
```json
{
  "id": "p_024", "name": "...", "title": "...",
  "team": {"id": "t_uni", "name": "...", "org_id": "org_rec"},
  "summary": "...", "focus_areas": ["campus events"],
  "github": {"top_directories": ["shared/kafka-client"], "languages": ["Python"],
             "commit_mix": {"fix": 12, "feat": 4}},
  "open_tasks": [{"id": 7, "summary": "...", "created_at": "...", "status": "open"}],
  "matches": [{"person_id": "p_031", "name": "...", "team_name": "...", "score": 0.91,
               "reason": "...", "state": "potential", "shared_dirs": []}],
  "connections": [{"id": 3, "other_id": "p_012", "other_name": "...", "status": "active",
                   "origin": "auto", "created_at": "...", "helpful": true}]
}
```
`github` is `null` for non-engineers. `matches` = top `TOPK_STORE`, ordered by score. `connections[].helpful` = this person's rating.

### `GET /api/pairs/{a}/{b}`
```json
{
  "a": {"id": "...", "name": "...", "team_name": "..."}, "b": {"...": "..."},
  "semantic_score": 0.86, "dir_overlap": 0.2, "score": 0.91,
  "reason": "...", "shared_dirs": ["shared/kafka-client"],
  "connections": [{"id": 3, "status": "active", "origin": "auto", "task_summary": "...",
                   "created_at": "...", "accepted_at": "...", "message_count": 6,
                   "helpful_requester": true, "helpful_helper": null,
                   "timeline": [{"time": "...", "event": "suggested", "person_id": null}]}]
}
```

### `GET /api/bridges/{team_a}/{team_b}`
```json
{
  "team_a": {"id": "...", "name": "...", "lead": {"id": "...", "name": "..."}},
  "team_b": {"...": "..."},
  "score": 0.88, "summary": "...", "shared_topics": ["hackathon booths"],
  "lead_brief": "markdown...",
  "top_pairs": [{"a": "...", "b": "...", "a_name": "...", "b_name": "...", "score": 0.91, "reason": "..."}],
  "connections_count": 0
}
```
`top_pairs`: up to 5 highest-scoring cross-team `similarities`.

### `GET /api/search?q=&viewer_id=`
```json
{"results": [{"person_id": "p_024", "name": "...", "team_id": "t_uni", "team_name": "...",
              "org_id": "org_rec", "score": 0.93, "reason": "..."}]}
```

### `GET /api/synopsis?days=30&viewer_id=`
Org-wide (see §15 open decision).
```json
{
  "summary": "AI-written, <= 60 words",
  "stats": {"connections_made": 14, "helpful_rate": 0.83,
            "cross_team_share": 0.57, "median_minutes_to_connect": 11},
  "missed_opportunities": [{"a": "...", "b": "...", "a_name": "...", "b_name": "...",
                            "a_team": "...", "b_team": "...", "score": 0.9, "reason": "..."}],
  "teams_should_talk": [{"team_a": "...", "team_b": "...", "team_a_name": "...",
                         "team_b_name": "...", "score": 0.88, "summary": "..."}],
  "trend": [{"day": "2026-09-01", "suggested": 3, "accepted": 2}]
}
```
Definitions (window = last `days`):
- `connections_made`: connections reaching `accepted` in window.
- `helpful_rate`: `true` ratings / all ratings in window.
- `cross_team_share`: accepted connections whose people are on different teams / accepted connections.
- `median_minutes_to_connect`: `tasks.created_at → connections.accepted_at`, auto-origin only.
- `missed_opportunities`: top 5 pairs with `score >= MISSED_MIN_SCORE` and no connection ever.
- `teams_should_talk`: top 3 bridges with `has_connection = false`.
- `trend`: from `connection_daily`, one row per day, zero-filled.
- `summary`: P11 on the stats; cache 10 minutes.

### `POST /api/actions/nudge`
Body `{viewer_id, employee_id, target_id}` → creates connection (`manager_nudge`) and sends §10 nudge. Response `{connection_id, delivered: "slack" | "simulated"}`. 409 if a `suggested|requested|accepted|active` connection already exists for the pair.

### `POST /api/actions/introduce-leads`
Body `{viewer_id, team_a, team_b}` → creates connection (`lead_intro`) between the leads. Response as above.

### Dev routes (`ENABLE_DEV_ROUTES=true` only)
- `POST /api/dev/simulate-event` body `{person_id, source: "email", title, text}` → runs the exact pipeline of a real incoming email. Response `{task_id | null, detection, candidates, connection_id | null}`. Fallback if Gmail misbehaves on camera.
- `POST /api/dev/reset-demo` → deletes non-seed tasks, connections, and connection events; resets planted pairs to no connection; restores the amber-case task. For rehearsing the video.

CORS: allow `http://localhost:5173`.

---

## 12. Frontend

### 12.1 Shell

- Routes: `/graph` (default) and `/synopsis`.
- **Top bar**: product name, tabs (Graph, Synopsis), search box, "Viewing as {viewer name}".
- Data: TanStack Query. `/api/graph` polls every `VITE_POLL_MS`. With `VITE_USE_MOCKS=true`, `api/client.ts` returns `src/mocks/*.json` (one per endpoint, matching §11) so frontend work proceeds before the backend exists.
- Colors as CSS variables in `theme.css`: one hue per org (teams use tints of their org hue); edges: potential = slate gray, pending = blue (dashed), connected = teal (solid); bridge = purple translucent band; **amber reserved for "needs connection" alerts only**.

### 12.2 Graph view — semantic zoom (Prezi-style)

**Layout**: `d3.hierarchy(root → orgs → teams → people)`, people `value = 1`, `d3.pack()` sized to the viewport with padding increasing by depth. Classic D3 zoomable circle packing: a `focus` node and a view `[x, y, r]` animated with `d3.interpolateZoom` over 750 ms.

**Levels** (the `focus` node):
| Focus | Labeled units (children of focus) | External units (outside focus) |
|---|---|---|
| root | orgs | none |
| org O | teams in O (people show as small unlabeled dots) | other orgs |
| team T | people in T | other teams (any org) |

- **Initial focus**: the viewer's team. Viewer's reports get a highlight ring; at org/root focus, the viewer's team circle is outlined.
- Labels only on units at the current level. Never more than ~20 labeled nodes on screen.
- Non-focus pack circles fade to ~0.08 opacity.

**Navigation**: click a unit → zoom into it (people open the side panel instead). Breadcrumb (`Meta › Recruiting › Events & Community Recruiting`) is always visible; clicking a crumb zooms there. `Esc` or scroll-wheel out (debounced 400 ms) → zoom to parent. Scroll-in does nothing.

**External ring** (`graph/externalRing.ts`): when focus ≠ root, every external unit that has at least one visible edge to a unit inside focus renders as a small labeled bubble on a ring at 1.15 × the focus circle's screen radius. Its angle = direction from the focus center to that unit's center in the pack layout (keeps spatial consistency). Clicking it zooms to that unit.

**Edge aggregation** (`graph/aggregate.ts`), recomputed on every focus change and poll:
```
unitOf(person, focus):
  root    -> person's org
  org O   -> person's team if team.org == O else person's org
  team T  -> person if person.team == T else person's team

1. Filter edges: state toggles; visible if rank <= topN OR rank == null OR state != 'potential'.
2. Map endpoints to units; drop edges where unitA == unitB.
3. At non-root focus, drop edges where neither unit is inside focus.
4. Group by unit pair: {connected, pending, potential, missed (potential && score >= MISSED_MIN), maxScore, messageTotal}.
```
MISSED_MIN on the frontend mirrors the backend default (0.70).

**Edge styling** (`graph/edges.ts`):
- Any `connected` → solid teal, width `1.5 + 1.5 * log2(1 + connected)` (person level: `2 + log2(1 + message_count)`).
- Else any `pending` → dashed blue (`stroke-dasharray: 6 4`).
- Else potential → gray, opacity `0.15 + 0.6 * maxScore`, width `1 + 2 * maxScore`.
- Hover an aggregated edge → tooltip "5 connections · 2 pending · 3 missed opportunities". Click → person level: pair panel; aggregated: zoom to the endpoint inside focus.

**Bridges**: at org and team focus, for each bridge whose two teams map to two different visible units, draw a wide translucent purple band between those units (under edges). Hidden at root. Click → bridge panel.

**Amber badges**: a person with `needs_connection` gets an amber dot. Teams/orgs show an amber count badge ("2") summing descendants; the badge tooltip reads "2 people have new tasks with nobody connected yet."

**Live updates**: diff each poll against the previous one by edge key `a|b`. An edge whose state became `connected` animates in (stroke-dashoffset draw, 1 s) and both endpoints pulse once. New pending edges fade in. If the changed edge is inside a collapsed unit, pulse the unit.

**Toolbar**: team filter chips (non-selected teams dim), edge-type toggles (Potential / Pending / Connected), "Matches per person" slider 1–8 (default 4 = `topN`).

**Search** (`SearchBox`): debounce 300 ms, call `/api/search`, show a dropdown of results with reasons (loading spinner; Muse takes a few seconds). Selecting a result animates root → org → team (500 ms each), pulses the person, and opens the person panel.

### 12.3 Side panels (slide in from the right)

**Person panel** (`/api/people/{id}`): name, title, team; AI summary; focus-area chips; engineers: top directories, languages, commit-mix mini bar; open tasks (amber styling if needs connection); "People who do similar work": top matches, each with score bar, reason, state chip, shared dirs, and a **Suggest they reach out** button (visible when the person is a viewer report and the pair state is `potential`) → `POST /api/actions/nudge` → toast "Sent via Slack" or "Sent (simulated)". Then past connections with helpful marks.

**Pair panel** (`/api/pairs/{a}/{b}`): both people, final score with semantic/code-overlap breakdown, reason, shared dirs, connection history with timeline.

**Bridge panel** (`/api/bridges/{a}/{b}`): both teams and leads, overlap summary, shared-topic chips, rendered lead brief, top person pairs, and **Introduce the team leads** → `POST /api/actions/introduce-leads`.

### 12.4 Synopsis view (`/api/synopsis`)

Top to bottom: AI summary card; 4 stat cards (Connections made, Rated helpful %, Cross-team %, Median time to connect); line chart (Recharts) of suggested vs accepted per day; "Missed opportunities" list (pair, teams, reason, Nudge button when one side is a viewer report); "Teams that should be talking" list (summary, Introduce leads button). Buttons call the same actions as the graph.

---

## 13. Runtime

- `uvicorn app.main:app --port 8000` runs everything: the API, the Slack Socket Mode handler (`ENABLE_SLACK`), the Gmail poller (`ENABLE_GMAIL`), and the feedback/simulation loop, as asyncio background tasks started in the FastAPI lifespan.
- `npm run dev` in `frontend/` (port 5173, `/api` proxied).
- Setup order: `pip install -r requirements.txt` (+ `requirements-hybrid.txt` for Mode B) → `python -m scripts.seed_load --reset` → `python -m scripts.run_pipeline` → start the server.

---

## 14. Build plan (phases can be split across agents)

**Phase 0 — Scaffold** (one agent): repo layout (§3), env files, `schema.sql` applied to Tiger Cloud, FastAPI health route, Vite app with routing and Tailwind.

**Phase 1A — Backend data + AI** (Mode A):
seed spec → `seed_generate` → `seed_load` → Muse client → profiles → similarities → team overlaps → read endpoints (`graph`, `people`, `pairs`, `bridges`, `search`, `synopsis`) → `export_mocks.py`.
Acceptance: pipeline report shows every planted pair at rank 1–2 with score ≥ `AUTO_NOTIFY_THRESHOLD`; the `t_uni`/`t_events` bridge exists; every person has ≥ 3 stored matches; all endpoints validate against §11.

**Phase 1B — Frontend on mocks** (in parallel):
circle packing with three zoom levels, breadcrumb, external ring, edge aggregation and styling, bridges, amber badges, toolbar, panels, synopsis.
Acceptance: navigating root → org → team → person and back never shows more than ~20 labeled nodes; aggregated edges and badges match hand-computed values from the mocks.

**Phase 2 — Integration**: frontend on the real API, polling, live edge animation, search fly-to.

**Phase 3 — Live loop**: task pipeline (§8.4), Slack bot flows (§10), actions, dev routes, Gmail poller, feedback loop.
Acceptance: `POST /api/dev/simulate-event` for Anthony with the MLH email → Anthony's Slack DM suggests Andy within ~10 s → Connect → Andy's Sure → group DM with icebreaker → dashboard edge turns dashed, then solid after both send 2 messages. Nudge and lead intro work end to end. The same flow works from a real email.

**Phase 4 — Mode B** (after the demo path is solid): embedder, embeddings in the pipeline, `HybridMatcher`.
Acceptance: with `MATCHER_MODE=hybrid` and a pipeline rerun, all Phase 1A acceptance checks still pass; no other code changes are needed to switch modes.

Rules for agents: don't add features, endpoints, or tables beyond this spec. Don't store chat content. Don't call any AI provider except the Meta Model API (and the local Contriever model in Mode B). Keep every Muse call behind `muse_json`.

---

## 15. Verify before building / open decisions

Verify:
- Exact Muse model ID via `GET https://api.meta.ai/v1/models`.
- Structured-output request format and prompt-caching mechanics in Meta's docs.
- Whether low reasoning effort is accepted.
- Continuous aggregate support on the Tiger Cloud free tier (fallback in §5).

Open decisions (defaults in place; change here if the team decides otherwise):
- Synopsis and graph are **org-wide** for every manager (default) vs scoped to the manager's area.
- Final threshold values (tune after the first pipeline run).
- Product name: **Musketeer** (decided).

---

## 16. Demo video flow (what the build must support)

1. Recruiter story as the hook (voiceover).
2. Live email to Anthony: "Can you run our booth at the MLH hackathon next month?"
3. Anthony's Slack DM suggests Andy with a reason → Connect → Andy (second screen) taps Sure → group DM with icebreaker; they exchange a couple of messages.
4. Dashboard (viewer = Anthony's manager): the Anthony–Andy edge animates from dashed to solid.
5. Zoom root → Recruiting → teams → people; show the `t_uni`/`t_events` bridge and "Introduce the team leads."
6. Amber dot on Sam → person panel → faint match to the Growth PM → "Suggest they reach out."
7. Search "who has built rate limiting?" → fly-to.
8. Synopsis page. Close on the AI's role: task detection, matching, reasons, icebreakers, summaries — all Muse Spark (Mode B: plus Meta's Contriever for retrieval at scale).
