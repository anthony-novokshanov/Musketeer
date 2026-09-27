"""Manager-editable settings (dashboard Settings tab), stored in app_settings and applied without a restart."""
from typing import Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from app.config import settings
from app.db import execute, fetch_one

CLASSIFIER_CONFIDENCE = {"conservative": 0.85, "balanced": 0.70, "eager": 0.50}


class Sources(BaseModel):
    gmail: bool = True      # task detection from the demo inbox
    slack: bool = True      # task detection from public-channel mentions
    calendar: bool = True   # free/busy for scheduling and "wait for meetings"


class Prefs(BaseModel):
    sources: Sources = Sources()
    classifier: Literal["conservative", "balanced", "eager"] = "balanced"
    max_requests_per_week: int = Field(3, ge=1, le=10)   # an expert at the cap is skipped
    prefer_new_connections: bool = True                  # the +0.10 "never interacted" bonus (spec §8.4)
    wait_for_busy: bool = True                           # hold requests while the expert's calendar is busy
    team_leads: bool = True                              # team-level work also introduces the two team leads
    feedback_after_min: int = Field(settings.FEEDBACK_AFTER_MIN, ge=1)


_cache: Prefs | None = None


async def get() -> Prefs:
    global _cache
    if _cache is None:
        row = await fetch_one("SELECT value FROM app_settings WHERE key = 'prefs'")
        _cache = Prefs.model_validate(row["value"]) if row else Prefs()
    return _cache


async def put(update: dict) -> Prefs:
    """Merge a partial update into the stored settings and return the result."""
    global _cache
    current = (await get()).model_dump()
    merged = {**current, **update, "sources": {**current["sources"], **update.get("sources", {})}}
    prefs = Prefs.model_validate(merged)
    await execute("""INSERT INTO app_settings (key, value) VALUES ('prefs', %s)
                     ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()""",
                  (Jsonb(prefs.model_dump()),))
    _cache = prefs
    return prefs
