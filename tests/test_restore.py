from pathlib import Path
from types import SimpleNamespace

import pytest

from btrbak import restore
import yaml


def _config(compression=None, encryption=None):
    return SimpleNamespace(compression=compression, encryption=encryption)


def test_codec_prefers_snapshot_settings():
    config = _config(compression={"algorithm": "xz", "level": 9})
    snap = {
        "compression": {"algorithm": "xz", "level": 6},
        "encryption": {"algorithm": "age", "recipients": ["age1abc"], "identity": "/key"},
    }
    assert restore.codec_for_snapshot(snap, config) == (
        {"algorithm": "xz", "level": 6},
        {"algorithm": "age", "recipients": ["age1abc"], "identity": "/key"},
    )


def test_codec_falls_back_to_config_when_snapshot_has_no_key():
    config = _config(compression={"algorithm": "xz", "level": 6})
    assert restore.codec_for_snapshot({}, config) == (
        {"algorithm": "xz", "level": 6},
        None,
    )


def test_codec_explicit_null_is_not_fallback():
    config = _config(compression={"algorithm": "xz", "level": 6})
    snap = {"compression": None, "encryption": None}
    assert restore.codec_for_snapshot(snap, config) == (None, None)


def test_build_chain_root_to_target():
    meta = {
        "profiles": {
            "p": {
                "snapshots": [
                    {"id": "full", "parent": None},
                    {"id": "incr1", "parent": "full"},
                    {"id": "incr2", "parent": "incr1"},
                ]
            }
        }
    }
    assert restore.build_chain(meta, "p", "incr2") == ["full", "incr1", "incr2"]


def test_build_chain_missing_snapshot():
    meta = {"profiles": {"p": {"snapshots": [{"id": "full", "parent": None}]}}}
    with pytest.raises(restore.BtrbakError):
        restore.build_chain(meta, "p", "missing")


def test_build_chain_detects_cycle():
    meta = {
        "profiles": {
            "p": {
                "snapshots": [
                    {"id": "a", "parent": "b"},
                    {"id": "b", "parent": "a"},
                ]
            }
        }
    }
    with pytest.raises(restore.BtrbakError):
        restore.build_chain(meta, "p", "a")


def test_build_chain_rejects_incremental_root():
    meta = {
        "profiles": {
            "p": {
                "snapshots": [
                    {"id": "a", "parent": None, "type": "incr"},
                    {"id": "b", "parent": "a", "type": "incr"},
                ]
            }
        }
    }
    with pytest.raises(restore.BtrbakError):
        restore.build_chain(meta, "p", "b")


def test_restore_target_is_file_raises(tmp_path):
    target = tmp_path / "target"
    target.write_text("x")
    with pytest.raises(restore.BtrbakError):
        restore.restore(None, "p", "sid", target, {}, tmp_path)


def test_load_meta_for_restore_tries_next_remote_on_corrupt(tmp_path, monkeypatch):
    valid_meta = {"version": 1, "profiles": {"p": {"snapshots": []}}}

    class FakeRemote:
        def __init__(self, payload):
            self.payload = payload

        def read(self, remote_path, local_dest):
            if isinstance(self.payload, Exception):
                raise self.payload
            local_dest.write_bytes(self.payload)

    remotes = [
        FakeRemote(b"version: [unclosed\n  profiles: {}"),  # corrupt YAML
        FakeRemote(yaml.safe_dump(valid_meta).encode()),
    ]

    def fake_create_remote(spec):
        return remotes.pop(0)

    monkeypatch.setattr(restore, "create_remote", fake_create_remote)

    config = SimpleNamespace(
        dest=tmp_path / "dest",
        tmpdir=tmp_path / "tmp",
        name="root",
        profiles={
            "p": SimpleNamespace(
                remotes=[SimpleNamespace(id="a"), SimpleNamespace(id="b")]
            )
        },
    )
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    result = restore.load_meta_for_restore(config, "p", tmpdir)
    assert result == valid_meta


def test_restore_does_not_create_target_on_non_btrfs(tmp_path, monkeypatch):
    target = tmp_path / "target" / "deep"
    monkeypatch.setattr(restore, "is_btrfs", lambda path: False)

    with pytest.raises(restore.BtrbakError, match="must be on a btrfs filesystem"):
        restore.restore(None, "p", "sid", target, {}, tmp_path)

    assert not target.exists()
    assert not (tmp_path / "target").exists()


