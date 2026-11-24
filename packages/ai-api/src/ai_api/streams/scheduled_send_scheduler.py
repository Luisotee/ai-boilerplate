"""Stream-worker task for scheduled sends (Envios programados, ``scheduled_sends/``).

Every ``TICK_SECONDS``: fire due slots (``runner.fire_due``), drop runs stuck
preparing too long, and prepare each claimable run in its own task (at most
``MAX_PREPARING`` at once), so one long prompt run doesn't hold back firing the
others. Delivery is the broadcast consumer's. A failing tick is logged and the
loop goes on; it never takes the worker down. Not the chat-created scheduled
tasks (``streams/scheduler.py``).
"""

from __future__ import annotations

import asyncio
import uuid

import httpx

from ..logger import logger
from ..scheduled_sends import runner
from ..services.scheduled_tasks import utcnow

TICK_SECONDS = 30.0
MAX_PREPARING = 2
HTTP_TIMEOUT_SECONDS = 30.0


async def tick(http: httpx.AsyncClient, in_flight: dict[uuid.UUID, asyncio.Task]) -> None:
    for run_id, task in list(in_flight.items()):
        if task.done():
            del in_flight[run_id]
    now = utcnow()
    await asyncio.to_thread(runner.fire_due, now)
    await asyncio.to_thread(runner.expire_stale_runs, now)
    for run_id in await asyncio.to_thread(runner.claimable_run_ids, now):
        if len(in_flight) >= MAX_PREPARING:
            break
        if run_id in in_flight:
            continue
        token = await asyncio.to_thread(runner.claim_run, run_id, now)
        if token is None:
            continue  # another worker took it
        in_flight[run_id] = asyncio.create_task(
            runner.safe_prepare(run_id, token, http), name=f"scheduled-send-{run_id}"
        )


async def run_scheduled_send_scheduler() -> None:
    logger.info("🗓️ Starting the scheduled-send scheduler")
    in_flight: dict[uuid.UUID, asyncio.Task] = {}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as http:
        try:
            while True:
                try:
                    await tick(http, in_flight)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.error("Scheduled-send tick failed", exc_info=True)
                await asyncio.sleep(TICK_SECONDS)
        finally:
            for task in in_flight.values():
                task.cancel()
            await asyncio.gather(*in_flight.values(), return_exceptions=True)
