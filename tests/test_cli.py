import os
from pathlib import Path

import pytest

import cli
import manifest
from config import Config, Profile, RemoteSpec


def _cfg(profiles):
    return Config(
        name="root",
        path=Path("/etc/btrbak/profiles.d/root.yaml"),
        src=Path("/mnt/data"),
        dest=Path("/mnt/data/.snapshots"),
        tmpdir=Path("/var/tmp/btrbak"),
        compression=None,
        encryption=None,
        profiles=profiles,
    )


def _profile(name, full, incr, keep, remotes=None):
    return Profile(
        name=name,
        freq_full=full,
        freq_incr=incr,
        keep=keep,
        remotes=remotes or [],
    )


def _remote(sid, created, parent=None, status="complete"):
    return {
        "id": sid,
        "created": created,
        "type": "incr" if parent else "full",
        "parent": parent,
        "uploads": [{"remote": "r", "status": status}],
    }


def _local(sid, created):
    return {"id": sid, "created": created, "type": "local", "parent": None, "uploads": []}


def test_first_run_creates_full():
    profile = _profile("daily", 7 * 86400, 86400, 30 * 86400, [RemoteSpec("r", "dir", {"path": "/x"})])
    cfg = _cfg({"daily": profile})
    due, stype, parent = cli.compute_plan(cfg, profile, {"profiles": {"daily": {"snapshots": []}}}, 5000)
    assert (due, stype, parent) == (True, "full", None)


def test_first_run_creates_full_when_full_never_but_incr_auto():
    profile = _profile("daily", -1, 86400, 30 * 86400, [RemoteSpec("r", "dir", {"path": "/x"})])
    cfg = _cfg({"daily": profile})
    due, stype, parent = cli.compute_plan(cfg, profile, {"profiles": {"daily": {"snapshots": []}}}, 5000)
    assert (due, stype, parent) == (True, "full", None)


def test_manual_remote_profile_not_due_on_first_run():
    profile = _profile("manual", -1, -1, 30 * 86400, [RemoteSpec("r", "dir", {"path": "/x"})])
    cfg = _cfg({"manual": profile})
    assert cli.compute_plan(cfg, profile, {"profiles": {"manual": {"snapshots": []}}}, 5000) == (False, None, None)


def test_full_due_after_window():
    profile = _profile("daily", 100, 86400, 30 * 86400, [RemoteSpec("r", "dir", {"path": "/x"})])
    cfg = _cfg({"daily": profile})
    meta = {"profiles": {"daily": {"snapshots": [_remote("f", 1000)]}}}
    due, stype, parent = cli.compute_plan(cfg, profile, meta, 5000)
    assert (due, stype, parent) == (True, "full", None)


def test_incremental_due():
    profile = _profile("daily", 7 * 86400, 100, 30 * 86400, [RemoteSpec("r", "dir", {"path": "/x"})])
    cfg = _cfg({"daily": profile})
    meta = {"profiles": {"daily": {"snapshots": [_remote("f", 4000)]}}}
    due, stype, parent = cli.compute_plan(cfg, profile, meta, 5000)
    assert (due, stype, parent) == (True, "incr", "f")


def test_nothing_due():
    profile = _profile("daily", 7 * 86400, 100, 30 * 86400, [RemoteSpec("r", "dir", {"path": "/x"})])
    cfg = _cfg({"daily": profile})
    meta = {"profiles": {"daily": {"snapshots": [_remote("f", 4990)]}}}
    due, stype, parent = cli.compute_plan(cfg, profile, meta, 5000)
    assert (due, stype, parent) == (False, None, None)


def test_force_creates_incremental_when_not_due():
    profile = _profile("daily", 7 * 86400, 100, 30 * 86400, [RemoteSpec("r", "dir", {"path": "/x"})])
    cfg = _cfg({"daily": profile})
    meta = {"profiles": {"daily": {"snapshots": [_remote("f", 4990)]}}}
    due, stype, parent = cli.compute_plan(cfg, profile, meta, 5000, force=True)
    assert (due, stype, parent) == (True, "incr", "f")


def test_full_flag_forces_full():
    profile = _profile("daily", 7 * 86400, 100, 30 * 86400, [RemoteSpec("r", "dir", {"path": "/x"})])
    cfg = _cfg({"daily": profile})
    meta = {"profiles": {"daily": {"snapshots": [_remote("f", 4990)]}}}
    due, stype, parent = cli.compute_plan(cfg, profile, meta, 5000, full=True)
    assert (due, stype, parent) == (True, "full", None)


def test_local_only_due_and_type():
    profile = _profile("local", 86400, -1, 14 * 86400)
    cfg = _cfg({"local": profile})
    assert cli.compute_plan(cfg, profile, {"profiles": {"local": {"snapshots": []}}}, 5000) == (
        True,
        "local",
        None,
    )

    meta = {"profiles": {"local": {"snapshots": [_local("a", 4000)]}}}
    assert cli.compute_plan(cfg, profile, meta, 5000) == (False, None, None)


