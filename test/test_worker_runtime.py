"""Tests for deployment-neutral worker execution and durable heartbeat evidence."""

import asyncio
import unittest
from unittest.mock import patch

from src.cmdb.repository import StateRepository
from src.cmdb.worker_runtime import (
    PeriodicWorker,
    process_role,
    run_periodic_worker,
    run_worker_cycle,
)


class WorkerRuntimeTests(unittest.TestCase):
    """Verify worker topology labels, counters, and failure evidence."""

    def setUp(self):
        self.state = {
            "companies": [],
            "users": [],
            "assets": [],
            "relationships": [],
        }
        self.repository = StateRepository(self.state, lambda _state: None)

    def test_one_shot_worker_records_success_and_stopped_heartbeat(self):
        worker = PeriodicWorker("integrations", 60, lambda: {"processed": 3})

        with patch.dict("os.environ", {"CMDB_PROCESS_ROLE": "worker"}):
            succeeded = asyncio.run(
                run_periodic_worker(
                    worker,
                    self.repository.record_worker_runtime,
                    one_shot=True,
                    identity="worker-test",
                )
            )

        runtime = self.repository.get_worker_runtime("integrations")
        self.assertTrue(succeeded)
        self.assertEqual(runtime["workerId"], "worker-test")
        self.assertEqual(runtime["deploymentMode"], "one_shot")
        self.assertEqual(runtime["status"], "stopped")
        self.assertEqual(runtime["cyclesCompleted"], 1)
        self.assertEqual(runtime["itemsProcessed"], 3)
        self.assertIsNotNone(runtime["lastSuccessAt"])
        self.assertEqual(runtime["metadata"]["result"]["processed"], 3)

    def test_failed_cycle_is_degraded_without_leaking_control_flow(self):
        def fail():
            raise RuntimeError("provider temporarily unavailable")

        worker = PeriodicWorker("integrations", 60, fail)
        succeeded = asyncio.run(
            run_worker_cycle(
                worker,
                self.repository.record_worker_runtime,
                worker_id="worker-test",
                mode="dedicated",
            )
        )

        runtime = self.repository.get_worker_runtime("integrations")
        self.assertFalse(succeeded)
        self.assertEqual(runtime["status"], "degraded")
        self.assertEqual(runtime["cyclesCompleted"], 1)
        self.assertIn("RuntimeError", runtime["lastError"])
        self.assertIsNotNone(runtime["lastErrorAt"])

    def test_cancelled_cycle_cancels_and_awaits_execution_task(self):
        async def exercise_cancellation() -> None:
            started = asyncio.Event()
            execution_finished = asyncio.Event()

            async def fake_to_thread(_execute):
                started.set()
                try:
                    await asyncio.Future()
                finally:
                    execution_finished.set()

            worker = PeriodicWorker("integrations", 60, lambda: {"processed": 0})
            with patch("src.cmdb.worker_runtime.asyncio.to_thread", fake_to_thread):
                cycle = asyncio.create_task(
                    run_worker_cycle(
                        worker,
                        self.repository.record_worker_runtime,
                        worker_id="worker-test",
                        mode="dedicated",
                    )
                )
                await started.wait()
                cycle.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await cycle

            self.assertTrue(execution_finished.is_set())

        asyncio.run(exercise_cancellation())

    def test_invalid_process_role_falls_back_to_combined(self):
        with patch.dict("os.environ", {"CMDB_PROCESS_ROLE": "not-a-role"}):
            self.assertEqual(process_role(), "combined")


if __name__ == "__main__":
    unittest.main()
