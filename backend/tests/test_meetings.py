"""Meeting notes / deliverable / next steps. No DB and no Slack: both are faked."""
from datetime import date

import pytest

from app.bot import meetings
from app.schemas import DraftOut, MeetingBriefOut, NextStepsOut

CANVAS_HTML = """<div class="quip-canvas-content"><h1>Anthony + Andy: MLH booth</h1>
<h2>Notes</h2><p>Bring twice the swag.</p><h2>Checklist</h2>
<ul><li class="checked">Order swag</li><li>Book two staffers</li></ul>
<h2>Open questions</h2><ul><li>Which sponsor tier?</li></ul></div>"""


def test_canvas_html_to_text_keeps_structure():
    text = meetings.canvas_html_to_text(CANVAS_HTML)
    assert text.splitlines() == ["# Anthony + Andy: MLH booth", "## Notes", "Bring twice the swag.", "## Checklist",
                                 "- [x] Order swag", "- Book two staffers", "## Open questions", "- Which sponsor tier?"]


def test_sections_and_carry_over():
    s = meetings.sections("## Notes\nx\n## Deliverable: Booth plan\n- a\n## Open questions\n- q\n## Next steps\n- [ ] s\n")
    carried = {k: v for k, v in s.items() if k.split(":")[0].strip() in meetings.CARRY_OVER}
    assert list(carried) == ["Deliverable: Booth plan", "Open questions", "Next steps"]


def test_brief_markdown():
    brief = MeetingBriefOut(why_matched="Both ran booths.", task="Run the MLH booth.", background=["Andy ran 3 booths"],
                            questions=["What ran out?", "Staffing?", "Sponsors?"], work_on=["Booth checklist"])
    req = {"name": "Anthony N", "team_name": "Events"}
    helper = {"name": "Andy S", "team_name": "Uni"}
    md = meetings.brief_markdown(req, helper, "Mon Oct 5, 3:00–3:30 PM", "https://meet/x", "MLH booth", brief)
    for heading in ("## Why you were matched", "## Questions to cover", "## Notes", "## Decisions", "## Open questions", "## Checklist"):
        assert heading in md
    assert "- [ ] " not in md  # no empty placeholder items
    assert "[Google Meet](https://meet/x)" in md and "## From last time" not in md
    follow = meetings.brief_markdown(req, helper, "x", None, "t", brief, {"Open questions": "- q\n"}, "https://prev")
    assert "## From last time" in follow and "[Previous meeting notes](https://prev)" in follow and "### Open questions" in follow


def test_steps_modal_and_parse_roundtrip():
    meetings.PENDING_STEPS[9] = [{"step": "Order swag", "owner": "Andy", "due": "2026-10-09"},
                                 {"step": "Draft booth plan", "owner": None, "due": None}]
    view = meetings.steps_modal(9, ["Anthony", "Andy"])
    assert view["private_metadata"] == "9" and len(view["blocks"]) == 3 * meetings.MAX_STEPS
    assert view["blocks"][1]["element"]["initial_option"]["value"] == "Andy"
    assert view["blocks"][2]["element"]["initial_date"] == "2026-10-09"
    values = {"step0": {"v": {"value": "Order swag"}}, "owner0": {"v": {"selected_option": {"value": "Andy"}}},
              "due0": {"v": {"selected_date": "2026-10-09"}},
              "step1": {"v": {"value": "Draft booth plan"}}, "owner1": {"v": {"selected_option": {"value": "-"}}},
              "due1": {"v": {"selected_date": None}}, "step2": {"v": {"value": "  "}}}
    assert meetings.parse_steps(values) == [{"step": "Order swag", "owner": "Andy", "due": "2026-10-09"},
                                            {"step": "Draft booth plan", "owner": None, "due": None}]


