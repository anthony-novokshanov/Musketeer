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
DROP TABLE IF EXISTS meetings, connection_events, connections, team_overlaps, similarities,
  tasks, activity_events, people, teams, orgs CASCADE;
"""


async def apply_schema(reset: bool = False) -> None:
    """Apply sql/schema.sql one statement at a time in autocommit mode
    (continuous aggregates can't be created inside a transaction)."""
    sql = re.sub(r"--[^\n]*", "", SCHEMA_FILE.read_text())
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    async with await psycopg.AsyncConnection.connect(settings.DATABASE_URL, autocommit=True) as conn:
        if reset:
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
