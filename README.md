# pydownlib

Async download manager with mirror failover, hash verification, and post-processing.

## Installation

```bash
pip install https://github.com/nbpm128/PyDownLib.git
```

## Examples

### Basic download

```python
import asyncio
from pydownlib import DownloadManager, DownloadTask, MirrorUrl

async def main():
    manager = DownloadManager(
        max_concurrent=3,
        default_download_path="./downloads",
    )

    task = DownloadTask(
        file_name="model.safetensors",
        folder_path="models/char",
        mirrors=[
            MirrorUrl(url="https://example.com/files/model.safetensors", priority=0),
        ],
        expected_hash="d41d8cd98f00b204e9800998ecf8427e",
        hash_algorithm="md5",
    )

    await manager.add_task(task)
    await manager.start_downloads()

    while task.status not in ("completed", "failed"):
        progress = manager.get_progress(task.task_id)
        print(f"{progress.percentage:.1f}% — {progress.status}")
        await asyncio.sleep(1)

    await manager.stop_downloads()

asyncio.run(main())
```

### Mirror failover + archive extraction

```python
import asyncio
from pydownlib import DownloadManager, DownloadTask, MirrorUrl, ExtractOptions, ArchiveFormat

async def main():
    manager = DownloadManager(max_concurrent=2)

    task = DownloadTask(
        file_name="dataset.zip",
        folder_path="data/raw",
        mirrors=[
            MirrorUrl(url="https://primary.example.com/dataset.zip", priority=0),
            MirrorUrl(url="https://mirror.example.com/dataset.zip",  priority=1),
            MirrorUrl(url="https://backup.example.com/dataset.zip",  priority=2),
        ],
        extract=ExtractOptions(
            format=ArchiveFormat.ZIP,
            destination="data/unpacked",
            remove_archive=True,
        ),
    )

    await manager.add_task(task)
    await manager.start_downloads()

    while task.status not in ("completed", "failed"):
        await asyncio.sleep(1)

    if task.status == "failed":
        print(f"Error: {task.error_message}")
    else:
        print("Done — archive extracted to data/unpacked/")

    await manager.stop_downloads()

asyncio.run(main())
```

## Key features

- **Parallel downloads** — configurable worker pool (`max_concurrent`)
- **Mirror failover** — automatically switches to the next mirror on failure
- **Hash verification** — md5 / sha1 / sha256
- **Archive extraction** — auto-unzip after download, with optional archive removal
- **State persistence** — save/restore task state via `state_file_path`
