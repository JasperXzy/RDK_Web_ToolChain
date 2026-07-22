from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .models import BoardRun, Device

BOARD_TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"}
BOARD_ACTIVE_STATUSES = {
    "CONNECTING",
    "UPLOADING",
    "RUNNING",
    "COLLECTING",
    "CLEANING",
    "CANCELLING",
}


@dataclass(frozen=True, slots=True)
class BoardExecution:
    run_id: str
    device_id: str | None
    conversion_run_id: str | None
    mode: str
    status: str
    options: dict[str, Any]
    device_snapshot: dict[str, Any]
    local_dir: str
    remote_dir: str


class BoardRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create_device(
        self,
        *,
        name: str,
        platform: str,
        host: str,
        port: int,
        user: str,
        auth_type: str,
        credential_ref: str,
        host_key_fingerprint: str | None,
    ) -> dict[str, Any]:
        row = Device(
            id=str(uuid.uuid4()),
            name=name,
            platform=platform,
            host=host,
            port=port,
            user=user,
            auth_type=auth_type,
            credential_ref=credential_ref,
            host_key_fingerprint=host_key_fingerprint,
        )
        with self._session_factory.begin() as session:
            session.add(row)
        return self.get_device(row.id)

    def list_devices(self) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            rows = session.scalars(select(Device).order_by(Device.updated_at.desc())).all()
            return [self._serialize_device(row) for row in rows]

    def get_device(self, device_id: str) -> dict[str, Any]:
        with self._session_factory() as session:
            row = session.get(Device, device_id)
            if row is None:
                raise KeyError(f"unknown device: {device_id}")
            return self._serialize_device(row)

    def credential_ref(self, device_id: str) -> str:
        with self._session_factory() as session:
            row = session.get(Device, device_id)
            if row is None:
                raise KeyError(f"unknown device: {device_id}")
            return row.credential_ref

    def update_device(self, device_id: str, **changes: Any) -> dict[str, Any]:
        allowed = {
            "name",
            "platform",
            "host",
            "port",
            "user",
            "auth_type",
            "credential_ref",
            "host_key_fingerprint",
        }
        if unknown := set(changes) - allowed:
            raise ValueError(f"unsupported device fields: {sorted(unknown)}")
        with self._session_factory.begin() as session:
            row = session.get(Device, device_id)
            if row is None:
                raise KeyError(f"unknown device: {device_id}")
            connection_changed = any(
                key in changes
                for key in {"platform", "host", "port", "user", "auth_type", "credential_ref"}
            )
            if connection_changed or "host_key_fingerprint" in changes:
                active = session.scalar(
                    select(func.count(BoardRun.id)).where(
                        BoardRun.device_id == device_id,
                        BoardRun.status.in_(BOARD_ACTIVE_STATUSES | {"QUEUED"}),
                    )
                )
                if active:
                    raise ValueError("device connection cannot change while board tasks are active")
            for key, value in changes.items():
                setattr(row, key, value)
            if connection_changed or "host_key_fingerprint" in changes:
                row.status = "UNVERIFIED"
                row.detected_platform = None
                row.probe_result = None
                row.last_probe_error = None
                row.last_probed_at = None
            row.updated_at = datetime.now(UTC)
        return self.get_device(device_id)

    def record_probe_success(
        self, device_id: str, *, result: dict[str, Any], detected_platform: str
    ) -> dict[str, Any]:
        with self._session_factory.begin() as session:
            row = session.get(Device, device_id)
            if row is None:
                raise KeyError(f"unknown device: {device_id}")
            row.status = "READY"
            row.detected_platform = detected_platform
            row.probe_result = result
            row.last_probe_error = None
            row.last_probed_at = datetime.now(UTC)
            row.updated_at = datetime.now(UTC)
        return self.get_device(device_id)

    def record_probe_failure(self, device_id: str, *, error: str) -> dict[str, Any]:
        with self._session_factory.begin() as session:
            row = session.get(Device, device_id)
            if row is None:
                raise KeyError(f"unknown device: {device_id}")
            row.status = "ERROR"
            row.last_probe_error = error
            row.last_probed_at = datetime.now(UTC)
            row.updated_at = datetime.now(UTC)
        return self.get_device(device_id)

    def delete_device(self, device_id: str) -> str:
        with self._session_factory.begin() as session:
            row = session.get(Device, device_id)
            if row is None:
                raise KeyError(f"unknown device: {device_id}")
            active = session.scalar(
                select(func.count(BoardRun.id)).where(
                    BoardRun.device_id == device_id,
                    BoardRun.status.in_(BOARD_ACTIVE_STATUSES | {"QUEUED"}),
                )
            )
            if active:
                raise ValueError("device has queued or running board tasks")
            credential_ref = row.credential_ref
            session.delete(row)
            return credential_ref

    def create_board_run(
        self,
        *,
        device_id: str,
        conversion_run_id: str,
        mode: str,
        device_snapshot: dict[str, Any],
        options: dict[str, Any],
        local_dir: str,
        remote_dir: str,
        hbm_sha256: str,
        hbm_size_bytes: int,
    ) -> dict[str, Any]:
        row = BoardRun(
            id=remote_dir.rsplit("/", 1)[-1],
            device_id=device_id,
            conversion_run_id=conversion_run_id,
            mode=mode,
            status="QUEUED",
            phase="QUEUED",
            device_snapshot=device_snapshot,
            options=options,
            local_dir=local_dir,
            remote_dir=remote_dir,
            hbm_sha256=hbm_sha256,
            hbm_size_bytes=hbm_size_bytes,
        )
        with self._session_factory.begin() as session:
            session.add(row)
        return self.get_board_run(row.id)

    def claim_board_run(self, run_id: str) -> bool:
        with self._session_factory.begin() as session:
            row = session.get(BoardRun, run_id)
            if row is None:
                raise KeyError(f"unknown board run: {run_id}")
            if row.status != "QUEUED":
                return False
            row.status = "CONNECTING"
            row.phase = "CONNECTING"
            row.started_at = datetime.now(UTC)
            return True

    def set_board_phase(self, run_id: str, phase: str) -> None:
        if phase not in BOARD_ACTIVE_STATUSES:
            raise ValueError(f"invalid board run phase: {phase}")
        with self._session_factory.begin() as session:
            row = session.get(BoardRun, run_id)
            if row is None:
                raise KeyError(f"unknown board run: {run_id}")
            if row.status in BOARD_TERMINAL_STATUSES or row.status == "CANCELLING":
                return
            row.status = phase
            row.phase = phase

    def finish_board_run(
        self,
        run_id: str,
        *,
        status: str,
        result_payload: dict[str, Any] | None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        if status not in BOARD_TERMINAL_STATUSES:
            raise ValueError(f"invalid board terminal status: {status}")
        with self._session_factory.begin() as session:
            row = session.get(BoardRun, run_id)
            if row is None:
                raise KeyError(f"unknown board run: {run_id}")
            row.status = status
            row.phase = status
            row.result_payload = result_payload
            row.error_code = error_code
            row.error_message = error_message
            row.finished_at = datetime.now(UTC)

    def request_cancel(self, run_id: str) -> dict[str, Any]:
        with self._session_factory.begin() as session:
            row = session.get(BoardRun, run_id)
            if row is None:
                raise KeyError(f"unknown board run: {run_id}")
            if row.status in BOARD_TERMINAL_STATUSES:
                raise ValueError(f"board run is already terminal: {row.status}")
            previous = row.status
            now = datetime.now(UTC)
            row.cancel_requested_at = now
            if row.status == "QUEUED":
                row.status = "CANCELLED"
                row.phase = "CANCELLED"
                row.finished_at = now
            else:
                row.status = "CANCELLING"
                row.phase = "CANCELLING"
            return {"id": row.id, "previous_status": previous, "status": row.status}

    def cancel_requested(self, run_id: str) -> bool:
        with self._session_factory() as session:
            row = session.get(BoardRun, run_id)
            if row is None:
                raise KeyError(f"unknown board run: {run_id}")
            return row.cancel_requested_at is not None

    def queued_board_runs(self) -> list[BoardExecution]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(BoardRun).where(BoardRun.status == "QUEUED").order_by(BoardRun.created_at)
            ).all()
            return [self._execution(row) for row in rows]

    def active_board_runs(self) -> list[BoardExecution]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(BoardRun)
                .where(BoardRun.status.in_(BOARD_ACTIVE_STATUSES))
                .order_by(BoardRun.created_at)
            ).all()
            return [self._execution(row) for row in rows]

    def board_execution(self, run_id: str) -> BoardExecution:
        with self._session_factory() as session:
            row = session.get(BoardRun, run_id)
            if row is None:
                raise KeyError(f"unknown board run: {run_id}")
            return self._execution(row)

    def list_board_runs(self, *, device_id: str | None = None) -> list[dict[str, Any]]:
        with self._session_factory() as session:
            query = select(BoardRun)
            if device_id is not None:
                query = query.where(BoardRun.device_id == device_id)
            rows = session.scalars(query.order_by(BoardRun.created_at.desc())).all()
            return [self._serialize_board_run(row, detail=False) for row in rows]

    def get_board_run(self, run_id: str) -> dict[str, Any]:
        with self._session_factory() as session:
            row = session.get(BoardRun, run_id)
            if row is None:
                raise KeyError(f"unknown board run: {run_id}")
            return self._serialize_board_run(row, detail=True)

    def interrupt_active(self) -> None:
        with self._session_factory.begin() as session:
            now = datetime.now(UTC)
            rows = session.scalars(
                select(BoardRun).where(BoardRun.status.in_(BOARD_ACTIVE_STATUSES))
            ).all()
            for row in rows:
                row.status = "INTERRUPTED"
                row.phase = "INTERRUPTED"
                row.error_code = "BOARD_RUN_INTERRUPTED"
                row.error_message = "Controller restarted during a board task"
                row.finished_at = now

    @staticmethod
    def _serialize_device(row: Device) -> dict[str, Any]:
        return {
            "id": row.id,
            "name": row.name,
            "platform": row.platform,
            "host": row.host,
            "port": row.port,
            "user": row.user,
            "auth_type": row.auth_type,
            "credential_configured": bool(row.credential_ref),
            "host_key_fingerprint": row.host_key_fingerprint,
            "status": row.status,
            "detected_platform": row.detected_platform,
            "probe": row.probe_result,
            "last_probe_error": row.last_probe_error,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
            "last_probed_at": None
            if row.last_probed_at is None
            else row.last_probed_at.isoformat(),
        }

    @staticmethod
    def _execution(row: BoardRun) -> BoardExecution:
        return BoardExecution(
            run_id=row.id,
            device_id=row.device_id,
            conversion_run_id=row.conversion_run_id,
            mode=row.mode,
            status=row.status,
            options=row.options,
            device_snapshot=row.device_snapshot,
            local_dir=row.local_dir,
            remote_dir=row.remote_dir,
        )

    @staticmethod
    def _serialize_board_run(row: BoardRun, *, detail: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": row.id,
            "device_id": row.device_id,
            "conversion_run_id": row.conversion_run_id,
            "mode": row.mode,
            "status": row.status,
            "phase": row.phase,
            "hbm_sha256": row.hbm_sha256,
            "hbm_size_bytes": row.hbm_size_bytes,
            "error": None
            if row.error_code is None
            else {"code": row.error_code, "message": row.error_message},
            "created_at": row.created_at.isoformat(),
            "started_at": None if row.started_at is None else row.started_at.isoformat(),
            "finished_at": None if row.finished_at is None else row.finished_at.isoformat(),
            "cancel_requested_at": None
            if row.cancel_requested_at is None
            else row.cancel_requested_at.isoformat(),
        }
        if detail:
            payload.update(
                {
                    "device": row.device_snapshot,
                    "options": row.options,
                    "remote_dir": row.remote_dir,
                    "result": row.result_payload,
                }
            )
        return payload
