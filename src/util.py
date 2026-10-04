"""Shared helpers: subprocess, hashing, locking, filesystem checks."""

import contextlib
import datetime
import fcntl
import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

BTRFS_MAGIC = 0x9123683E


class BtrbakError(Exception):
    """Base class for expected, user-facing failures."""


def run(cmd, stdout=None, stdin=None, check=True) -> subprocess.CompletedProcess:
    """Run a command, returning the CompletedProcess.

    By default stdout/stderr are captured and a non-zero exit raises
    :class:`BtrbakError`. Pass ``stdout=file`` to stream stdout to a file.
    """
    proc = subprocess.run(
        cmd,
        stdout=stdout if stdout is not None else subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=stdin,
    )
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
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def same_device(a, b) -> bool:
    """Return True when *a* and *b* live on the same filesystem (st_dev)."""
    return os.stat(a).st_dev == os.stat(b).st_dev


def is_nested(inner, outer) -> bool:
    """Return True when *inner* is inside *outer*."""
    try:
        return Path(inner).resolve().is_relative_to(Path(outer).resolve())
    except OSError:
        return False


def is_btrfs(path) -> bool:
    """Return True when *path* sits on a btrfs filesystem."""
    try:
        return os.statvfs(path).f_type == BTRFS_MAGIC
    except OSError:
        return False


def is_subvolume(path) -> bool:
    """Return True when *path* is a btrfs subvolume."""
    proc = subprocess.run(
        ["btrfs", "subvolume", "show", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.returncode == 0


def which(binary) -> bool:
    """Return True when *binary* is on PATH."""
    return shutil.which(binary) is not None
