from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass

from rdkwt_controller.application.run_service import RunService
from rdkwt_controller.infrastructure.db import RunRepository
from rdkwt_controller.infrastructure.docker import DockerGateway

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorkItem:
    run_id: str
    attempt: int
    recover: bool = False


class RunOrchestrator:
    """Persistent SQLite-backed, single-concurrency local task orchestrator."""

    def __init__(
        self,
        *,
        run_service: RunService,
        repository: RunRepository,
        docker_gateway: DockerGateway,
    ) -> None:
        self._run_service = run_service
        self._repository = repository
        self._docker = docker_gateway
        self._queue: queue.Queue[WorkItem | None] = queue.Queue()
        self._pending: set[tuple[str, int]] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._worker,
                name="rdkwt-single-runner",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._queue.put(None)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=1)

    def enqueue(self, run_id: str, attempt: int, *, recover: bool = False) -> None:
        key = (run_id, attempt)
        with self._lock:
            if key in self._pending:
                return
            self._pending.add(key)
        self._queue.put(WorkItem(run_id=run_id, attempt=attempt, recover=recover))

    def _worker(self) -> None:
        self._reconcile()
        while not self._stop.is_set():
            item = self._queue.get()
            if item is None:
                return
            try:
                self._run_service.execute(
                    item.run_id, item.attempt, recover=item.recover
                )
            except BaseException:
                logger.exception(
                    "task execution failed for %s/%s", item.run_id, item.attempt
                )
            finally:
                with self._lock:
                    self._pending.discard((item.run_id, item.attempt))
                self._queue.task_done()

    def _reconcile(self) -> None:
        active = self._repository.active()
        active_keys = {(item.run_id, item.attempt) for item in active}
        try:
            containers = self._docker.managed_containers()
        except Exception as exc:
            logger.warning("Docker reconciliation unavailable: %s", exc)
            for item in active:
                self._run_service.mark_recovery_interrupted(
                    item,
                    f"Controller restarted but Docker reconciliation failed: {exc}",
                )
            active = []
            active_keys = set()
            containers = []

        containers_by_key = {}
        for container in containers:
            identity = self._docker.managed_identity(container)
            if identity is not None:
                containers_by_key[identity] = container

        recovered_keys: set[tuple[str, int]] = set()
        for item in active:
            key = (item.run_id, item.attempt)
            container = containers_by_key.get(key)
            if container is None or item.container_id != container.id:
                self._run_service.mark_recovery_interrupted(
                    item,
                    "Controller restarted and the recorded managed container was not found",
                )
                continue
            recovered_keys.add(key)
            self.enqueue(item.run_id, item.attempt, recover=True)

        for key, container in containers_by_key.items():
            if key in recovered_keys or key in active_keys:
                continue
            run_id, attempt = key
            logger.warning("cleaning orphan managed container %s", container.id)
            try:
                container.reload()
                if container.status == "running":
                    self._docker.stop_managed(
                        container.id, run_id=run_id, attempt=attempt
                    )
                self._docker.remove_managed(
                    container.id, run_id=run_id, attempt=attempt
                )
            except Exception:
                logger.exception("failed to clean orphan managed container %s", container.id)

        for item in self._repository.queued():
            self.enqueue(item.run_id, item.attempt)
