"""Muse Spark client (spec §7.1). Every Muse call in the app goes through muse_json."""
import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.config import settings

log = logging.getLogger("musketeer.muse")

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
MAX_RETRIES = 2
REQUEST_TIMEOUT_SEC = 90   # the SDK default (600 s x 3 attempts) can stall a pipeline or a live suggestion
_semaphore = asyncio.Semaphore(4)
_client: AsyncOpenAI | None = None


class MuseError(Exception):
    pass


def render(prompt_name: str, variables: dict) -> str:
    template = (PROMPTS_DIR / f"{prompt_name}.md").read_text(encoding="utf-8")
    missing = set(re.findall(r"\{\{(\w+)\}\}", template)) - variables.keys()
    if missing:
        raise KeyError(f"prompt {prompt_name} missing variables: {sorted(missing)}")
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(variables[m.group(1)]), template)


async def _complete(messages: list[dict], schema: type[BaseModel]) -> str:
    global _client
    if _client is None:
        _client = AsyncOpenAI(base_url="https://api.meta.ai/v1", api_key=settings.MODEL_API_KEY,
                              timeout=REQUEST_TIMEOUT_SEC)
    # Verified against the live API: standard json_schema structured output works; reasoning_effort
    # accepts minimal|low|medium|high|xhigh|max ("none" is rejected); prompt caching is automatic.
    resp = await _client.chat.completions.create(
        model=settings.MUSE_MODEL,
        messages=messages,
        reasoning_effort="low",
        response_format={"type": "json_schema",
                         "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema()}},
    )
    return resp.choices[0].message.content


def _cache_path(key_material: str) -> Path:
    return Path(settings.MUSE_CACHE_DIR) / f"{hashlib.sha256(key_material.encode()).hexdigest()}.json"


async def muse_json(prompt_name: str, variables: dict, schema: type[BaseModel],
                    *, cache: bool = True, prefix: str | None = None) -> BaseModel:
    prompt = render(prompt_name, variables)
    # The roster prefix goes first and byte-identical so the provider can cache it.
    messages = ([{"role": "system", "content": prefix}] if prefix else []) + [{"role": "user", "content": prompt}]

    path = _cache_path(settings.MUSE_MODEL + prompt_name + (prefix or "") + prompt)
    if cache and path.exists():
        return schema.model_validate_json(path.read_text(encoding="utf-8"))

    last_error = None
    for attempt in range(MAX_RETRIES + 1):
        async with _semaphore:
            raw = await _complete(messages, schema)
        try:
            result = schema.model_validate_json(raw)
        except ValidationError as e:
            last_error = e
            log.warning("muse %s: invalid output (attempt %d): %s", prompt_name, attempt + 1, e.errors()[:1])
            continue
        if cache:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(result.model_dump(mode="json")), encoding="utf-8")
        return result
    raise MuseError(f"{prompt_name}: invalid output after {MAX_RETRIES + 1} attempts: {last_error}")
