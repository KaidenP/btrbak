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


def test_scratch_dir_removes_itself_when_left_empty(tmp_path):
    path = tmp_path / "work" / "verify"
    with util.scratch_dir(path) as scratch:
        assert scratch.is_dir()
        (scratch / "a.send").write_bytes(b"x")
        (scratch / "a.send").unlink()
    assert not path.exists()
    assert path.parent.is_dir()


def test_scratch_dir_keeps_a_non_empty_directory(tmp_path):
    """A leftover file from a crashed run means the directory is still in use."""
    path = tmp_path / "verify"
    with util.scratch_dir(path) as scratch:
        (scratch / "leftover").write_bytes(b"x")
    assert path.is_dir()
    assert (path / "leftover").exists()


def test_scratch_dir_prunes_empty_parents_but_stops_at_root(tmp_path):
    """`<tmpdir>/<subvol>/` must not outlive the run, but tmpdir itself stays."""
    subvol = tmp_path / "root"
    path = subvol / "verify"
    with util.scratch_dir(path, tmp_path) as scratch:
        (scratch / "a.send").write_bytes(b"x")
        (scratch / "a.send").unlink()
    assert not path.exists()
    assert not subvol.exists()
    assert tmp_path.is_dir()


def test_scratch_dir_leaves_a_used_parent_alone(tmp_path):
    subvol = tmp_path / "root"
    (subvol / "daily").mkdir(parents=True)
    (subvol / "daily" / "inflight.send").write_bytes(b"x")
    with util.scratch_dir(subvol / "verify", tmp_path):
        pass
    assert subvol.is_dir()
    assert (subvol / "daily" / "inflight.send").exists()


def test_prune_empty_dir_defaults_to_the_immediate_parent(tmp_path):
    nested = tmp_path / "root" / "daily"
    nested.mkdir(parents=True)
    util.prune_empty_dir(nested)
    assert not nested.exists()
    assert (tmp_path / "root").is_dir()


def test_prune_empty_dir_never_removes_root(tmp_path):
    (tmp_path / "keep").mkdir()
    util.prune_empty_dir(tmp_path / "keep", tmp_path)
    assert tmp_path.is_dir()


def test_prune_empty_dir_refuses_to_walk_above_root(tmp_path):
    """A root that is not an ancestor must not turn into an rmdir('/')."""
    path = tmp_path / "work"
    path.mkdir()
    other = tmp_path.parent / "unrelated"
    util.prune_empty_dir(path, other)
    assert not path.exists()
    assert tmp_path.is_dir()


def test_rmdir_quiet_ignores_missing_and_non_empty(tmp_path):
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "child").mkdir()
    util.rmdir_quiet(tmp_path / "full")
    util.rmdir_quiet(tmp_path / "does-not-exist")
    assert (tmp_path / "full" / "child").is_dir()


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


def test_exclusive_lock_waits_for_release_within_timeout(tmp_path):
    """A briefly-held lock (a run's staging sweep) must not fail verify/restore."""
    import fcntl
    import os
    import threading

    path = tmp_path / "a.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)

    def release():
        import time

        time.sleep(0.2)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    thread = threading.Thread(target=release)
    thread.start()
    try:
        with util.exclusive_lock(path, timeout=10):
            pass
    finally:
        thread.join()


def test_exclusive_lock_timeout_expires(tmp_path):
    import fcntl
    import os

    path = tmp_path / "a.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(util.BtrbakError, match="waited"):
            with util.exclusive_lock(path, timeout=0.2):
                pass
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_exclusive_lock_default_still_fails_fast(tmp_path):
    import fcntl
    import os

    path = tmp_path / "a.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(util.BtrbakError, match="held by another process"):
            with util.exclusive_lock(path):
                pass
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --- private staging permissions --------------------------------------------
#
# Staged send streams are entire filesystems (decrypted plaintext during a
# restore of an encrypted profile), so nothing under tmpdir may be created
# world-readable.


def test_private_dir_creates_missing_parents_as_0700(tmp_path):
    target = tmp_path / "a" / "b" / "c"
    util.private_dir(target)
    assert target.is_dir()
    for directory in (tmp_path / "a", tmp_path / "a" / "b", target):
        assert directory.stat().st_mode & 0o777 == 0o700


def test_private_dir_leaves_existing_directories_alone(tmp_path):
    existing = tmp_path / "exists"
    existing.mkdir(mode=0o755)
    util.private_dir(existing / "new")
    assert existing.stat().st_mode & 0o777 == 0o755
    assert (existing / "new").stat().st_mode & 0o777 == 0o700


def test_open_private_creates_0600(tmp_path):
    path = tmp_path / "staged.send"
    with util.open_private(path) as handle:
        handle.write(b"backup data")
    assert path.read_bytes() == b"backup data"
    assert path.stat().st_mode & 0o777 == 0o600


def test_scratch_dir_is_created_private(tmp_path):
    path = tmp_path / "subvol" / ".verify"
    with util.scratch_dir(path, tmp_path) as scratch:
        assert scratch.stat().st_mode & 0o777 == 0o700
        assert (tmp_path / "subvol").stat().st_mode & 0o777 == 0o700
