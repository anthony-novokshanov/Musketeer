from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    DATABASE_URL: str
    MODEL_API_KEY: str = ""
    MUSE_MODEL: str = "muse-spark-1.3"
    MUSE_CACHE_DIR: str = ".cache/muse"
    MATCHER_MODE: str = "muse"

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
    AUTO_NOTIFY_THRESHOLD: float = 0.75
    MISSED_MIN_SCORE: float = 0.70
    BRIDGE_MIN_SCORE: float = 0.70
    AMBER_AFTER_MIN: int = 2
    FEEDBACK_AFTER_MIN: int = 3


settings = Settings()
