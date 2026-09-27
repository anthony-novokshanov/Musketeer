"""Tests run against the real Tiger service, isolated in a throwaway `musketeer_test` schema.

DATABASE_URL is rewritten (before `app` is imported) so every connection uses
search_path=musketeer_test,public; extensions stay in public.
"""
import asyncio
import os
import sys
from urllib.parse import quote

import psycopg
import pytest
from dotenv import dotenv_values

TEST_SCHEMA = "musketeer_test"

_base_url = os.environ.get("DATABASE_URL") or dotenv_values(".env")["DATABASE_URL"]
_sep = "&" if "?" in _base_url else "?"
os.environ["DATABASE_URL"] = f"{_base_url}{_sep}options={quote(f'-csearch_path={TEST_SCHEMA},public')}"
os.environ["ENABLE_SLACK"] = "false"
os.environ["ENABLE_GMAIL"] = "false"

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from app.config import settings  # noqa: E402
from app.db import apply_schema, pool  # noqa: E402

# Never touch the real tables: if `app` was configured before this file could point it at the test schema
# (e.g. an IDE runner that imported it early), stop before any test runs.
if f"search_path%3D{TEST_SCHEMA}" not in settings.DATABASE_URL:
    pytest.exit(f"tests must use the `{TEST_SCHEMA}` schema; app.config loaded the real database first. "
                "Run pytest from backend/ (python -m pytest).", returncode=3)


def _recreate_schema(drop_only: bool = False) -> None:
    with psycopg.connect(_base_url, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA IF EXISTS {TEST_SCHEMA} CASCADE")
        if not drop_only:
            conn.execute(f"CREATE SCHEMA {TEST_SCHEMA}")


@pytest.fixture
def fake_muse(monkeypatch, tmp_path):
    """Replace the Muse network call. Set `fake.handler(prompt_text, schema) -> dict | str`.
    Records every call in `fake.calls` as (schema_name, messages)."""
    import json

    from app.ai import muse
    from app.config import settings

    class Fake:
        calls: list = []
        handler = None

    fake = Fake()
    fake.calls = []

    async def _complete(messages, schema):
        fake.calls.append((schema.__name__, messages))
        out = fake.handler(messages[-1]["content"], schema)
        return out if isinstance(out, str) else json.dumps(out)

    monkeypatch.setattr(muse, "_complete", _complete)
    monkeypatch.setattr(settings, "MUSE_CACHE_DIR", str(tmp_path / "muse"))
    return fake


@pytest.fixture(scope="module")
async def seeded(tmp_path_factory):
    """Fresh tables + synthetic fixtures loaded + batch pipeline run with a fake Muse.
    Module-scoped because other modules reset the tables. Yields the roster dict."""
    import json

    from app.ai import muse
    from app.config import settings
    from scripts.run_pipeline import run
    from scripts.seed_load import load
    from tests.synthetic import build_fixtures, fake_muse_handler

    fixtures = tmp_path_factory.mktemp("fixtures")
    roster = build_fixtures(fixtures)
    handler = fake_muse_handler(roster)

    async def _complete(messages, schema):
        return json.dumps(handler(messages[-1]["content"], schema))

    import scripts.seed_load

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(scripts.seed_load, "SLACK_MAP", fixtures / "no_slack_map.json")  # never the real mapping
        mp.setattr(muse, "_complete", _complete)
        mp.setattr(settings, "MUSE_CACHE_DIR", str(tmp_path_factory.mktemp("muse")))
        summary = await load(fixtures, reset=True)
        await run()
    yield {**roster, "load_summary": summary, "fixtures": fixtures}


@pytest.fixture(scope="session", autouse=True)
async def test_db():
    _recreate_schema()
    async with await psycopg.AsyncConnection.connect(settings.DATABASE_URL) as conn:
        schema = (await (await conn.execute("SELECT current_schema()")).fetchone())[0]
    if schema != TEST_SCHEMA:
        pytest.exit(f"test connection resolved to schema {schema!r}, not {TEST_SCHEMA!r}; refusing to run", returncode=3)
    await apply_schema()
    await pool.open()
    yield
    await pool.close()
    _recreate_schema(drop_only=True)
