"""Intentional meetings: every booked meeting gets a shared Slack canvas in the pair's group DM.

before  -> Muse drafts the context (why matched, the task, background, questions); both can edit
during  -> the same canvas holds notes, decisions, open questions, and a checklist
after   -> "Draft output" turns the notes into a checklist / action plan / decision summary / anything;
           they accept it (or designate their own checklist or a link) as the deliverable,
           confirm next steps with owners and dates, and optionally book a follow-up or a reminder.
A follow-up gets a new canvas that carries the deliverable, open questions, and next steps forward.

Canvas content lives in Slack. Musketeer reads it only to draft and never stores it.
"""
import logging
from datetime import date, datetime, time, timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

import aiohttp

from app.ai.muse import muse_json
from app.bot import flows
from app.bot.messages import when
from app.config import settings
from app.db import execute, fetch_one
from app.schemas import DraftOut, MeetingBriefOut, NextStepsOut

log = logging.getLogger("musketeer.meetings")

KINDS = {"checklist": "Checklist", "action_plan": "Action plan", "decision_summary": "Decision summary"}
# Drafts go into the page's existing section when there is one; other kinds get one new section.
TARGET_SECTION = {"checklist": "Checklist", "decision_summary": "Decisions"}
CARRY_OVER = ("Deliverable", "Open questions", "Still open", "Next steps")
MAX_STEPS = 5


# ---------- canvas plumbing ----------

class _Text(HTMLParser):
    """Canvas HTML -> plain text with headings, bullets, and checkbox state kept."""
    BLOCKS = {"h1": "# ", "h2": "## ", "h3": "### ", "p": "", "li": "- ", "div": ""}

    def __init__(self):
        super().__init__()
        self.lines, self.cur = [], ""

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCKS:
            self._flush()
            classes = dict(attrs).get("class") or ""
            self.cur = self.BLOCKS[tag] + ("[x] " if tag == "li" and "checked" in classes else "")

    def handle_endtag(self, tag):
        if tag in self.BLOCKS:
            self._flush()

    def handle_data(self, data):
        self.cur += data

    def _flush(self):
        if self.cur.strip() and self.cur.strip() not in ("-", "#", "##", "###"):
            self.lines.append(self.cur.rstrip())
        self.cur = ""


def canvas_html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    p._flush()
    return "\n".join(p.lines)


def sections(text: str) -> dict[str, str]:
    """Split canvas text into {heading: body} by '#' headings (first matching word wins)."""
    out, head = {}, None
    for line in text.splitlines():
        if line.startswith("#"):
            head = line.lstrip("#").strip()
            out[head] = ""
        elif head:
            out[head] += line + "\n"
    return out


async def _api(method: str, **payload) -> dict:
    return await flows._slack().api_call(method, json=payload)


async def create_canvas(title: str, markdown: str, channel_id: str) -> tuple[str, str]:
    """A standalone canvas, editable by everyone in the group DM. Returns (canvas_id, permalink)."""
    res = await _api("canvases.create", title=title, document_content={"type": "markdown", "markdown": markdown})
    canvas_id = res["canvas_id"]
    await _api("canvases.access.set", canvas_id=canvas_id, access_level="write", channel_ids=[channel_id])
    info = await flows._slack().files_info(file=canvas_id)
    return canvas_id, info["file"]["permalink"]


async def read_canvas(canvas_id: str) -> str:
    info = await flows._slack().files_info(file=canvas_id)
    url = info["file"].get("url_private_download") or info["file"]["url_private"]
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers={"Authorization": f"Bearer {settings.SLACK_BOT_TOKEN}"}) as r:
            r.raise_for_status()
            return canvas_html_to_text(await r.text())


async def append(canvas_id: str, markdown: str) -> None:
    await _api("canvases.edit", canvas_id=canvas_id,
               changes=[{"operation": "insert_at_end", "document_content": {"type": "markdown", "markdown": markdown}}])


async def _heading_id(canvas_id: str, contains: str) -> str | None:
    found = await _api("canvases.sections.lookup", canvas_id=canvas_id,
                       criteria={"section_types": ["any_header"], "contains_text": contains})
    return found["sections"][-1]["id"] if found.get("sections") else None


async def replace_heading(canvas_id: str, contains: str, markdown: str) -> bool:
    section_id = await _heading_id(canvas_id, contains)
    if section_id:
        await _api("canvases.edit", canvas_id=canvas_id, changes=[{
            "operation": "replace", "section_id": section_id, "document_content": {"type": "markdown", "markdown": markdown}}])
    return bool(section_id)


