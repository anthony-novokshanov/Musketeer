from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import settings

pool = AsyncConnectionPool(
    settings.DATABASE_URL,
    min_size=1,
    max_size=10,
    open=False,
    kwargs={"row_factory": dict_row},
)


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