def test_restore_creates_target_on_btrfs(tmp_path, monkeypatch):
    target = tmp_path / "target" / "deep"
    monkeypatch.setattr(restore, "is_btrfs", lambda path: True)
    meta = {
        "version": 1,
        "profiles": {
            "p": {
                "snapshots": [
                    {
                        "id": "sid",
                        "type": "full",
                        "parent": None,
                        "file": "p/sid.send",
                        "sha256": "d",
                        "size": 1,
                        "uploads": [{"remote": "r", "status": "complete"}],
                    }
                ]
            }
        },
    }

    received = []

    monkeypatch.setattr(restore, "create_remote", lambda spec: _fake_remote())
    monkeypatch.setattr(restore, "sha256_file", lambda path: "d")
    monkeypatch.setattr(
        restore.send, "restore_stream", lambda f, t, c, e: received.append((f, t))
    )

    restore.restore(
        SimpleNamespace(
            profiles={
                "p": SimpleNamespace(remotes=[SimpleNamespace(id="r", type="dir")])
            },
            compression=None,
            encryption=None,
        ),
        "p",
        "sid",
        target,
        meta,
        tmp_path,
    )

    assert target.is_dir()
    assert len(received) == 1


def test_restore_does_not_create_target_when_chain_is_unusable(tmp_path, monkeypatch):
    """A rejected restore must not leave an empty directory tree behind."""
    target = tmp_path / "target" / "deep"
    monkeypatch.setattr(restore, "is_btrfs", lambda path: True)
    meta = {
        "version": 1,
        "profiles": {
            "p": {"snapshots": [{"id": "sid", "type": "local", "parent": None}]}
        },
    }

    with pytest.raises(restore.BtrbakError, match="local-only"):
        restore.restore(
            SimpleNamespace(profiles={"p": SimpleNamespace(remotes=[])}),
            "p",
            "sid",
            target,
            meta,
            tmp_path,
        )

    assert not target.exists()
    assert not (tmp_path / "target").exists()


# --- resumable restore ------------------------------------------------------


def _chain_meta():
    return {
        "version": 1,
        "profiles": {
            "p": {
                "snapshots": [
                    {
                        "id": "s1",
                        "type": "full",
                        "parent": None,
                        "file": "p/s1.send",
                        "sha256": "d",
                        "size": 1,
                        "uploads": [{"remote": "r", "status": "complete"}],
                    },
                    {
                        "id": "s2",
                        "type": "incr",
                        "parent": "s1",
                        "file": "p/s2.send",
                        "sha256": "d",
                        "size": 1,
                        "uploads": [{"remote": "r", "status": "complete"}],
                    },
                ]
            }
        },
    }


def _restore_cfg():
    return SimpleNamespace(
        profiles={"p": SimpleNamespace(remotes=[SimpleNamespace(id="r", type="dir")])},
        compression=None,
        encryption=None,
    )


def _fake_remote():
    class _FakeRemote:
        def read(self, remote_path, local_dest):
            local_dest.write_bytes(b"x")

    return _FakeRemote()


def _patch_restore(monkeypatch):
    received = []

    class _Remote:
        def read(self, remote_path, local_dest):
            local_dest.write_bytes(b"x")

    monkeypatch.setattr(restore, "create_remote", lambda spec: _Remote())
    monkeypatch.setattr(restore, "sha256_file", lambda path: "d")
    monkeypatch.setattr(
        restore.send,
        "restore_stream",
        lambda f, t, c, e: received.append(t / Path(f).name),
    )
    return received


def test_restore_skips_already_received_links(tmp_path, monkeypatch):
    """An interrupted restore resumes instead of failing with 'File exists'."""
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr(restore, "is_btrfs", lambda path: True)
    monkeypatch.setattr(restore, "is_subvolume", lambda path: True)
    # s1 is already present from the interrupted run.
    (target / "s1").mkdir()
    received = _patch_restore(monkeypatch)

    restore.restore(_restore_cfg(), "p", "s2", target, _chain_meta(), tmp_path)

    assert received == [target / "s2.send"]


