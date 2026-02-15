# Asynchronous Download Manager

Asynchronous download manager in Python with support for concurrent downloads, resumption, and file integrity verification.

## Features

- ✅ **Async concurrent downloads** - download multiple files simultaneously
- ✅ **Download resumption** - continue downloading after a connection interruption
- ✅ **Integrity verification** - verify files by hash (MD5, SHA1, SHA256)
- ✅ **State persistence** - restore downloads after application restart
- ✅ **Task management** - stop, resume, and track downloads

## Requirements

- Python >= 3.12
- aiofiles >= 25.1.0
- httpx >= 0.28.1

## Installation

```bash
# Install dependencies
pip install -r requirements.txt

# Or using uv
uv sync
```

## Usage

### 1. CLI Interface (Interactive Menu)

> **Note:** The CLI interface is provided for testing purposes only.

```bash
python cli.py
```

Available actions:
- **1. Start Download** - add a new download
- **2. Show Progress Download** - show download progress
- **3. List All Download Tasks** - list all tasks
- **5. Stop Download by ID** - stop a download by ID
- **7. Stop All Downloads** - stop all downloads
- **9. Exit** - exit

### 2. Programmatic Usage

```python
import asyncio
from pydownlib import DownloadManager

async def main():
    # Create a manager with a maximum of 3 concurrent downloads
    manager = DownloadManager(max_concurrent=3)
    
    # Add a download
    task_id = manager.add_download(
        url="https://example.com/file.zip",
        filepath="downloads/file.zip"
    )
    
    # Add a download with hash verification
    manager.add_download(
        url="https://example.com/file2.zip",
        filepath="downloads/file2.zip",
        expected_hash="abc123def456",
        hash_algorithm="sha256"
    )
    
    # Start downloads
    await manager.start_downloads()
    
    # Get progress
    progress = manager.get_progress(task_id)
    print(f"Progress: {progress['percentage']:.1f}%")

asyncio.run(main())
```

## API Documentation

### DownloadManager

#### Methods

##### `add_download(url, filepath, expected_hash=None, hash_algorithm="md5")`
Add a new download task.

**Parameters:**
- `url` (str): URL to download
- `filepath` (str): Local path to save the file
- `expected_hash` (str, optional): Expected file hash
- `hash_algorithm` (str): Hash algorithm (md5, sha1, sha256)

**Returns:** Task ID (str)

```python
task_id = manager.add_download(
    url="https://example.com/file.zip",
    filepath="downloads/file.zip",
    expected_hash="abc123",
    hash_algorithm="md5"
)
```

##### `start_downloads()`
Start downloading all pending tasks.

```python
await manager.start_downloads()
```

##### `get_progress(task_id)`
Get the progress of a specific task.

**Returns:** Dictionary with progress information or None

```python
progress = manager.get_progress(task_id)
# {
#     'task_id': 'task_1',
#     'url': 'https://...',
#     'status': 'downloading',
#     'downloaded_bytes': 1024,
#     'total_bytes': 2048,
#     'percentage': 50.0
# }
```

##### `get_all_progress()`
Get progress of all tasks.

**Returns:** List of dictionaries with progress

```python
all_progress = manager.get_all_progress()
```

##### `list_all_tasks()`
Get a list of all tasks.

**Returns:** List of dictionaries with task information

```python
tasks = manager.list_all_tasks()
```

##### `stop_download(task_id)`
Stop a specific download.

**Parameters:**
- `task_id` (str): ID of the task to stop

**Returns:** True if stopped, False if not found

```python
manager.stop_download(task_id)
```

##### `stop_all_downloads()`
Stop all active downloads.

```python
manager.stop_all_downloads()
```

##### `resume_download(task_id)`
Resume a stopped download.

**Parameters:**
- `task_id` (str): ID of the task to resume

**Returns:** True if resumed, False if not found

```python
manager.resume_download(task_id)
```

##### `verify_file(task_id)`
Verify the integrity of a downloaded file.

**Parameters:**
- `task_id` (str): Task ID

**Returns:** Dictionary with verification result or None

```python
verification = manager.verify_file(task_id)
# {
#     'task_id': 'task_1',
#     'verified': True,
#     'expected_hash': 'abc123',
#     'actual_hash': 'abc123',
#     'algorithm': 'md5'
# }
```

## Examples

### Example 1: Simple Download

```python
import asyncio
from pydownlib import DownloadManager

async def main():
    manager = DownloadManager()
    manager.add_download(
        url="https://httpbin.org/bytes/1024",
        filepath="downloads/test.bin"
    )
    await manager.start_downloads()

asyncio.run(main())
```

### Example 2: Download with Hash Verification

```python
import asyncio
from pydownlib import DownloadManager

async def main():
    manager = DownloadManager()
    manager.add_download(
        url="https://example.com/file.zip",
        filepath="downloads/file.zip",
        expected_hash="e99a18c428cb38d5f260853678922e03",
        hash_algorithm="md5"
    )
    await manager.start_downloads()
    
    # Check the result
    verification = manager.verify_file("task_1_...")
    if verification['verified']:
        print("File verified successfully!")

asyncio.run(main())
```

### Example 3: Concurrent Downloads

```python
import asyncio
from pydownlib import DownloadManager

async def main():
    manager = DownloadManager(max_concurrent=5)
    
    # Add multiple downloads
    for i in range(10):
        manager.add_download(
            url=f"https://example.com/file{i}.zip",
            filepath=f"downloads/file{i}.zip"
        )
    
    # Start all simultaneously
    await manager.start_downloads()

asyncio.run(main())
```

### Example 4: Resuming a Download

```python
import asyncio
from pydownlib import DownloadManager

async def main():
    manager = DownloadManager()
    
    # Add a download
    task_id = manager.add_download(
        url="https://example.com/large_file.zip",
        filepath="downloads/large_file.zip"
    )
    
    # Start the download
    await manager.start_downloads()
    
    # If the connection was interrupted, you can resume:
    manager.resume_download(task_id)
    await manager.start_downloads()

asyncio.run(main())
```

### Example 5: State Persistence

```python
import asyncio
from pydownlib import DownloadManager

async def main():
    # Create a manager with a state file
    manager = DownloadManager(state_file="my_downloads.json")
    
    # Add downloads
    manager.add_download(
        url="https://example.com/file1.zip",
        filepath="downloads/file1.zip"
    )
    
    # State is saved automatically
    # On application restart:
    manager2 = DownloadManager(state_file="my_downloads.json")
    # All tasks will be loaded from the file

asyncio.run(main())
```

## Download Statuses

- `pending` - waiting to download
- `downloading` - download in progress
- `paused` - download stopped
- `completed` - download finished
- `failed` - error during download

## Supported Hashing Algorithms

- `md5` - MD5 hash
- `sha1` - SHA-1 hash
- `sha256` - SHA-256 hash

## Features

### Asynchronous
The application is fully asynchronous and non-blocking during file downloads. Multiple files can be downloaded simultaneously.

### Download Resumption
If the connection was interrupted, the manager can resume the download from where it left off using HTTP Range requests.

### State Persistence
All tasks are automatically saved to a JSON file. On application restart, all incomplete downloads can be restored.

### Integrity Verification
Verification of downloaded files by MD5, SHA1, and SHA256 hashes is supported.

## License

MIT
