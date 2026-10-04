"""Integration tests against a real (loopback) btrfs filesystem.

These tests create a small btrfs filesystem in a raw file, mount it, and
exercise snapshot / send / restore end to end. They are skipped unless running
as root and btrfs tooling is available.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import send
import snapshot

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="requires root")

_REQUIRED = ["mkfs.btrfs", "btrfs", "mount", "umount"]

_IMG_SIZE = 512 * 1024 * 1024  # sparse


def _run(cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True)


def _have(binary):
    return shutil.which(binary) is not None


@pytest.fixture(scope="module")
def btrfs_fs(tmp_path_factory):
    if not all(_have(binary) for binary in _REQUIRED):
        pytest.skip("missing btrfs tooling")

    work = tmp_path_factory.mktemp("btrfs")
    image = work / "disk.img"
    mount_point = work / "mnt"
    mount_point.mkdir()

    with open(image, "wb") as handle:
        handle.truncate(_IMG_SIZE)

    try:
        _run(["mkfs.btrfs", "-f", str(image)])
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"mkfs.btrfs failed: {exc.stderr.strip()}")

    try:
        _run(["mount", "-o", "loop", str(image), str(mount_point)])
    except subprocess.CalledProcessError as exc:
        pytest.skip(f"loop mount failed (no loop support?): {exc.stderr.strip()}")

    try:
        yield mount_point
    finally:
        subprocess.run(["umount", str(mount_point)], check=False, capture_output=True)
        shutil.rmtree(work, ignore_errors=True)


def _make_subvol(path):
    _run(["btrfs", "subvolume", "create", str(path)])


def _setup(mnt, name):
    """Create a unique per-test source subvolume and snapshots dir."""
    root = mnt / name
    root.mkdir()
    src = root / "src"
    snapshots = root / "snapshots"
    target = root / "target"
    _make_subvol(src)
    snapshots.mkdir()
    return src, snapshots, target


def test_full_snapshot_send_restore(btrfs_fs):
    mnt = btrfs_fs
    src, snapshots, target = _setup(mnt, "full")
    (src / "hello.txt").write_text("hello btrfs\n")

    snap = snapshots / "s1"
    snapshot.create_ro_snapshot(src, snap)

    sendfile = mnt / "full.send"
    send.send_snapshot(snap, None, sendfile)

    target.mkdir()
    send.restore_stream(sendfile, target)

    assert (target / "s1" / "hello.txt").read_text() == "hello btrfs\n"


def test_incremental_send_restore_chain(btrfs_fs):
    mnt = btrfs_fs
    src, snapshots, target = _setup(mnt, "incr")
    (src / "a.txt").write_text("a\n")

    snap1 = snapshots / "s1"
    snap2 = snapshots / "s2"
    snapshot.create_ro_snapshot(src, snap1)

    (src / "b.txt").write_text("b\n")
    snapshot.create_ro_snapshot(src, snap2)

    send1 = mnt / "incr1.send"
    send2 = mnt / "incr2.send"
    send.send_snapshot(snap1, None, send1)
    send.send_snapshot(snap2, snap1, send2)

    target.mkdir()
    send.restore_stream(send1, target)
    send.restore_stream(send2, target)

    restored = target / "s2"
    assert (restored / "a.txt").read_text() == "a\n"
    assert (restored / "b.txt").read_text() == "b\n"


def test_xz_compressed_roundtrip(btrfs_fs):
    mnt = btrfs_fs
    src, snapshots, target = _setup(mnt, "xz")
    (src / "data.txt").write_text("x" * 100000)

    snap = snapshots / "s1"
    snapshot.create_ro_snapshot(src, snap)

    compression = {"algorithm": "xz", "level": 6}
    sendfile = mnt / "xz.send"
    send.send_snapshot(snap, None, sendfile, compression=compression)

    target.mkdir()
    send.restore_stream(sendfile, target, compression=compression)

    assert (target / "s1" / "data.txt").read_text() == "x" * 100000


@pytest.mark.skipif(not _have("age-keygen"), reason="age-keygen not available")
def test_age_encrypted_roundtrip(btrfs_fs):
    mnt = btrfs_fs
    keyfile = mnt / "age.key"
    _run(["age-keygen", "-o", str(keyfile)])
    pubkey = next(
        line.split(": ", 1)[1]
        for line in keyfile.read_text().splitlines()
        if line.startswith("# public key: ")
    )

    src, snapshots, target = _setup(mnt, "age")
    (src / "secret.txt").write_text("sensitive data\n")

    snap = snapshots / "s1"
    snapshot.create_ro_snapshot(src, snap)

    compression = {"algorithm": "xz", "level": 6}
    encryption = {"algorithm": "age", "recipients": [pubkey], "identity": str(keyfile)}
    sendfile = mnt / "age.send"
    send.send_snapshot(snap, None, sendfile, compression=compression, encryption=encryption)

    target.mkdir()
    send.restore_stream(sendfile, target, compression=compression, encryption=encryption)

    assert (target / "s1" / "secret.txt").read_text() == "sensitive data\n"


def test_delete_snapshot(btrfs_fs):
    mnt = btrfs_fs
    src, snapshots, _target = _setup(mnt, "del")

    snap = snapshots / "s1"
    snapshot.create_ro_snapshot(src, snap)
    assert snap.exists()

    snapshot.delete_snapshot(snap)
    assert not snap.exists()
