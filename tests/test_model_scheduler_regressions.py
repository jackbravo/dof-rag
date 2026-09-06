"""Regression coverage for shared scheduler lifecycle and maintenance races."""

from __future__ import annotations

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
