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

import cli
import manifest
import restore
import send
import snapshot
from config import Config, Profile, RemoteSpec

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


def test_full_pipeline_backup_restore_and_prune(btrfs_fs, monkeypatch):
    mnt = btrfs_fs
    src, snapshots, target = _setup(mnt, "pipeline")
    remote_root = mnt / "pipeline" / "remote"
    tmpdir = mnt / "pipeline" / "tmp"
    remote_root.mkdir()
    tmpdir.mkdir()

    cfg_path = mnt / "pipeline" / "root.yaml"
    cfg_path.write_text(
        f"src: {src}\n"
        f"dest: {snapshots}\n"
        f"tmpdir: {tmpdir}\n"
        "profiles:\n"
        "  daily:\n"
        "    freq:\n"
        "      full: 7d\n"
        "      incr: 1d\n"
        "    keep: 100s\n"
        "    remotes:\n"
        "      - name: offsite\n"
        "        type: dir\n"
        f"        path: {remote_root}\n"
    )

    cfg = Config(
        name="pipeline",
        path=cfg_path,
        src=src,
        dest=snapshots,
        tmpdir=tmpdir,
        compression=None,
        encryption=None,
        profiles={
            "daily": Profile(
                name="daily",
                freq_full=7 * 86400,
                freq_incr=86400,
                keep=100,
                remotes=[
                    RemoteSpec(
                        "offsite",
                        "dir",
                        {"type": "dir", "path": str(remote_root)},
                    )
                ],
            )
        },
    )

    (src / "a.txt").write_text("a\n")
    monkeypatch.setattr(cli.util, "now", lambda: 0)
    assert cli.run_config(cfg, None, force=True, force_config=False, full=False, dry_run=False) == 0

    meta = manifest.load(snapshots / "meta.yaml")
    snaps = manifest.snapshots(meta, "daily")
    assert len(snaps) == 1
    full_id = snaps[0]["id"]
    assert snaps[0]["type"] == "full"
    assert (remote_root / "daily" / f"{full_id}.send").exists()

    (src / "b.txt").write_text("b\n")
    monkeypatch.setattr(cli.util, "now", lambda: 90)
    assert cli.run_config(cfg, None, force=True, force_config=False, full=False, dry_run=False) == 0

    meta = manifest.load(snapshots / "meta.yaml")
    snaps = manifest.snapshots(meta, "daily")
    assert len(snaps) == 2
    incr = snaps[1]
    assert incr["type"] == "incr"
    assert incr["parent"] == full_id
    incr_id = incr["id"]

    restore.run_restore(cfg, "daily", incr_id, target)
    assert (target / incr_id / "a.txt").read_text() == "a\n"
    assert (target / incr_id / "b.txt").read_text() == "b\n"

    # A young child must protect its old full parent from pruning.
    meta = manifest.load(snapshots / "meta.yaml")
    by_profile = cli.collect_remotes(cfg)
    monkeypatch.setattr(cli.util, "now", lambda: 150)
    cli.prune(cfg, meta, by_profile)
    remaining = [snap["id"] for snap in manifest.snapshots(meta, "daily")]
    assert full_id in remaining and incr_id in remaining

    # Once both are past retention, pruning cascades leaf-first.
    monkeypatch.setattr(cli.util, "now", lambda: 1000)
    cli.prune(cfg, meta, by_profile)
    assert manifest.snapshots(meta, "daily") == []
    assert not (snapshots / "daily" / full_id).exists()
    assert not (snapshots / "daily" / incr_id).exists()
    assert not (remote_root / "daily" / f"{full_id}.send").exists()
    assert not (remote_root / "daily" / f"{incr_id}.send").exists()
