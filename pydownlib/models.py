"""
Data models for download manager
"""

from dataclasses import dataclass, asdict, field
from typing import Optional, Dict
from enum import StrEnum


class DownloadStatus(StrEnum):
    """Download task status enumeration"""
    PENDING = "pending"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class DownloadTask:
    """Represents a single download task"""

    url: str
    filepath: str
    status: str = DownloadStatus.PENDING.value
    downloaded_bytes: int = 0
    total_bytes: int = 0
    created_at: str = ""
    task_id: str = None
    completed_at: Optional[str] = None
    error_message: Optional[str] = None
    expected_hash: Optional[str] = None
    hash_algorithm: Optional[str] = None
    actual_hash: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)
    cookies: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert task to dictionary"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "DownloadTask":
        """Create task from dictionary"""
        # Backward compatibility: state files saved before headers/cookies
        # were introduced won't have these keys
        data.setdefault("headers", {})
        data.setdefault("cookies", {})
        return cls(**data)