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
import time
from pathlib import Path


class BtrbakError(Exception):
    """Base class for expected, user-facing failures."""


#: Returned by :func:`age_recipient_error` (and compared against in
#: :mod:`btrbak.config`) when the ``age`` binary is not installed.
AGE_MISSING_ERROR = "the 'age' binary was not found"


# Scratch subdirectories under ``<tmpdir>/<SUBVOL>/`` used by ``verify`` and
# ``restore`` for their downloads. The leading dot namespaces them away from
# the per-profile staging directories, whose names come from the profile name
# and therefore can never start with one (``PROFILE_NAME_RE`` requires a
# leading letter or digit). Without it a profile literally named ``verify`` or
# ``restore`` -- both legal -- would stage a send file at the very path a
# concurrent verify or restore is downloading into, and the two locks (the
# ``dest`` lock vs. the ``tmpdir`` lock) do not exclude each other.
VERIFY_SCRATCH = ".verify"
RESTORE_SCRATCH = ".restore"


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
        raise BtrbakError(f"command failed ({proc.returncode}): {cmd[0]}\n{stderr}")
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
def exclusive_lock(path, timeout=None):
    """Acquire an exclusive ``flock`` on *path*.

    Raises :class:`BtrbakError` when another process holds the lock for
    longer than *timeout* seconds; with the default ``timeout=None`` the
    acquisition is non-blocking and a held lock fails immediately.

    ``run`` uses the non-blocking form (a timer-driven run should skip rather
    than queue behind a long restore), while ``verify``/``restore``/``forget``
    pass a timeout so the brief moment a concurrent ``run`` holds the tmpdir
    lock for its staging sweep does not fail them outright.
    """
    fd = _open_lock(path)
    try:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if deadline is None:
                    raise BtrbakError(f"lock is held by another process: {path}")
                if time.monotonic() >= deadline:
                    raise BtrbakError(
                        f"lock is held by another process: {path} "
                        f"(waited {timeout:g}s)"
                    )
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def optional_lock(path):
    """Yield True when the lock was acquired, False when it was already held.

    Used where contention is expected and benign: the current holder should
    keep working rather than have the newcomer fail outright.
    """
    fd = _open_lock(path)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def private_dir(path) -> Path:
    """Create *path* (and any missing parents) as root-only ``0700`` directories.

    Staging and scratch directories hold raw backup data -- send streams and,
    during a restore of an encrypted profile, decrypted payloads -- so every
    directory btrbak creates for them must be off-limits to other local
    users. ``mkdir(mode=...)`` is not enough: the mode is masked by the
    process umask and only applies to the final component, so each created
    level is chmod'ed explicitly. Existing directories are left untouched.
    """
    path = Path(path)
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        os.chmod(directory, 0o700)
    return path


def open_private(path, mode="wb"):
    """Open *path* for writing as a ``0600`` file, returning the file object.

    A plain ``open(path, "wb")`` creates the file ``0644`` under the default
    umask, which would leave staged send streams -- entire filesystems,
    possibly neither compressed nor encrypted -- world-readable in ``/var/tmp``
    for the lifetime of an upload.
    """
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    return os.fdopen(fd, mode)


def rmdir_quiet(path) -> None:
    """Remove an empty directory, ignoring a missing or non-empty one."""
    try:
        Path(path).rmdir()
    except OSError:
        pass


@contextlib.contextmanager
def scratch_dir(path, root=None):
    """Yield an existing scratch directory, removing it again when it ends empty.

    ``verify`` and ``restore`` download send files into a directory under
    ``tmpdir``; both unlink their downloads as they go, so the directory is
    empty by the time they finish and is removed instead of being left behind
    for every future invocation to trip over. Empty parents are pruned as
    well, so ``<tmpdir>/<subvol>/`` does not outlive the run either.

    *root* bounds the walk: it is never removed, which keeps the user's
    tmpdir root (and anything above it) safe when a scratch directory sits
    directly inside it.
    """
    path = Path(path)
    private_dir(path)
    try:
        yield path
    finally:
        prune_empty_dir(path, root)


def prune_empty_dir(path, root=None) -> None:
    """Remove *path* and every parent directory left empty by it.

    The walk stops before *root*, which is never removed, so a staging
    directory can never take the caller's tmpdir root with it. A directory
    that still holds files (from an interrupted run) simply stays put, as
    does every parent below the one that did not empty. When *root* is not an
    ancestor of *path*, only *path* itself is removed.
    """
    path = Path(path)
    root = Path(root) if root is not None else path.parent
    bounded = path.is_relative_to(root)
    while path != root:
        rmdir_quiet(path)
        if not bounded:
            return
        path = path.parent


