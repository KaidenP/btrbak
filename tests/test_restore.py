import pytest
from types import SimpleNamespace

import yaml

import restore


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
