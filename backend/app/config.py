from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str
    MODEL_API_KEY: str = ""
    MUSE_MODEL: str = "muse-spark-1.3"
    MUSE_CACHE_DIR: str = ".cache/muse"
    MATCHER_MODE: str = "muse"
    EMBED_MODEL: str = "facebook/contriever-msmarco"   # hybrid mode: Meta FAIR Contriever (spec §8.6)
    EMBED_POOL: int = 10                        # hybrid: extra candidates found by meaning, beyond the temporal pool
    EMBED_EVENT_NEIGHBORS: int = 200            # hybrid: nearest work items fetched before recency weighting
    TEMPORAL_MODEL: str = "decay"               # decay | learned (learned = stretch, §7.7)

    # Temporal expertise graph (§7)
    HALF_LIFE_DAYS: float = 30                  # default evidence half-life
    HALF_LIFE_HELPED_DAYS: float = 60           # 'helped' evidence (from feedback) fades slower
    EXPERTISE_WINDOW_DAYS: int = 180            # evidence older than this is ignored
    STRENGTH_SCALE: float = 2.0                 # level = 1 - exp(-strength / STRENGTH_SCALE)
    MIN_EVIDENCE_CONFIDENCE: float = 0.6        # drop extracted evidence below this
    RELATED_SKILL_WEIGHT: float = 0.35          # how much related skills count (skill-graph smoothing)
    SKILL_EDGE_MIN_COOCCUR: int = 2
    NEW_SKILL_DAYS: int = 14                    # first_seen within this -> trend 'new'
    EXPERTISE_REFRESH_MIN: int = 10             # background recompute of state + temporal scores (no Muse)

    # Scoring blend (§9.3)
    W_SEMANTIC: float = 0.6                     # Muse judgment
    W_TEMPORAL: float = 0.4                     # temporal graph (decay or learned)
    DIR_BONUS_WEIGHT: float = 0.15
    NOVELTY_BONUS: float = 0.10                 # never interacted with requester
    LOAD_PENALTY_PER_REQUEST: float = 0.05      # per helper request in last 7 days
    LOAD_PENALTY_MAX: float = 0.15
    UNHELPFUL_PENALTY: float = 0.10             # same helper, overlapping skills, 30 days after "Not really"
    FEEDBACK_HELPED_WEIGHT: float = 1.5

    SLACK_BOT_TOKEN: str = ""
    SLACK_APP_TOKEN: str = ""
    ENABLE_SLACK: bool = False
    SIMULATE_UNMAPPED: bool = True
    SIMULATED_ACCEPT_SEC: int = 8

    ENABLE_GMAIL: bool = False
    GMAIL_POLL_SEC: int = 5
    GMAIL_QUERY: str = ""                       # required: which mail the demo may read, e.g. "to:me+musketeer@x.com"; fires once read
    GMAIL_CREDENTIALS: str = "credentials.json"
    GOOGLE_TOKEN_DIR: str = "tokens"            # per-person Google sign-ins: python -m app.google <person_id>
    DEMO_TIMEZONE: str = "America/New_York"
    MEETING_MINUTES: int = 30
    DEMO_EMAIL_FALLBACK_PERSON: str = "p_031"

    DEFAULT_VIEWER_ID: str = "p_029"
    ENABLE_DEV_ROUTES: bool = True

    TOPK_STORE: int = 8
    CANDIDATE_POOL: int = 15                    # temporal candidates handed to Muse for rerank
    AUTO_NOTIFY_THRESHOLD: float = 0.75
    MISSED_MIN_SCORE: float = 0.70
    BRIDGE_MIN_SCORE: float = 0.70
    AMBER_AFTER_MIN: int = 2
    FEEDBACK_AFTER_MIN: int = 3


settings = Settings()
