from btrbak import snapshot


def test_create_ro_snapshot_is_read_only(monkeypatch):
    """The `-r` flag is the entire immutability guarantee."""
    captured = {}

    def fake_run(cmd, stdout=None, stdin=None, check=True):
        captured["cmd"] = cmd

    monkeypatch.setattr(snapshot, "run", fake_run)
    snapshot.create_ro_snapshot("/mnt/data", "/mnt/data/.snapshots/s1")

    assert captured["cmd"] == [
        "btrfs",
        "subvolume",
        "snapshot",
        "-r",
        "/mnt/data",
        "/mnt/data/.snapshots/s1",
    ]


def test_delete_snapshot_command(monkeypatch):
    captured = {}

    def fake_run(cmd, stdout=None, stdin=None, check=True):
        captured["cmd"] = cmd

    monkeypatch.setattr(snapshot, "run", fake_run)
    snapshot.delete_snapshot("/mnt/data/.snapshots/s1")

    assert captured["cmd"] == ["btrfs", "subvolume", "delete", "/mnt/data/.snapshots/s1"]
