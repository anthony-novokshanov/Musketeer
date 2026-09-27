"""Background loop: feedback requests and feedback -> expertise evidence (spec §11, §7.6). Simulated deliveries are scheduled by flows.spawn."""
import asyncio
import logging

from app.bot import flows
from app.expertise import feedback

log = logging.getLogger("musketeer.jobs")

LOOP_SEC = 30


async def feedback_loop() -> None:
    while True:
        try:
            n = await flows.request_due_feedback()
            if n:
                log.info("requested feedback for %d connections", n)
            n = await feedback.apply_pending()
            if n:
                log.info("feedback -> %d evidence rows", n)
        except Exception:
            log.exception("feedback loop iteration failed")
        await asyncio.sleep(LOOP_SEC)
