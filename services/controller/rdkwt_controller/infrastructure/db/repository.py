from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload, sessionmaker

from .models import Attempt, ConversionRun


class RunRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create(self, *, run: ConversionRun, attempt: Attempt) -> None:
        with self._session_factory.begin() as session:
            session.add(run)
            session.add(attempt)

    def set_running(self, run_id: str, attempt: int, container_id: str) -> None:
        now = datetime.now(UTC)
        with self._session_factory.begin() as session:
            run, row = self._load_pair(session, run_id, attempt)
            run.status = "RUNNING"
            row.status = "RUNNING"
            row.container_id = container_id
            row.started_at = now

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
        with self._session_factory.begin() as session:
            run, row = self._load_pair(session, run_id, attempt)
            run.status = status
            run.error_code = error_code
            run.error_message = error_message
            row.status = status
            row.exit_code = exit_code
            row.result_payload = result_payload
            row.finished_at = datetime.now(UTC)

    def list(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(ConversionRun)
                .options(selectinload(ConversionRun.attempts))
                .order_by(ConversionRun.created_at.desc())
            ).all()
            return [self._serialize(row) for row in rows]

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._session_factory() as session:
            row = session.scalar(
                select(ConversionRun)
                .where(ConversionRun.id == run_id)
                .options(selectinload(ConversionRun.attempts))
            )
            return None if row is None else self._serialize(row)

    @staticmethod
    def _load_pair(session: Session, run_id: str, attempt: int) -> tuple[ConversionRun, Attempt]:
        run = session.get(ConversionRun, run_id)
        row = session.scalar(
            select(Attempt).where(Attempt.run_id == run_id, Attempt.number == attempt)
        )
        if run is None or row is None:
            raise KeyError(f"unknown run attempt: {run_id}/{attempt}")
        return run, row

    @staticmethod
    def _serialize(row: ConversionRun) -> dict[str, Any]:
        return {
            "id": row.id,
            "profile_id": row.profile_id,
            "status": row.status,
            "error": (
                None
                if row.error_code is None
                else {"code": row.error_code, "message": row.error_message}
            ),
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
            "attempts": [
                {
                    "number": attempt.number,
                    "status": attempt.status,
                    "container_id": attempt.container_id,
                    "exit_code": attempt.exit_code,
                    "created_at": attempt.created_at.isoformat(),
                    "started_at": (
                        None if attempt.started_at is None else attempt.started_at.isoformat()
                    ),
                    "finished_at": (
                        None if attempt.finished_at is None else attempt.finished_at.isoformat()
                    ),
                }
                for attempt in row.attempts
            ],
        }
