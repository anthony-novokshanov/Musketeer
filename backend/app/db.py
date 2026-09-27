import asyncio
import re
import selectors
import sys
from pathlib import Path
from typing import Any, Coroutine

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import settings

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"
EXPERTISE_FILE = SCHEMA_FILE.with_name("expertise.sql")   # idempotent; also applied alone by migrate()

pool = AsyncConnectionPool(
    settings.DATABASE_URL,
    min_size=1,
    max_size=10,
    open=False,
    kwargs={"row_factory": dict_row},
)


def run_async(coro: Coroutine) -> Any:
    """asyncio.run, but on Windows with a SelectorEventLoop (psycopg async can't use Proactor)."""
    if sys.platform == "win32":
        return asyncio.run(coro, loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()))
    return asyncio.run(coro)


async def fetch_all(sql: str, params: dict | tuple | None = None) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def fetch_one(sql: str, params: dict | tuple | None = None) -> dict[str, Any] | None:
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchone()


async def execute(sql: str, params: dict | tuple | None = None) -> None:
    async with pool.connection() as conn:
        await conn.execute(sql, params)


DROP_ALL = """
DROP MATERIALIZED VIEW IF EXISTS connection_daily CASCADE;
DROP TABLE IF EXISTS match_feedback, skill_edges, person_skill_state, expertise_events, skill_labels,
  skills, app_settings, meetings, connection_events, connections, team_overlaps, similarities,
  tasks, activity_events, people, teams, orgs CASCADE;
"""


async def apply_schema(reset: bool = False, allow_public_reset: bool = False) -> None:
    """Apply sql/schema.sql then sql/expertise.sql one statement at a time in autocommit mode
    (continuous aggregates can't be created inside a transaction).

    reset drops every table first. Wiping the real `public` schema is refused unless the caller
    explicitly opts in (only `python -m scripts.seed_load --reset` does); tests never can."""
    await _apply([SCHEMA_FILE, EXPERTISE_FILE], reset, allow_public_reset)


async def migrate() -> None:
    """Bring an existing database up to date without touching its data (expertise.sql is idempotent)."""
    await _apply([EXPERTISE_FILE])


async def _apply(files: list[Path], reset: bool = False, allow_public_reset: bool = False) -> None:
    sql = re.sub(r"--[^\n]*", "", "\n".join(f.read_text() for f in files))
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    async with await psycopg.AsyncConnection.connect(settings.DATABASE_URL, autocommit=True) as conn:
        if reset:
            schema = (await (await conn.execute("SELECT current_schema()")).fetchone())[0]
            if schema == "public" and not allow_public_reset:
                raise RuntimeError("refusing to drop the real `public` tables; only `seed_load --reset` may do that")
            await conn.execute(DROP_ALL)
        search_path = (await (await conn.execute("SHOW search_path")).fetchone())[0]
        for stmt in statements:
            if stmt.upper().startswith("CREATE EXTENSION"):
                # Extensions live in public; Tiger's extension hook fails under another
                # search_path and resets it afterwards, so restore it.
                await conn.execute(f"{stmt} SCHEMA public")
                await conn.execute("SELECT set_config('search_path', %s, false)", (search_path,))
            else:
                await conn.execute(stmt)
