"""A remote backed by a local filesystem directory."""

import os
import shutil
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
        shutil.copyfile(source, local_dest)

    def write(self, local_src: Path, remote_path: str) -> None:
        destination = self._resolve(remote_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.parent / (destination.name + ".tmp-btrbak")
        shutil.copyfile(local_src, tmp)
        os.replace(tmp, destination)

    def delete(self, remote_path: str) -> None:
        try:
            self._resolve(remote_path).unlink()
        except FileNotFoundError:
            pass

    def list(self, prefix: str = "") -> list[str]:
        base = self._resolve(prefix) if prefix else self.root
        if not base.exists():
            return []
        results = []
        for root, _dirs, files in os.walk(base):
            for name in files:
                full = Path(root) / name
                results.append(full.relative_to(self.root).as_posix())
        return sorted(results)
