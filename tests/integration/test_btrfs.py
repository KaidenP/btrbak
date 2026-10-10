"""Integration tests against a real (loopback) btrfs filesystem.

These tests create a small btrfs filesystem in a raw file, mount it, and
exercise snapshot / send / restore end to end. They are skipped unless running
as root and btrfs tooling is available.
"""

import os
import shutil
import subprocess
from dataclasses import replace

import pytest
import yaml

from btrbak import cli
from btrbak import manifest
from btrbak import restore
from btrbak import send
from btrbak import snapshot
from btrbak import util
from btrbak.config import Config, Profile, RemoteSpec

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


def _pipeline_cfg(mnt, name, src, snapshots, remote_root, tmpdir):
    """Write a single-profile config and return the matching Config object."""
    cfg_path = mnt / name / "root.yaml"
    cfg_path.write_text(
        f"src: {src}\n"
        f"dest: {snapshots}\n"
        f"tmpdir: {tmpdir}\n"
        "profiles:\n"
        "  daily:\n"
        "    freq:\n"
        "      full: 7d\n"
        "      incr: 1d\n"
        "    keep: 1\n"
        "    remotes:\n"
        "      - name: offsite\n"
        "        type: dir\n"
        f"        path: {remote_root}\n"
    )
    return Config(
        name=name,
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
                keep=1,
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


def test_full_pipeline_backup_restore_and_prune(btrfs_fs, monkeypatch):
    mnt = btrfs_fs
    src, snapshots, target = _setup(mnt, "pipeline")
    remote_root = mnt / "pipeline" / "remote"
    tmpdir = mnt / "pipeline" / "tmp"
    remote_root.mkdir()
    tmpdir.mkdir()

    cfg = _pipeline_cfg(mnt, "pipeline", src, snapshots, remote_root, tmpdir)

    (src / "a.txt").write_text("a\n")
    monkeypatch.setattr(cli.util, "now", lambda: 50)
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

    # Re-root the chain with a new full. keep=1 retains only the newest
    # snapshot (plus any parents it needs), so the superseded full1 -> incr
    # branch is pruned leaf-first in the same run.
    (src / "c.txt").write_text("c\n")
    monkeypatch.setattr(cli.util, "now", lambda: 150)
    assert cli.run_config(cfg, None, force=True, force_config=False, full=True, dry_run=False) == 0

    meta = manifest.load(snapshots / "meta.yaml")
    snaps = manifest.snapshots(meta, "daily")
    assert len(snaps) == 1
    full2_id = snaps[0]["id"]
    assert snaps[0]["type"] == "full"
    assert (snapshots / "daily" / full2_id).exists()
    assert not (snapshots / "daily" / full_id).exists()
    assert not (snapshots / "daily" / incr_id).exists()
    assert (remote_root / "daily" / f"{full2_id}.send").exists()
    assert not (remote_root / "daily" / f"{full_id}.send").exists()
    assert not (remote_root / "daily" / f"{incr_id}.send").exists()


def test_restore_resumes_after_partial_failure(btrfs_fs, monkeypatch):
    """An interrupted restore resumes instead of failing with 'File exists'."""
    mnt = btrfs_fs
    src, snapshots, target = _setup(mnt, "resume")
    remote_root = mnt / "resume" / "remote"
    tmpdir = mnt / "resume" / "tmp"
    remote_root.mkdir()
    tmpdir.mkdir()
    cfg = _pipeline_cfg(mnt, "resume", src, snapshots, remote_root, tmpdir)

    (src / "a.txt").write_text("a\n")
    monkeypatch.setattr(cli.util, "now", lambda: 0)
    cli.run_config(cfg, None, force=True, force_config=False, full=False, dry_run=False)
    full_id = manifest.snapshots(manifest.load(snapshots / "meta.yaml"), "daily")[0]["id"]

    (src / "b.txt").write_text("b\n")
    monkeypatch.setattr(cli.util, "now", lambda: 60)
    cli.run_config(cfg, None, force=True, force_config=False, full=False, dry_run=False)
    incr_id = manifest.snapshots(manifest.load(snapshots / "meta.yaml"), "daily")[1]["id"]

    # Interrupt the restore after the first link has been received.
    calls = []
    real_stream = send.restore_stream

    def failing_stream(send_file, tgt, compression=None, encryption=None):
        real_stream(send_file, tgt, compression, encryption)
        calls.append(send_file)
        if len(calls) == 1:
            raise RuntimeError("interrupted")

    monkeypatch.setattr(send, "restore_stream", failing_stream)
    with pytest.raises(RuntimeError, match="interrupted"):
        restore.run_restore(cfg, "daily", incr_id, target)

    assert (target / full_id).exists()
    assert not (target / incr_id).exists()

    # Re-running skips the already-received link and completes the chain.
    monkeypatch.setattr(send, "restore_stream", real_stream)
    restore.run_restore(cfg, "daily", incr_id, target)

    assert (target / incr_id / "a.txt").read_text() == "a\n"
    assert (target / incr_id / "b.txt").read_text() == "b\n"


def test_restore_rejects_foreign_entry_in_target(btrfs_fs, monkeypatch):
    """A plain directory where a received subvolume belongs blocks the restore."""
    mnt = btrfs_fs
    src, snapshots, target = _setup(mnt, "foreign")
    remote_root = mnt / "foreign" / "remote"
    tmpdir = mnt / "foreign" / "tmp"
    remote_root.mkdir()
    tmpdir.mkdir()
    cfg = _pipeline_cfg(mnt, "foreign", src, snapshots, remote_root, tmpdir)

    (src / "a.txt").write_text("a\n")
    monkeypatch.setattr(cli.util, "now", lambda: 0)
    cli.run_config(cfg, None, force=True, force_config=False, full=False, dry_run=False)
    snap_id = manifest.snapshots(manifest.load(snapshots / "meta.yaml"), "daily")[0]["id"]

    target.mkdir()
    (target / snap_id).mkdir()

    with pytest.raises(restore.BtrbakError, match="not a btrfs subvolume"):
        restore.run_restore(cfg, "daily", snap_id, target)


def test_restore_rejects_foreign_subvolume_in_target(btrfs_fs, monkeypatch):
    """A same-named subvolume that is not the snapshot must not be skipped.

    Treating it as an already-received link would replay the rest of the chain
    on top of the wrong base and exit 0 with silently wrong data.
    """
    mnt = btrfs_fs
    src, snapshots, target = _setup(mnt, "decoy")
    remote_root = mnt / "decoy" / "remote"
    tmpdir = mnt / "decoy" / "tmp"
    remote_root.mkdir()
    tmpdir.mkdir()
    cfg = _pipeline_cfg(mnt, "decoy", src, snapshots, remote_root, tmpdir)

    (src / "a.txt").write_text("a\n")
    monkeypatch.setattr(cli.util, "now", lambda: 0)
    cli.run_config(cfg, None, force=True, force_config=False, full=False, dry_run=False)
    full_id = manifest.snapshots(manifest.load(snapshots / "meta.yaml"), "daily")[0]["id"]

    # An unrelated subvolume squatting on the chain link's name.
    decoy = mnt / "decoy" / "decoy"
    target.mkdir()
    _run(["btrfs", "subvolume", "create", str(decoy)])
    _run(["btrfs", "subvolume", "set-default", str(decoy)])
    (decoy / "decoy.txt").write_text("not the real data\n")
    _run(["btrfs", "subvolume", "snapshot", "-r", str(decoy), str(target / full_id)])

    # The manifest records the sent identity, which a locally-created decoy
    # can never match.
    assert util.subvolume_uuid(snapshots / "daily" / full_id)
    assert util.subvolume_uuid(decoy) != util.subvolume_uuid(snapshots / "daily" / full_id)

    with pytest.raises(restore.BtrbakError, match="remove it before restoring"):
        restore.run_restore(cfg, "daily", full_id, target)


def test_restore_resumes_into_a_recovered_target(btrfs_fs, monkeypatch):
    """A link received by an earlier restore matches the recorded UUID.

    ``btrfs receive`` assigns the recovered subvolume a fresh UUID of its own
    and keeps the sent one as ``Received UUID``, so a resumed chain has to
    compare against the latter.
    """
    mnt = btrfs_fs
    src, snapshots, target = _setup(mnt, "recovered")
    remote_root = mnt / "recovered" / "remote"
    tmpdir = mnt / "recovered" / "tmp"
    remote_root.mkdir()
    tmpdir.mkdir()
    cfg = _pipeline_cfg(mnt, "recovered", src, snapshots, remote_root, tmpdir)

    (src / "a.txt").write_text("a\n")
    monkeypatch.setattr(cli.util, "now", lambda: 0)
    cli.run_config(cfg, None, force=True, force_config=False, full=False, dry_run=False)
    full_id = manifest.snapshots(manifest.load(snapshots / "meta.yaml"), "daily")[0]["id"]

    restore.run_restore(cfg, "daily", full_id, target)
    assert (target / full_id / "a.txt").read_text() == "a\n"

    # Re-running over the already-received link is a no-op, not a failure.
    restore.run_restore(cfg, "daily", full_id, target)
    assert (target / full_id / "a.txt").read_text() == "a\n"
    assert not (cfg.tmpdir / cfg.name / util.RESTORE_SCRATCH).exists()


def test_restore_rejects_a_corrupt_stream_without_creating_the_target(
    btrfs_fs, monkeypatch
):
    """Integrity is checked before the target exists, so nothing is left behind.

    Everything else in the chain is validated from the manifest up front; the
    stream's own checksum can only be checked by fetching it, and discovering
    that it is bad must not leave an empty target directory behind.
    """
    mnt = btrfs_fs
    src, snapshots, _target = _setup(mnt, "corrupt")
    remote_root = mnt / "corrupt" / "remote"
    tmpdir = mnt / "corrupt" / "tmp"
    remote_root.mkdir()
    tmpdir.mkdir()
    cfg = _pipeline_cfg(mnt, "corrupt", src, snapshots, remote_root, tmpdir)

    (src / "a.txt").write_text("a\n")
    monkeypatch.setattr(cli.util, "now", lambda: 0)
    cli.run_config(cfg, None, force=True, force_config=False, full=False, dry_run=False)
    snap_id = manifest.snapshots(manifest.load(snapshots / "meta.yaml"), "daily")[0]["id"]

    send_file = remote_root / "daily" / f"{snap_id}.send"
    send_file.write_bytes(send_file.read_bytes() + b"tampered")

    target = mnt / "corrupt" / "fresh-target"
    with pytest.raises(restore.BtrbakError, match="checksum mismatch"):
        restore.run_restore(cfg, "daily", snap_id, target)

    assert not target.exists()


def test_verify_reports_a_snapshot_whose_remotes_were_all_removed(btrfs_fs, monkeypatch):
    """A profile that loses its remotes settles into a state verify must accept.

    Reconcile marks the orphaned entry `committed: true` so retention prunes
    it like any committed snapshot; verify flagging it INCOMPLETE would make
    every run exit 1 until retention prunes it.
    """
    mnt = btrfs_fs
    src, snapshots, _target = _setup(mnt, "orphaned")
    remote_root = mnt / "orphaned" / "remote"
    tmpdir = mnt / "orphaned" / "tmp"
    remote_root.mkdir()
    tmpdir.mkdir()
    cfg = _pipeline_cfg(mnt, "orphaned", src, snapshots, remote_root, tmpdir)

    (src / "a.txt").write_text("a\n")
    monkeypatch.setattr(cli.util, "now", lambda: 0)
    cli.run_config(cfg, None, force=True, force_config=False, full=False, dry_run=False)

    # The remote is removed from the profile; the snapshot keeps no uploads.
    local_only = replace(
        cfg, profiles={"daily": replace(cfg.profiles["daily"], remotes=[])}
    )
    monkeypatch.setattr(cli.util, "now", lambda: 10)
    cli.run_config(local_only, None, False, False, False, dry_run=False)

    meta = manifest.load(snapshots / "meta.yaml")
    orphaned = manifest.snapshots(meta, "daily")[0]
    assert orphaned["uploads"] == []
    assert orphaned["committed"] is True

    assert cli._verify_config(cfg) == 0


def test_verify_leaves_no_staging_directory(btrfs_fs, monkeypatch):
    mnt = btrfs_fs
    src, snapshots, _target = _setup(mnt, "verify_tmp")
    remote_root = mnt / "verify_tmp" / "remote"
    tmpdir = mnt / "verify_tmp" / "tmp"
    remote_root.mkdir()
    tmpdir.mkdir()
    cfg = _pipeline_cfg(mnt, "verify_tmp", src, snapshots, remote_root, tmpdir)

    (src / "a.txt").write_text("a\n")
    monkeypatch.setattr(cli.util, "now", lambda: 0)
    cli.run_config(cfg, None, force=True, force_config=False, full=False, dry_run=False)

    assert cli._verify_config(cfg) == 0
    assert not (cfg.tmpdir / cfg.name / util.VERIFY_SCRATCH).exists()


def test_restore_rejects_a_hand_edited_codec_without_a_traceback(btrfs_fs, monkeypatch):
    """A malformed `compression` in meta.yaml must be a clean error.

    meta.yaml is untrusted input -- the disaster-recovery path downloads it
    from a remote -- and its per-snapshot codec is fed straight into the xz
    decoder. A scalar where a mapping belongs used to raise AttributeError out
    of the CLI's top level and print a traceback.
    """
    mnt = btrfs_fs
    src, snapshots, target = _setup(mnt, "codec")
    remote_root = mnt / "codec" / "remote"
    tmpdir = mnt / "codec" / "tmp"
    remote_root.mkdir()
    tmpdir.mkdir()
    cfg = _pipeline_cfg(mnt, "codec", src, snapshots, remote_root, tmpdir)

    (src / "a.txt").write_text("a\n")
    monkeypatch.setattr(cli.util, "now", lambda: 0)
    cli.run_config(cfg, None, force=True, force_config=False, full=False, dry_run=False)
    snap_id = manifest.snapshots(manifest.load(snapshots / "meta.yaml"), "daily")[0]["id"]

    meta = manifest.load(snapshots / "meta.yaml")
    manifest.snapshots(meta, "daily")[0]["compression"] = "xz"
    # Corrupt BOTH copies: with a valid remote to fall back to (BTR-034),
    # restore would succeed, but the untrusted remote manifest itself must
    # still surface as a clean one-line error, not a traceback.
    for manifest_path in (snapshots / "meta.yaml", remote_root / "meta.yaml"):
        with open(manifest_path, "w") as handle:
            yaml.safe_dump(meta, handle)

    with pytest.raises(util.BtrbakError, match="'compression' must be a mapping"):
        restore.run_restore(cfg, "daily", snap_id, target)
    assert not target.exists()


def test_restore_reports_a_truncated_xz_stream_as_a_clean_error(btrfs_fs, monkeypatch):
    """A corrupt send stream must not surface as a raw LZMAError/EOFError."""
    mnt = btrfs_fs
    src, snapshots, target = _setup(mnt, "truncated")
    remote_root = mnt / "truncated" / "remote"
    tmpdir = mnt / "truncated" / "tmp"
    remote_root.mkdir()
    tmpdir.mkdir()

    cfg = replace(
        _pipeline_cfg(mnt, "truncated", src, snapshots, remote_root, tmpdir),
        compression={"algorithm": "xz", "level": 1},
    )
    (src / "a.txt").write_text("a\n")
    monkeypatch.setattr(cli.util, "now", lambda: 0)
    cli.run_config(cfg, None, force=True, force_config=False, full=False, dry_run=False)
    meta = manifest.load(snapshots / "meta.yaml")
    snap = manifest.snapshots(meta, "daily")[0]

    # Publish a truncated object *and* record it faithfully, so the manifest
    # checksum check passes and the decoder is what rejects it.
    send_file = remote_root / snap["file"]
    send_file.write_bytes(send_file.read_bytes()[: len(send_file.read_bytes()) // 2])
    snap["sha256"] = util.sha256_file(send_file)
    snap["size"] = send_file.stat().st_size
    with open(snapshots / "meta.yaml", "w") as handle:
        yaml.safe_dump(meta, handle)

    with pytest.raises(util.BtrbakError, match="xz (decompression failed|stream is truncated)"):
        restore.run_restore(cfg, "daily", snap["id"], target)


def test_verify_reports_a_profile_removed_from_the_config(btrfs_fs, monkeypatch, capsys):
    """Deleting a profile must not strand its snapshots in silence."""
    mnt = btrfs_fs
    src, snapshots, _target = _setup(mnt, "retired")
    remote_root = mnt / "retired" / "remote"
    tmpdir = mnt / "retired" / "tmp"
    remote_root.mkdir()
    tmpdir.mkdir()
    cfg = _pipeline_cfg(mnt, "retired", src, snapshots, remote_root, tmpdir)

    (src / "a.txt").write_text("a\n")
    monkeypatch.setattr(cli.util, "now", lambda: 0)
    cli.run_config(cfg, None, force=True, force_config=False, full=False, dry_run=False)
    assert cli._verify_config(cfg) == 0

    # The profile disappears from the config; its snapshot, its offsite object
    # and its manifest entry all stay put.
    without = replace(cfg, profiles={})
    assert manifest.snapshots(manifest.load(snapshots / "meta.yaml"), "daily")
    assert (remote_root / "daily").exists()

    assert cli._verify_config(without, configured=set()) == 0
    out = capsys.readouterr().out
    assert "ORPHANED PROFILE retired/daily" in out
    assert "no longer configured" in out
    assert "1 orphaned profile(s)" in out
