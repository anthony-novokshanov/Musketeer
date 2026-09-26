import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db import fetch_one, pool

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await pool.open()
    if settings.ENABLE_SLACK:
        from app.bot import slack_bot
        await slack_bot.start()
    # Gmail poller and background jobs start here (Phase 3).
    yield
    if settings.ENABLE_SLACK:
        await slack_bot.stop()
    await pool.close()


app = FastAPI(title="Bridge", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health():
    row = await fetch_one("SELECT extversion AS timescaledb FROM pg_extension WHERE extname = 'timescaledb'")
    return {"ok": True, "db": row}