def _open_lock(path) -> int:
    """Open (creating if needed) *path* for flock and return the descriptor."""
    path = Path(path)
    private_dir(path.parent)
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o600)


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
    # btrfs-progs spells this `uuid:` in some versions and `UUID:` in others.
    match = re.search(r"\buuid:\s*([0-9a-fA-F-]+)", text, re.IGNORECASE)
    return match.group(1).lower() if match else None


def _unescape_mounts(value: str) -> str:
    """Decode octal escapes (``\\040`` etc.) used in ``/proc/self/mounts``."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)


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
                mount = Path(_unescape_mounts(parts[1]))
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


def _subvolume_show(path: Path) -> str | None:
    """Return ``btrfs subvolume show`` output for *path*, or ``None``.

    ``None`` covers both "not a subvolume" and "btrfs binary not installed",
    which are indistinguishable here and equally mean "no subvolume info".
    """
    try:
        proc = subprocess.run(
            ["btrfs", "subvolume", "show", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def is_subvolume(path) -> bool:
    """Return True when *path* is a btrfs subvolume."""
    return _subvolume_show(path) is not None


_OWN_UUID_RE = re.compile(r"^\s*UUID:\s*(\S+)", re.MULTILINE)
_RECEIVED_UUID_RE = re.compile(r"^\s*Received UUID:\s*(\S+)", re.MULTILINE)


def subvolume_uuid(path) -> str | None:
    """Return the UUID btrfs carries for *path* in a ``btrfs send`` stream.

    A local snapshot reports that identity as its own ``UUID``. A copy
    recovered by ``btrfs receive`` instead reports the sent subvolume's UUID as
    its ``Received UUID`` and gets a freshly assigned ``UUID`` of its own, so
    the two are only comparable when ``Received UUID`` wins when present --
    which is precisely the identity ``btrfs receive`` matches an incremental
    parent against. Recording it in ``meta.yaml`` lets ``restore`` tell a
    genuinely already-received link apart from an unrelated subvolume that
    merely occupies the same name in the target.
    """
    text = _subvolume_show(path)
    if text is None:
        return None
    for pattern in (_RECEIVED_UUID_RE, _OWN_UUID_RE):
        match = pattern.search(text)
        if match and match.group(1) != "-":
            return match.group(1).strip().lower()
    return None


def which(binary) -> bool:
    """Return True when *binary* is on PATH."""
    return shutil.which(binary) is not None


def age_recipient_kind(recipient) -> str:
    """Classify an age recipient as ``"file"``, ``"key"``, or ``"unknown"``.

    An existing filesystem path is a recipients file (``age -R``); an ``age1``
    string is an inline public key (``age -r``). Anything else is unknown and
    should be surfaced to the user rather than passed blindly to ``age``.
    """
    recipient = str(recipient)
    if os.path.exists(recipient):
        return "file"
    if recipient.startswith("age1"):
        return "key"
    return "unknown"


def age_recipient_error(recipient) -> str | None:
    """Return why *recipient* is unusable, or ``None`` when it is valid.

    A string with the right ``age1`` prefix is not necessarily a real key: a
    typo, a truncated key or an upper-cased one only fails deep inside ``age``
    when a backup is actually sent. Validating here instead means ``config
    check`` catches it up front rather than after the snapshot subvolume has
    already been created.

    The key is validated by asking ``age`` to encrypt a throwaway payload --
    the exact code path :mod:`send` uses. Age public keys are not secret, so
    echoing one into an error message is safe.
    """
    kind = age_recipient_kind(recipient)
    if kind == "unknown":
        return "neither an existing file nor an inline age1 key"
    cmd = ["age", "-R" if kind == "file" else "-r", str(recipient), "-o", os.devnull]
    try:
        proc = subprocess.run(
            cmd, input=b"", stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
    except FileNotFoundError:
        return AGE_MISSING_ERROR
    if proc.returncode == 0:
        return None
    stderr = proc.stderr.decode("utf-8", "replace").strip()
    first = stderr.splitlines()[0] if stderr else f"exit status {proc.returncode}"
    return first.removeprefix("age: error: ")
