import asyncio
import uuid
from urllib.parse import urlparse

import aiofiles
import httpx
import json
import os
from pathlib import Path
from typing import Optional, Dict, List
from datetime import datetime

from .async_logger import AsyncLogger
from .models import DownloadTask, DownloadStatus
from .hash_utils import HashVerifier
from .queue_manager import QueueManager


# ---------------------------------------------------------------------------
# DownloadManager
# ---------------------------------------------------------------------------

class DownloadManager:
    """Asynchronous download manager."""

    def __init__(
            self,
            max_concurrent: int = 3,
            state_file_path: Optional[str] = None,
            default_download_path: str = "./downloads",
            log_file: Optional[str] = None,
    ) -> None:
        self._default_download_path = default_download_path
        self._state_file_path = state_file_path
        self.tasks: Dict[str, DownloadTask] = {}

        self._log = AsyncLogger(
            name=__name__,
            log_file=log_file,
        )

        self._queue_manager = QueueManager(
            max_concurrent=max_concurrent,
            handler=self._handle_task,
            logger=self._log,
        )

        if self._state_file_path:
            self.load_state()

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    @property
    def state_file(self) -> Optional[str]:
        return self._state_file_path

    @state_file.setter
    def state_file(self, value: Optional[str]) -> None:
        self._state_file_path = value

    def load_state(self, state_file_path: Optional[str] = None) -> None:
        path = state_file_path or self._state_file_path
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "r") as f:
                data = json.load(f)
            for task_id, task_data in data.items():
                self.tasks[task_id] = DownloadTask.from_dict(task_data)
            self._log.info("Loaded %d tasks from state file", len(self.tasks))
        except Exception as e:
            self._log.error("Failed to load state: %s", e)

    def save_state(self, state_file_path: Optional[str] = None) -> None:
        path = state_file_path or self._state_file_path
        if not path:
            return
        try:
            state_data = {tid: t.to_dict() for tid, t in self.tasks.items()}
            with open(path, "w") as f:
                json.dump(state_data, f, indent=2)
        except Exception as e:
            self._log.error("Failed to save state: %s", e)

    # ------------------------------------------------------------------
    # Task registration
    # ------------------------------------------------------------------

    def add_tasks(self, tasks: "DownloadTask | list[DownloadTask]") -> None:
        if not isinstance(tasks, list):
            tasks = [tasks]
        for task in tasks:
            self._register_task(task)

    def add_download(
            self,
            url: str,
            filepath: str,
            expected_hash: Optional[str] = None,
            hash_algorithm: str = "md5",
            headers: Optional[Dict[str, str]] = None,
            cookies: Optional[Dict[str, str]] = None,
    ) -> str:
        """Register a new download task.

        Args:
            url:           Remote URL to fetch.
            filepath:      Local destination path.
            expected_hash: Optional integrity hash for the finished file.
            hash_algorithm: Algorithm for the hash check (default: ``"md5"``).
            headers:       Extra HTTP headers sent with every request for this
                           task, e.g. ``{"Authorization": "Bearer <token>"}``.
            cookies:       Cookies sent with every request for this task,
                           e.g. ``{"session": "abc123"}``.

        Returns:
            The ``task_id`` of the newly registered task.
        """
        task = DownloadTask(
            url=url,
            filepath=filepath,
            status=DownloadStatus.PENDING,
            created_at=datetime.now().isoformat(),
            expected_hash=expected_hash,
            hash_algorithm=hash_algorithm,
            headers=headers or {},
            cookies=cookies or {},
        )
        self._register_task(task)

        # задача могла быть отклонена в _register_task — проверяем по наличию в self.tasks
        if task.task_id in self.tasks and self._queue_manager.is_running:
            self._queue_manager.enqueue_nowait(task.task_id)

        return task.task_id

    def _register_task(self, task: DownloadTask) -> None:
        task.task_id = self._assign_task_id(task)
        self._resolve_filepath(task)

        # _resolve_filepath может выставить FAILED (дубликат URL) — не перезаписываем
        if task.status == DownloadStatus.FAILED:
            self._log.warning("Task %s rejected: %s", task.task_id, task.error_message)
            return

        if not self._verify_task(task):
            self._log.warning("Task %s rejected: %s", task.task_id, task.error_message)
            return

        # Ensure fields always exist as dicts — guards against tasks loaded from
        # state files that predate the introduction of headers/cookies.
        if not task.headers:
            task.headers = {}
        if not task.cookies:
            task.cookies = {}

        self.tasks[task.task_id] = task
        self._log.info("Registered task %s → %s", task.task_id, task.filepath)

    def _resolve_filepath(self, task: DownloadTask) -> None:
        """
        Если filepath не задан явно — генерируем из URL.

        При коллизии пути различаем два случая:
        - Та же URL → файл уже скачивается/скачан, ставим FAILED.
        - Другая URL → переименовываем с суффиксом task_id (два разных файла
          случайно имеют одинаковое имя).
        """
        if task.filepath:
            return

        filename = (
                os.path.basename(urlparse(task.url).path)
                or f"file_{task.task_id}"
        )
        candidate = os.path.join(self._default_download_path, filename)

        for existing in self.tasks.values():
            if existing.filepath != candidate:
                continue

            if existing.url == task.url:
                # Тот же файл с того же источника — явный дубликат
                task.status = DownloadStatus.FAILED
                task.error_message = (
                    f"Already downloading this file "
                    f"(duplicate of task {existing.task_id})"
                )
                self._log.warning(
                    "Task %s rejected: duplicate URL+filepath of task %s — '%s'",
                    task.task_id, existing.task_id, candidate,
                )
                task.filepath = candidate  # всё равно проставляем для читаемости
                return

            # Разные URL, одинаковое имя файла — просто переименовываем
            stem = Path(candidate).stem
            suffix = Path(candidate).suffix
            candidate = str(
                Path(candidate).with_name(f"{stem}_{task.task_id[:8]}{suffix}")
            )
            self._log.warning(
                "Filepath collision for task %s (different URL) — resolved to: %s",
                task.task_id, candidate,
            )
            break

        task.filepath = candidate

    def _assign_task_id(self, task: DownloadTask) -> str:
        if not task.task_id:
            task.task_id = str(uuid.uuid4())
            return task.task_id
        if task.task_id in self.tasks:
            self._log.warning("Task %s already exists — generating new ID", task.task_id)
            task.task_id = str(uuid.uuid4())
        return task.task_id

    def _verify_task(self, task: DownloadTask) -> bool:
        url_ok = self._verify_url(task)
        path_ok = self._verify_filepath(task)
        return url_ok and path_ok

    def _verify_url(self, task: DownloadTask) -> bool:
        try:
            result = urlparse(task.url)
            if result.scheme in ("http", "https") and bool(result.netloc):
                return True
        except Exception:
            pass
        task.status = DownloadStatus.FAILED
        task.error_message = "Invalid URL"
        self._log.warning("Task %s has invalid URL: '%s'", task.task_id, task.url)
        return False

    def _verify_filepath(self, task: DownloadTask) -> bool:
        try:
            Path(task.filepath)
        except TypeError:
            task.status = DownloadStatus.FAILED
            task.error_message = "Invalid filepath"
            self._log.warning("Task %s has invalid filepath: '%s'", task.task_id, task.filepath)
            return False

        for existing in self.tasks.values():
            if existing.filepath == task.filepath and existing.task_id != task.task_id:
                task.status = DownloadStatus.FAILED
                task.error_message = "Duplicate output file path"
                self._log.warning(
                    "Task %s duplicates filepath of %s: '%s'",
                    task.task_id, existing.task_id, task.filepath,
                )
                return False

        return True

    # ------------------------------------------------------------------
    # Download lifecycle
    # ------------------------------------------------------------------

    async def start_downloads(self) -> None:
        """Start the logger and worker pool, process all pending tasks."""
        self._log.start()

        pending = [
            tid for tid, t in self.tasks.items()
            if t.status in (DownloadStatus.PENDING, DownloadStatus.FAILED)
        ]

        if not pending:
            self._log.info("No pending tasks to download")
            return

        await self._queue_manager.start(pending)
        await self._log.stop()

    def stop_download(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if not task or task.status != DownloadStatus.DOWNLOADING:
            return False

        cancelled = self._queue_manager.cancel_download(task_id)
        if not cancelled:
            task.status = DownloadStatus.PAUSED
        self._log.info("Stop requested for task %s", task_id)
        return True

    def stop_all_downloads(self) -> None:
        self._queue_manager.cancel_all()
        self._log.info("All downloads stopped")

    def resume_download(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if task and task.status == DownloadStatus.PAUSED:
            task.status = DownloadStatus.PENDING
            self._log.info("Resumed task %s", task_id)
            return True
        return False

    # ------------------------------------------------------------------
    # Core download logic (used as QueueManager handler)
    # ------------------------------------------------------------------

    async def _handle_task(self, task_id: str) -> None:
        task = self.tasks.get(task_id)
        if not task or task.status not in (DownloadStatus.PENDING, DownloadStatus.FAILED):
            return
        await self._download_file(task)

    async def _download_file(self, task: DownloadTask) -> None:
        self._log.debug("Starting download for task %s: %s", task.task_id, task.url)
        try:
            task.status = DownloadStatus.DOWNLOADING

            # Build a client scoped to this task's auth credentials.
            # Headers from the task are passed at the client level so they are
            # automatically included in every request (HEAD, GET, range resume).
            # Cookies are loaded into a Cookies jar for the same reason.
            async with httpx.AsyncClient(
                    headers=task.headers,
                    cookies=task.cookies,
            ) as client:
                remote_size = await self._fetch_content_length(client, task)

                file_exists = os.path.exists(task.filepath)
                local_size = os.path.getsize(task.filepath) if file_exists else 0

                self._log.debug(
                    "Task %s: file_exists=%s, local=%s, remote=%s",
                    task.task_id, file_exists, local_size, remote_size,
                )

                if not file_exists:
                    await self._stream_download(client, task, 0, remote_size)

                elif task.expected_hash:
                    hash_valid = await self._verify_hash(task)
                    if hash_valid:
                        self._mark_completed(task, local_size, "hash verified")
                        return
                    if remote_size is not None and local_size < remote_size:
                        await self._stream_download(client, task, local_size, remote_size)
                    else:
                        raise RuntimeError(
                            f"Hash mismatch and local size ({local_size}) >= remote ({remote_size}); cannot recover"
                        )

                else:
                    if remote_size is None or local_size == remote_size:
                        self._mark_completed(task, local_size, "size match or no remote size")
                        return
                    if local_size < remote_size:
                        await self._stream_download(client, task, local_size, remote_size)
                    else:
                        raise RuntimeError(
                            f"Local file ({local_size}) is larger than remote ({remote_size})"
                        )

            if task.expected_hash:
                if not await self._verify_hash(task):
                    raise RuntimeError(
                        f"Hash mismatch after download: expected {task.expected_hash}, got {task.actual_hash}"
                    )

            self._mark_completed(task, task.downloaded_bytes, "download completed")

        except asyncio.CancelledError:
            if task.status == DownloadStatus.DOWNLOADING:
                task.status = DownloadStatus.PAUSED
                self._log.info("Download paused (cancelled): %s", task.task_id)
            raise

        except Exception as e:
            task.status = DownloadStatus.FAILED
            task.error_message = str(e)
            self._log.error("Download failed for %s: %s", task.task_id, e)

    async def _fetch_content_length(
            self, client: httpx.AsyncClient, task: DownloadTask
    ) -> Optional[int]:
        try:
            head = await client.head(task.url)
            if "content-length" in head.headers:
                return int(head.headers["content-length"])
        except Exception as e:
            self._log.warning("HEAD request failed for %s: %s", task.task_id, e)
        return None

    async def _verify_hash(self, task: DownloadTask) -> bool:
        task.actual_hash = await HashVerifier.calculate_hash(task.filepath, task.hash_algorithm)
        return task.actual_hash == task.expected_hash

    async def _stream_download(
            self,
            client: httpx.AsyncClient,
            task: DownloadTask,
            local_size: int,
            remote_size: Optional[int],
    ) -> None:
        headers = {}
        if remote_size is not None and 0 < local_size < remote_size:
            headers["Range"] = f"bytes={local_size}-"

        async with client.stream("GET", task.url, headers=headers) as response:
            if response.status_code not in (200, 206):
                raise RuntimeError(f"HTTP {response.status_code}")

            if "content-length" in response.headers:
                content_length = int(response.headers["content-length"])
                task.total_bytes = (
                    remote_size if response.status_code == 206 else content_length
                )
            else:
                task.total_bytes = local_size

            task.downloaded_bytes = local_size
            mode = "ab" if local_size > 0 else "wb"

            Path(task.filepath).parent.mkdir(parents=True, exist_ok=True)

            async with aiofiles.open(task.filepath, mode) as f:
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    if task.status == DownloadStatus.PAUSED:
                        self._log.info("Stream interrupted mid-download: %s", task.task_id)
                        return
                    await f.write(chunk)
                    task.downloaded_bytes += len(chunk)

    def _mark_completed(self, task: DownloadTask, total_bytes: int, reason: str) -> None:
        task.total_bytes = total_bytes
        task.status = DownloadStatus.COMPLETED
        task.completed_at = datetime.now().isoformat()
        self._log.info("Completed (%s): %s", reason, task.task_id)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_progress(self, task_id: str) -> Optional[Dict]:
        task = self.tasks.get(task_id)
        if not task:
            return None
        percentage = (
            (task.downloaded_bytes / task.total_bytes) * 100
            if task.total_bytes > 0
            else 0.0
        )
        return {
            "task_id": task.task_id,
            "url": task.url,
            "status": task.status,
            "downloaded_bytes": task.downloaded_bytes,
            "total_bytes": task.total_bytes,
            "percentage": round(percentage, 2),
        }

    def get_all_progress(self) -> List[Dict]:
        return [self.get_progress(tid) for tid in self.tasks]

    def list_all_tasks(self) -> List[Dict]:
        return [t.to_dict() for t in self.tasks.values()]

    def verify_file(self, task_id: str) -> Optional[Dict]:
        task = self.tasks.get(task_id)
        if not task:
            return None
        if not os.path.exists(task.filepath):
            return {"task_id": task_id, "verified": False, "reason": "File not found"}
        if not task.expected_hash:
            return {"task_id": task_id, "verified": False, "reason": "Hash not provided"}
        return {
            "task_id": task_id,
            "verified": task.actual_hash == task.expected_hash if task.actual_hash else False,
            "expected_hash": task.expected_hash,
            "actual_hash": task.actual_hash,
            "algorithm": task.hash_algorithm,
        }

    @classmethod
    def get_file_info(cls, filepath: str) -> Optional[Dict]:
        if not os.path.exists(filepath):
            return None
        try:
            stat = os.stat(filepath)
            return {
                "filepath": filepath,
                "size_bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "exists": True,
            }
        except Exception as e:
            return None

    @classmethod
    async def get_url_info(cls, url: str) -> Optional[Dict]:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.head(url, follow_redirects=True)
                if response.status_code not in (200, 206):
                    return {"url": url, "status_code": response.status_code,
                            "error": f"HTTP {response.status_code}"}
                return {
                    "url": url,
                    "status_code": response.status_code,
                    "size_bytes": int(response.headers["content-length"])
                    if "content-length" in response.headers
                    else None,
                    "content_type": response.headers.get("content-type"),
                    "supports_range": response.headers.get("accept-ranges") == "bytes",
                }
        except Exception as e:
            return None

    @classmethod
    async def verify_download(
            cls, filepath: str, expected_hash: str, algorithm: str = "md5"
    ) -> Dict:
        if not os.path.exists(filepath):
            return {"filepath": filepath, "verified": False, "reason": "File not found"}
        try:
            actual_hash = await HashVerifier.calculate_hash(filepath, algorithm)
            return {
                "filepath": filepath,
                "verified": actual_hash.lower() == expected_hash.lower(),
                "algorithm": algorithm,
                "expected_hash": expected_hash,
                "actual_hash": actual_hash,
            }
        except Exception as e:
            return {"filepath": filepath, "verified": False, "reason": str(e)}
