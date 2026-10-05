import os
from pathlib import Path
from types import SimpleNamespace

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
    due, stype, parent = cli.compute_plan(profile, {"profiles": {"daily": {"snapshots": []}}}, 5000)
    assert (due, stype, parent) == (True, "full", None)


def test_first_run_creates_full_when_full_never_but_incr_auto():
    profile = _profile("daily", -1, 86400, 30 * 86400, [RemoteSpec("r", "dir", {"path": "/x"})])
    due, stype, parent = cli.compute_plan(profile, {"profiles": {"daily": {"snapshots": []}}}, 5000)
    assert (due, stype, parent) == (True, "full", None)


def test_manual_remote_profile_not_due_on_first_run():
    profile = _profile("manual", -1, -1, 30 * 86400, [RemoteSpec("r", "dir", {"path": "/x"})])
    assert cli.compute_plan(profile, {"profiles": {"manual": {"snapshots": []}}}, 5000) == (False, None, None)


def test_full_due_after_window():
    profile = _profile("daily", 100, 86400, 30 * 86400, [RemoteSpec("r", "dir", {"path": "/x"})])
    meta = {"profiles": {"daily": {"snapshots": [_remote("f", 1000)]}}}
    due, stype, parent = cli.compute_plan(profile, meta, 5000)
    assert (due, stype, parent) == (True, "full", None)


def test_incremental_due():
    profile = _profile("daily", 7 * 86400, 100, 30 * 86400, [RemoteSpec("r", "dir", {"path": "/x"})])
    meta = {"profiles": {"daily": {"snapshots": [_remote("f", 4000)]}}}
    due, stype, parent = cli.compute_plan(profile, meta, 5000)
    assert (due, stype, parent) == (True, "incr", "f")


def test_nothing_due():
    profile = _profile("daily", 7 * 86400, 100, 30 * 86400, [RemoteSpec("r", "dir", {"path": "/x"})])
    meta = {"profiles": {"daily": {"snapshots": [_remote("f", 4990)]}}}
    due, stype, parent = cli.compute_plan(profile, meta, 5000)
    assert (due, stype, parent) == (False, None, None)


def test_force_creates_incremental_when_not_due():
    profile = _profile("daily", 7 * 86400, 100, 30 * 86400, [RemoteSpec("r", "dir", {"path": "/x"})])
    meta = {"profiles": {"daily": {"snapshots": [_remote("f", 4990)]}}}
    due, stype, parent = cli.compute_plan(profile, meta, 5000, force=True)
    assert (due, stype, parent) == (True, "incr", "f")


def test_full_flag_forces_full():
    profile = _profile("daily", 7 * 86400, 100, 30 * 86400, [RemoteSpec("r", "dir", {"path": "/x"})])
    meta = {"profiles": {"daily": {"snapshots": [_remote("f", 4990)]}}}
    due, stype, parent = cli.compute_plan(profile, meta, 5000, full=True)
    assert (due, stype, parent) == (True, "full", None)


