from __future__ import annotations

import uuid

from rdkwt_controller.application.orchestrator import RunOrchestrator
from rdkwt_controller.infrastructure.db import ExecutionRecord


def _record(*, status: str, container_id: str | None = None) -> ExecutionRecord:
    return ExecutionRecord(
        run_id=str(uuid.uuid4()),
        attempt=1,
        kind="CONVERSION",
        status=status,
        model_version_id=None,
        request_snapshot={},
        runner_image_reference="runner:fixed",
        runner_image_id="sha256:" + "a" * 64,
        container_id=container_id,
    )


class FakeRunService:
    def __init__(self) -> None:
        self.interrupted: list[tuple[ExecutionRecord, str]] = []

    def mark_recovery_interrupted(
        self, record: ExecutionRecord, message: str
    ) -> None:
        self.interrupted.append((record, message))


class FakeRepository:
    def __init__(
        self, *, active: list[ExecutionRecord], queued: list[ExecutionRecord]
    ) -> None:
        self._active = active
        self._queued = queued

    def active(self) -> list[ExecutionRecord]:
        return self._active

    def queued(self) -> list[ExecutionRecord]:
        return self._queued


class FakeContainer:
    def __init__(self, container_id: str) -> None:
        self.id = container_id


class FakeDocker:
    def __init__(self, containers=None, *, error: Exception | None = None) -> None:
        self.containers = containers or []
        self.error = error

    def managed_containers(self):
        if self.error is not None:
            raise self.error
        return self.containers

    @staticmethod
    def managed_identity(container):
        return container.identity


def test_reconcile_recovers_matching_container_and_restores_queue() -> None:
    active = _record(status="COMPILING", container_id="container-one")
    queued = _record(status="QUEUED")
    container = FakeContainer("container-one")
    container.identity = (active.run_id, active.attempt)
    service = FakeRunService()
    repository = FakeRepository(active=[active], queued=[queued])
    orchestrator = RunOrchestrator(
        run_service=service,
        repository=repository,
        docker_gateway=FakeDocker([container]),
    )

    orchestrator._reconcile()

    items = [orchestrator._queue.get_nowait(), orchestrator._queue.get_nowait()]
    assert [(item.run_id, item.recover) for item in items] == [
        (active.run_id, True),
        (queued.run_id, False),
    ]
    assert service.interrupted == []


def test_reconcile_marks_active_interrupted_when_docker_is_unavailable() -> None:
    active = _record(status="CHECKING", container_id="missing")
    queued = _record(status="QUEUED")
    service = FakeRunService()
    repository = FakeRepository(active=[active], queued=[queued])
    orchestrator = RunOrchestrator(
        run_service=service,
        repository=repository,
        docker_gateway=FakeDocker(error=RuntimeError("socket unavailable")),
    )

    orchestrator._reconcile()

    assert service.interrupted[0][0] == active
    assert "Docker reconciliation failed" in service.interrupted[0][1]
    restored = orchestrator._queue.get_nowait()
    assert restored.run_id == queued.run_id
    assert restored.recover is False
