"""Web-side admission and query service, independent of the HTTP transport.

Web processes only create ``queued`` runs and read state. Execution lives in
the singleton scheduler process (``human_eval.scheduler``), which is the only
component that transitions runs to ``started`` or terminal states.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from math import ceil
from typing import Any

from .contracts import FeedbackRequest, RunRequest
from .store import (
    ActiveRunConflict,
    DailyQuotaConflict,
    EvaluationStore,
    IdempotencyPayloadConflict,
    QueueCapacityConflict,
)

LOGGER = logging.getLogger(__name__)


ProgressCallback = Callable[[str, dict[str, Any]], None]


# Fallback per-run inference estimate when no run has finished yet. From the
# first local measurements (244-1,136 s per question); used only until
# recent_durations() has real samples.
DEFAULT_RUN_SECONDS = 480.0


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


class EvaluationService:
    """Admission and read model shared by every web worker process."""

    def __init__(
        self,
        store: EvaluationStore,
        *,
        queue_capacity: int = 20,
        model_concurrency: int = 1,
    ):
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be positive")
        if model_concurrency < 1:
            raise ValueError("model_concurrency must be positive")
        self.store = store
        self.queue_capacity = queue_capacity
        # Display-only mirror of the scheduler's concurrency: the scheduler
        # enforces the real limit; the web process shows it in the UI.
        self.model_concurrency = model_concurrency
        self._lifecycle_lock = threading.Lock()
        self._started = False

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started:
                return
            # Web workers validate but never migrate; the scheduler owns the
            # schema, so a migration can never race web startup.
            self.store.validate_schema()
            self._started = True

    def close(self) -> None:
        with self._lifecycle_lock:
            self._started = False

    def submit(
        self,
        request: RunRequest,
        *,
        user_id: str,
        admin: bool = False,
        daily_question_limit: int = 1,
    ) -> dict[str, Any]:
        # create_run performs the admission checks in one SQLite transaction;
        # the checks below only produce nicer errors without paying for a
        # transaction first. The transactional checks are authoritative
        # across all web processes.
        with self._lifecycle_lock:
            if not self._started:
                raise RuntimeError("service has not started")
            existing = self.idempotent_run(request, user_id=user_id)
            if existing is not None:
                return existing
            if self.store.has_active_run(user_id):
                LOGGER.warning(
                    "admission rejected: active run exists (depth=%s, capacity=%s)",
                    self.store.queue_depth(),
                    self.queue_capacity,
                )
                raise ActiveRunError("user already has an active run")
            daily_since: str | None = None
            if not admin:
                if not self.store.has_review_since_last_submission(user_id):
                    raise ReviewRequiredError(
                        "a published-answer review is required before asking"
                    )
                if daily_question_limit >= 1:
                    daily_since = (
                        (datetime.now(timezone.utc) - timedelta(hours=24))
                        .isoformat()
                        .replace("+00:00", "Z")
                    )
                    if (
                        self.store.count_submissions_since(user_id, daily_since)
                        >= daily_question_limit
                    ):
                        raise QuotaExceededError("daily question limit reached")
            queue_depth = self.store.queue_depth()
            if queue_depth >= self.queue_capacity:
                LOGGER.warning(
                    "admission rejected: queue full (depth=%s, capacity=%s)",
                    queue_depth,
                    self.queue_capacity,
                )
                raise QueueFullError("execution queue is full")
            try:
                run, created = self.store.create_run(
                    request,
                    user_id=user_id,
                    provenance=None,
                    enforce_active_run=True,
                    queue_capacity=self.queue_capacity,
                    daily_question_limit=(
                        daily_question_limit
                        if not admin and daily_question_limit >= 1
                        else None
                    ),
                    daily_since=daily_since,
                )
            except ActiveRunConflict as exc:
                LOGGER.warning(
                    "admission rejected: active run exists (depth=%s, capacity=%s)",
                    self.store.queue_depth(),
                    self.queue_capacity,
                )
                raise ActiveRunError("user already has an active run") from exc
            except QueueCapacityConflict as exc:
                LOGGER.warning(
                    "admission rejected: queue full (depth=%s, capacity=%s)",
                    self.store.queue_depth(),
                    self.queue_capacity,
                )
                raise QueueFullError("execution queue is full") from exc
            except DailyQuotaConflict as exc:
                raise QuotaExceededError("daily question limit reached") from exc
            except IdempotencyPayloadConflict as exc:
                raise IdempotencyConflictError(
                    "client_request_id was already used for a different request"
                ) from exc
            if not created:
                # Another web process won the idempotency race.
                return self.public_run(run["run_id"], user_id=user_id, admin=True)
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
        available = self.store.model_activity(self.model_concurrency)["available"]
        batches = ceil(max(0, position - available) / self.model_concurrency)
        return max(0, int(round(batches * average)))

    def queue_retry_after(self) -> int:
        """Estimate when the next completion should free one queue place."""
        durations = self.store.recent_durations(limit=10)
        average = (
            sum(durations) / len(durations) if durations else DEFAULT_RUN_SECONDS
        )
        return max(60, int(round(average)))

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
