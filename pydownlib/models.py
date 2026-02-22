from dataclasses import dataclass, asdict, field
from typing import Optional, Dict, List, ClassVar
from enum import StrEnum
from pathlib import Path


class DownloadStatus(StrEnum):
    """Download task status enumeration"""
    PENDING = "pending"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CHECKING = "checking"


class ArchiveFormat(StrEnum):
    ZIP = "zip"


class DictSerializable:
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict):
        return cls(**data)


@dataclass
class ExtractOptions(DictSerializable):
    """ Parameters for post-download archive extraction. """
    format: ArchiveFormat = ArchiveFormat.ZIP
    destination: Optional[str] = None  # None → extract to same folder as archive
    remove_archive: bool = False  # remove archive file after successful extraction
    password: Optional[str] = None  # for encrypted archives (not currently supported)

    @classmethod
    def from_dict(cls, data: dict) -> "ExtractOptions":
        data = dict(data)
        data["format"] = ArchiveFormat(data.get("format", "zip"))
        return cls(**data)


@dataclass
class MirrorUrl(DictSerializable):
    url: str = ""
    priority: int = 0
    headers: Dict[str, str] = field(default_factory=dict)
    cookies: Dict[str, str] = field(default_factory=dict)

    _NULL: ClassVar["MirrorUrl"]

    @property
    def is_empty(self) -> bool:
        return not self.url

    @classmethod
    def empty(cls) -> "MirrorUrl":
        return cls._NULL

    @classmethod
    def from_dict(cls, data: dict) -> "MirrorUrl":
        data.setdefault("headers", {})
        data.setdefault("cookies", {})
        return cls(**data)

MirrorUrl._NULL = MirrorUrl()


@dataclass
class MirrorInfo(DictSerializable):
    using_fallback_mirror: bool
    mirror_index: int
    current_url: str
    total_mirrors: int
    failed_urls: List[str]
    remaining_mirrors: int


