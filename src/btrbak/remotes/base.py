"""Remote backend abstraction.

``btrbak`` owns the logical layout of the remote store; remotes translate
logical paths (e.g. ``meta.yaml``, ``<profile>/<id>.send``) into calls against
their backing store.
"""

from abc import ABC, abstractmethod
from pathlib import Path


class RemoteError(Exception):
    """Base class for remote failures."""


class RemoteNotFoundError(RemoteError):
    """Raised when a requested remote file does not exist."""


def split_safe_path(remote_path: str) -> list[str]:
    """Split *remote_path* into safe components.

    Rejects absolute paths, empty or whitespace-only components, and the
    ``.`` / ``..`` entries so a logical path can never escape its remote
    root. Shared by the ``dir`` and ``gdrive`` remotes.
    """
    if not isinstance(remote_path, str) or not remote_path:
        raise RemoteError(f"invalid remote path: {remote_path!r}")
    if remote_path.startswith("/") or remote_path != remote_path.strip():
        raise RemoteError(f"invalid remote path: {remote_path!r}")
    parts = remote_path.split("/")
    if any(part in ("", ".", "..") or not part.strip() for part in parts):
        raise RemoteError(f"invalid remote path: {remote_path!r}")
    return parts


class Remote(ABC):
    """Path-based interface implemented by every remote backend."""

    def __init__(self, settings: dict):
        self.settings = settings

    @abstractmethod
    def validate(self) -> None:
        """Raise :class:`RemoteError` if unreachable or misconfigured."""

    @abstractmethod
    def read(self, remote_path: str, local_dest: Path) -> None:
        """Download *remote_path* to the local file *local_dest*."""

    @abstractmethod
    def write(self, local_src: Path, remote_path: str) -> None:
        """Upload the local file *local_src* to *remote_path*."""

    @abstractmethod
    def delete(self, remote_path: str) -> None:
        """Delete *remote_path*; a missing file is not an error."""
