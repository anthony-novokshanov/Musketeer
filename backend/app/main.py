import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import actions, auto_group, bridges, dev, graph, pairs, people, search, settings_api, synopsis
from app.bot import flows, jobs
from app.config import settings
from app.db import fetch_one, pool
from app.pipeline import similarity

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await pool.open()
    if settings.ENABLE_SLACK:
        from app.bot import slack_bot
        await slack_bot.start()
    loops = [asyncio.create_task(jobs.feedback_loop()),
             asyncio.create_task(similarity.refresh_loop(settings.EXPERTISE_REFRESH_MIN))]
    if settings.ENABLE_GMAIL:
        from app.connectors import gmail
        loops.append(asyncio.create_task(gmail.poll_loop()))
    yield
    for t in loops:
        t.cancel()
    flows.cancel_background()
    if settings.ENABLE_SLACK:
        await slack_bot.stop()
    await pool.close()


app = FastAPI(title="Musketeer", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
for module in (graph, people, pairs, bridges, search, synopsis, actions, settings_api, auto_group):
    app.include_router(module.router, prefix="/api")
if settings.ENABLE_DEV_ROUTES:
    app.include_router(dev.router, prefix="/api")


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    return JSONResponse({"error": str(exc.detail)}, status_code=exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    return JSONResponse({"error": "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())},
                        status_code=422)


FRONTEND = Path(__file__).resolve().parents[2] / "musketeerfront.html"
AVATARS = Path(__file__).resolve().parents[1] / "static" / "avatars"
AVATARS.mkdir(parents=True, exist_ok=True)
app.mount("/avatars", StaticFiles(directory=AVATARS), name="avatars")  # headshots: python -m scripts.fetch_avatars


@app.get("/", include_in_schema=False)
async def frontend():
    """The dashboard: a single HTML page that talks to /api on the same origin."""
    return FileResponse(FRONTEND)


@app.get("/api/health")
async def health():
    row = await fetch_one("SELECT extversion AS timescaledb FROM pg_extension WHERE extname = 'timescaledb'")
    return {"ok": True, "db": row}
