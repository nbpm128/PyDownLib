# ---------------------------------------------------------------------------
# QueueManager
# ---------------------------------------------------------------------------
import asyncio
from typing import Optional, Dict, List

from pydownlib.async_logger import AsyncLogger


class QueueManager:
    """
    Owns the asyncio.Queue and the pool of worker coroutines.

    Responsibilities:
    - creating / destroying the queue
    - starting / stopping workers
    - enqueuing task IDs
    - routing each task ID to a handler callback
    """

    def __init__(
        self,
        max_concurrent: int,
        handler,            # async callable(task_id: str) -> None
        logger: AsyncLogger,
    ) -> None:
        self._max_concurrent = max_concurrent
        self._handler = handler
        self._log = logger

        self._queue: Optional[asyncio.Queue] = None
        self._worker_tasks: List[asyncio.Task] = []
        self.active_downloads: Dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._queue is not None and bool(self._worker_tasks)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self, initial_task_ids: List[str]) -> None:
        """Create queue, enqueue initial tasks, start workers, wait for completion."""
        if self.is_running:
            self._log.warning("QueueManager is already running")
            return

        self._queue = asyncio.Queue()

        for task_id in initial_task_ids:
            await self._queue.put(task_id)

        self._log.debug(
            "QueueManager started: %d tasks, %d workers",
            len(initial_task_ids), self._max_concurrent
        )

        self._worker_tasks = [
            asyncio.create_task(self._worker())
            for _ in range(self._max_concurrent)
        ]

        await self._queue.join()
        self._log.debug("All queued tasks processed")
        await self._shutdown_workers()

    async def enqueue(self, task_id: str) -> None:
        """Add a task ID to the running queue (no-op if queue not started)."""
        if self._queue is None:
            self._log.warning("Queue is not started; cannot enqueue task %s", task_id)
            return
        await self._queue.put(task_id)

    def enqueue_nowait(self, task_id: str) -> None:
        """Thread-safe enqueue via call_soon_threadsafe."""
        if self._queue is None:
            return
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(
            lambda tid=task_id: asyncio.ensure_future(self._queue.put(tid))
        )

    def cancel_download(self, task_id: str) -> bool:
        """Cancel an active download task. Returns True if cancelled."""
        active = self.active_downloads.get(task_id)
        if active and not active.done():
            active.cancel()
            return True
        return False

    def cancel_all(self) -> None:
        """Cancel all active downloads and workers."""
        for task_id in list(self.active_downloads.keys()):
            self.cancel_download(task_id)
        for wt in self._worker_tasks:
            if not wt.done():
                wt.cancel()
        self._log.info("All downloads and workers cancelled")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _worker(self) -> None:
        worker_id = id(asyncio.current_task())
        self._log.debug("Worker started: id=%d", worker_id)

        while True:
            try:
                task_id = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                self._log.debug("Worker %d cancelled", worker_id)
                break

            if task_id is None:             # sentinel
                self._queue.task_done()
                self._log.debug("Worker %d received sentinel, stopping", worker_id)
                break

            try:
                download_task = asyncio.create_task(self._handler(task_id))
                self.active_downloads[task_id] = download_task
                try:
                    await download_task
                except asyncio.CancelledError:
                    pass
                finally:
                    self.active_downloads.pop(task_id, None)
            except Exception as e:
                self._log.error("Worker %d error on task %s: %s", worker_id, task_id, e)
            finally:
                self._queue.task_done()

    async def _shutdown_workers(self) -> None:
        for _ in range(self._max_concurrent):
            await self._queue.put(None)     # one sentinel per worker
        await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks.clear()
        self._queue = None

