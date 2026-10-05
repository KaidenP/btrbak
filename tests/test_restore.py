import pytest
from types import SimpleNamespace

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


def test_restore_target_is_file_raises(tmp_path):
    target = tmp_path / "target"
    target.write_text("x")
    with pytest.raises(restore.BtrbakError):
        restore.restore(None, "p", "sid", target, {}, tmp_path)
