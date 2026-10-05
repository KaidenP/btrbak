"""Shared helpers: subprocess, hashing, locking, filesystem checks."""

import contextlib
import datetime
import fcntl
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


class BtrbakError(Exception):
    """Base class for expected, user-facing failures."""


def run(cmd, stdout=None, stdin=None, check=True) -> subprocess.CompletedProcess:
    """Run a command, returning the CompletedProcess.

    By default stdout/stderr are captured and a non-zero exit raises
    :class:`BtrbakError`. Pass ``stdout=file`` to stream stdout to a file.
    A missing executable also raises :class:`BtrbakError`.
    """
    try:
        proc = subprocess.run(
            cmd,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=stdin,
        )
    except FileNotFoundError as exc:
        raise BtrbakError(f"required command not found: {cmd[0]}") from exc
    if check and proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
        raise BtrbakError(
            f"command failed ({proc.returncode}): {' '.join(map(str, cmd))}\n{stderr}"
        )
    return proc


def sha256_file(path) -> str:
    """Return the hex sha256 digest of a file."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def now() -> int:
    """Return the current unix timestamp (UTC)."""
    return int(datetime.datetime.now(datetime.timezone.utc).timestamp())


def snapshot_id(ts=None) -> str:
    """Format a unix timestamp as a sortable snapshot id (UTC)."""
    ts = ts if ts is not None else now()
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )


@contextlib.contextmanager
def exclusive_lock(path):
    """Acquire a non-blocking exclusive ``flock`` on *path*.

    Raises :class:`BtrbakError` when another process already holds the lock.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BtrbakError(f"lock is held by another process: {path}")
        yield
    finally:
        os.close(fd)


def atomic_write_text(path, text: str) -> None:
    """Atomically write *text* to *path* (temp file + fsync + rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


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


def btrfs_fsid(path) -> str | None:
    """Return the btrfs filesystem UUID for *path*, or ``None``.

    btrfs subvolumes report distinct ``st_dev`` values, so the filesystem
    UUID of the containing mount point is the reliable identifier.
    """
    try:
        mount = _mount_point(Path(path))
        proc = subprocess.run(
            ["btrfs", "filesystem", "show", str(mount)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    text = proc.stdout.decode("utf-8", "replace")
    match = re.search(r"\buuid:\s*([0-9a-fA-F-]+)", text)
    return match.group(1).lower() if match else None


def _mount_point(path: Path) -> Path:
    """Return the real mount point containing *path*.

    ``os.path.ismount`` is unusable here: btrfs subvolumes report distinct
    ``st_dev`` values and therefore appear to be mount points themselves.
    Parse ``/proc/self/mounts`` so we resolve to the actual filesystem mount.
    """
    path = Path(path).resolve()
    best = None
    try:
        with open("/proc/self/mounts", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 2:
                    continue
                mount = Path(parts[1].replace("\\040", " "))
                if path == mount or mount in path.parents:
                    if best is None or len(mount.parts) > len(best.parts):
                        best = mount
    except OSError:
        return path
    return best or path


def same_device(a, b) -> bool:
    """Return True when *a* and *b* live on the same btrfs filesystem."""
    fsid_a = btrfs_fsid(a)
    fsid_b = btrfs_fsid(b)
    return fsid_a is not None and fsid_a == fsid_b


def is_nested(inner, outer) -> bool:
    """Return True when *inner* is inside *outer*."""
    try:
        return Path(inner).resolve().is_relative_to(Path(outer).resolve())
    except OSError:
        return False


def is_btrfs(path) -> bool:
    """Return True when *path* sits on a btrfs filesystem."""
    return btrfs_fsid(path) is not None


def is_subvolume(path) -> bool:
    """Return True when *path* is a btrfs subvolume."""
    try:
        proc = subprocess.run(
            ["btrfs", "subvolume", "show", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return False
    return proc.returncode == 0


def which(binary) -> bool:
    """Return True when *binary* is on PATH."""
    return shutil.which(binary) is not None
