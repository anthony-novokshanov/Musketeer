from fastapi import APIRouter, HTTPException
from pydantic import ValidationError

from app import google, prefs
from app.config import settings

router = APIRouter()


async def _out() -> dict:
    p = await prefs.get()
    return {**p.model_dump(),
            # what is actually wired up, so the page can show honest "Not connected" states
            "available": {"gmail": settings.ENABLE_GMAIL and bool(settings.GMAIL_QUERY.strip()),
                          "slack": settings.ENABLE_SLACK,
                          "calendar": google.has_account(settings.DEMO_EMAIL_FALLBACK_PERSON)}}


@router.get("/settings")
async def get_settings():
    return await _out()


@router.put("/settings")
async def put_settings(body: dict):
    try:
        await prefs.put({k: v for k, v in body.items() if k != "available"})
    except ValidationError as e:
        raise HTTPException(422, "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()))
    return await _out()
