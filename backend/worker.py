"""Dedicated CMDB worker process using the same image and repository as the API."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from backend import main as backend_main
from src.cmdb.worker_runtime import process_role

LOGGER = logging.getLogger("cmdb.worker")


def _enabled_workers() -> list:
    """Return explicitly enabled worker definitions."""

    workers = []
    if backend_main._worker_flag("NOTIFICATION_WORKER_ENABLED"):
        workers.append(backend_main._notification_worker_definition())
    if backend_main._worker_flag("INTEGRATION_WORKER_ENABLED"):
        workers.append(backend_main._integration_worker_definition())
    return workers


async def run_worker_service(*, one_shot: bool = False) -> int:
    """Run configured jobs continuously or once for a scheduled-job deployment."""

    workers = _enabled_workers()
    if not workers:
        LOGGER.error(
            "No workers are enabled; set NOTIFICATION_WORKER_ENABLED and/or "
            "INTEGRATION_WORKER_ENABLED"
        )
        return 2
    if not one_shot and process_role() != "worker":
        LOGGER.warning(
            "Dedicated worker started with CMDB_PROCESS_ROLE=%s; treating this process as worker",
            process_role(),
        )
        os.environ["CMDB_PROCESS_ROLE"] = "worker"
    results = await asyncio.gather(
        *(backend_main._run_worker_definition(worker, one_shot=one_shot) for worker in workers)
    )
    return 0 if all(results) else 1


def main() -> None:
    """Parse the worker mode and return a process-friendly exit code."""

    parser = argparse.ArgumentParser(description="Run IPT CMDB background workers")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run each enabled worker once and exit (for scheduled container jobs)",
    )
    arguments = parser.parse_args()
    raise SystemExit(asyncio.run(run_worker_service(one_shot=arguments.once)))


if __name__ == "__main__":
    main()