def test_local_only_due_and_type():
    profile = _profile("local", 86400, -1, 14 * 86400)
    assert cli.compute_plan(
        profile, {"profiles": {"local": {"snapshots": []}}}, 5000) == (
        True,
        "local",
        None,
    )

    meta = {"profiles": {"local": {"snapshots": [_local("a", 4000)]}}}
    assert cli.compute_plan(profile, meta, 5000) == (False, None, None)


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
    assert cli.compute_plan(
        profile, {"profiles": {"manual": {"snapshots": []}}}, 5000
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
    meta = {"profiles": {"local": {"snapshots": [_local("a", 4990)]}}}
    assert cli.compute_plan(profile, meta, 5000, full=True) == (False, None, None)
    assert cli.compute_plan(profile, meta, 5000, force=True) == (True, "local", None)


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


def test_reconcile_uploads_empty_uploads_no_remotes_marks_committed():
    snap = {"type": "full", "uploads": []}
    cli.reconcile_uploads(snap, set())
    assert snap["committed"] is True
    assert manifest.committed(snap)


def test_reconcile_uploads_empty_uploads_with_remotes_not_committed():
    snap = {"type": "full", "uploads": []}
    cli.reconcile_uploads(snap, {"r"})
    assert "committed" not in snap
    assert not manifest.committed(snap)


def test_reconcile_uploads_backfills_new_remote():
    snap = {"type": "full", "uploads": [{"remote": "r", "status": "complete"}]}
    cli.reconcile_uploads(snap, {"r", "r2"})
    assert snap["uploads"] == [
        {"remote": "r", "status": "complete"},
        {"remote": "r2", "status": "failed"},
    ]
    assert not manifest.committed(snap)


def test_reconcile_uploads_backfills_after_all_remotes_removed():
    snap = {"type": "full", "committed": True, "uploads": []}
    cli.reconcile_uploads(snap, {"r"})
    assert snap["uploads"] == [{"remote": "r", "status": "failed"}]
    assert "committed" not in snap
    assert not manifest.committed(snap)


def test_reconcile_uploads_does_not_backfill_local_deleted():
    snap = {
        "type": "full",
        "local_deleted": True,
        "uploads": [{"remote": "r", "status": "complete"}],
    }
    cli.reconcile_uploads(snap, {"r", "r2"})
    assert snap["uploads"] == [{"remote": "r", "status": "complete"}]
    assert manifest.committed(snap)


def test_cmd_run_continues_after_config_error(monkeypatch, capsys):
    cfg_bad = _cfg({"daily": _profile("daily", 86400, -1, 30 * 86400)})
    cfg_good = _cfg({"daily": _profile("daily", 86400, -1, 30 * 86400)})
    monkeypatch.setattr(
        cli.config_mod, "discover_configs", lambda subvol=None: [cfg_bad, cfg_good]
    )
    ran = []

    def fake_run_config(cfg, *args, **kwargs):
        ran.append(cfg)
        if cfg is cfg_bad:
            raise cli.config_mod.ConfigError("bad")
        return 0

    monkeypatch.setattr(cli, "run_config", fake_run_config)
    args = SimpleNamespace(
        subvol=None,
        profile=None,
        force=False,
        force_config=False,
        full=False,
        dry_run=False,
    )

    assert cli.cmd_run(args) == 2
    assert ran == [cfg_bad, cfg_good]
    assert "config error" in capsys.readouterr().err


def test_prune_marks_local_deleted_when_remote_delete_fails(tmp_path, monkeypatch):
    profile = _profile(
        "daily", 7 * 86400, 86400, 1, [RemoteSpec("r", "dir", {"path": "/x"})]
    )
    cfg = _cfg({"daily": profile})
    cfg.dest = tmp_path / "snapshots"
    cfg.tmpdir = tmp_path / "tmp"
    meta = {
        "profiles": {
            "daily": {
                "snapshots": [
                    {
                        "id": "s1",
                        "created": 1000,
                        "type": "full",
                        "parent": None,
                        "file": "daily/s1.send",
                        "uploads": [{"remote": "r", "status": "complete"}],
                    }
                ]
            }
        }
    }

    monkeypatch.setattr(cli.util, "now", lambda: 5000)
    (cfg.dest / "daily" / "s1").mkdir(parents=True)
    deleted = []
    monkeypatch.setattr(cli.snapshot, "delete_snapshot", lambda path: deleted.append(path))

    class FailRemote:
        def delete(self, remote_path):
            raise cli.util.BtrbakError("nope")

    by_profile = {
        "daily": [(RemoteSpec("r", "dir", {"path": "/x"}), FailRemote())]
    }

    cli.prune(cfg, meta, by_profile)

    assert len(deleted) == 1
    assert meta["profiles"]["daily"]["snapshots"][0]["local_deleted"] is True
    assert manifest.last_committed(meta, "daily") is None


def test_prune_persists_progress_when_local_delete_fails(tmp_path, monkeypatch):
    profile = _profile(
        "daily", 7 * 86400, 86400, 1, [RemoteSpec("r", "dir", {"path": "/x"})]
    )
    cfg = _cfg({"daily": profile})
    cfg.dest = tmp_path / "snapshots"
    cfg.tmpdir = tmp_path / "tmp"
    meta_path = cfg.dest / "meta.yaml"
    meta = {
        "version": 1,
        "profiles": {
            "daily": {
                "snapshots": [
                    {
                        "id": "s1",
                        "created": 1000,
                        "type": "full",
                        "parent": None,
                        "file": "daily/s1.send",
                        "uploads": [{"remote": "r", "status": "complete"}],
                    },
                    {
                        "id": "s2",
                        "created": 2000,
                        "type": "incr",
                        "parent": "s1",
                        "file": "daily/s2.send",
                        "uploads": [{"remote": "r", "status": "complete"}],
                    },
                ]
            }
        }
    }
    (cfg.dest / "daily" / "s1").mkdir(parents=True)
    (cfg.dest / "daily" / "s2").mkdir(parents=True)
    manifest.save(meta_path, meta)

    calls = []

    def fake_delete(path):
        calls.append(path)
        if len(calls) == 2:
            raise cli.util.BtrbakError("boom")

    monkeypatch.setattr(cli.snapshot, "delete_snapshot", fake_delete)
    monkeypatch.setattr(cli.util, "now", lambda: 5000)

    class OkRemote:
        def delete(self, remote_path):
            pass

    by_profile = {
        "daily": [(RemoteSpec("r", "dir", {"path": "/x"}), OkRemote())]
    }

    with pytest.raises(cli.util.BtrbakError):
        cli.prune(cfg, meta, by_profile, meta_path)

    # The first (leaf) deletion must already be persisted even though the
    # second local delete aborted the prune.
    on_disk = manifest.load(meta_path)
    assert [s["id"] for s in manifest.snapshots(on_disk, "daily")] == ["s1"]


def test_cmd_list_skips_configs_without_profile(monkeypatch, capsys):
    cfg_a = _cfg({"daily": _profile("daily", 86400, -1, 30 * 86400)})
    cfg_b = _cfg({"weekly": _profile("weekly", 86400, -1, 30 * 86400)})
    monkeypatch.setattr(
        cli.config_mod, "discover_configs", lambda subvol=None: [cfg_a, cfg_b]
    )
    monkeypatch.setattr(
        cli.manifest,
        "load",
        lambda path: {"version": 1, "profiles": {"daily": {"snapshots": []}}},
    )

    assert cli.cmd_list(SimpleNamespace(subvol=None, profile="daily")) == 0
    assert "root/daily" in capsys.readouterr().out


def test_cmd_list_unknown_profile_raises(monkeypatch):
    cfg = _cfg({"daily": _profile("daily", 86400, -1, 30 * 86400)})
    monkeypatch.setattr(
        cli.config_mod, "discover_configs", lambda subvol=None: [cfg]
    )

    with pytest.raises(cli.config_mod.ConfigError):
        cli.cmd_list(SimpleNamespace(subvol=None, profile="nope"))


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


# --- config.yaml sync state (shared by `run` and `--dry-run`) --------------


class _ConfigRemote:
    """Minimal remote recording reads/writes of ``config.yaml``."""

    def __init__(self, stored=None, read_error=None):
        self.stored = stored
        self.read_error = read_error
        self.writes = []

    def read(self, remote_path, local_dest):
        if self.read_error is not None:
            raise self.read_error
        if self.stored is None:
            raise cli.RemoteNotFoundError("missing")
        local_dest.write_bytes(self.stored)

    def write(self, local_src, remote_path):
        self.stored = Path(local_src).read_bytes()
        self.writes.append(remote_path)


def _sync_cfg(tmp_path, remote):
    profile = _profile(
        "daily", 7 * 86400, 86400, 30 * 86400, [RemoteSpec("r", "dir", {"path": "/x"})]
    )
    cfg = _cfg({"daily": profile})
    cfg.src = tmp_path / "src"
    cfg.dest = tmp_path / "dest"
    cfg.tmpdir = tmp_path / "tmp"
    cfg.path = tmp_path / "root.yaml"
    cfg.path.write_text("src: /x\n")
    for path in (cfg.src, cfg.dest, cfg.tmpdir):
        path.mkdir(parents=True)
    spec = cfg.profiles["daily"].remotes[0]
    return cfg, [(spec, remote)]


def test_dry_run_reports_missing_config(capsys, tmp_path):
    remote = _ConfigRemote()
    cfg, remotes = _sync_cfg(tmp_path, remote)
    cli.dry_run_config_sync(cfg, {"daily": remotes}, force_config=False)
    assert "would upload config.yaml to remote r" in capsys.readouterr().out
    assert remote.writes == []


def test_dry_run_reports_in_sync_config(capsys, tmp_path):
    remote = _ConfigRemote(stored=b"src: /x\n")
    cfg, remotes = _sync_cfg(tmp_path, remote)
    cli.dry_run_config_sync(cfg, {"daily": remotes}, force_config=False)
    assert "config.yaml already in sync on remote r" in capsys.readouterr().out
    assert remote.writes == []


def test_dry_run_warns_when_config_differs(capsys, tmp_path):
    remote = _ConfigRemote(stored=b"tampered\n")
    cfg, remotes = _sync_cfg(tmp_path, remote)
    cli.dry_run_config_sync(cfg, {"daily": remotes}, force_config=False)
    err = capsys.readouterr().err
    assert "has a differing config.yaml" in err
    assert "without --force-config" in err
    assert remote.writes == []


def test_dry_run_reports_overwrite_with_force_config(capsys, tmp_path):
    remote = _ConfigRemote(stored=b"tampered\n")
    cfg, remotes = _sync_cfg(tmp_path, remote)
    cli.dry_run_config_sync(cfg, {"daily": remotes}, force_config=True)
    assert "would overwrite differing config.yaml on remote r" in capsys.readouterr().out
    assert remote.writes == []


def test_dry_run_survives_unreachable_remote(capsys, tmp_path):
    remote = _ConfigRemote(read_error=cli.util.BtrbakError("network down"))
    cfg, remotes = _sync_cfg(tmp_path, remote)
    cli.dry_run_config_sync(cfg, {"daily": remotes}, force_config=False)
    assert "could not read config.yaml from remote r" in capsys.readouterr().err


def test_dry_run_leaves_no_temp_file_behind(tmp_path):
    remote = _ConfigRemote(stored=b"tampered\n")
    cfg, remotes = _sync_cfg(tmp_path, remote)
    cli.dry_run_config_sync(cfg, {"daily": remotes}, force_config=False)
    assert not (cfg.tmpdir / cfg.name / "config.yaml.dry-run").exists()


def test_sync_settings_uploads_when_missing(tmp_path):
    remote = _ConfigRemote()
    cfg, remotes = _sync_cfg(tmp_path, remote)
    cli.sync_settings(cfg, remotes, force_config=False)
    assert remote.writes == ["config.yaml"]
    assert remote.stored == b"src: /x\n"


def test_sync_settings_skips_identical_config(tmp_path):
    remote = _ConfigRemote(stored=b"src: /x\n")
    cfg, remotes = _sync_cfg(tmp_path, remote)
    cli.sync_settings(cfg, remotes, force_config=False)
    assert remote.writes == []


def test_sync_settings_overwrites_with_force_config(tmp_path):
    remote = _ConfigRemote(stored=b"tampered\n")
    cfg, remotes = _sync_cfg(tmp_path, remote)
    cli.sync_settings(cfg, remotes, force_config=True)
    assert remote.stored == b"src: /x\n"


def test_run_config_warns_about_nesting_exactly_once(tmp_path, monkeypatch, capsys):
    remote = _ConfigRemote()
    cfg, remotes = _sync_cfg(tmp_path, remote)
    cfg.dest = cfg.src / "snapshots"
    cfg.dest.mkdir()

    monkeypatch.setattr(cli.config_mod, "is_nested", lambda inner, outer: True)
    monkeypatch.setattr(cli.config_mod, "validate", lambda cfg, **kw: ([], []))
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(cli, "collect_remotes", lambda cfg: {"daily": remotes})

    cli.run_config(cfg, None, force=True, force_config=True, full=False, dry_run=True)
    assert capsys.readouterr().err.count("nested inside src") == 1


def test_run_config_dry_run_does_not_sleep(tmp_path, monkeypatch, capsys):
    remote = _ConfigRemote()
    cfg, _remotes = _sync_cfg(tmp_path, remote)
    slept = []
    monkeypatch.setattr(cli.config_mod, "is_nested", lambda inner, outer: True)
    monkeypatch.setattr(cli.config_mod, "validate", lambda cfg, **kw: ([], []))
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: slept.append(seconds))
    monkeypatch.setattr(cli, "collect_remotes", lambda cfg: {})

    cli.run_config(cfg, None, force=True, force_config=False, full=False, dry_run=True)
    assert slept == []
    assert "Ctrl-C" not in capsys.readouterr().err


# --- verbosity --------------------------------------------------------------


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["list"], 0),
        (["-v", "list"], 1),
        (["list", "-v"], 1),
        (["-v", "list", "-v"], 2),
        (["-vv", "list"], 2),
    ],
)
def test_verbosity_accepted_before_and_after_subcommand(monkeypatch, argv, expected):
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cli, "cmd_list", lambda args: 0)
    assert cli.main(argv) == 0
    assert cli.VERBOSITY == expected
