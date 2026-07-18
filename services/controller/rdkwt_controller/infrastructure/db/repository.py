from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from .models import Attempt, ConversionRun

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}
ACTIVE_STATUSES = {
    "PROVISIONING",
    "RUNNING",
    "INSPECTING",
    "CHECKING",
    "PREPROCESSING",
    "COMPILING",
    "VERIFYING",
    "COLLECTING",
    "CANCELLING",
}
RETRYABLE_STATUSES = {"FAILED", "CANCELLED", "INTERRUPTED"}


@dataclass(frozen=True, slots=True)
class ExecutionRecord:
    run_id: str
    attempt: int
    kind: str
    status: str
    model_version_id: str | None
    request_snapshot: dict[str, Any]
    runner_image_reference: str | None
    runner_image_id: str | None
    container_id: str | None


class RunRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create(self, *, run: ConversionRun, attempt: Attempt) -> None:
        with self._session_factory.begin() as session:
            session.add_all((run, attempt))

    def claim_queued(self, run_id: str, attempt: int) -> bool:
        with self._session_factory.begin() as session:
            run, row = self._load_pair(session, run_id, attempt)
            if run.status != "QUEUED" or row.status != "QUEUED":
                return False
            now = datetime.now(UTC)
            run.status = "PROVISIONING"
            row.status = "PROVISIONING"
            row.stage = "PROVISIONING"
            row.started_at = now
            return True

    def set_running(self, run_id: str, attempt: int, container_id: str) -> bool:
        with self._session_factory.begin() as session:
            run, row = self._load_pair(session, run_id, attempt)
            row.container_id = container_id
            if run.status == "CANCELLING" or row.status == "CANCELLING":
                return False
            if run.status in TERMINAL_STATUSES or row.status in TERMINAL_STATUSES:
                return False
            run.status = "RUNNING"
            row.status = "RUNNING"
            row.stage = "RUNNING"
            if row.started_at is None:
                row.started_at = datetime.now(UTC)
            return True

    def set_stage(self, run_id: str, attempt: int, stage: str) -> None:
        with self._session_factory.begin() as session:
            run, row = self._load_pair(session, run_id, attempt)
            if run.status in TERMINAL_STATUSES or row.status in TERMINAL_STATUSES:
                return
            if run.status == "CANCELLING" or row.status == "CANCELLING":
                return
            run.status = stage
            row.status = stage
            row.stage = stage

    def request_cancel(self, run_id: str) -> dict[str, Any]:
        with self._session_factory.begin() as session:
            run = session.scalar(
                select(ConversionRun)
                .where(ConversionRun.id == run_id)
                .options(selectinload(ConversionRun.attempts))
            )
            if run is None or not run.attempts:
                raise KeyError(f"unknown run: {run_id}")
            row = run.attempts[-1]
            if run.status in TERMINAL_STATUSES:
                raise ValueError(f"run is already terminal: {run.status}")
            now = datetime.now(UTC)
            previous = run.status
            if run.status == "QUEUED" and row.status == "QUEUED":
                run.status = "CANCELLED"
                row.status = "CANCELLED"
                row.stage = "CANCELLED"
                row.cancel_requested_at = now
                row.finished_at = now
                terminal = True
            else:
                run.status = "CANCELLING"
                row.status = "CANCELLING"
                row.stage = "CANCELLING"
                row.cancel_requested_at = now
                terminal = False
            return {
                "run_id": run.id,
                "attempt": row.number,
                "previous_status": previous,
                "status": run.status,
                "container_id": row.container_id,
                "terminal": terminal,
            }

    def is_cancel_requested(self, run_id: str, attempt: int) -> bool:
        with self._session_factory() as session:
            run, row = self._load_pair(session, run_id, attempt)
            return (
                run.status in {"CANCELLING", "CANCELLED"}
                or row.cancel_requested_at is not None
            )

    def finish(
        self,
        run_id: str,
        attempt: int,
        *,
        status: str,
        exit_code: int | None,
        result_payload: dict[str, Any] | None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"invalid terminal run status: {status}")
        with self._session_factory.begin() as session:
            run, row = self._load_pair(session, run_id, attempt)
            run.status = status
            run.error_code = error_code
            run.error_message = error_message
            row.status = status
            row.stage = status
            row.exit_code = exit_code
            row.result_payload = result_payload
            row.finished_at = datetime.now(UTC)

    def retry(self, run_id: str, *, attempt: Attempt) -> None:
        with self._session_factory.begin() as session:
            run = session.scalar(
                select(ConversionRun)
                .where(ConversionRun.id == run_id)
                .options(selectinload(ConversionRun.attempts))
            )
            if run is None:
                raise KeyError(f"unknown run: {run_id}")
            if run.status not in RETRYABLE_STATUSES:
                raise ValueError(f"run status {run.status} cannot be retried")
            expected = len(run.attempts) + 1
            if attempt.number != expected:
                raise ValueError(f"retry attempt must be {expected}")
            run.status = "QUEUED"
            run.error_code = None
            run.error_message = None
            session.add(attempt)

    def mark_recovered(self, run_id: str, attempt: int) -> None:
        with self._session_factory.begin() as session:
            _run, row = self._load_pair(session, run_id, attempt)
            row.recovered = True

    def mark_interrupted(self, run_id: str, attempt: int, message: str) -> None:
        self.finish(
            run_id,
            attempt,
            status="INTERRUPTED",
            exit_code=None,
            result_payload=None,
            error_code="RUN_RECOVERY_FAILED",
            error_message=message,
        )

    def queued(self) -> list[ExecutionRecord]:
        with self._session_factory() as session:
            rows = session.execute(
                select(ConversionRun, Attempt)
                .join(Attempt, Attempt.run_id == ConversionRun.id)
                .where(ConversionRun.status == "QUEUED", Attempt.status == "QUEUED")
                .order_by(ConversionRun.created_at, Attempt.number)
            ).all()
            return [self._execution(run, attempt) for run, attempt in rows]

    def active(self) -> list[ExecutionRecord]:
        with self._session_factory() as session:
            rows = session.execute(
                select(ConversionRun, Attempt)
                .join(Attempt, Attempt.run_id == ConversionRun.id)
                .where(
                    ConversionRun.status.in_(ACTIVE_STATUSES),
                    Attempt.status.in_(ACTIVE_STATUSES),
                )
                .order_by(ConversionRun.created_at, Attempt.number)
            ).all()
            return [self._execution(run, attempt) for run, attempt in rows]

    def latest_kind(self, kind: str) -> dict[str, Any] | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ConversionRun)
                .where(ConversionRun.kind == kind)
                .options(selectinload(ConversionRun.attempts))
                .order_by(ConversionRun.created_at.desc())
                .limit(1)
            )
            if row is None:
                return None
            return self._serialize(row, detail=True, queue_position=None)

    def execution(self, run_id: str, attempt: int) -> ExecutionRecord:
        with self._session_factory() as session:
            run, row = self._load_pair(session, run_id, attempt)
            return self._execution(run, row)

    def list(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(ConversionRun)
                .options(selectinload(ConversionRun.attempts))
                .order_by(ConversionRun.created_at.desc())
            ).all()
            queued_ids = {
                run_id: position
                for position, run_id in enumerate(
                    session.scalars(
                        select(ConversionRun.id)
                        .where(ConversionRun.status == "QUEUED")
                        .order_by(ConversionRun.created_at)
                    ).all(),
                    start=1,
                )
            }
            return [
                self._serialize(row, detail=False, queue_position=queued_ids.get(row.id))
                for row in rows
            ]

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ConversionRun)
                .where(ConversionRun.id == run_id)
                .options(selectinload(ConversionRun.attempts))
            )
            if row is None:
                return None
            queue_position = None
            if row.status == "QUEUED":
                queue_position = session.scalar(
                    select(func.count(ConversionRun.id)).where(
                        ConversionRun.status == "QUEUED",
                        ConversionRun.created_at <= row.created_at,
                    )
                )
            return self._serialize(row, detail=True, queue_position=queue_position)

    @staticmethod
    def _load_pair(
        session: Session, run_id: str, attempt: int
    ) -> tuple[ConversionRun, Attempt]:
        run = session.get(ConversionRun, run_id)
        row = session.scalar(
            select(Attempt).where(Attempt.run_id == run_id, Attempt.number == attempt)
        )
        if run is None or row is None:
            raise KeyError(f"unknown run attempt: {run_id}/{attempt}")
        return run, row

    @staticmethod
    def _execution(run: ConversionRun, attempt: Attempt) -> ExecutionRecord:
        return ExecutionRecord(
            run_id=run.id,
            attempt=attempt.number,
            kind=run.kind,
            status=run.status,
            model_version_id=run.model_version_id,
            request_snapshot=run.request_snapshot,
            runner_image_reference=run.runner_image_reference,
            runner_image_id=run.runner_image_id,
            container_id=attempt.container_id,
        )

    @staticmethod
    def _serialize_attempt(attempt: Attempt, *, detail: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "number": attempt.number,
            "status": attempt.status,
            "stage": attempt.stage,
            "container_id": attempt.container_id,
            "exit_code": attempt.exit_code,
            "recovered": attempt.recovered,
            "created_at": attempt.created_at.isoformat(),
            "started_at": (
                None if attempt.started_at is None else attempt.started_at.isoformat()
            ),
            "finished_at": (
                None if attempt.finished_at is None else attempt.finished_at.isoformat()
            ),
            "cancel_requested_at": (
                None
                if attempt.cancel_requested_at is None
                else attempt.cancel_requested_at.isoformat()
            ),
        }
        if detail:
            payload["result"] = attempt.result_payload
        return payload

    @classmethod
    def _serialize(
        cls,
        row: ConversionRun,
        *,
        detail: bool,
        queue_position: int | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": row.id,
            "kind": row.kind,
            "project_id": row.project_id,
            "model_version_id": row.model_version_id,
            "calibration_version_id": row.calibration_version_id,
            "profile_id": row.profile_id,
            "profile_sha256": row.profile_sha256,
            "status": row.status,
            "queue_position": queue_position,
            "runner_image": {
                "reference": row.runner_image_reference,
                "immutable_id": row.runner_image_id,
            },
            "contract_version": row.contract_version,
            "app_version": row.app_version,
            "error": (
                None
                if row.error_code is None
                else {"code": row.error_code, "message": row.error_message}
            ),
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
            "attempts": [
                cls._serialize_attempt(attempt, detail=detail) for attempt in row.attempts
            ],
        }
        if detail:
            payload["request"] = row.request_snapshot
            payload["generated_yaml"] = row.generated_yaml
        return payload
