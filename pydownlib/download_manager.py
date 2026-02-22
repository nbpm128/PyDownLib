import asyncio
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List
from urllib.parse import urlparse

import aiofiles
import httpx

from .async_logger import AsyncLogger
from .hash_utils import HashVerifier
from .models import DownloadTask, DownloadStatus, DownloadProgress, VerificationResult, \
    UrlInfo, FileInfo
from .post_processor import PostProcessor
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
        self._post_processor = PostProcessor()
        self.tasks: Dict[str, DownloadTask] = {}

        self._log = AsyncLogger(
            name=__name__,
            log_file=log_file,
        )

        self.queue_manager = QueueManager(
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

    async def add_task(self, *tasks: DownloadTask):
        for task in tasks:
            await self._register_task(task)

    async def delete_task(self, task: DownloadTask, delete_file: bool = False) -> bool:
        return await self._delete_task(task, delete_file)

    async def _delete_task(self, task: DownloadTask, delete_file: bool = False) -> bool:
        if task.task_id not in self.tasks:
            self._log.warning("Attempted to delete non-existent task %s", task.task_id)
            return False

        active = self.queue_manager.active_downloads.get(task.task_id)
        if active and not active.done():
            active.cancel()
            try:
                await active
            except (asyncio.CancelledError, Exception):
                pass

        del self.tasks[task.task_id]

        if delete_file and task.filepath and os.path.exists(task.filepath):
            try:
                os.remove(task.filepath)
                self._log.info("Deleted file for task %s: %s", task.task_id, task.filepath)
            except Exception as e:
                self._log.error("Failed to delete file for task %s: %s", task.task_id, e)
                return False

        self._log.info("Deleted task %s", task.task_id)
        return True

    async def _register_task(self, task: DownloadTask) -> None:
        task.task_id = self._assign_task_id(task)
        self._resolve_filepath(task)

        if task.status == DownloadStatus.FAILED:
            self._log.warning("Task %s rejected: %s", task.task_id, task.error_message)
            return

        if not self._verify_task(task):
            self._log.warning("Task %s rejected: %s", task.task_id, task.error_message)
            return

        self.tasks[task.task_id] = task

        extra_mirrors = len(task.mirrors) - 1
        mirror_info = f" with {extra_mirrors} fallback mirror(s)" if extra_mirrors > 0 else ""
        self._log.info(
            "Registered task %s → %s%s", task.task_id, task.filepath, mirror_info
        )

        if task.task_id in self.tasks and self.queue_manager.is_running:
            await self.queue_manager.enqueue(task.task_id)

    def _resolve_filepath(self, task: DownloadTask) -> None:
        """
        Resolve and set ``folder_path`` and ``file_name`` on *task*.
        """
        # ------------------------------------------------------------------
        # 1. Resolve folder_path
        # ------------------------------------------------------------------
        if not task.folder_path:
            task.folder_path = self._default_download_path
        else:
            folder = Path(task.folder_path)

            if folder.is_absolute():
                task.folder_path = str(folder)
            else:
                task.folder_path = str(Path(self._default_download_path) / folder)

        # ------------------------------------------------------------------
        # 2. Resolve file_name
        # ------------------------------------------------------------------
        if not task.file_name:
            task.file_name = task.task_id

        # ------------------------------------------------------------------
        # 3. Collision detection
        # ------------------------------------------------------------------
        candidate_path = task.filepath  # uses the property: folder_path / file_name

        for existing in self.tasks.values():
            if existing.filepath != candidate_path:
                continue

            if existing.get_current_mirror().url == task.get_current_mirror().url:
                # True duplicate — same source, same destination
                self._mark_failed(
                    task,
                    task_error_message=f"Already downloading this file (duplicate of task {existing.task_id})",
                    log_error_message=f"Task {task.task_id} rejected: "
                                      f"duplicate URL+filepath of task {existing.task_id} — '{candidate_path}'"
                )
                return

            # Different URL, same resolved path → rename
            stem = Path(task.file_name).stem
            suffix = Path(task.file_name).suffix
            task.file_name = f"{stem}_{task.task_id[:8]}{suffix}"
            self._log.warning(
                "Filepath collision for task %s (different URL) — resolved to: %s",
                task.task_id, task.filepath,
            )
            break

    def _assign_task_id(self, task: DownloadTask) -> str:
        if not task.task_id:
            task.task_id = str(uuid.uuid4())
            return task.task_id
        if task.task_id in self.tasks:
            self._log.warning("Task %s already exists — generating new ID", task.task_id)
            task.task_id = str(uuid.uuid4())
        return task.task_id

    def _verify_task(self, task: DownloadTask) -> bool:
        url_ok = self._verify_mirrors(task)
        path_ok = self._verify_filepath(task)
        return url_ok and path_ok

    def _verify_mirrors(self, task: DownloadTask) -> bool:
        """Verify that the task has at least one valid URL in its mirrors list."""
        if not task.mirrors:
            self._mark_failed(
                task,
                task_error_message="No URLs provided (mirrors list is empty)",
                log_error_message=f"Task {task.task_id} has no URLs"
            )
            return False

        valid = []
        for mirror in task.mirrors:
            try:
                result = urlparse(mirror.url)
                if result.scheme in ("http", "https") and bool(result.netloc):
                    valid.append(mirror)
                else:
                    self._log.warning(
                        "Task %s: invalid mirror URL skipped: '%s'",
                        task.task_id, mirror.url,
                    )
            except Exception:
                self._log.warning(
                    "Task %s: could not parse mirror URL: '%s'",
                    task.task_id, mirror.url,
                )

        if not valid:
            self._mark_failed(
                task,
                task_error_message="No valid URLs in mirrors list",
                log_error_message=f"Task {task.task_id} has no valid URLs in mirrors list"
            )
            return False

        task.mirrors = valid
        return True

    def _verify_filepath(self, task: DownloadTask) -> bool:
        filepath = task.filepath
        if filepath is None:
            self._mark_failed(
                task,
                task_error_message="Filepath could not be resolved",
                log_error_message=f"Task {task.task_id} has unresolved filepath (folder_path or file_name missing)"
            )
            return False

        try:
            Path(filepath)
        except TypeError:
            self._mark_failed(
                task,
                task_error_message="Invalid filepath",
                log_error_message=f"Task {task.task_id} has invalid filepath: '{filepath}'"
            )
            return False

        for existing in self.tasks.values():
            if existing.filepath == filepath and existing.task_id != task.task_id:
                self._mark_failed(
                    task,
                    task_error_message="Duplicate output file path",
                    log_error_message=f"Task {task.task_id} duplicates filepath of {existing.task_id}: '{filepath}'"
                )
                return False

        return True

    # ------------------------------------------------------------------
    # Download lifecycle
    # ------------------------------------------------------------------

    async def start_downloads(self) -> None:
        if not self.queue_manager.is_running:
            self._log.start()
            await self.queue_manager.start()

        pending = [
            tid for tid, t in self.tasks.items()
            if t.status in (DownloadStatus.PENDING, DownloadStatus.PAUSED, DownloadStatus.FAILED)
        ]

        for tid in pending:
            self.tasks[tid].status = DownloadStatus.PENDING
            await self.queue_manager.enqueue(tid)

    # async def pause_downloads(self) -> None:
    #     """Interrupt active downloads, keep queue alive for new tasks."""
    #     await self.queue_manager.pause()
    #
    #     paused_count = 0
    #     for task in self.tasks.values():
    #         if task.status == DownloadStatus.PENDING:
    #             task.status = DownloadStatus.PAUSED
    #             paused_count += 1
    #
    #     self._log.info("Downloads paused (%d pending task(s) marked paused)", paused_count)

    async def stop_downloads(self) -> None:
        """Stop all downloads and shut down workers. Requires start_downloads() to resume."""
        for task in self.tasks.values():
            if task.status in (DownloadStatus.DOWNLOADING, DownloadStatus.PENDING):
                task.status = DownloadStatus.PAUSED

        await self.queue_manager.stop()
        await self._log.stop()



    def stop_download(self, task_id: str) -> bool:
        """Pause a single active download."""
        task = self.tasks.get(task_id)
        if not task or task.status != DownloadStatus.DOWNLOADING:
            return False

        cancelled = self.queue_manager.cancel(task_id)
        if not cancelled:
            task.status = DownloadStatus.PAUSED
        self._log.info("Stop requested for task %s", task_id)
        return True

    async def resume_download(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if task and task.status == DownloadStatus.PAUSED:
            task.status = DownloadStatus.PENDING
            if self.queue_manager.is_running:
                await self.queue_manager.enqueue(task_id)
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
        """
        Download a file with automatic mirror fallback.

        On failure, automatically switches to the next available mirror.
        """
        max_retries = len(task.mirrors)

        for attempt in range(max_retries):
            current_url = task.get_current_mirror().url
            mirror_info = task.get_mirror_info()

            if mirror_info.using_fallback_mirror:
                self._log.info(
                    "Task %s: Attempting download from fallback mirror %d/%d: %s",
                    task.task_id,
                    mirror_info.mirror_index + 1,
                    mirror_info.total_mirrors,
                    current_url,
                )
            else:
                self._log.debug(
                    "Starting download for task %s: %s", task.task_id, current_url
                )

            try:
                task.status = DownloadStatus.DOWNLOADING

                mirror = task.get_current_mirror()
                async with httpx.AsyncClient(
                        headers=mirror.headers,
                        cookies=mirror.cookies,
                        timeout=httpx.Timeout(30.0, connect=10.0),
                        follow_redirects=True,
                ) as client:
                    remote_size, remote_filename = await self._fetch_head_info(client, task)

                    if remote_filename and task.file_name == task.task_id:
                        task.file_name = remote_filename.replace(" ", "-")
                        self._log.info(
                            "Task %s: filename resolved from Content-Disposition: %s",
                            task.task_id, task.file_name,
                        )
                    elif task.file_name == task.task_id:
                        parsed = urlparse(task.get_current_mirror().url)
                        task.file_name = os.path.basename(parsed.path)

                    file_exists = os.path.exists(task.filepath)
                    local_size = os.path.getsize(task.filepath) if file_exists else 0

                    self._log.debug(
                        "Task %s: file_exists=%s, local=%s, remote=%s",
                        task.task_id, file_exists, local_size, remote_size,
                    )

                    if not file_exists:
                        await self._stream_download(client, task, 0, remote_size)

                    elif isinstance(remote_size, int) and local_size < remote_size:
                        await self._stream_download(client, task, local_size, remote_size)

                    elif isinstance(remote_size, int) and local_size == remote_size:
                        if task.expected_hash:
                            hash_valid = await self._verify_hash(task)
                            if hash_valid:
                                await self._mark_completed(task, local_size, "hash verified")
                                return
                            else:
                                # Local file and remote file matches in size, but hash mismatch → redownload
                                self._log.error(
                                    "Hash mismatch for task %s with existing file",
                                    task.task_id,
                                )
                                raise RuntimeError(
                                    f"Hash mismatch for existing file: "
                                    f"expected {task.expected_hash}, got {task.actual_hash}"
                                )
                        else:
                            await self._mark_completed(task, local_size, "size verified")
                            return


                    elif remote_size in [None, 0] and local_size != 0:
                        if task.expected_hash:
                            hash_valid = await self._verify_hash(task)
                            if hash_valid:
                                await self._mark_completed(task, local_size, "hash verified")
                                return
                        else:
                            raise RuntimeError(
                                f"{task.task_id} is failed: remote content length is unknown, "
                                f"but local file exists with size {local_size} bytes"
                            )
                    else:
                        raise RuntimeError(
                            f"{task.task_id} is failed: local file size ({local_size} bytes), "
                            f"but remote content length is {remote_size} bytes"
                        )

                if task.expected_hash:
                    if not await self._verify_hash(task):
                        raise RuntimeError(
                            f"Hash mismatch after download: "
                            f"expected {task.expected_hash}, got {task.actual_hash}"
                        )

                await self._mark_completed(task, task.downloaded_bytes, "download completed")
                return  # success

            except asyncio.CancelledError:
                if task.status == DownloadStatus.DOWNLOADING:
                    task.status = DownloadStatus.PAUSED
                    self._log.info("Download paused (cancelled): %s", task.task_id)
                raise

            except RuntimeError as e:
                error_msg = str(e)
                self._log.warning(
                    "Download failed for task %s from %s: %s",
                    task.task_id, current_url, error_msg,
                )

                if task.switch_to_next_mirror():
                    self._log.info(
                        "Task %s: Switching to next mirror: %s",
                        task.task_id, task.get_current_mirror().url,
                    )
                    continue
                else:
                    failed_count = len(task.failed_urls)
                    self._mark_failed(
                        task,
                        task_error_message=f"Download failed from all sources",
                        log_error_message=(f"Download failed from all sources ({failed_count} "
                                          f"URL(s) tried). Last error: {error_msg}")
                    )
                    break
            except httpx.HTTPError as e:
                self._mark_failed(
                    task,
                    task_error_message=f"HTTP Error",
                    log_error_message=f"HTTP error for task {task.task_id}: {e}"
                )
            except Exception as e:
                self._mark_failed(
                    task,
                    task_error_message=f"Unexpected Error",
                    log_error_message=f"Unexpected error for task {task.task_id}: {e}"
                )
                raise Exception(f"Unexpected Error: {task.task_id} - " + str(e))

    @staticmethod
    def _filename_from_content_disposition(header: str) -> Optional[str]:
        """
        Extract filename from Content-Disposition header.
        Handles both:
          filename="model.safetensors"
          filename*=UTF-8''model%20name.safetensors
        """
        import re
        # filename*= (RFC 5987, приоритет выше)
        match = re.search(r"filename\*=(?:UTF-8'')?([^\s;]+)", header, re.IGNORECASE)
        if match:
            from urllib.parse import unquote
            return unquote(match.group(1).strip('"'))
        # filename=
        match = re.search(r'filename=["\']?([^"\';]+)["\']?', header, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return None

    async def _fetch_head_info(
            self, client: httpx.AsyncClient, task: DownloadTask
    ) -> tuple[Optional[int], Optional[str]]:
        """Returns (content_length, filename_from_headers)."""
        current_url = task.get_current_mirror().url
        try:
            head = await client.head(current_url)

            content_length = None
            if head.status_code >= 400:
                head = await client.get(current_url, headers={"Range": "bytes=0-0"})

                content_range = head.headers.get("Content-Range")
                if content_range:
                    content_length = int(content_range.split("/")[-1])

            if not content_length and "content-length" in head.headers:
                content_length = int(head.headers["content-length"])


            filename = None
            if "content-disposition" in head.headers:
                filename = self._filename_from_content_disposition(
                    head.headers["content-disposition"]
                )

            return content_length, filename
        except httpx.ConnectError as e:
            self._log.warning("HEAD request failed for %s: %s", task.task_id, e)
            raise RuntimeError(f"HEAD request failed: {e}") from e

    async def _verify_hash(self, task: DownloadTask) -> bool:
        current_status = task.status
        task.status = DownloadStatus.CHECKING
        task.actual_hash = await HashVerifier.calculate_hash(
            task.filepath, task.hash_algorithm
        )
        task.status = current_status
        return task.actual_hash == task.expected_hash

    async def _stream_download(
            self,
            client: httpx.AsyncClient,
            task: DownloadTask,
            local_size: int,
            remote_size: int | None = None,
    ) -> None:
        current_url = task.get_current_mirror().url
        headers = {}
        if isinstance(remote_size, int) and 0 < local_size < remote_size:
            headers["Range"] = f"bytes={local_size}-"

        async with client.stream("GET", current_url, headers=headers) as response:
            if response.status_code not in (200, 206):
                raise httpx.HTTPError(f"HTTP {response.status_code}")

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
                        self._log.info(
                            "Stream interrupted mid-download: %s", task.task_id
                        )
                        return
                    await f.write(chunk)
                    task.downloaded_bytes += len(chunk)

    async def _mark_completed(self, task: DownloadTask, total_bytes: int, reason: str) -> None:
        task.total_bytes = total_bytes

        if task.downloaded_bytes == 0:
            task.downloaded_bytes = total_bytes

        task.status = DownloadStatus.COMPLETED
        task.completed_at = datetime.now().isoformat()
        self._log.info("Completed (%s): %s", reason, task.task_id)
        await self._post_processor_exec(task)

    def _mark_failed(self, task: DownloadTask, task_error_message: str, log_error_message: str | None) -> None:
        task.status = DownloadStatus.FAILED
        task.error_message = task_error_message
        if not log_error_message:
            log_error_message = task_error_message
        self._log.error(log_error_message)

    async def _post_processor_exec(self, task: DownloadTask) -> None:
        if task.extract:
            try:
                await self._post_processor.run(task)
            except Exception as e:
                # не роняем задачу — скачивание прошло успешно
                self._log.error("Post-processing failed for %s: %s", task.task_id, e)
                task.error_message = f"extract failed: {e}"

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_progress(self, task_id: str) -> Optional[DownloadProgress]:
        task = self.tasks.get(task_id)
        if not task:
            return None
        percentage = (
            (task.downloaded_bytes / task.total_bytes) * 100
            if task.total_bytes > 0
            else 0.0
        )

        result = DownloadProgress.from_dict(
            {
                "task_id": task.task_id,
                "current_url": task.get_current_mirror().url,
                "folder_path": task.folder_path,
                "file_name": task.file_name,
                "filepath": task.filepath,
                "status": task.status,
                "downloaded_bytes": task.downloaded_bytes,
                "total_bytes": task.total_bytes,
                "percentage": round(percentage, 2),
            }
        )

        mirror_info = task.get_mirror_info()
        if len(task.mirrors) > 1 or mirror_info.using_fallback_mirror:
            result.mirror_info = mirror_info

        return result

    def get_all_progress(self) -> List[DownloadProgress]:
        return [self.get_progress(tid) for tid in self.tasks]

    def list_all_tasks(self) -> List[DownloadTask]:
        return [task for task in self.tasks.values()]

    def verify_file(self, task_id: str) -> Optional[VerificationResult]:
        task = self.tasks.get(task_id)
        if not task:
            return None

        filepath = task.filepath
        if not filepath or not os.path.exists(filepath):
            return VerificationResult.from_dict({"task_id": task_id, "verified": False, "reason": "File not found"})

        if not task.expected_hash:
            return VerificationResult.from_dict({"task_id": task_id, "verified": False, "reason": "Hash not provided"})

        return VerificationResult.from_dict({
            "task_id": task_id,
            "verified": task.actual_hash == task.expected_hash if task.actual_hash else False,
            "expected_hash": task.expected_hash,
            "actual_hash": task.actual_hash,
            "algorithm": task.hash_algorithm,
        })

    @classmethod
    def get_file_info(cls, filepath: str) -> Optional[FileInfo]:
        if not os.path.exists(filepath):
            return None
        try:
            stat = os.stat(filepath)
            return FileInfo.from_dict({
                "filepath": filepath,
                "size_bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_ctime).isoformat(),
                "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "exists": True,
            })
        except Exception:
            return None

    @classmethod
    async def get_url_info(cls, url: str) -> Optional[UrlInfo]:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.head(url, follow_redirects=True)
                if response.status_code not in (200, 206):
                    return UrlInfo.from_dict({
                        "url": url,
                        "status_code": response.status_code,
                        "error": f"HTTP {response.status_code}",
                    })
                return UrlInfo.from_dict({
                    "url": url,
                    "status_code": response.status_code,
                    "size_bytes": int(response.headers["content-length"])
                    if "content-length" in response.headers
                    else None,
                    "content_type": response.headers.get("content-type"),
                    "supports_range": response.headers.get("accept-ranges") == "bytes",
                })
        except Exception:
            return None

    @classmethod
    async def verify_download(
            cls, filepath: str, expected_hash: str, algorithm: str = "md5"
    ) -> VerificationResult:
        if not os.path.exists(filepath):
            return VerificationResult.from_dict({"filepath": filepath, "verified": False, "reason": "File not found"})
        try:
            actual_hash = await HashVerifier.calculate_hash(filepath, algorithm)
            return VerificationResult.from_dict({
                "filepath": filepath,
                "verified": actual_hash.lower() == expected_hash.lower(),
                "algorithm": algorithm,
                "expected_hash": expected_hash,
                "actual_hash": actual_hash,
            })
        except Exception as e:
            return VerificationResult.from_dict({"filepath": filepath, "verified": False, "reason": str(e)})
