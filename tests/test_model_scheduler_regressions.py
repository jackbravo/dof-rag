"""Regression coverage for shared scheduler lifecycle and maintenance races."""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from human_eval.app import _queue_status_event, _status_fragment
from human_eval.contracts import RunRequest
from human_eval.service import EvaluationService
from human_eval.store import EvaluationStore
from scripts.seed_human_eval_v4_hybrid import seed_live_run


class BlockingExecutor:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def provenance(self):
        return {}

    def execute(self, request, *, on_progress=None):
        self.started.set()
        if not self.release.wait(3):
            raise RuntimeError("test executor timed out")
        if on_progress:
            on_progress("agent_started", {})
        return {}


class SchedulerRegressionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = EvaluationStore(Path(self.directory.name) / "evaluation.sqlite")
        self.store.initialize()

    def make_service(self, **kwargs):
        executor = BlockingExecutor()
        service = EvaluationService(self.store, executor, executor.provenance, **kwargs)
        service.start()
        self.addCleanup(service.close)
        self.addCleanup(executor.release.set)
        return service, executor

    def test_deleted_recovered_run_does_not_kill_worker(self):
        service, executor = self.make_service()
        run = service.submit(RunRequest("first question"), user_id="one", admin=True)
        self.assertTrue(executor.started.wait(1))
        self.store.expire_model_leases(service.worker_id)
        self.assertEqual(self.store.recover_expired_model_slots(), 1)
        self.store.delete_run(run["run_id"])
        executor.release.set()
        following = service.submit(
            RunRequest("next question"), user_id="one", admin=True
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.store.get_run(following["run_id"])["status"] == "succeeded":
                break
            time.sleep(0.01)
        self.assertTrue(service.worker.is_alive())
        self.assertEqual(self.store.get_run(following["run_id"])["status"], "succeeded")

    def test_terminal_commit_before_close_releases_slot(self):
        service, executor = self.make_service(shutdown_timeout=0.01)
        committed = threading.Event()
        finish = threading.Event()
        self.addCleanup(finish.set)
        append = service._append_event_if_open

        def pause_after_commit(*args, **kwargs):
            written = append(*args, **kwargs)
            committed.set()
            if not finish.wait(3):
                raise RuntimeError("test commit barrier timed out")
            return written

        with mock.patch.object(
            service, "_append_event_if_open", side_effect=pause_after_commit
        ):
            run = service.submit(RunRequest("question"), user_id="one", admin=True)
            executor.release.set()
            self.assertTrue(committed.wait(1))
            service.close()
            finish.set()
            service.worker.join(2)
        self.assertFalse(service.worker.is_alive())
        self.assertEqual(self.store.get_run(run["run_id"])["status"], "succeeded")
        self.assertEqual(self.store.model_activity(1)["available"], 1)

    def test_shutdown_workers_share_one_deadline(self):
        service = EvaluationService(
            self.store, BlockingExecutor(), lambda: {}, shutdown_timeout=5
        )
        service._started = True
        clock = [100.0]
        waits = []

        def join(*, timeout):
            waits.append(timeout)
            clock[0] += timeout

        service.workers = [
            mock.Mock(join=mock.Mock(side_effect=join)) for _ in range(3)
        ]
        with mock.patch(
            "human_eval.service.time.monotonic", side_effect=lambda: clock[0]
        ):
            service.close()
        self.assertEqual(waits, [5.0, 0.0, 0.0])

    def test_seed_cannot_be_claimed_between_creation_and_execution(self):
        self.store.initialize_model_slots(1)
        competitor = EvaluationStore(self.store.path)
        create = self.store.create_run
        claims = []

        def create_and_compete(*args, **kwargs):
            record = create(*args, **kwargs)
            claims.append(
                competitor.claim_next_run(
                    worker_id="web", concurrency=1, lease_seconds=60
                )
            )
            return record

        executor = BlockingExecutor()
        executor.release.set()
        with mock.patch.object(
            self.store, "create_run", side_effect=create_and_compete
        ):
            outcome = seed_live_run(
                self.store,
                executor,
                {"id": "seed-race", "question": "question"},
                publish=False,
            )
        self.assertEqual(claims, [None])
        self.assertEqual(outcome, "created")

    def test_wait_estimate_accounts_for_idle_and_partial_capacity(self):
        self.store.initialize_model_slots(4)
        service = EvaluationService(
            self.store, BlockingExecutor(), lambda: {}, model_concurrency=4
        )
        self.assertEqual(
            [service.estimated_wait_seconds(p) for p in range(1, 6)], [0, 0, 0, 0, 480]
        )
        for i in range(2):
            self.store.create_run(RunRequest("question"), user_id=str(i), provenance={})
            self.store.claim_next_run(
                worker_id="other", concurrency=4, lease_seconds=60
            )
        self.assertEqual(
            [service.estimated_wait_seconds(p) for p in range(1, 8)],
            [0, 0, 480, 480, 480, 480, 960],
        )

    def test_near_zero_wait_has_consistent_html_and_stream_text(self):
        for wait in (0, 1, 59):
            with self.subTest(wait=wait):
                run = {
                    "created_at": "2026-09-05T00:00:00Z",
                    "run_id": "test",
                    "status": "queued",
                    "queue_position": 1,
                    "estimated_wait_seconds": wait,
                    "queue_snapshot": {"active": 0, "capacity": 4},
                }
                event = _queue_status_event(run)
                self.assertIn("Espera aproximada: menos de 1 min", event[1])
                html = _status_fragment(run)
                self.assertIn("Espera aproximada: menos de 1 min", html)

    def test_claim_is_serialized_with_shutdown(self):
        executor = BlockingExecutor()
        service = EvaluationService(
            self.store, executor, lambda: {}, shutdown_timeout=0.01
        )
        entered = threading.Event()
        release = threading.Event()
        observed = []
        original = self.store.claim_next_run

        def claim(**kwargs):
            acquired = service._write_lock.acquire(blocking=False)
            observed.append(acquired)
            self.assertLessEqual(kwargs["timeout"], service.shutdown_timeout)
            if acquired:
                service._write_lock.release()
            entered.set()
            if not release.wait(3):
                raise RuntimeError("claim barrier timed out")
            return original(**kwargs)

        with mock.patch.object(self.store, "claim_next_run", side_effect=claim):
            service.start()
            service.queue.put_nowait("wake")
            try:
                self.assertTrue(entered.wait(1))
                self.assertEqual(observed, [False])
            finally:
                release.set()
                service.close()
        # Once the shutdown transition holds the lock, no later claim is made.
        with mock.patch.object(self.store, "claim_next_run") as later_claim:
            service._worker_loop()
        later_claim.assert_not_called()

    def test_scheduler_retries_busy_claim_and_completes_queued_work(self):
        executor = BlockingExecutor()
        executor.release.set()
        service = EvaluationService(self.store, executor, lambda: {})
        claim = self.store.claim_next_run
        busy = sqlite3.OperationalError("database is locked")
        busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
        attempts = []

        def fail_once(**kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise busy
            return claim(**kwargs)

        with mock.patch.object(self.store, "claim_next_run", side_effect=fail_once):
            service.start()
            try:
                run = service.submit(RunRequest("question"), user_id="one", admin=True)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if self.store.get_run(run["run_id"])["status"] == "succeeded":
                        break
                    time.sleep(0.01)
                self.assertEqual(
                    self.store.get_run(run["run_id"])["status"], "succeeded"
                )
                self.assertTrue(service.worker.is_alive())
                self.assertGreaterEqual(len(attempts), 2)
            finally:
                service.close()

    def test_heartbeat_retries_busy_renewal_while_execution_continues(self):
        service, executor = self.make_service(lease_seconds=1)
        renew = self.store.renew_model_slot
        renewed = threading.Event()
        busy = sqlite3.OperationalError("database is locked")
        busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
        attempts = []

        def fail_once(**kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise busy
            result = renew(**kwargs)
            if result:
                renewed.set()
            return result

        with mock.patch.object(self.store, "renew_model_slot", side_effect=fail_once):
            run = service.submit(RunRequest("question"), user_id="one", admin=True)
            self.assertTrue(renewed.wait(2))
            executor.release.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if self.store.get_run(run["run_id"])["status"] == "succeeded":
                    break
                time.sleep(0.01)
            self.assertEqual(self.store.get_run(run["run_id"])["status"], "succeeded")

    def test_heartbeat_stops_retrying_at_deadline(self):
        service = EvaluationService(
            self.store, BlockingExecutor(), lambda: {}, lease_seconds=1
        )
        clock = [0.0]
        waits = []
        stop = mock.Mock()

        def wait(seconds):
            waits.append(seconds)
            clock[0] += seconds
            return False

        stop.wait.side_effect = wait
        busy = sqlite3.OperationalError("database is locked")
        busy.sqlite_errorcode = sqlite3.SQLITE_LOCKED
        with (
            mock.patch(
                "human_eval.service.time.monotonic", side_effect=lambda: clock[0]
            ),
            mock.patch.object(
                self.store, "renew_model_slot", side_effect=busy
            ) as renew,
        ):
            service._lease_heartbeat(stop, "run", 1)
        self.assertAlmostEqual(clock[0], 1.0)
        self.assertGreater(renew.call_count, 1)
        self.assertLess(renew.call_count, 10)
        self.assertTrue(all(0 < value <= 1 / 3 for value in waits))

    def test_heartbeat_does_not_retry_non_transient_errors(self):
        service = EvaluationService(self.store, BlockingExecutor(), lambda: {})
        stop = mock.Mock()
        stop.wait.return_value = False
        error = sqlite3.OperationalError("no such table: model_slots")
        error.sqlite_errorcode = sqlite3.SQLITE_ERROR
        with mock.patch.object(
            self.store, "renew_model_slot", side_effect=error
        ) as renew:
            with self.assertRaises(sqlite3.OperationalError):
                service._lease_heartbeat(stop, "run", 1)
        self.assertEqual(renew.call_count, 1)

    def test_renewal_respects_short_sqlite_busy_timeout(self):
        self.store.initialize_model_slots(1)
        run, _ = self.store.create_run(
            RunRequest("question"), user_id="one", provenance={}
        )
        self.store.claim_next_run(worker_id="owner", concurrency=1, lease_seconds=60)
        with self.store._connect() as locked:
            locked.execute("BEGIN IMMEDIATE")
            with self.assertRaises(sqlite3.OperationalError) as raised:
                self.store.renew_model_slot(
                    run_id=run["run_id"],
                    slot_id=1,
                    worker_id="owner",
                    lease_seconds=60,
                    timeout=0,
                )
            self.assertEqual(raised.exception.sqlite_errorcode, sqlite3.SQLITE_BUSY)
        self.assertTrue(
            self.store.renew_model_slot(
                run_id=run["run_id"], slot_id=1, worker_id="owner", lease_seconds=60
            )
        )
