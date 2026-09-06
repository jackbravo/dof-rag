"""Regression tests for scheduler cleanup, duration sampling, and seed paths."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from human_eval.contracts import RunRequest
from human_eval.scheduler import RunScheduler
from human_eval.store import EvaluationStore
from scripts import seed_human_eval_v4_hybrid as seed


class SchedulerCleanupTests(unittest.TestCase):
    def test_poll_errors_drain_work_before_closing_executor(self):
        for source in ("queue_depth", "provenance", "claim_next_run"):
            with self.subTest(source=source):
                store = mock.Mock()
                store.queue_depth.return_value = 1
                executor = mock.Mock()
                target = executor if source == "provenance" else store
                getattr(target, source).side_effect = RuntimeError(source)
                scheduler = RunScheduler(store, executor)
                completed = []
                original_poll = scheduler.poll_once

                def poll():
                    scheduler._pool.submit(lambda: completed.append(True))
                    return original_poll()

                closed_after = []
                executor.close.side_effect = lambda: closed_after.extend(completed)
                with mock.patch.object(scheduler, "poll_once", side_effect=poll):
                    with self.assertRaisesRegex(RuntimeError, source):
                        scheduler.run()
                executor.close.assert_called_once()
                self.assertEqual(closed_after, [True])
                self.assertIsNone(scheduler._pool)


class DurationSamplingTests(unittest.TestCase):
    def test_recovery_is_excluded_before_sample_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            store = EvaluationStore(Path(directory) / "eval.sqlite")
            store.initialize()
            for index, terminal in enumerate(("succeeded", "failed", "recovery")):
                run, _ = store.create_run(RunRequest("question"), user_id=str(index))
                run_id = run["run_id"]
                store.append_event(run_id, "started")
                if terminal == "recovery":
                    store.fail_interrupted_runs()
                else:
                    store.append_event(run_id, terminal, {})
                with store._connect() as connection:
                    connection.execute(
                        "UPDATE run_events SET created_at = ? WHERE run_id = ? "
                        "AND event_type = 'started'",
                        ("2026-01-01T00:00:00+00:00", run_id),
                    )
                    connection.execute(
                        "UPDATE run_events SET created_at = ? WHERE run_id = ? "
                        "AND event_type IN ('succeeded', 'failed')",
                        (f"2026-01-0{index + 1}T00:00:10+00:00", run_id),
                    )
            self.assertEqual(store.recent_durations(limit=2), [86410.0, 10.0])


class SeedDatabaseTests(unittest.TestCase):
    def test_database_precedence_matches_lock_and_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for cli, env, expected in (
                (None, None, root / "var/human_evaluation.sqlite"),
                (None, str(root / "env.sqlite"), root / "env.sqlite"),
                (str(root / "cli.sqlite"), str(root / "env.sqlite"), root / "cli.sqlite"),
            ):
                with self.subTest(cli=cli, env=env):
                    argv = ["seed", "--repo-root", str(root)]
                    if cli:
                        argv.extend(["--db", cli])
                    environment = {"DOF_HUMAN_EVAL_DB": env} if env else {}
                    with (
                        mock.patch.dict(os.environ, environment, clear=True),
                        mock.patch("sys.argv", argv),
                        mock.patch.object(seed, "load_queries", return_value=[]),
                        mock.patch.object(seed, "EvaluationStore") as store,
                        mock.patch.object(seed, "acquire_execution_lock", return_value=123) as lock,
                        mock.patch.object(seed, "AgentExecutorConfig"),
                        mock.patch.object(seed, "AgentRunExecutor"),
                        mock.patch.object(seed.os, "close"),
                        mock.patch("builtins.print"),
                    ):
                        self.assertEqual(seed.main(), 0)
                    store.assert_called_once_with(expected)
                    lock.assert_called_once_with(expected)