def test_resume_point_returns_first_missing(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    (target / "s1").mkdir()
    monkeypatch.setattr(restore, "is_subvolume", lambda path: True)
    assert restore._resume_point(target, ["s1", "s2"], _chain_meta(), "p") == 1


def test_resume_point_complete_chain(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    (target / "s1").mkdir()
    (target / "s2").mkdir()
    monkeypatch.setattr(restore, "is_subvolume", lambda path: True)
    assert restore._resume_point(target, ["s1", "s2"], _chain_meta(), "p") == 2


def test_resume_point_rejects_non_subvolume_entry(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    (target / "s1").mkdir()
    monkeypatch.setattr(restore, "is_subvolume", lambda path: False)
    with pytest.raises(restore.BtrbakError, match="not a btrfs subvolume"):
        restore._resume_point(target, ["s1", "s2"], _chain_meta(), "p")


def test_resume_point_accepts_matching_uuid(tmp_path, monkeypatch):
    """A correctly received link carries the sent subvolume's UUID."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "s1").mkdir()
    meta = _chain_meta()
    meta["profiles"]["p"]["snapshots"][0]["uuid"] = "aaaa"
    monkeypatch.setattr(restore, "is_subvolume", lambda path: True)
    monkeypatch.setattr(restore, "subvolume_uuid", lambda path: "aaaa")
    assert restore._resume_point(target, ["s1", "s2"], meta, "p") == 1


def test_resume_point_rejects_foreign_subvolume(tmp_path, monkeypatch):
    """A same-named subvolume that is not this snapshot must not be skipped.

    Skipping it would replay the rest of the chain on top of the wrong base
    and report success with silently incorrect data.
    """
    target = tmp_path / "target"
    target.mkdir()
    (target / "s1").mkdir()
    meta = _chain_meta()
    meta["profiles"]["p"]["snapshots"][0]["uuid"] = "aaaa"
    monkeypatch.setattr(restore, "is_subvolume", lambda path: True)
    monkeypatch.setattr(restore, "subvolume_uuid", lambda path: "bbbb")
    with pytest.raises(restore.BtrbakError, match="remove it before restoring"):
        restore._resume_point(target, ["s1", "s2"], meta, "p")


def test_resume_point_rejects_subvolume_with_unreadable_uuid(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    (target / "s1").mkdir()
    meta = _chain_meta()
    meta["profiles"]["p"]["snapshots"][0]["uuid"] = "aaaa"
    monkeypatch.setattr(restore, "is_subvolume", lambda path: True)
    monkeypatch.setattr(restore, "subvolume_uuid", lambda path: None)
    with pytest.raises(restore.BtrbakError, match="unknown"):
        restore._resume_point(target, ["s1", "s2"], meta, "p")


def test_resume_point_ignores_uuid_when_manifest_has_none(tmp_path, monkeypatch):
    """Manifests written before UUIDs were recorded fall back to name-only."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "s1").mkdir()
    monkeypatch.setattr(restore, "is_subvolume", lambda path: True)
    monkeypatch.setattr(
        restore, "subvolume_uuid", lambda path: pytest.fail("should not be read")
    )
    assert restore._resume_point(target, ["s1", "s2"], _chain_meta(), "p") == 1


def test_restore_rejects_foreign_subvolume_in_target(tmp_path, monkeypatch):
    """An end-to-end restore must refuse a decoy subvolume at a chain link."""
    target = tmp_path / "target"
    target.mkdir()
    (target / "s1").mkdir()
    meta = _chain_meta()
    meta["profiles"]["p"]["snapshots"][0]["uuid"] = "aaaa"
    monkeypatch.setattr(restore, "is_btrfs", lambda path: True)
    monkeypatch.setattr(restore, "is_subvolume", lambda path: True)
    monkeypatch.setattr(restore, "subvolume_uuid", lambda path: "bbbb")
    monkeypatch.setattr(restore, "create_remote", lambda spec: _fake_remote())
    monkeypatch.setattr(
        restore.send,
        "restore_stream",
        lambda *a, **k: pytest.fail("must not receive onto a foreign subvolume"),
    )
    with pytest.raises(restore.BtrbakError, match="remove it before restoring"):
        restore.restore(_restore_cfg(), "p", "s2", target, meta, tmp_path)


def test_restore_reports_resume(tmp_path, monkeypatch, capsys):
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setattr(restore, "is_btrfs", lambda path: True)
    monkeypatch.setattr(restore, "is_subvolume", lambda path: True)
    (target / "s1").mkdir()
    _patch_restore(monkeypatch)

    restore.restore(_restore_cfg(), "p", "s2", target, _chain_meta(), tmp_path)

    err = capsys.readouterr().err
    assert "resuming restore: 1 of 2 link(s) already present" in err