def test_unique_snapshot_id(tmp_path):
    profile_dir = tmp_path / "daily"
    profile_dir.mkdir()
    assert cli.unique_snapshot_id("20251004T000000Z", profile_dir) == "20251004T000000Z"
    (profile_dir / "20251004T000000Z").mkdir()
    assert cli.unique_snapshot_id("20251004T000000Z", profile_dir) == "20251004T000000Z-1"


def test_run_profile_records_codec_per_snapshot(tmp_path, monkeypatch):
    profile = _profile(
        "daily",
        7 * 86400,
        86400,
        30 * 86400,
        [RemoteSpec("r", "dir", {"path": str(tmp_path / "remote")})],
    )
    cfg = _cfg({"daily": profile})
    cfg.dest = tmp_path / "snapshots"
    cfg.tmpdir = tmp_path / "tmp"
    cfg.compression = {"algorithm": "xz", "level": 6}
    cfg.encryption = {"algorithm": "age", "recipients": ["age1abc"], "identity": "/key"}

    sent = {}

    def fake_send(snapshot, parent, out_path, compression=None, encryption=None):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"data")
        sent["compression"] = compression
        sent["encryption"] = encryption

    monkeypatch.setattr(cli.snapshot, "create_ro_snapshot", lambda src, dest: None)
    monkeypatch.setattr(cli.send, "send_snapshot", fake_send)
    monkeypatch.setattr(cli.util, "sha256_file", lambda path: "abc123")
    monkeypatch.setattr(cli.util, "now", lambda: 5000)
    monkeypatch.setattr(cli.util, "snapshot_id", lambda ts=None: "20250101T000000Z")

    class FakeRemote:
        def write(self, local_src, remote_path):
            pass

    remotes = [(RemoteSpec("r", "dir", {"path": str(tmp_path / "remote")}), FakeRemote())]
    meta = {"profiles": {}}

    cli.run_profile(cfg, profile, meta, remotes, force=False, full=False)

    snaps = meta["profiles"]["daily"]["snapshots"]
    assert len(snaps) == 1
    assert snaps[0]["compression"] == {"algorithm": "xz", "level": 6}
    assert snaps[0]["encryption"] == {
        "algorithm": "age",
        "recipients": ["age1abc"],
        "identity": "/key",
    }
    assert sent["compression"] == {"algorithm": "xz", "level": 6}
    assert sent["encryption"] == cfg.encryption


def test_reconcile_uploads_drops_removed_remote():
    snap = {"uploads": [
        {"remote": "kept", "status": "complete"},
        {"remote": "removed", "status": "failed"},
    ]}
    cli.reconcile_uploads(snap, {"kept"})
    assert snap["uploads"] == [{"remote": "kept", "status": "complete"}]


def test_reconcile_uploads_dedupes():
    snap = {"uploads": [
        {"remote": "r", "status": "failed"},
        {"remote": "r", "status": "complete"},
    ]}
    cli.reconcile_uploads(snap, {"r"})
    assert snap["uploads"] == [{"remote": "r", "status": "complete"}]


def test_reconcile_uploads_noop_when_current():
    uploads = [{"remote": "r", "status": "complete"}]
    snap = {"uploads": list(uploads)}
    cli.reconcile_uploads(snap, {"r"})
    assert snap["uploads"] == uploads


def test_local_only_manual_profile_not_due_on_first_run():
    profile = _profile("manual", -1, -1, 30 * 86400)
    cfg = _cfg({"manual": profile})
    assert cli.compute_plan(
        cfg, profile, {"profiles": {"manual": {"snapshots": []}}}, 5000
    ) == (False, None, None)


def test_collect_remotes_distinguishes_same_name_different_settings():
    cfg = _cfg(
        {
            "a": _profile("a", 86400, -1, 30 * 86400, [RemoteSpec("offsite", "dir", {"path": "/remoteA"})]),
            "b": _profile("b", 86400, -1, 30 * 86400, [RemoteSpec("offsite", "dir", {"path": "/remoteB"})]),
        }
    )
    by_profile = cli.collect_remotes(cfg)
    assert by_profile["a"][0][1].settings["path"] == "/remoteA"
    assert by_profile["b"][0][1].settings["path"] == "/remoteB"


def test_unique_remotes_deduplicates_identical_endpoints():
    cfg = _cfg(
        {
            "a": _profile("a", 86400, -1, 30 * 86400, [RemoteSpec("offsite", "dir", {"path": "/remote"})]),
            "b": _profile("b", 86400, -1, 30 * 86400, [RemoteSpec("offsite", "dir", {"path": "/remote"})]),
        }
    )
    unique = cli.unique_remotes(cli.collect_remotes(cfg))
    assert len(unique) == 1


class _FakeRemote:
    def __init__(self, data=b"data"):
        self.data = data
        self.writes = []

    def read(self, remote_path, local_dest):
        local_dest.write_bytes(self.data)

    def write(self, local_src, remote_path):
        self.writes.append(remote_path)


