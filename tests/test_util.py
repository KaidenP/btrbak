import subprocess

import pytest

from btrbak import util


def test_run_missing_binary_raises_btrbak_error(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory: btrfs")

    monkeypatch.setattr(util.subprocess, "run", fake_run)
    with pytest.raises(util.BtrbakError):
        util.run(["btrfs", "subvolume", "show", "/x"])


def test_is_subvolume_missing_binary_returns_false(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory: btrfs")

    monkeypatch.setattr(util.subprocess, "run", fake_run)
    assert util.is_subvolume("/x") is False


def _stub_subvolume_show(monkeypatch, text, returncode=0):
    completed = subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=text.encode()
    )
    monkeypatch.setattr(util.subprocess, "run", lambda *a, **k: completed)


def test_subvolume_uuid_parses_own_uuid(monkeypatch):
    """A local snapshot carries its own UUID (no Received UUID set)."""
    _stub_subvolume_show(
        monkeypatch,
        "snap/subvol\n"
        "\tName: \t\t\tsnap\n"
        "\tUUID: \t\t\t32fce6b5-1603-f54d-a7d8-db6135d2316f\n"
        "\tParent UUID: \t\t1a4e89ce-51ad-f745-92f1-b1f1e79aac12\n"
        "\tReceived UUID: \t-\n",
    )
    assert util.subvolume_uuid("/x") == "32fce6b5-1603-f54d-a7d8-db6135d2316f"


def test_subvolume_uuid_prefers_received_uuid(monkeypatch):
    """A received copy keeps the sent UUID as Received UUID and gets a new own one.

    Only the Received UUID round-trips through send/receive, so comparing the
    plain UUID column would reject every correctly received link.
    """
    _stub_subvolume_show(
        monkeypatch,
        "\tUUID: \t\t\tbbbbbbbb-1111-2222-3333-444444444444\n"
        "\tParent UUID: \t\t-\n"
        "\tReceived UUID: \taaaaaaaa-1111-2222-3333-444444444444\n",
    )
    assert util.subvolume_uuid("/x") == "aaaaaaaa-1111-2222-3333-444444444444"


def test_subvolume_uuid_is_case_insensitive(monkeypatch):
    _stub_subvolume_show(
        monkeypatch,
        "\tUUID: \t\t\t32FCE6B5-1603-F54D-A7D8-DB6135D2316F\n"
        "\tReceived UUID: \t-\n",
    )
    assert util.subvolume_uuid("/x") == "32fce6b5-1603-f54d-a7d8-db6135d2316f"


def test_subvolume_uuid_returns_none_when_not_a_subvolume(monkeypatch):
    _stub_subvolume_show(monkeypatch, "", returncode=1)
    assert util.subvolume_uuid("/x") is None


def test_subvolume_uuid_returns_none_when_binary_missing(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory: btrfs")

    monkeypatch.setattr(util.subprocess, "run", fake_run)
    assert util.subvolume_uuid("/x") is None


def test_age_recipient_kind(tmp_path):
    keyfile = tmp_path / "recipients.txt"
    keyfile.write_text("age1abc\n")
    assert util.age_recipient_kind(str(keyfile)) == "file"
    assert util.age_recipient_kind("age1abc") == "key"
    assert util.age_recipient_kind("not-a-key.txt") == "unknown"


def test_btrfs_fsid_matches_uppercase_uuid(monkeypatch):
    """btrfs-progs spells the fs uuid ``uuid:`` or ``UUID:`` depending on version."""
    for label in ("uuid", "UUID"):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=f"Label: none  {label}: 7574148f-c138-4c09-9203-0352942dfe4f\n".encode(),
        )
        monkeypatch.setattr(util.subprocess, "run", lambda *a, **k: completed)
        assert util.btrfs_fsid("/mnt/data") == "7574148f-c138-4c09-9203-0352942dfe4f"


def test_btrfs_fsid_returns_none_without_match(monkeypatch):
    completed = subprocess.CompletedProcess(
        args=[], returncode=0, stdout=b"Label: none\n"
    )
    monkeypatch.setattr(util.subprocess, "run", lambda *a, **k: completed)
    assert util.btrfs_fsid("/mnt/data") is None


# --- age recipient validation -----------------------------------------------


def _completed(returncode, stderr=b""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stderr=stderr
    )


def test_age_recipient_error_unknown_kind():
    problem = util.age_recipient_error("/nonexistent/rec.txt")
    assert "neither an existing file nor an inline age1 key" in problem


def test_age_recipient_error_accepts_valid_key(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return _completed(0)

    monkeypatch.setattr(util.subprocess, "run", fake_run)
    assert util.age_recipient_error("age1abc") is None
    assert seen["cmd"][:2] == ["age", "-r"]


def test_age_recipient_error_uses_R_for_files(monkeypatch, tmp_path):
    recipients = tmp_path / "rec.txt"
    recipients.write_text("age1abc\n")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return _completed(0)

    monkeypatch.setattr(util.subprocess, "run", fake_run)
    assert util.age_recipient_error(str(recipients)) is None
    assert seen["cmd"][:2] == ["age", "-R"]


def test_age_recipient_error_surfaces_age_message(monkeypatch):
    monkeypatch.setattr(
        util.subprocess,
        "run",
        lambda *a, **k: _completed(1, b'age: error: malformed recipient "age1TYPO": mixed case\n'),
    )
    assert util.age_recipient_error("age1TYPO") == (
        'malformed recipient "age1TYPO": mixed case'
    )


def test_age_recipient_error_falls_back_to_status(monkeypatch):
    monkeypatch.setattr(util.subprocess, "run", lambda *a, **k: _completed(2, b""))
    assert util.age_recipient_error("age1abc") == "exit status 2"


def test_age_recipient_error_handles_missing_age_binary(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError(2, "No such file or directory: age")

    monkeypatch.setattr(util.subprocess, "run", fake_run)
    assert util.age_recipient_error("age1abc") == "the 'age' binary was not found"


@pytest.mark.skipif(not util.which("age"), reason="age not installed")
def test_age_recipient_error_real_invalid_key():
    """A syntactically plausible but malformed key must not pass validation."""
    assert util.age_recipient_error("age1TYPO") is not None


# --- locking ----------------------------------------------------------------


def test_optional_lock_yields_true_when_free(tmp_path):
    with util.optional_lock(tmp_path / "a.lock") as got:
        assert got is True


def test_optional_lock_yields_false_when_held(tmp_path):
    import fcntl
    import os

    path = tmp_path / "a.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with util.optional_lock(path) as got:
            assert got is False
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_optional_lock_does_not_raise_when_held(tmp_path):
    import fcntl
    import os

    path = tmp_path / "a.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with util.optional_lock(path):
            pass
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
