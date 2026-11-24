"""Per-process heartbeats: what each running process actually loaded (GET /admin/processes).

``/admin/settings`` is answered by the ``api`` process, but the chat agent runs
in the ``worker``, a separate container with its own startup environment (and,
when only one service was rebuilt, its own code). Each process writes, at start
and every ``HEARTBEAT_INTERVAL_SECONDS``, its start time, a fingerprint of its
prompt code and the value it uses for every REGISTRY setting (secrets masked
like /admin) to ``admin:process:<process>:<hostname>:<pid>`` with a TTL, so a
process that stopped drops out on its own. A heartbeat failure is logged and
never stops the process.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from .logger import logger
from .runtime_config import REGISTRY, display_value, runtime_config
from .schemas import ProcessInfo

HEARTBEAT_PREFIX = "admin:process:"
HEARTBEAT_INTERVAL_SECONDS = 60
HEARTBEAT_TTL_SECONDS = 180
_FAILURE_LOG_EVERY_SECONDS = 600


def effective_settings() -> dict[str, Any]:
    """This process's value for every REGISTRY setting, masked like /admin/settings."""
    return {spec.key: display_value(spec, runtime_config.get(spec.key)) for spec in REGISTRY}


def heartbeat_payload(process: str, started_at: datetime) -> dict[str, Any]:
    from .agent.effective_prompt import prompt_code_hash  # the agent is heavy; import late

    return {
        "process": process,
        "instance": socket.gethostname(),
        "pid": os.getpid(),
        "started_at": started_at.isoformat(),
        "last_seen_at": datetime.now(UTC).isoformat(),
        "prompt_code_hash": prompt_code_hash(),
        "settings": effective_settings(),
    }


def heartbeat_key(payload: dict[str, Any]) -> str:
    return f"{HEARTBEAT_PREFIX}{payload['process']}:{payload['instance']}:{payload['pid']}"


async def publish_heartbeat(redis, process: str, started_at: datetime) -> None:
    # runtime_config.get may read the DB (hot overrides): keep it off the loop.
    payload = await asyncio.to_thread(heartbeat_payload, process, started_at)
    await redis.set(
        heartbeat_key(payload), json.dumps(payload, default=str), ex=HEARTBEAT_TTL_SECONDS
    )


async def run_heartbeat(redis, process: str) -> None:
    """Publish this process's heartbeat now and every minute, until cancelled.

    On cancel (shutdown) it deletes its own key, so a replaced container does
    not linger in /admin/processes until the TTL.
    """
    started_at = datetime.now(UTC)
    last_failure_log = 0.0
    own_key = heartbeat_key(
        {"process": process, "instance": socket.gethostname(), "pid": os.getpid()}
    )
    try:
        while True:
            try:
                await publish_heartbeat(redis, process, started_at)
            except asyncio.CancelledError:
                raise
            except Exception:
                now = time.monotonic()
                if now - last_failure_log >= _FAILURE_LOG_EVERY_SECONDS or not last_failure_log:
                    last_failure_log = now
                    logger.warning(f"Process heartbeat ({process}) failed", exc_info=True)
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        try:
            async with asyncio.timeout(2):
                await redis.delete(own_key)
        except Exception:
            pass  # the TTL removes it anyway
        raise


#: A live process refreshes every interval; one silent for longer has stopped.
_SILENT_AFTER = timedelta(seconds=HEARTBEAT_INTERVAL_SECONDS + 15)


def _drop_replaced_instances(
    processes: list[dict[str, Any]], now: datetime | None = None
) -> list[dict[str, Any]]:
    """Drop an older instance of a process once it has stopped refreshing.

    A deploy replaces the container, so the old one's heartbeat (another
    hostname) can outlive it by up to the TTL when it could not delete its key.
    A concurrent replica still refreshes every interval, so it stays listed.
    """
    now = now or datetime.now(UTC)
    newest: dict[str, tuple[datetime, str]] = {}
    for p in processes:
        seen = newest.get(p["process"])
        if seen is None or p["started_at"] > seen[0]:
            newest[p["process"]] = (p["started_at"], p["instance"])
    return [
        p
        for p in processes
        if p["instance"] == newest[p["process"]][1] or now - p["last_seen_at"] <= _SILENT_AFTER
    ]


async def read_processes(redis) -> list[dict[str, Any]]:
    """Every live heartbeat, sorted by process then start time; malformed ones skipped."""
    processes = []
    async for key in redis.scan_iter(match=f"{HEARTBEAT_PREFIX}*", count=100):
        raw = await redis.get(key)
        if raw is None:
            continue  # expired between SCAN and GET
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(payload.get("settings"), dict):
                raise ValueError("no settings")
            # Every field /admin/processes and /admin/settings read, typed (an
            # older or newer process may write another shape).
            payload.update(ProcessInfo.model_validate(payload).model_dump())
            if payload["started_at"].tzinfo is None:
                raise ValueError("naive started_at")  # couldn't be compared with the others
            processes.append(payload)
        except (ValueError, TypeError, KeyError, ValidationError):
            logger.warning(f"Ignoring a malformed process heartbeat at {key!r}")
    processes = _drop_replaced_instances(processes)
    return sorted(processes, key=lambda p: (p["process"], p["started_at"]))
