"""A remote backed by a local filesystem directory."""

import os
import shutil
import tempfile
from pathlib import Path

from .base import Remote, RemoteError, RemoteNotFoundError


class DirRemote(Remote):
    """Maps logical paths onto ``<root>/<logical-path>``."""

    def __init__(self, settings: dict):
        super().__init__(settings)
        if "path" not in settings:
            raise RemoteError("'dir' remote requires a 'path' setting")
        self.root = Path(settings["path"]).expanduser().resolve()

    def _resolve(self, remote_path: str) -> Path:
        target = (self.root / remote_path).resolve()
        if target != self.root and not target.is_relative_to(self.root):
            raise RemoteError(f"path escapes remote root: {remote_path!r}")
        return target

    def validate(self) -> None:
        if self.root.exists():
            if not self.root.is_dir():
                raise RemoteError(f"remote path is not a directory: {self.root}")
            if not os.access(self.root, os.W_OK):
                raise RemoteError(f"remote path is not writable: {self.root}")
        else:
            parent = self.root.parent
            if not parent.exists():
                raise RemoteError(f"remote parent does not exist: {parent}")
            if not os.access(parent, os.W_OK):
                raise RemoteError(f"remote parent is not writable: {parent}")

    def read(self, remote_path: str, local_dest: Path) -> None:
        source = self._resolve(remote_path)
        if not source.exists():
            raise RemoteNotFoundError(f"remote file not found: {remote_path}")
        local_dest = Path(local_dest)
        local_dest.parent.mkdir(parents=True, exist_ok=True)
        # Downloads are raw backup data; never let them land world-readable
        # (shutil.copyfile would create them 0644 under the default umask).
        fd = os.open(str(local_dest), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as dst, open(source, "rb") as src:
            shutil.copyfileobj(src, dst)

    def write(self, local_src: Path, remote_path: str) -> None:
        destination = self._resolve(remote_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(destination.parent),
            prefix=destination.name + ".",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "wb") as tmp_handle:
                with open(local_src, "rb") as src_handle:
                    shutil.copyfileobj(src_handle, tmp_handle)
                tmp_handle.flush()
                os.fsync(tmp_handle.fileno())
            os.replace(tmp_name, destination)
            _fsync_dir(destination.parent)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def delete(self, remote_path: str) -> None:
        try:
            self._resolve(remote_path).unlink()
        except FileNotFoundError:
            pass


def _fsync_dir(path: Path) -> None:
    """Best-effort fsync of a directory after a rename."""
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
