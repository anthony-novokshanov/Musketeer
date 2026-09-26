import logging

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from app.config import settings

log = logging.getLogger("bridge.slack")

app = AsyncApp(token=settings.SLACK_BOT_TOKEN)
handler = AsyncSocketModeHandler(app, settings.SLACK_APP_TOKEN)


@app.event("message")
async def on_message(event, say):
    if event.get("bot_id") or event.get("subtype"):
        return
    channel_type = event.get("channel_type")
    # Never log message text (spec §1 privacy).
    log.info("message channel_type=%s user=%s channel=%s", channel_type, event.get("user"), event.get("channel"))
    if channel_type == "im":
        # Temporary smoke test; replaced by the real flows in Phase 3.
        await say("Bridge is connected.")


async def start() -> None:
    await handler.connect_async()
    log.info("Slack Socket Mode connected")


async def stop() -> None:
    await handler.close_async()
