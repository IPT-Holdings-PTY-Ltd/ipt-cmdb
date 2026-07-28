"""Reusable execution loop for embedded, dedicated, and one-shot workers."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

LOGGER = logging.getLogger("cmdb.worker")


@dataclass(frozen=True)
class PeriodicWorker:
    """Describe one bounded background operation without coupling it to FastAPI."""

    name: str
    interval_seconds: int
    execute: Callable[[], dict[str, Any]]


def process_role() -> str:
    """Return the deployment process role, preserving the combined default."""

    configured = os.getenv("CMDB_PROCESS_ROLE", "combined").strip().casefold()
    if configured not in {"combined", "web", "worker"}:
        LOGGER.warning("Invalid CMDB_PROCESS_ROLE=%r; using combined", configured)
        return "combined"
    return configured


def deployment_mode(*, one_shot: bool = False) -> str:
    """Return an operator-facing worker topology label."""

    if one_shot:
        return "one_shot"
    return "dedicated" if process_role() == "worker" else "embedded"


def worker_identity() -> str:
    """Build a non-secret identity that distinguishes replicas and restarts."""

    host = socket.gethostname().strip()[:60] or "unknown-host"
    return f"{host}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


def _processed_count(result: dict[str, Any]) -> int:
    """Extract a conservative work-item count from a worker result."""

    for key in ("processed", "queued", "candidates", "rulesEvaluated"):
        value = result.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return 0


def _observe(
    observer: Callable[..., Any],
    worker: PeriodicWorker,
    worker_id: str,
    mode: str,
    event: str,
    *,
    processed: int = 0,
    error: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Persist runtime evidence without making telemetry a worker dependency."""

    try:
        observer(
            worker.name,
            worker_id,
            mode,
            worker.interval_seconds,
            event,
            processed=processed,
            error=error[:500],
            metadata=metadata or {},
        )
    except Exception:
        LOGGER.exception("Could not persist %s worker telemetry", worker.name)


async def run_worker_cycle(
    worker: PeriodicWorker,
    observer: Callable[..., Any],
    *,
    worker_id: str,
    mode: str,
) -> bool:
    """Run one worker cycle and record its bounded result."""

    _observe(observer, worker, worker_id, mode, "cycle_started")
    try:
        execution = asyncio.create_task(asyncio.to_thread(worker.execute))
        heartbeat_interval = max(5, min(worker.interval_seconds, 30))
        while not execution.done():
            completed, _pending = await asyncio.wait(
                {execution},
                timeout=heartbeat_interval,
            )
            if not completed:
                _observe(observer, worker, worker_id, mode, "heartbeat")
        result = await execution
    except Exception as error:
        LOGGER.exception("%s worker cycle failed", worker.name.capitalize())
        _observe(
            observer,
            worker,
            worker_id,
            mode,
            "cycle_failed",
            error=f"{type(error).__name__}: {error}",
        )
        return False
    _observe(
        observer,
        worker,
        worker_id,
        mode,
        "cycle_succeeded",
        processed=_processed_count(result),
        metadata={"result": result},
    )
    return True


async def run_periodic_worker(
    worker: PeriodicWorker,
    observer: Callable[..., Any],
    *,
    one_shot: bool = False,
    identity: str | None = None,
) -> bool:
    """Run one cycle or poll continuously until the task is cancelled."""

    worker_id = identity or worker_identity()
    mode = deployment_mode(one_shot=one_shot)
    _observe(observer, worker, worker_id, mode, "starting")
    last_succeeded = True
    try:
        while True:
            last_succeeded = await run_worker_cycle(
                worker,
                observer,
                worker_id=worker_id,
                mode=mode,
            )
            if one_shot:
                return last_succeeded
            await asyncio.sleep(worker.interval_seconds)
    finally:
        _observe(observer, worker, worker_id, mode, "stopped")
