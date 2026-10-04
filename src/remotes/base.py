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

    @abstractmethod
    def list(self, prefix: str = "") -> list[str]:
        """Return logical paths under *prefix*."""
