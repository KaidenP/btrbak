import pytest
import yaml

from btrbak import manifest


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


def test_committed_local_deleted():
    assert manifest.committed(
        {
            "type": "full",
            "local_deleted": True,
            "uploads": [{"remote": "r", "status": "failed"}],
        }
    )


def test_last_committed_skips_local_deleted():
    meta = manifest.default()
    manifest.add_snapshot(
        meta,
        "p",
        {
            "id": "a",
            "type": "full",
            "uploads": [{"remote": "r", "status": "complete"}],
            "local_deleted": True,
        },
    )
    assert manifest.last_committed(meta, "p") is None
    assert manifest.last_full_committed(meta, "p") is None


def test_last_committed_prefers_live_over_local_deleted():
    meta = manifest.default()
    manifest.add_snapshot(
        meta,
        "p",
        {
            "id": "a",
            "type": "full",
            "uploads": [{"remote": "r", "status": "complete"}],
            "local_deleted": True,
        },
    )
    manifest.add_snapshot(
        meta,
        "p",
        {
            "id": "b",
            "type": "full",
            "uploads": [{"remote": "r", "status": "complete"}],
        },
    )
    assert manifest.last_committed(meta, "p")["id"] == "b"
    assert manifest.last_full_committed(meta, "p")["id"] == "b"


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


def test_profile_entry_orders_src_before_snapshots():
    meta = manifest.default()
    entry = manifest.profile(meta, "daily")
    assert list(entry) == ["src", "snapshots"]
    assert entry["src"] is None
    assert entry["snapshots"] == []


def test_profile_entry_roundtrips_key_order(tmp_path):
    meta = manifest.default()
    entry = manifest.profile(meta, "daily")
    entry["src"] = "/mnt/data"
    manifest.add_snapshot(meta, "daily", {"id": "a", "type": "full"})
    path = tmp_path / "meta.yaml"
    manifest.save(path, meta)

    text = path.read_text()
    assert text.index("src:") < text.index("snapshots:")
    assert manifest.load(path)["profiles"]["daily"]["src"] == "/mnt/data"


# --- structural validation --------------------------------------------------


def _write_meta(tmp_path, snapshots, name="p"):
    path = tmp_path / "meta.yaml"
    path.write_text(
        yaml.safe_dump({"version": 1, "profiles": {name: {"snapshots": snapshots}}})
    )
    return path


def test_load_rejects_snapshot_without_id(tmp_path):
    path = _write_meta(tmp_path, [{"type": "full"}])
    with pytest.raises(manifest.BtrbakError, match="non-empty string 'id'"):
        manifest.load(path)


def test_load_rejects_snapshot_with_empty_id(tmp_path):
    path = _write_meta(tmp_path, [{"id": "", "type": "full"}])
    with pytest.raises(manifest.BtrbakError, match="non-empty string 'id'"):
        manifest.load(path)


def test_load_rejects_non_mapping_snapshot(tmp_path):
    path = _write_meta(tmp_path, ["not-a-mapping"])
    with pytest.raises(manifest.BtrbakError, match="must be a mapping"):
        manifest.load(path)


def test_load_rejects_duplicate_snapshot_ids(tmp_path):
    path = _write_meta(tmp_path, [{"id": "a"}, {"id": "a"}])
    with pytest.raises(manifest.BtrbakError, match="duplicate snapshot id"):
        manifest.load(path)


def test_load_rejects_non_string_parent(tmp_path):
    path = _write_meta(tmp_path, [{"id": "a", "parent": 7}])
    with pytest.raises(manifest.BtrbakError, match="'parent' must be a string"):
        manifest.load(path)


def test_load_rejects_non_list_uploads(tmp_path):
    path = _write_meta(tmp_path, [{"id": "a", "uploads": "nope"}])
    with pytest.raises(manifest.BtrbakError, match="'uploads' must be a list"):
        manifest.load(path)


def test_load_rejects_non_mapping_upload(tmp_path):
    path = _write_meta(tmp_path, [{"id": "a", "uploads": ["nope"]}])
    with pytest.raises(manifest.BtrbakError, match="each upload must be a mapping"):
        manifest.load(path)


def test_load_normalises_missing_snapshots_list(tmp_path):
    path = _write_meta(tmp_path, [])
    meta = manifest.load(path)
    assert manifest.snapshots(meta, "p") == []


def test_created_tolerates_bad_values():
    assert manifest.created({"created": 5}) == 5
    assert manifest.created({}) == 0
    assert manifest.created({"created": "nope"}) == 0
    assert manifest.created({"created": None}) == 0
    assert manifest.created({"created": True}) == 0
