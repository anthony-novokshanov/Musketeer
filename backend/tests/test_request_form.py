"""Connect opens a form; the helper is only messaged once the requester writes a specific ask."""
from app.bot import messages


def form_values(note, links=""):
    return {"note": {"v": {"value": note}}, "links": {"v": {"value": links}}}


def test_everything_is_optional():
    assert messages.parse_request_form(form_values("")) == (None, [], {})
    assert messages.parse_request_form({})[0] is None       # Slack omits empty optional inputs


def test_blank_form_sends_the_standard_intro():
    args = (7, "Anthony N", "Events", "Run the MLH booth", "Ran MLH booths.", True)
    standard = messages.request(*args)
    assert messages.request(*args, note=None, links=[]) == standard
    assert standard["text"] == ("Anthony N (Events) is working on *Run the MLH booth*. "
                                "You were suggested because you ran MLH booths. Up for a quick chat?")
    assert messages.request(*args, links=["https://d/1"])["text"] == standard["text"] + "\n*Attached:* <https://d/1>"


def test_parse_links_and_validate():
    note, links, errors = messages.parse_request_form(form_values(
        "Could I ask you about swag quantities and staffing the opening hour?",
        "https://docs.google.com/doc/1\n\n  https://slack.com/files/x  \n"))
    assert errors == {} and links == ["https://docs.google.com/doc/1", "https://slack.com/files/x"]
    _, _, errors = messages.parse_request_form(form_values("A specific enough question here", "docs.google.com/x"))
    assert "links" in errors


def test_request_message_carries_note_and_links():
    m = messages.request(7, "Anthony N", "Events", "Run the MLH booth", "Ran MLH booths.", reason_is_about_helper=True,
                         note="Could I ask about swag?\nAlso staffing.", links=["https://docs.google.com/doc/1"])
    assert "would like your help with *Run the MLH booth*" in m["text"]
    assert "> Could I ask about swag?\n> Also staffing." in m["text"]
    assert "*Attached:* <https://docs.google.com/doc/1>" in m["text"]
    assert "_You were suggested because you ran MLH booths._" in m["text"]
    assert [e["action_id"] for e in m["blocks"][1]["elements"]] == [messages.SURE, messages.CANT]


def test_request_form_shape():
    view = messages.request_form('{"conn": 7}', "Andy", messages.request_draft("Andy", "running the MLH booth"))
    assert view["callback_id"] == messages.REQUEST_FORM and view["submit"]["text"] == "Send request"
    assert [b.get("block_id") for b in view["blocks"][1:]] == ["note", "links"]
    note_box = view["blocks"][1]
    assert note_box["optional"] is True and "initial_value" not in note_box["element"]
    assert note_box["element"]["placeholder"]["text"].startswith("e.g. Hi Andy")