async def insert_under(canvas_id: str, heading: str, markdown: str) -> bool:
    """Insert right below an existing heading. False if the page no longer has that heading."""
    section_id = await _heading_id(canvas_id, heading)
    if section_id:
        await _api("canvases.edit", canvas_id=canvas_id, changes=[{
            "operation": "insert_after", "section_id": section_id, "document_content": {"type": "markdown", "markdown": markdown}}])
    return bool(section_id)


def body_only(markdown: str) -> str:
    """Drop any headings Muse adds: the page already has its title and structure."""
    return "\n".join(line for line in markdown.splitlines() if not line.lstrip().startswith("#")).strip()


# ---------- the page ----------

def brief_markdown(req: dict, helper: dict, when_str: str, meet_link: str | None, topic: str,
                   brief: MeetingBriefOut, carried: dict[str, str] | None = None, parent_url: str | None = None) -> str:
    bullets = lambda xs: "\n".join(f"- {x}" for x in xs) or "- "
    md = [f"**When:** {when_str}" + (f" · [Google Meet]({meet_link})" if meet_link else ""),
          f"**Who:** {req['name']} ({req['team_name']}) and {helper['name']} ({helper['team_name']})",
          "_Musketeer drafted the first sections. Fix anything that's off; it's your page._"]
    if carried is not None:
        md += ["## From last time", f"[Previous meeting notes]({parent_url})" if parent_url else ""]
        md += [f"### {k}\n{v.strip()}" for k, v in carried.items() if v.strip()] or ["- Nothing carried over."]
    md += ["## Why you were matched", brief.why_matched,
           "## The task", brief.task,
           "## Background", bullets(brief.background),
           "## Questions to cover", bullets(brief.questions),
           "## What to work on together", bullets(brief.work_on),
           "## Resources", "- Add links to docs you're working on",
           "## Notes", "## Decisions", "## Open questions", "## Checklist"]
    return "\n\n".join(m for m in md if m != "")


def _buttons(meeting_id: int, *items: tuple[str, str, str]) -> dict:
    return {"type": "actions", "elements": [
        # Slack needs unique action_ids per message; the handler strips the ":n" suffix.
        {"type": "button", "text": {"type": "plain_text", "text": label}, "action_id": f"{action}:{i}",
         "value": f"{meeting_id}|{extra}", **({"style": "primary"} if i == 0 else {})}
        for i, (label, action, extra) in enumerate(items)]}


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def draft_menu(meeting_id: int) -> list[dict]:
    return [_section("When you're ready, I can turn your notes into something you keep. Pick one:"),
            _buttons(meeting_id, ("Draft a checklist", "mtg_draft", "checklist"),
                     ("Draft an action plan", "mtg_draft", "action_plan"),
                     ("Draft a decision summary", "mtg_draft", "decision_summary"),
                     ("Something else…", "mtg_other", ""),
                     ("We already made one", "mtg_designate", ""))]


async def _meeting(meeting_id: int) -> dict:
    return await fetch_one("""SELECT m.*, c.requester_id, c.helper_id, c.slack_channel_id, c.reason, c.task_id,
                                     c.origin, c.id AS conn_id
                              FROM meetings m JOIN connections c ON c.id = m.connection_id WHERE m.id = %s""",
                           (meeting_id,))


async def _post(channel: str, text: str, blocks: list[dict] | None = None) -> None:
    await flows._slack().chat_postMessage(channel=channel, text=text, blocks=blocks or [_section(text)])


