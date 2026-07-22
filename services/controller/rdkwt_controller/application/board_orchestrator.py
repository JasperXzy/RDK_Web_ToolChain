from __future__ import annotations

import logging
import queue
import threading

from rdkwt_controller.application.board_service import BoardService
from rdkwt_controller.infrastructure.board import BoardGateway
from rdkwt_controller.infrastructure.db import BoardRepository

logger = logging.getLogger(__name__)


class BoardOrchestrator:
    """Persistent single-concurrency queue for direct board access."""

    def __init__(
        self,
        *,
        service: BoardService,
        repository: BoardRepository,
        gateway: BoardGateway,
    ) -> None:
        self._service = service
        self._repository = repository
        self._gateway = gateway
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._current: str | None = None

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._repository.interrupt_active()
            self._thread = threading.Thread(
                target=self._worker, name="rdkwt-board-runner", daemon=True
            )
            self._thread.start()
        for item in self._repository.queued_board_runs():
            self.enqueue(item.run_id)

    def stop(self) -> None:
        self._stop.set()
        current = self._current
        if current is not None:
            self._gateway.cancel(current)
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=2)

    def enqueue(self, run_id: str) -> None:
        with self._lock:
            if run_id in self._pending:
                return
            self._pending.add(run_id)
        self._queue.put(run_id)

    def _worker(self) -> None:
        while not self._stop.is_set():
            run_id = self._queue.get()
            if run_id is None:
                return
            self._current = run_id
            try:
                self._service.execute(run_id)
            except BaseException:
                logger.exception("board task execution failed for %s", run_id)
            finally:
                self._current = None
                with self._lock:
                    self._pending.discard(run_id)
                self._queue.task_done()
