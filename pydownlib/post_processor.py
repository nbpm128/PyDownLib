import asyncio
import tarfile
import zipfile
from pathlib import Path
from .models import DownloadTask, ArchiveFormat

TAR_FORMATS = {
    ArchiveFormat.TAR,
    ArchiveFormat.TAR_GZ,
    ArchiveFormat.TAR_BZ2,
    ArchiveFormat.TAR_XZ,
}


class PostProcessor:

    async def run(self, task: DownloadTask) -> None:
        if not task.extract:
            return

        fmt = task.extract.format
        if fmt == ArchiveFormat.ZIP:
            await asyncio.to_thread(self._extract_zip, task)
        elif fmt in TAR_FORMATS:
            await asyncio.to_thread(self._extract_tar, task)
        else:
            raise NotImplementedError(f"Unsupported format: {fmt}")

    def _extract_zip(self, task: DownloadTask) -> None:
        src = Path(task.filepath)
        dest = Path(task.extract.destination or task.folder_path)
        dest.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(src, "r") as zf:
            members = zf.infolist()
            total_size = sum(m.file_size for m in members)
            extracted = 0
            task.extract_progress = 0.0

            for member in members:
                zf.extract(member, path=dest, pwd=task.extract.password)
                extracted += member.file_size
                if total_size > 0:
                    task.extract_progress = round(extracted / total_size * 100, 2)

        task.extract_progress = 100.0

        if task.extract.remove_archive:
            src.unlink()

    def _extract_tar(self, task: DownloadTask) -> None:
        src = Path(task.filepath)
        dest = Path(task.extract.destination or task.folder_path)
        dest.mkdir(parents=True, exist_ok=True)

        with tarfile.open(src, "r:*") as tf:
            members = tf.getmembers()
            total_size = sum(m.size for m in members)
            extracted = 0
            task.extract_progress = 0.0

            for member in members:
                tf.extract(member, path=dest, filter="data")
                extracted += member.size
                if total_size > 0:
                    task.extract_progress = round(extracted / total_size * 100, 2)

        task.extract_progress = 100.0

        if task.extract.remove_archive:
            src.unlink()