async def start_meeting(conn_id: int, start: datetime, end: datetime, event: dict, parent_id: int | None = None) -> None:
    """Called when a meeting is booked: create the shared page and link it in the group DM."""
    conn = await fetch_one("SELECT * FROM connections WHERE id = %s", (conn_id,))
    req, helper = await flows.person(conn["requester_id"]), await flows.person(conn["helper_id"])
    topic = await flows._topic(conn, req)
    describe = lambda p: f"{p['name']}, {p['title']}, {p['team_name']}: {p['summary'] or ''}"
    try:
        brief = await muse_json("meeting_brief", {"requester": describe(req), "helper": describe(helper),
                                                  "task_summary": topic, "reason": conn["reason"]}, MeetingBriefOut, cache=False)
    except Exception as e:  # the page must still exist on camera
        log.warning("meeting brief unavailable, using a plain one: %s", e)
        brief = MeetingBriefOut(why_matched=conn["reason"], task=topic, background=[], questions=[], work_on=[])

    carried, parent_url = None, None
    if parent_id:
        parent = await fetch_one("SELECT canvas_id, canvas_url FROM meetings WHERE id = %s", (parent_id,))
        parent_url = parent and parent["canvas_url"]
        try:
            prev = sections(await read_canvas(parent["canvas_id"]))
            carried = {k: v for k, v in prev.items() if k.split(":")[0].strip() in CARRY_OVER}
        except Exception as e:
            log.warning("could not read the previous page: %s", e)
            carried = {}

    row = await fetch_one("""INSERT INTO meetings (connection_id, parent_id, start_at, event_link, meet_link)
                             VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                          (conn_id, parent_id, start, event.get("htmlLink"), event.get("hangoutLink")))
    title = f"{'Follow-up: ' if parent_id else ''}{flows.first_name(req)} + {flows.first_name(helper)}: {topic}"[:120]
    canvas_id, url = await create_canvas(title, brief_markdown(req, helper, when(start, end), event.get("hangoutLink"),
                                                                topic, brief, carried, parent_url), conn["slack_channel_id"])
    await execute("UPDATE meetings SET canvas_id = %s, canvas_url = %s WHERE id = %s", (canvas_id, url, row["id"]))
    intro = (f"I started a shared page for your {'follow-up' if parent_id else 'meeting'}: <{url}|meeting notes>. "
             "I drafted the context from what I know. Fix anything that's off, add your questions and links, "
             "and use it for notes during the meeting." + (" It carries over your deliverable, open questions, and next steps."
                                                            if parent_id else ""))
    await _post(conn["slack_channel_id"], intro, [_section(intro), *draft_menu(row["id"])])


# ---------- deliverable ----------

async def draft(meeting_id: int, kind: str) -> None:
    m = await _meeting(meeting_id)
    label = KINDS.get(kind, kind)
    await _post(m["slack_channel_id"], f"Reading your notes and drafting: {label.lower()}…")
    notes = await read_canvas(m["canvas_id"])
    out = await muse_json("draft_output", {"kind": label, "notes": notes[:12000]}, DraftOut, cache=False)
    body = body_only(out.markdown)
    heading = TARGET_SECTION.get(kind)
    if not (heading and await insert_under(m["canvas_id"], heading, body)):
        heading = out.title.replace("|", "/")
        await append(m["canvas_id"], f"## {heading}\n\n{body}")
    added_q = bool(out.open_questions) and await insert_under(
        m["canvas_id"], "Open questions", "\n".join(f"- {q}" for q in out.open_questions))
    text = (f"I added a {label.lower()} under *{heading}* on <{m['canvas_url']}|your page>"
            + (f" and {len(out.open_questions)} unresolved item(s) under *Open questions*" if added_q else "")
            + ". Edit it there, then accept it as your deliverable.")
    await _post(m["slack_channel_id"], text, [_section(text), _buttons(
        meeting_id, ("Accept as our deliverable", "mtg_accept", heading))])


async def accept(meeting_id: int, title: str) -> None:
    """title is the heading the draft went under; it becomes "Deliverable: <title>"."""
    m = await _meeting(meeting_id)
    await replace_heading(m["canvas_id"], title, f"## Deliverable: {title}")
    await execute("UPDATE meetings SET deliverable = %s WHERE id = %s", (title, meeting_id))
    await propose_next_steps(meeting_id, f"*{title}* is now your deliverable.")


async def designate(meeting_id: int, use_checklist: bool, link: str | None) -> None:
    m = await _meeting(meeting_id)
    if use_checklist and await replace_heading(m["canvas_id"], "Checklist", "## Deliverable: Checklist"):
        title = "Checklist"
    else:
        title = "Shared document"
        await append(m["canvas_id"], f"## Deliverable: {title}\n\n{link or ''}")
    await execute("UPDATE meetings SET deliverable = %s WHERE id = %s", (title, meeting_id))
    await propose_next_steps(meeting_id, f"Marked your *{title.lower()}* as the deliverable.")


# ---------- next steps + follow-up ----------

def _people(m_req: dict, m_helper: dict) -> dict[str, str]:
    return {flows.first_name(m_req): m_req["id"], flows.first_name(m_helper): m_helper["id"]}


async def propose_next_steps(meeting_id: int, lead: str) -> None:
    m = await _meeting(meeting_id)
    req, helper = await flows.person(m["requester_id"]), await flows.person(m["helper_id"])
    names = list(_people(req, helper))
    try:
        steps = (await muse_json("next_steps", {"people": " and ".join(names), "notes": (await read_canvas(m["canvas_id"]))[:12000],
                                                "today": date.today().isoformat()}, NextStepsOut, cache=False)).steps[:MAX_STEPS]
    except Exception as e:
        log.warning("next steps unavailable: %s", e)
        steps = []
    PENDING_STEPS[meeting_id] = [s.model_dump() for s in steps]
    listing = "\n".join(f"• {s.step}" + (f" ({s.owner})" if s.owner else "") + (f", by {s.due}" if s.due else "") for s in steps)
    text = f"{lead} Here are the next steps I see in your notes; confirm owners and dates:\n{listing or '• (none yet, add your own)'}"
    await _post(m["slack_channel_id"], text, [_section(text), _buttons(
        meeting_id, ("Confirm next steps", "mtg_steps", ""), ("Skip", "mtg_skip_steps", ""))])


PENDING_STEPS: dict[int, list[dict]] = {}  # proposed steps waiting for the confirm form (demo-scale, in memory)


def steps_modal(meeting_id: int, names: list[str]) -> dict:
    steps = PENDING_STEPS.get(meeting_id, [])
    options = [{"text": {"type": "plain_text", "text": n}, "value": n} for n in names] + \
              [{"text": {"type": "plain_text", "text": "Unassigned"}, "value": "-"}]
    blocks = []
    for i in range(MAX_STEPS):
        s = steps[i] if i < len(steps) else {"step": "", "owner": None, "due": None}
        owner = next((o for o in options if o["value"] == s["owner"]), options[-1])
        blocks += [
            {"type": "input", "block_id": f"step{i}", "optional": True, "label": {"type": "plain_text", "text": f"Step {i + 1}"},
             "element": {"type": "plain_text_input", "action_id": "v", **({"initial_value": s["step"]} if s["step"] else {})}},
            {"type": "input", "block_id": f"owner{i}", "optional": True, "label": {"type": "plain_text", "text": "Owner"},
             "element": {"type": "static_select", "action_id": "v", "options": options, "initial_option": owner}},
            {"type": "input", "block_id": f"due{i}", "optional": True, "label": {"type": "plain_text", "text": "Due"},
             "element": {"type": "datepicker", "action_id": "v", **({"initial_date": s["due"]} if s["due"] else {})}}]
    return {"type": "modal", "callback_id": "mtg_steps_submit", "private_metadata": str(meeting_id),
            "title": {"type": "plain_text", "text": "Next steps"}, "submit": {"type": "plain_text", "text": "Save"},
            "blocks": blocks}


def parse_steps(values: dict) -> list[dict]:
    out = []
    for i in range(MAX_STEPS):
        step = (values.get(f"step{i}", {}).get("v", {}).get("value") or "").strip()
        if not step:
            continue
        owner = ((values.get(f"owner{i}", {}).get("v", {}).get("selected_option") or {}).get("value"))
        out.append({"step": step, "owner": None if owner in (None, "-") else owner,
                    "due": values.get(f"due{i}", {}).get("v", {}).get("selected_date")})
    return out


async def save_steps(meeting_id: int, steps: list[dict]) -> None:
    m = await _meeting(meeting_id)
    PENDING_STEPS.pop(meeting_id, None)
    lines = [f"- [ ] {s['step']}" + (f" — {s['owner']}" if s["owner"] else "") +
             (f", due {date.fromisoformat(s['due']):%a %b} {date.fromisoformat(s['due']).day}" if s["due"] else "") for s in steps]
    await append(m["canvas_id"], "## Next steps\n\n" + ("\n".join(lines) or "- None yet"))
    dated = [s for s in steps if s["due"]]
    items = [("Book a follow-up", "mtg_followup", "")]
    if dated:
        first = min(dated, key=lambda s: s["due"])
        items.append((f"Remind us on {date.fromisoformat(first['due']):%b} {date.fromisoformat(first['due']).day}",
                      "mtg_remind", f"{first['due']}|{first['step'][:80].replace('|', '/')}"))
    items.append(("No thanks", "mtg_done", ""))
    text = f"Next steps are on <{m['canvas_url']}|your page>. Want a follow-up meeting or a reminder?"
    await _post(m["slack_channel_id"], text, [_section(text), _buttons(meeting_id, *items)])


async def remind(meeting_id: int, due: str, step: str) -> str:
    m = await _meeting(meeting_id)
    tz = ZoneInfo(settings.DEMO_TIMEZONE)
    at = datetime.combine(date.fromisoformat(due), time(9, 0), tz)
    if at <= datetime.now(tz):
        at = datetime.now(tz) + timedelta(minutes=1)
    await flows._slack().chat_scheduleMessage(
        channel=m["slack_channel_id"], post_at=int(at.timestamp()),
        text=f"Reminder from your meeting: *{step}* is due today. Notes: <{m['canvas_url']}|meeting page>")
    return f"{at:%a %b} {at.day}, 9:00 AM"


async def follow_up(meeting_id: int) -> None:
    m = await _meeting(meeting_id)
    await flows.propose_meeting(m["conn_id"], follow_up_of=meeting_id)


# ---------- Slack wiring ----------

def register(app) -> None:
    import re

    async def who(body) -> dict | None:
        return await fetch_one("SELECT id FROM people WHERE slack_user_id = %s", (body["user"]["id"],))

    @app.action(re.compile(r"^mtg_"))
    async def on_meeting_button(ack, body, action, respond, client):
        await ack()
        meeting_id, extra = action["value"].split("|", 1)
        meeting_id, act = int(meeting_id), action["action_id"].split(":")[0]
        original = body["message"]["text"]
        try:
            if act == "mtg_draft":
                flows.spawn(lambda: draft(meeting_id, extra))
            elif act == "mtg_other":
                await client.views_open(trigger_id=body["trigger_id"], view={
                    "type": "modal", "callback_id": "mtg_other_submit", "private_metadata": str(meeting_id),
                    "title": {"type": "plain_text", "text": "Draft output"}, "submit": {"type": "plain_text", "text": "Draft it"},
                    "blocks": [{"type": "input", "block_id": "kind", "label": {"type": "plain_text", "text": "What should I draft from your notes?"},
                                "element": {"type": "plain_text_input", "action_id": "v", "placeholder": {"type": "plain_text", "text": "e.g. a one-page booth plan"}}}]})
            elif act == "mtg_designate":
                await client.views_open(trigger_id=body["trigger_id"], view={
                    "type": "modal", "callback_id": "mtg_designate_submit", "private_metadata": str(meeting_id),
                    "title": {"type": "plain_text", "text": "Your deliverable"}, "submit": {"type": "plain_text", "text": "Use it"},
                    "blocks": [{"type": "input", "block_id": "which", "label": {"type": "plain_text", "text": "What did you make?"},
                                "element": {"type": "radio_buttons", "action_id": "v", "options": [
                                    {"text": {"type": "plain_text", "text": "The checklist on our page"}, "value": "checklist"},
                                    {"text": {"type": "plain_text", "text": "A document (paste the link below)"}, "value": "link"}]}},
                               {"type": "input", "block_id": "link", "optional": True, "label": {"type": "plain_text", "text": "Link"},
                                "element": {"type": "url_text_input", "action_id": "v"}}]})
            elif act == "mtg_accept":
                await respond(replace_original=True, text=f"{original}\n_Accepted._")
                flows.spawn(lambda: accept(meeting_id, extra))
            elif act == "mtg_steps":
                m = await _meeting(meeting_id)
                names = list(_people(await flows.person(m["requester_id"]), await flows.person(m["helper_id"])))
                await client.views_open(trigger_id=body["trigger_id"], view=steps_modal(meeting_id, names))
            elif act == "mtg_skip_steps":
                await respond(replace_original=True, text=f"{original}\n_Skipped._")
            elif act == "mtg_followup":
                await respond(replace_original=True, text=f"{original}\n_Finding a follow-up time…_")
                flows.spawn(lambda: follow_up(meeting_id))
            elif act == "mtg_remind":
                due, step = extra.split("|", 1)
                at = await remind(meeting_id, due, step)
                await respond(replace_original=True, text=f"{original}\n_I'll remind you both on {at}._")
            elif act == "mtg_done":
                await respond(replace_original=True, text=f"{original}\n_All set. Your notes stay on the page._")
        except Exception:
            log.exception("meeting action %s failed", act)
            await respond(text="Sorry, that didn't work. Try again in a moment.", replace_original=False)

    @app.view("mtg_other_submit")
    async def on_other(ack, view):
        await ack()
        kind = view["state"]["values"]["kind"]["v"]["value"]
        flows.spawn(lambda: draft(int(view["private_metadata"]), kind))

    @app.view("mtg_designate_submit")
    async def on_designate(ack, view):
        v = view["state"]["values"]
        which = (v["which"]["v"].get("selected_option") or {}).get("value")
        link = v["link"]["v"].get("value")
        if which == "link" and not link:
            await ack(response_action="errors", errors={"link": "Paste the link to your document."})
            return
        await ack()
        flows.spawn(lambda: designate(int(view["private_metadata"]), which == "checklist", link))

    @app.view("mtg_steps_submit")
    async def on_steps(ack, view):
        await ack()
        flows.spawn(lambda: save_steps(int(view["private_metadata"]), parse_steps(view["state"]["values"])))
