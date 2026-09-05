"""Queue and lifecycle management independent of the HTTP transport."""

from __future__ import annotations

import logging
import os
import queue
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Any, Protocol

from .contracts import FeedbackRequest, RunRequest
from .store import ActiveRunConflict, EvaluationStore, QueueCapacityConflict

LOGGER = logging.getLogger(__name__)


ProgressCallback = Callable[[str, dict[str, Any]], None]


class RunExecutor(Protocol):
    def execute(
        self,
        request: RunRequest,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, Any]: ...


class PublicExecutionError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class QueueFullError(RuntimeError):
    pass


class ActiveRunError(RuntimeError):
    pass


class IdempotencyConflictError(RuntimeError):
    pass


class QuotaExceededError(RuntimeError):
    """The user already submitted the maximum questions in the window."""


class ReviewRequiredError(RuntimeError):
    """The user must evaluate a published answer before asking a question."""


# Fallback per-run inference estimate when no run has finished yet. From the
# first local measurements (244-1,136 s per question); used only until
# recent_durations() has real samples.
DEFAULT_RUN_SECONDS = 480.0
DEFAULT_LEASE_SECONDS = 120.0
DEFAULT_POLL_SECONDS = 0.25


class EvaluationService:
    def __init__(
        self,
        store: EvaluationStore,
        executor: RunExecutor,
        provenance_factory: Callable[[], dict[str, Any]],
        *,
        queue_capacity: int = 20,
        model_concurrency: int = 1,
        scheduler_workers: int | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        shutdown_timeout: float = 5.0,
    ):
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be positive")
        if model_concurrency < 1:
            raise ValueError("model_concurrency must be positive")
        if scheduler_workers is not None and scheduler_workers < 1:
            raise ValueError("scheduler_workers must be positive")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if shutdown_timeout < 0:
            raise ValueError("shutdown_timeout must not be negative")
        self.store = store
        self.executor = executor
        self.provenance_factory = provenance_factory
        # This is only a local wake-up queue. SQLite is the authoritative
        # queue, so another web process can claim work independently.
        self.queue: queue.Queue[str | None] = queue.Queue(maxsize=queue_capacity)
        self.model_concurrency = model_concurrency
        self.scheduler_workers = scheduler_workers or model_concurrency
        self.lease_seconds = lease_seconds
        self.worker_id = f"{os.getpid()}-{uuid.uuid4()}"
        self.workers: list[threading.Thread] = []
        self.worker = threading.Thread(
            target=self._worker_loop, name="dof-human-eval-worker-1", daemon=True
        )
        self.workers.append(self.worker)
        for index in range(2, self.scheduler_workers + 1):
            self.workers.append(
                threading.Thread(
                    target=self._worker_loop,
                    name=f"dof-human-eval-worker-{index}",
                    daemon=True,
                )
            )
        self.shutdown_timeout = shutdown_timeout
        self._closing = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._executor_close_lock = threading.Lock()
        self._executor_closed = False
        self._started = False

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started:
                return
            if self.worker.ident is not None:
                raise RuntimeError("create a new service instance after closing")
            self._closing.clear()
            self.store.initialize()
            self.store.initialize_model_slots(self.model_concurrency)
            recovered = self.store.recover_expired_model_slots()
            recovered += self.store.recover_unclaimed_started_runs()
            if recovered:
                LOGGER.warning("recovered %s interrupted executions", recovered)
            for worker in self.workers:
                worker.start()
            self._started = True

    def close(self) -> None:
        with self._lifecycle_lock:
            if not self._started:
                return
            self._started = False
            # Serialize this transition with event writes so no progress or
            # terminal result can be persisted after shutdown begins.
            with self._write_lock:
                self._closing.set()
            for _ in self.workers:
                try:
                    self.queue.put_nowait(None)
                except queue.Full:
                    break
        for worker in self.workers:
            worker.join(timeout=self.shutdown_timeout)
        if any(worker.is_alive() for worker in self.workers):
            expired = self.store.expire_model_leases(self.worker_id)
            if expired:
                LOGGER.warning("expired %s model leases during shutdown", expired)
            LOGGER.warning(
                "human-evaluation worker is still waiting for an in-flight call"
            )
        else:
            self._close_executor()

    def submit(
        self,
        request: RunRequest,
        *,
        user_id: str,
        admin: bool = False,
        daily_question_limit: int = 1,
    ) -> dict[str, Any]:
        # create_run performs the admission checks in one SQLite transaction;
        # this lock only serializes lifecycle and executor preparation locally.
        with self._lifecycle_lock:
            if not self._started:
                raise RuntimeError("service has not started")
            existing = self.idempotent_run(request, user_id=user_id)
            if existing is not None:
                return existing
            # These checks avoid preparing an expensive executor for an
            # obviously rejected request. The transactional checks inside
            # create_run remain authoritative across web processes.
            if self.store.has_active_run(user_id):
                raise ActiveRunError("user already has an active run")
            if not admin:
                if not self.store.has_review_since_last_submission(user_id):
                    raise ReviewRequiredError(
                        "a published-answer review is required before asking"
                    )
                if daily_question_limit >= 1:
                    cutoff = (
                        (datetime.now(timezone.utc) - timedelta(hours=24))
                        .isoformat()
                        .replace("+00:00", "Z")
                    )
                    if (
                        self.store.count_submissions_since(user_id, cutoff)
                        >= daily_question_limit
                    ):
                        raise QuotaExceededError("daily question limit reached")
            if self.store.queue_depth() >= self.queue.maxsize:
                raise QueueFullError("execution queue is full")
            prepare_executor = getattr(self.executor, "prepare", None)
            if callable(prepare_executor):
                prepare_executor()
            try:
                run, created = self.store.create_run(
                    request,
                    user_id=user_id,
                    provenance=self.provenance_factory(),
                    queue_capacity=self.queue.maxsize,
                )
            except ActiveRunConflict as exc:
                raise ActiveRunError("user already has an active run") from exc
            except QueueCapacityConflict as exc:
                raise QueueFullError("execution queue is full") from exc
            if created:
                try:
                    self.queue.put_nowait(run["run_id"])
                except queue.Full:
                    # Notification loss is harmless: every worker polls the
                    # persistent queue as a fallback.
                    pass
            return self.public_run(run["run_id"], user_id=user_id, admin=True)

    def idempotent_run(
        self, request: RunRequest, *, user_id: str
    ) -> dict[str, Any] | None:
        existing = self.store.find_idempotent_run(user_id, request.client_request_id)
        if existing is None:
            return None
        if any(
            (
                existing["question"] != request.question,
                existing["as_of"] != request.as_of,
                existing["required_hops"] != request.required_hops,
            )
        ):
            raise IdempotencyConflictError(
                "client_request_id was already used for a different request"
            )
        return self.public_run(existing["run_id"], user_id=user_id, admin=True)

    def public_run(
        self,
        run_id: str,
        *,
        user_id: str | None = None,
        admin: bool = False,
    ) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if not admin and not self._is_public(run):
            if user_id is None or not self.store.run_belongs_to(run_id, user_id):
                raise KeyError(run_id)
        run["events_url"] = f"/runs/{run_id}/events"
        if run["status"] == "queued":
            position = self.store.queue_position(run_id)
            if position is not None:
                run["queue_position"] = position
                run["estimated_wait_seconds"] = self.estimated_wait_seconds(position)
                run["queue_snapshot"] = self.queue_snapshot()
        elif run["status"] == "running":
            run["queue_snapshot"] = self.queue_snapshot()
        return run

    def estimated_wait_seconds(self, position: int) -> int:
        """Rough wait for a queued run at a 1-based FIFO position."""
        durations = self.store.recent_durations(limit=10)
        average = (
            sum(durations) / len(durations) if durations else DEFAULT_RUN_SECONDS
        )
        batches = ceil(position / self.model_concurrency)
        return max(1, int(round(batches * average)))

    def queue_retry_after(self) -> int:
        """Seconds a client should wait before retrying a full queue."""
        durations = self.store.recent_durations(limit=10)
        average = (
            sum(durations) / len(durations) if durations else DEFAULT_RUN_SECONDS
        )
        depth = max(self.store.queue_depth(), 1)
        batches = ceil(depth / self.model_concurrency)
        return max(60, int(round(batches * average)))

    def queue_snapshot(self) -> dict[str, int]:
        """Return shared queue state for the status UI and health endpoints."""
        activity = self.store.model_activity(self.model_concurrency)
        activity["queued"] = self.store.queue_depth()
        return activity

    @staticmethod
    def _is_public(run: dict[str, Any]) -> bool:
        return run["status"] == "succeeded" and run.get("published_at") is not None

    def submit_feedback(
        self,
        run_id: str,
        request: FeedbackRequest,
        *,
        user_id: str,
        admin: bool = False,
    ) -> dict[str, Any]:
        # Any signed-in user may evaluate a published answer; unpublished
        # runs stay private to their author (and admins).
        self.public_run(run_id, user_id=user_id, admin=admin)
        return self.store.add_feedback(run_id, request, user_id=user_id)

    def publish(self, run_id: str, *, admin_id: str) -> None:
        self.store.publish_run(run_id, publisher_id=admin_id)

    def unpublish(self, run_id: str) -> None:
        self.store.unpublish_run(run_id)

    def delete_run(self, run_id: str) -> None:
        """Delete a terminal run and all its data (admin-only action)."""
        self.store.delete_run(run_id)

    def _worker_loop(self) -> None:
        try:
            while not self._closing.is_set():
                try:
                    notification = self.queue.get(timeout=DEFAULT_POLL_SECONDS)
                except queue.Empty:
                    notification = "__poll__"
                try:
                    if notification is None or self._closing.is_set():
                        return
                    claim = self.store.claim_next_run(
                        worker_id=self.worker_id,
                        concurrency=self.model_concurrency,
                        lease_seconds=self.lease_seconds,
                    )
                    if claim is None:
                        continue
                    run_id, slot_id = claim
                    request = self.store.get_request(run_id)
                    if request is None:
                        self.store.release_model_slot(
                            run_id=run_id,
                            slot_id=slot_id,
                            worker_id=self.worker_id,
                        )
                        continue
                    heartbeat_stop = threading.Event()
                    heartbeat = threading.Thread(
                        target=self._lease_heartbeat,
                        args=(heartbeat_stop, run_id, slot_id),
                        name=f"dof-human-eval-lease-{slot_id}",
                        daemon=True,
                    )
                    heartbeat.start()
                    try:
                        try:
                            result = self.executor.execute(
                                request,
                                on_progress=lambda event_type,
                                payload: self._append_progress_if_open(
                                    run_id, event_type, payload
                                ),
                            )
                        except PublicExecutionError as exc:
                            if exc.__cause__ is not None:
                                LOGGER.exception(
                                    "human-evaluation run %s failed with %s",
                                    run_id,
                                    exc.code,
                                )
                            self._append_event_if_open(
                                run_id,
                                "failed",
                                {"code": exc.code, "message": str(exc)},
                            )
                        except Exception:
                            LOGGER.exception("human-evaluation run %s failed", run_id)
                            self._append_event_if_open(
                                run_id,
                                "failed",
                                {
                                    "code": "internal_error",
                                    "message": "La ejecución no pudo completarse.",
                                },
                            )
                        else:
                            self._append_event_if_open(run_id, "succeeded", result)
                    finally:
                        heartbeat_stop.set()
                        heartbeat.join(timeout=1)
                        if not self._closing.is_set():
                            self.store.release_model_slot(
                                run_id=run_id,
                                slot_id=slot_id,
                                worker_id=self.worker_id,
                            )
                finally:
                    if notification != "__poll__":
                        self.queue.task_done()
        finally:
            if self._closing.is_set() and not any(
                worker.is_alive() and worker is not threading.current_thread()
                for worker in self.workers
            ):
                self._close_executor()

    def _lease_heartbeat(
        self, stop: threading.Event, run_id: str, slot_id: int
    ) -> None:
        interval = max(self.lease_seconds / 3, 0.1)
        while not stop.wait(interval):
            if self._closing.is_set():
                return
            if not self.store.renew_model_slot(
                run_id=run_id,
                slot_id=slot_id,
                worker_id=self.worker_id,
                lease_seconds=self.lease_seconds,
            ):
                LOGGER.warning("lost model lease for run %s", run_id)
                return

    def _close_executor(self) -> None:
        # If shutdown timed out while a run was active, the worker calls this
        # after execute() returns so it never tears down an active run.
        with self._executor_close_lock:
            if self._executor_closed:
                return
            self._executor_closed = True
        close_executor = getattr(self.executor, "close", None)
        if callable(close_executor):
            try:
                close_executor()
            except Exception:
                LOGGER.exception("executor shutdown hook failed")

    def _append_event_if_open(
        self,
        run_id: str,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        with self._write_lock:
            if self._closing.is_set():
                return False
            try:
                self.store.append_event(run_id, event_type, payload)
            except ValueError:
                # Another process may have recovered this run after our lease
                # lapsed (restart, stalled heartbeat) and written a terminal
                # event. That terminal event wins: drop our late result and
                # keep the worker alive instead of dying on the rejected
                # transition.
                LOGGER.warning(
                    "run %s already terminal; dropping %s event", run_id, event_type
                )
                return False
            return True

    def _append_progress_if_open(
        self, run_id: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        with self._write_lock:
            if self._closing.is_set():
                LOGGER.debug(
                    "Dropping late progress event %r for run %s during shutdown",
                    event_type,
                    run_id,
                )
                return
            self.store.append_progress(run_id, event_type, payload)
