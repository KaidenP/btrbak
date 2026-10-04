from pathlib import Path

import cli
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


def _remote_meta():
    return {"profiles": {"daily": {"snapshots": []}}}


def test_first_run_creates_full():
    profile = _profile("daily", 7 * 86400, 86400, 30 * 86400, [RemoteSpec("r", "dir", {"path": "/x"})])
    cfg = _cfg({"daily": profile})
    due, stype, parent = cli.compute_plan(cfg, profile, _remote_meta(), 5000)
    assert (due, stype, parent) == (True, "full", None)


def test_full_due_after_window():
    profile = _profile("daily", 100, 86400, 30 * 86400, [RemoteSpec("r", "dir", {"path": "/x"})])
    cfg = _cfg({"daily": profile})
    meta = {"profiles": {"daily": {"snapshots": [{"id": "f", "created": 1000, "type": "full", "parent": None, "uploads": []}]}}}
    due, stype, parent = cli.compute_plan(cfg, profile, meta, 5000)
    assert (due, stype, parent) == (True, "full", None)


def test_incremental_due():
    profile = _profile("daily", 7 * 86400, 100, 30 * 86400, [RemoteSpec("r", "dir", {"path": "/x"})])
    cfg = _cfg({"daily": profile})
    meta = {"profiles": {"daily": {"snapshots": [{"id": "f", "created": 4000, "type": "full", "parent": None, "uploads": []}]}}}
    due, stype, parent = cli.compute_plan(cfg, profile, meta, 5000)
    assert (due, stype, parent) == (True, "incr", "f")


def test_nothing_due():
    profile = _profile("daily", 7 * 86400, 100, 30 * 86400, [RemoteSpec("r", "dir", {"path": "/x"})])
    cfg = _cfg({"daily": profile})
    meta = {"profiles": {"daily": {"snapshots": [{"id": "f", "created": 4990, "type": "full", "parent": None, "uploads": []}]}}}
    due, stype, parent = cli.compute_plan(cfg, profile, meta, 5000)
    assert (due, stype, parent) == (False, None, None)


def test_force_creates_incremental_when_not_due():
    profile = _profile("daily", 7 * 86400, 100, 30 * 86400, [RemoteSpec("r", "dir", {"path": "/x"})])
    cfg = _cfg({"daily": profile})
    meta = {"profiles": {"daily": {"snapshots": [{"id": "f", "created": 4990, "type": "full", "parent": None, "uploads": []}]}}}
    due, stype, parent = cli.compute_plan(cfg, profile, meta, 5000, force=True)
    assert (due, stype, parent) == (True, "incr", "f")


def test_full_flag_forces_full():
    profile = _profile("daily", 7 * 86400, 100, 30 * 86400, [RemoteSpec("r", "dir", {"path": "/x"})])
    cfg = _cfg({"daily": profile})
    meta = {"profiles": {"daily": {"snapshots": [{"id": "f", "created": 4990, "type": "full", "parent": None, "uploads": []}]}}}
    due, stype, parent = cli.compute_plan(cfg, profile, meta, 5000, full=True)
    assert (due, stype, parent) == (True, "full", None)


def test_local_only_due_and_type():
    profile = _profile("local", 86400, -1, 14 * 86400)
    cfg = _cfg({"local": profile})
    assert cli.compute_plan(cfg, profile, _remote_meta(), 5000) == (True, "local", None)

    meta = {"profiles": {"local": {"snapshots": [{"id": "a", "created": 4000, "type": "local", "parent": None, "uploads": []}]}}}
    assert cli.compute_plan(cfg, profile, meta, 5000) == (False, None, None)


def test_unique_snapshot_id(tmp_path):
    profile_dir = tmp_path / "daily"
    profile_dir.mkdir()
    assert cli.unique_snapshot_id("20251004T000000Z", profile_dir) == "20251004T000000Z"
    (profile_dir / "20251004T000000Z").mkdir()
    assert cli.unique_snapshot_id("20251004T000000Z", profile_dir) == "20251004T000000Z-2"