@pytest.fixture
def fake_env(monkeypatch):
    """Fake meeting row, people, canvas, Slack posts, and Muse."""
    calls = {"api": [], "posts": [], "muse": []}
    meeting = {"id": 5, "canvas_id": "F1", "canvas_url": "https://slack/canvas", "slack_channel_id": "G1",
               "requester_id": "p_a", "helper_id": "p_b", "conn_id": 3}
    people = {"p_a": {"id": "p_a", "name": "Anthony N"}, "p_b": {"id": "p_b", "name": "Andy S"}}

    async def fake_meeting(mid):
        return meeting

    async def fake_person(pid):
        return people[pid]

    async def fake_read(cid):
        return meetings.canvas_html_to_text(CANVAS_HTML)

    async def fake_api(method, **payload):
        calls["api"].append((method, payload))
        return {"sections": [{"id": "S1"}]} if method == "canvases.sections.lookup" else {}

    async def fake_post(channel, text, blocks=None):
        calls["posts"].append((text, blocks))

    async def fake_execute(sql, params=None):
        calls["api"].append(("sql", params))

    async def fake_muse(name, variables, schema, **kw):
        calls["muse"].append((name, variables))
        if schema is DraftOut:
            return DraftOut(title="MLH booth plan", markdown="# MLH booth plan\n## Checklist\n- [ ] Order swag",
                            open_questions=["Which sponsor tier?"])
        return NextStepsOut(steps=[{"step": "Book two staffers", "owner": "Andy", "due": None}])

    for name, fn in [("_meeting", fake_meeting), ("read_canvas", fake_read), ("_api", fake_api), ("_post", fake_post),
                     ("execute", fake_execute), ("muse_json", fake_muse)]:
        monkeypatch.setattr(meetings, name, fn)
    monkeypatch.setattr(meetings.flows, "person", fake_person)
    return calls


async def test_draft_then_accept_then_next_steps(fake_env):
    await meetings.draft(5, "checklist")
    name, variables = fake_env["muse"][0]
    assert name == "draft_output" and variables["kind"] == "Checklist" and "Bring twice the swag." in variables["notes"]
    lookups = [p["criteria"]["contains_text"] for m, p in fake_env["api"] if m == "canvases.sections.lookup"]
    edits = [p["changes"][0] for m, p in fake_env["api"] if m == "canvases.edit"]
    assert lookups == ["Checklist", "Open questions"]                      # existing sections, no new ones
    assert [e["operation"] for e in edits] == ["insert_after", "insert_after"]
    assert edits[0]["document_content"]["markdown"] == "- [ ] Order swag"  # the Muse heading was dropped
    assert edits[1]["document_content"]["markdown"] == "- Which sponsor tier?"
    accept_btn = fake_env["posts"][-1][1][1]["elements"][0]
    assert accept_btn["action_id"] == "mtg_accept:0" and accept_btn["value"] == "5|Checklist"
    assert len(fake_env["posts"][-1][1][1]["elements"]) == 1                # no Redo (it would duplicate items)

    await meetings.accept(5, "Checklist")
    replace = [p for m, p in fake_env["api"] if m == "canvases.edit"][-1]["changes"][0]
    assert replace["operation"] == "replace" and replace["document_content"]["markdown"] == "## Deliverable: Checklist"
    assert fake_env["muse"][-1][1]["people"] == "Anthony and Andy"
    assert "Book two staffers (Andy)" in fake_env["posts"][-1][0]
    assert meetings.PENDING_STEPS[5][0]["owner"] == "Andy"


async def test_save_steps_writes_page_and_offers_follow_up(fake_env):
    await meetings.save_steps(5, [{"step": "Order swag", "owner": "Andy", "due": "2026-10-09"},
                                  {"step": "Draft booth plan", "owner": None, "due": None}])
    md = [p for m, p in fake_env["api"] if m == "canvases.edit"][0]["changes"][0]["document_content"]["markdown"]
    assert md == "## Next steps\n\n- [ ] Order swag — Andy, due Fri Oct 9\n- [ ] Draft booth plan"
    buttons = fake_env["posts"][-1][1][1]["elements"]
    assert [b["action_id"].split(":")[0] for b in buttons] == ["mtg_followup", "mtg_remind", "mtg_done"]
    assert len({b["action_id"] for b in buttons}) == len(buttons)  # Slack rejects duplicates
    assert buttons[1]["value"] == "5|2026-10-09|Order swag" and buttons[1]["text"]["text"] == "Remind us on Oct 9"


def test_draft_menu_action_ids_are_unique():
    ids = [b["action_id"] for b in meetings.draft_menu(1)[1]["elements"]]
    assert len(set(ids)) == len(ids)
