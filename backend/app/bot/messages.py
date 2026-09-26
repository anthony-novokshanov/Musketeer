"""Block Kit templates for every Slack flow (spec §10). Button values carry the connection id."""


def _clause(s: str) -> str:
    """Reason as a mid-sentence clause: lowercase first letter, no trailing period."""
    s = s.strip().rstrip(".")
    return s[:1].lower() + s[1:]


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _buttons(conn_id: int, yes: tuple[str, str], no: tuple[str, str] | None = None) -> dict:
    elements = [{"type": "button", "style": "primary", "text": {"type": "plain_text", "text": yes[1]},
                 "action_id": yes[0], "value": str(conn_id)}]
    if no:
        elements.append({"type": "button", "text": {"type": "plain_text", "text": no[1]},
                         "action_id": no[0], "value": str(conn_id)})
    return {"type": "actions", "elements": elements}


def _msg(text: str, conn_id: int, yes: tuple[str, str], no: tuple[str, str] | None = None) -> dict:
    return {"text": text, "blocks": [_section(text), _buttons(conn_id, yes, no)]}


CONNECT, NOT_NOW = "musketeer_connect", "musketeer_not_now"
SURE, CANT = "musketeer_sure", "musketeer_cant"
GRAB_15 = "musketeer_grab15"
HELPFUL_YES, HELPFUL_NO = "musketeer_helpful_yes", "musketeer_helpful_no"


def suggestion(conn_id: int, task_summary: str, helper_name: str, helper_team: str, reason: str) -> dict:
    return _msg(f"New task spotted: *{task_summary}*. {helper_name} on {helper_team} "
                f"{_clause(reason)}. Want to connect?", conn_id, (CONNECT, "Connect"), (NOT_NOW, "Not now"))


def request(conn_id: int, requester_name: str, requester_team: str, task_summary: str, reason: str,
            reason_is_about_helper: bool = False) -> dict:
    # Task-match reasons are verb-first about the helper ("Ran MLH booths..."), so they read "because you ran...".
    because = f"you {_clause(reason)}" if reason_is_about_helper else _clause(reason)
    return _msg(f"{requester_name} ({requester_team}) is working on *{task_summary}*. "
                f"You were suggested because {because}. Up for a quick chat?",
                conn_id, (SURE, "Sure"), (CANT, "Can't right now"))


def nudge(conn_id: int, manager_name: str, target_name: str, target_team: str, reason: str) -> dict:
    return _msg(f"{manager_name} suggests reaching out to {target_name} on {target_team}. {reason}",
                conn_id, (CONNECT, "Reach out"), (NOT_NOW, "Not now"))


def lead_intro(conn_id: int, manager_name: str, lead_b_name: str, team_b: str, overlap_summary: str) -> dict:
    return _msg(f"{manager_name} wants to introduce you to {lead_b_name}, lead of {team_b}. {overlap_summary}",
                conn_id, (CONNECT, "Connect"), (NOT_NOW, "Not now"))


def icebreaker(conn_id: int, message: str, questions: list[str], lead_brief: str | None = None) -> dict:
    text = message + "\n\n" + "\n".join(f"• {q}" for q in questions)
    blocks = [_section(text)]
    if lead_brief:
        blocks.append(_section(lead_brief))
    blocks.append(_buttons(conn_id, (GRAB_15, "Grab 15 min")))
    return {"text": text, "blocks": blocks}


GRAB_15_REPLY = "Suggested time: tomorrow 2:00–2:15 PM. Add it to your calendars?"  # when calendars aren't linked
BOOK, OTHER_TIME = "musketeer_book", "musketeer_other_time"


def when(start, end) -> str:
    """'Tue Sep 29, 2:30–3:00 PM' (portable; no platform-specific strftime flags)."""
    t = lambda d: f"{d.hour % 12 or 12}:{d.minute:02d}"
    return f"{start:%a %b} {start.day}, {t(start)}–{t(end)} {end:%p}"


def meeting_proposal(conn_id: int, first_a: str, first_b: str, start, end, skip: int,
                     approved_by: str = "", approved_first: str = "", waiting_for: str = "",
                     follow_up_of: int | None = None) -> dict:
    """Both people must tap Book it. The first approver's id rides in the button value (restart-safe)."""
    what = "a follow-up" if follow_up_of else "it"
    text = f"{first_a} and {first_b}, you're both free *{when(start, end)}*. Want me to book {what}? I'll book once you both confirm."
    if approved_by:
        text += f"\n✓ {approved_first} is in. Waiting for {waiting_for}."
    return {"text": text, "blocks": [_section(text), {"type": "actions", "elements": [
        {"type": "button", "style": "primary", "text": {"type": "plain_text", "text": "Book it"},
         "action_id": BOOK, "value": f"{conn_id}|{start.isoformat()}|{skip}|{approved_by}|{follow_up_of or ''}"},
        {"type": "button", "text": {"type": "plain_text", "text": "Another time"},
         "action_id": OTHER_TIME, "value": f"{conn_id}|{skip + 1}|{follow_up_of or ''}"}]}]}


def meeting_booked(start, end, link: str) -> dict:
    return {"text": f"Booked *{when(start, end)}*. The invite with a Google Meet link is on both calendars. <{link}|Open the event>"}


NO_COMMON_TIME = {"text": "I couldn't find a time you're both free in the next week. Try picking one here in the chat."}


def helper_cant(helper_first: str) -> dict:
    return {"text": f"{helper_first} can't right now. Looking for someone else."}


def feedback(conn_id: int, other_name: str) -> dict:
    return _msg(f"Was your chat with {other_name} helpful?", conn_id, (HELPFUL_YES, "Yes"), (HELPFUL_NO, "Not really"))
