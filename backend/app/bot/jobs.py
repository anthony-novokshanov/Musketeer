"""Background loop: feedback requests (spec §10). Simulated deliveries are scheduled by flows.spawn."""
import asyncio
import logging

from app.bot import flows

log = logging.getLogger("musketeer.jobs")

LOOP_SEC = 30


async def feedback_loop() -> None:
    while True:
        try:
            n = await flows.request_due_feedback()
            if n:
                log.info("requested feedback for %d connections", n)
        except Exception:
            log.exception("feedback loop iteration failed")
        await asyncio.sleep(LOOP_SEC)
