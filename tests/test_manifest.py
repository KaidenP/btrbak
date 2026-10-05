import pytest

import manifest


def test_default_and_roundtrip(tmp_path):
    meta = manifest.default()
    assert meta == {"version": 1, "profiles": {}}
    path = tmp_path / "meta.yaml"
    manifest.save(path, meta)
    assert manifest.load(path) == meta


def test_add_remove_get(tmp_path):
    meta = manifest.default()
    manifest.add_snapshot(meta, "daily", {"id": "a", "type": "full"})
    manifest.add_snapshot(meta, "daily", {"id": "b", "type": "incr", "parent": "a"})
    assert [s["id"] for s in manifest.snapshots(meta, "daily")] == ["a", "b"]
    assert manifest.get_snapshot(meta, "daily", "a")["type"] == "full"
    manifest.remove_snapshot(meta, "daily", "a")
    assert [s["id"] for s in manifest.snapshots(meta, "daily")] == ["b"]


def test_committed_and_last():
    meta = manifest.default()
    manifest.add_snapshot(meta, "p", {"id": "a", "type": "full", "uploads": [{"remote": "r", "status": "complete"}]})
    manifest.add_snapshot(
        meta,
        "p",
        {"id": "b", "type": "incr", "parent": "a", "uploads": [{"remote": "r", "status": "failed"}]},
    )
    assert manifest.committed({"type": "full", "uploads": [{"status": "complete"}]})
    assert not manifest.committed({"type": "full", "uploads": []})
    assert not manifest.committed({"type": "incr", "uploads": [{"status": "failed"}]})
    assert manifest.committed({"type": "local", "uploads": []})
    assert manifest.last_committed(meta, "p")["id"] == "a"
    assert manifest.last_full_committed(meta, "p")["id"] == "a"


def test_committed_flag_overrides_empty_uploads():
    assert manifest.committed({"type": "full", "committed": True, "uploads": []})


def test_children():
    meta = manifest.default()
    manifest.add_snapshot(meta, "p", {"id": "a", "type": "full"})
    manifest.add_snapshot(meta, "p", {"id": "b", "type": "incr", "parent": "a"})
    assert manifest.children(meta, "p", "a")[0]["id"] == "b"


def test_invalid_yaml_raises(tmp_path):
    path = tmp_path / "meta.yaml"
    path.write_text("version: [unclosed\n  profiles: {}")
    with pytest.raises(manifest.BtrbakError):
        manifest.load(path)
