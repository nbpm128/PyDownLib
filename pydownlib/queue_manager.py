import asyncio
from typing import Optional, Dict, List

from pydownlib import AsyncLogger


class QueueManager:

    def __init__(
            self,
            max_concurrent: int,
            handler,  # async callable(task_id: str) -> None
            logger: AsyncLogger,
    ) -> None:
        self._max_concurrent = max_concurrent
        self._handler = handler
        self._log = logger

        self._queue: Optional[asyncio.Queue] = None
        self._paused: bool = False
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

    def get_max_concurrent(self) -> int:
        return self._max_concurrent

    async def set_concurrency(self, new_max: int) -> None:
        """ Change the number of worker coroutines at runtime. Can increase or decrease. """
        if new_max < 1:
            raise ValueError("new_max must be >= 1")
        if self._queue is None:
            self._max_concurrent = new_max
            return

        diff = new_max - self._max_concurrent
        self._max_concurrent = new_max

        if diff > 0:
            new_tasks = [asyncio.create_task(self._worker()) for _ in range(diff)]
            self._worker_tasks.extend(new_tasks)
            self._log.info("Added %d workers, total: %d", diff, new_max)
        elif diff < 0:
            for _ in range(-diff):
                await self._queue.put(None)
            self._log.info("Removing %d workers, target: %d", -diff, new_max)

    async def start(self) -> None:
        """Launch workers if not already running."""
        await self._launch_workers()

    async def pause(self) -> None:
        """Interrupt active downloads, keep workers alive."""
        for task_id in list(self.active_downloads.keys()):
            self.cancel(task_id)
        self._log.info("Queue paused: active downloads interrupted, workers alive")

    async def stop(self) -> None:
        """Drain active downloads, stop workers, destroy queue."""
        await self.pause()
        await self._shutdown_workers()
        self._log.info("Queue stopped")

    def cancel(self, task_id: str) -> bool:
        """Cancel an active download task. Returns True if cancelled."""
        active = self.active_downloads.get(task_id)
        if active and not active.done():
            active.cancel()
            return True
        return False

    async def enqueue(self, task_id: str) -> None:
        """Add a task ID to the running queue (no-op if queue not started)."""
        if self._queue is None:
            self._log.warning("Queue is not started; cannot enqueue task %s", task_id)
            return
        await self._queue.put(task_id)

    def enqueue_threadsafe(self, task_id: str) -> None:
        """Thread-safe enqueue for calls from sync context or other threads."""
        if self._queue is None:
            return
        loop = asyncio.get_event_loop()
        loop.call_soon_threadsafe(
            lambda tid=task_id: asyncio.ensure_future(self._queue.put(tid))
        )


    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _launch_workers(self) -> None:
        """Start workers and return immediately (persistent mode)."""
        if self.is_running:
            return

        self._queue = asyncio.Queue()
        self._worker_tasks = [
            asyncio.create_task(self._worker())
            for _ in range(self._max_concurrent)
        ]
        self._log.debug("QueueManager launched: %d workers", self._max_concurrent)

    async def _shutdown_workers(self) -> None:
        for _ in range(self._max_concurrent):
            await self._queue.put(None)  # one sentinel per worker
        await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks.clear()
        self._queue = None

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

            if task_id is None:  # sentinel
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
