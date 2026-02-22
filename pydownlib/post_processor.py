import asyncio
import zipfile
from pathlib import Path
from .models import DownloadTask, ArchiveFormat


class PostProcessor:

    async def run(self, task: DownloadTask) -> None:
        if not task.extract:
            return

        fmt = task.extract.format
        if fmt == ArchiveFormat.ZIP:
            await asyncio.to_thread(self._extract_zip, task)
        else:
            raise NotImplementedError(f"Unsupported format: {fmt}")

    def _extract_zip(self, task: DownloadTask) -> None:
        src = Path(task.filepath)
        dest = Path(task.extract.destination or task.folder_path)
        dest.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(src, "r") as zf:
            zf.extractall(path=dest, pwd=task.extract.password)

        if task.extract.remove_archive:
            src.unlink()
