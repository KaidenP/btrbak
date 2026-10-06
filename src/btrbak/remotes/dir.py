"""A remote backed by a local filesystem directory."""

import os
import shutil
import stat
import tempfile
from pathlib import Path

from .base import Remote, RemoteError, RemoteNotFoundError, split_safe_path


class DirRemote(Remote):
    """Maps logical paths onto ``<root>/<logical-path>``."""

    def __init__(self, settings: dict):
        super().__init__(settings)
        if "path" not in settings:
            raise RemoteError("'dir' remote requires a 'path' setting")
        self.root = Path(settings["path"]).expanduser().resolve()

    def _resolve(self, remote_path: str) -> Path:
        # Build the target lexically from validated components; never follow
        # symlinks while resolving. A symlink swap after this point is caught
        # by the per-operation ``_recheck`` immediately before each syscall.
        return self.root.joinpath(*split_safe_path(remote_path))

    def _recheck(self, target: Path, remote_path: str) -> None:
        """Refuse symlinks anywhere between the root and *target*.

        Uses ``os.lstat`` (which does not follow symlinks) immediately before
        each operation to close the resolve-then-use window as far as is
        practical without a full dirfd rewrite.
        """
        current = self.root
        try:
            st = os.lstat(current)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(st.st_mode):
            raise RemoteError(f"refusing symlink in remote path: {remote_path!r}")
        for part in target.relative_to(self.root).parts:
            current = current / part
            try:
                st = os.lstat(current)
            except FileNotFoundError:
                return
            if stat.S_ISLNK(st.st_mode):
                raise RemoteError(
                    f"refusing symlink in remote path: {remote_path!r}"
                )

    def validate(self) -> None:
        if self.root.exists():
            if not self.root.is_dir():
                raise RemoteError(f"remote path is not a directory: {self.root}")
            self._write_probe(
                self.root, f"remote path is not writable: {self.root}"
            )
        else:
            parent = self.root.parent
            if not parent.exists():
                raise RemoteError(f"remote parent does not exist: {parent}")
            self._write_probe(
                parent, f"remote parent is not writable: {parent}"
            )

    @staticmethod
    def _write_probe(directory: Path, message: str) -> None:
        # os.access is unreliable (notably when running as root), so prove
        # writability by creating and removing a real temp file.
        try:
            fd, tmp_name = tempfile.mkstemp(
                dir=str(directory), prefix=".btrbak-probe-", suffix=".tmp"
            )
        except OSError as exc:
            raise RemoteError(message) from exc
        try:
            os.close(fd)
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    def read(self, remote_path: str, local_dest: Path) -> None:
        source = self._resolve(remote_path)
        self._recheck(source, remote_path)
        if not source.exists():
            raise RemoteNotFoundError(f"remote file not found: {remote_path}")
        local_dest = Path(local_dest)
        local_dest.parent.mkdir(parents=True, exist_ok=True)
        # Download to a temp file in the destination directory and atomically
        # replace, so a failed download never leaves a partial file. mkstemp
        # creates the temp file (and hence the final file) as 0600: downloads
        # are raw backup data and must never land world-readable.
        fd, tmp_name = tempfile.mkstemp(
            dir=str(local_dest.parent),
            prefix=local_dest.name + ".",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "wb") as dst, open(source, "rb") as src:
                shutil.copyfileobj(src, dst)
            os.replace(tmp_name, local_dest)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def write(self, local_src: Path, remote_path: str) -> None:
        destination = self._resolve(remote_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._recheck(destination, remote_path)
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
            self._recheck(destination, remote_path)
            os.replace(tmp_name, destination)
            _fsync_dir(destination.parent)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def delete(self, remote_path: str) -> None:
        target = self._resolve(remote_path)
        self._recheck(target, remote_path)
        try:
            target.unlink()
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
