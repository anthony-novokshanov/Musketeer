import re

import pytest

from app.ai.muse import PROMPTS_DIR, MuseError, muse_json, render
from app.schemas import DetectTaskOut

DETECT = {"title": "MLH booth", "text": "Can you run our booth?", "recipient_name": "Jordan Lee",
          "recipient_title": "Recruiter", "recipient_team": "Events"}
GOOD = {"is_new_task": True, "confidence": 0.9, "summary": "Run the MLH booth", "task_type": "event"}


def test_every_prompt_renders_and_ranking_prompts_carry_rubric():
    for path in PROMPTS_DIR.glob("*.md"):
        names = set(re.findall(r"\{\{(\w+)\}\}", path.read_text(encoding="utf-8")))
        text = render(path.stem, {n: "X" for n in names})
        assert "{{" not in text
    for name in ("rank_for_person", "rank_for_task", "search"):
        assert "Below 50: omit." in (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def test_render_missing_variable_raises():
    with pytest.raises(KeyError):
        render("detect_task", {"title": "x"})


async def test_cache_hit_skips_network(fake_muse):
    fake_muse.handler = lambda prompt, schema: GOOD
    a = await muse_json("detect_task", DETECT, DetectTaskOut)
    b = await muse_json("detect_task", DETECT, DetectTaskOut)
    assert a == b and a.summary == "Run the MLH booth"
    assert len(fake_muse.calls) == 1


async def test_cache_off_always_calls(fake_muse):
    fake_muse.handler = lambda prompt, schema: GOOD
    await muse_json("detect_task", DETECT, DetectTaskOut, cache=False)
    await muse_json("detect_task", DETECT, DetectTaskOut, cache=False)
    assert len(fake_muse.calls) == 2


async def test_retries_invalid_output_then_succeeds(fake_muse):
    replies = iter(["not json", {"is_new_task": True}, GOOD])
    fake_muse.handler = lambda prompt, schema: next(replies)
    out = await muse_json("detect_task", DETECT, DetectTaskOut)
    assert out.task_type == "event"
    assert len(fake_muse.calls) == 3


async def test_gives_up_after_two_retries(fake_muse):
    fake_muse.handler = lambda prompt, schema: "nope"
    with pytest.raises(MuseError):
        await muse_json("detect_task", DETECT, DetectTaskOut)
    assert len(fake_muse.calls) == 3


async def test_prefix_is_first_message(fake_muse):
    fake_muse.handler = lambda prompt, schema: GOOD
    await muse_json("detect_task", DETECT, DetectTaskOut, prefix="ROSTER")
    messages = fake_muse.calls[0][1]
    assert messages[0] == {"role": "system", "content": "ROSTER"}
    assert messages[1]["role"] == "user"
