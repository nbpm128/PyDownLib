import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional


class AsyncLogger:
    """
    Non-blocking async logger backed by an asyncio.Queue.
    Writes are enqueued instantly; a background task flushes them to handlers.
    """

    def __init__(
        self,
        name: str,
        level: int = logging.DEBUG,
        log_file: Optional[str] = None,
        fmt: str = "%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
    ) -> None:
        self._name = name
        self._level = level
        self._fmt = fmt
        self._log_file = log_file
        self._queue: asyncio.Queue[Optional[logging.LogRecord]] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._logger = self._build_sync_logger()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_sync_logger(self) -> logging.Logger:
        logger = logging.getLogger(self._name)
        logger.setLevel(self._level)
        logger.propagate = False

        formatter = logging.Formatter(self._fmt, datefmt="%Y-%m-%d %H:%M:%S")

        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

        if self._log_file:
            Path(self._log_file).parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(self._log_file, encoding="utf-8")
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)

        return logger

    async def _consumer(self) -> None:
        """Background task: drain the queue and emit records."""
        while True:
            record = await self._queue.get()
            if record is None:          # sentinel — stop
                self._queue.task_done()
                break
            try:
                self._logger.handle(record)
            except Exception:
                pass                    # never let the logger crash the app
            finally:
                self._queue.task_done()

    def _make_record(self, level: int, msg: str, *args, **kwargs) -> logging.LogRecord:
        return self._logger.makeRecord(
            self._name, level, "(async)", 0, msg, args, None
        )

    def _enqueue(self, level: int, msg: str, *args) -> None:
        if not self._logger.isEnabledFor(level):
            return
        record = self._make_record(level, msg, *args)
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            pass  # drop if queue is full (shouldn't happen with unbounded queue)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Schedule the consumer coroutine. Call once inside a running event loop."""
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._consumer())

    async def stop(self) -> None:
        """Flush remaining records and stop the consumer gracefully."""
        await self._queue.put(None)          # sentinel
        await self._queue.join()
        if self._task:
            await self._task

    # ------------------------------------------------------------------
    # Public logging API
    # ------------------------------------------------------------------

    def debug(self, msg: str, *args) -> None:
        self._enqueue(logging.DEBUG, msg, *args)

    def info(self, msg: str, *args) -> None:
        self._enqueue(logging.INFO, msg, *args)

    def warning(self, msg: str, *args) -> None:
        self._enqueue(logging.WARNING, msg, *args)

    def error(self, msg: str, *args) -> None:
        self._enqueue(logging.ERROR, msg, *args)

    def critical(self, msg: str, *args) -> None:
        self._enqueue(logging.CRITICAL, msg, *args)