@dataclass
class DownloadTask(DictSerializable):
    """
    Represents a single download task.

    Destination path is split into two mutable fields:
      • ``file_name``   – bare filename, e.g. ``"model.safetensors"``
      • ``folder_path`` – destination directory, e.g. ``"/data/models/char"``

    The read-only ``filepath`` property joins them:
      ``str(Path(folder_path) / file_name)``

    All download sources (primary and fallback mirrors) are stored in
    ``mirrors``, sorted by ``priority`` (lower = higher priority).
    The primary URL is the mirror with the lowest priority value, which
    is always placed first when the task is created via
    :meth:`DownloadManager.add_download`.
    """

    file_name: Optional[str] = None
    folder_path: Optional[str] = None
    mirrors: List[MirrorUrl] = field(default_factory=list)
    status: str = DownloadStatus.PENDING
    downloaded_bytes: int = 0
    total_bytes: int = 0
    created_at: str = ""
    task_id: str = None
    completed_at: Optional[str] = None
    error_message: Optional[str] = None
    expected_hash: Optional[str] = None
    hash_algorithm: Optional[str] = None
    actual_hash: Optional[str] = None
    current_mirror_index: int = 0  # index into mirrors list (by priority)
    failed_urls: List[str] = field(default_factory=list)
    extract: Optional[ExtractOptions] = None

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    @property
    def filepath(self) -> Optional[str]:
        """Read-only: full destination path = folder_path / file_name.

        Returns ``None`` if either component is missing (not yet resolved).
        """
        if self.folder_path and self.file_name:
            return str(Path(self.folder_path) / self.file_name)
        return None

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Convert task to dictionary.

        Uses ``asdict`` which serialises dataclass *fields* only — the
        read-only ``filepath`` property is intentionally excluded.
        """
        data = asdict(self)
        data['mirrors'] = [
            m.to_dict() if isinstance(m, MirrorUrl) else m
            for m in self.mirrors
        ]
        if self.extract:
            data["extract"] = self.extract.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "DownloadTask":
        """Create task from dictionary.

        Backward-compatible with two legacy formats:

        1. Old ``url`` top-level field -> injected as priority-0 MirrorUrl.
        2. Old flat ``filepath`` string -> split into ``folder_path`` +
           ``file_name`` so existing state files load correctly.
        """

        data = dict(data)

        data.setdefault("mirrors", [])
        data.setdefault("current_mirror_index", 0)
        data.setdefault("failed_urls", [])

        # Convert mirror dicts -> MirrorUrl objects
        mirror_objects: List[MirrorUrl] = []
        for m in data["mirrors"]:
            mirror_objects.append(MirrorUrl.from_dict(m) if isinstance(m, dict) else m)

        raw_extract = data.pop("extract", None)
        data["mirrors"] = mirror_objects

        task = cls(**data)
        task.extract = ExtractOptions.from_dict(raw_extract) if raw_extract else None
        return task

    # ------------------------------------------------------------------
    # Active-source accessors
    # ------------------------------------------------------------------

    def _sorted_mirrors(self) -> List:
        """Return (original_index, mirror) pairs sorted by priority asc."""
        return sorted(enumerate(self.mirrors), key=lambda x: x[1].priority)

    def get_current_mirror(self) -> Optional[MirrorUrl]:
        """Return the MirrorUrl currently being used."""
        if 0 <= self.current_mirror_index < len(self.mirrors):
            return self.mirrors[self.current_mirror_index]
        return MirrorUrl.empty()

    # ------------------------------------------------------------------
    # Mirror failover
    # ------------------------------------------------------------------

    def switch_to_next_mirror(self) -> bool:
        """
        Mark the current source as failed and switch to the next one.

        Returns:
            ``True`` if a new mirror was selected, ``False`` if all
            sources are exhausted.
        """
        mirror = self.get_current_mirror()
        if mirror and mirror.url not in self.failed_urls:
            self.failed_urls.append(mirror.url)

        for idx, mirror in self._sorted_mirrors():
            if mirror.url not in self.failed_urls:
                self.current_mirror_index = idx
                return True

        return False

    def reset_mirrors(self) -> None:
        """Reset mirror state (for retry)."""
        sorted_mirrors = self._sorted_mirrors()
        self.current_mirror_index = sorted_mirrors[0][0] if sorted_mirrors else 0
        self.failed_urls.clear()

    def get_mirror_info(self) -> MirrorInfo:
        """Return diagnostic information about the current mirror state."""
        current = self.get_current_mirror()
        return MirrorInfo.from_dict(
            {
                "using_fallback_mirror": bool(self.failed_urls),
                "mirror_index": self.current_mirror_index,
                "current_url": current.url if current else "",
                "total_mirrors": len(self.mirrors),
                "failed_urls": list(self.failed_urls),
                "remaining_mirrors": len(
                    [m for m in self.mirrors if m.url not in self.failed_urls]
                ),
            }
        )


@dataclass
class DownloadProgress(DictSerializable):
    task_id: str
    current_url: str
    folder_path: str
    file_name: str
    filepath: str
    status: DownloadStatus
    downloaded_bytes: int
    total_bytes: int
    percentage: float
    mirror_info: Optional[MirrorInfo] = None


@dataclass
class VerificationResult(DictSerializable):
    verified: bool
    task_id: Optional[str] = None
    filepath: Optional[str] = None
    reason: Optional[str] = None
    expected_hash: Optional[str] = None
    actual_hash: Optional[str] = None
    algorithm: Optional[str] = None


@dataclass
class FileInfo(DictSerializable):
    filepath: str
    size_bytes: int
    created_at: str
    modified_at: str
    exists: bool


@dataclass
class UrlInfo(DictSerializable):
    url: str
    status_code: int
    size_bytes: Optional[int] = None
    content_type: Optional[str] = None
    supports_range: bool = False
    error: Optional[str] = None