def test_retry_upload_reuses_complete_copy(tmp_path, monkeypatch):
    cfg = _cfg({"p": _profile("p", 86400, -1, 30 * 86400)})
    cfg.dest = tmp_path / "snapshots"
    cfg.tmpdir = tmp_path / "tmp"

    snap = {
        "id": "s1",
        "type": "full",
        "parent": None,
        "file": "p/s1.send",
        "sha256": "deadbeef",
        "size": 4,
        "uploads": [
            {"remote": "a", "status": "complete"},
            {"remote": "b", "status": "failed"},
        ],
    }
    profile = cfg.profiles["p"]
    spec_a = RemoteSpec("a", "dir", {"path": "/a"})
    spec_b = RemoteSpec("b", "dir", {"path": "/b"})
    remote_a = _FakeRemote(b"data")
    remote_b = _FakeRemote(b"data")

    monkeypatch.setattr(cli.util, "sha256_file", lambda path: "deadbeef")

    failed = cli.retry_upload(cfg, profile, snap, [(spec_a, remote_a), (spec_b, remote_b)])

    assert failed == 0
    assert remote_b.writes == ["p/s1.send"]
    assert remote_a.writes == []
    assert snap["sha256"] == "deadbeef"
    assert snap["uploads"][1]["status"] == "complete"


def test_retry_upload_resends_when_no_complete_copy(tmp_path, monkeypatch):
    cfg = _cfg({"p": _profile("p", 86400, -1, 30 * 86400)})
    cfg.dest = tmp_path / "snapshots"
    cfg.tmpdir = tmp_path / "tmp"
    (cfg.dest / "p" / "s1").mkdir(parents=True)

    snap = {
        "id": "s1",
        "type": "full",
        "parent": None,
        "file": "p/s1.send",
        "sha256": None,
        "size": None,
        "uploads": [
            {"remote": "a", "status": "failed"},
            {"remote": "b", "status": "failed"},
        ],
    }
    profile = cfg.profiles["p"]
    spec_a = RemoteSpec("a", "dir", {"path": "/a"})
    spec_b = RemoteSpec("b", "dir", {"path": "/b"})
    remote_a = _FakeRemote()
    remote_b = _FakeRemote()

    def fake_send(snapshot, parent, out_path, compression=None, encryption=None):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"newdata")

    monkeypatch.setattr(cli.send, "send_snapshot", fake_send)
    monkeypatch.setattr(cli.util, "sha256_file", lambda path: "newhash")

    failed = cli.retry_upload(cfg, profile, snap, [(spec_a, remote_a), (spec_b, remote_b)])

    assert failed == 0
    assert remote_a.writes == ["p/s1.send"]
    assert remote_b.writes == ["p/s1.send"]
    assert snap["sha256"] == "newhash"
    assert snap["size"] == len(b"newdata")


def test_full_flag_ignored_for_local_only():
    profile = _profile("local", 86400, -1, 14 * 86400)
    cfg = _cfg({"local": profile})
    meta = {"profiles": {"local": {"snapshots": [_local("a", 4990)]}}}
    assert cli.compute_plan(cfg, profile, meta, 5000, full=True) == (False, None, None)
    assert cli.compute_plan(cfg, profile, meta, 5000, force=True) == (True, "local", None)


def test_reconcile_uploads_marks_committed_when_all_remotes_removed():
    snap = {
        "type": "full",
        "uploads": [
            {"remote": "removed1", "status": "complete"},
            {"remote": "removed2", "status": "failed"},
        ],
    }
    cli.reconcile_uploads(snap, set())
    assert snap["uploads"] == []
    assert snap["committed"] is True
    assert manifest.committed(snap)


def test_clean_tmpdir_removes_empty_dirs(tmp_path, monkeypatch):
    cfg = _cfg({})
    cfg.tmpdir = tmp_path / "tmp"
    root = cfg.tmpdir / cfg.name
    (root / "a" / "b").mkdir(parents=True)
    old = root / "a" / "old.txt"
    old.write_text("x")
    os.utime(old, (5000, 5000))
    monkeypatch.setattr(cli.util, "now", lambda: 59000)

    cli.clean_tmpdir(cfg, max_age=100)

    assert not old.exists()
    assert not (root / "a").exists()


def test_sync_settings_conflict_is_runtime_error(tmp_path):
    cfg = _cfg({})
    cfg.tmpdir = tmp_path / "tmp"
    cfg.path = tmp_path / "root.yaml"
    cfg.path.write_text("src: /x\n")

    class ConflictRemote:
        def read(self, remote_path, local_dest):
            local_dest.write_bytes(b"different")

        def write(self, local_src, remote_path):
            pass

    with pytest.raises(cli.util.BtrbakError):
        cli.sync_settings(
            cfg,
            [(RemoteSpec("r", "dir", {"path": "/x"}), ConflictRemote())],
            force_config=False,
        )
