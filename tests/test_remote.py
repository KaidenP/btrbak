import pytest

from remotes.base import RemoteError, RemoteNotFoundError
from remotes.dir import DirRemote


def _remote(tmp_path):
    return DirRemote({"type": "dir", "path": str(tmp_path / "offsite")})


def test_write_read_roundtrip(tmp_path):
    remote = _remote(tmp_path)
    src = tmp_path / "in.bin"
    src.write_bytes(b"hello btrbak")
    remote.write(src, "daily/20251004T000000Z.send")
    dst = tmp_path / "out.bin"
    remote.read("daily/20251004T000000Z.send", dst)
    assert dst.read_bytes() == b"hello btrbak"


def test_read_missing_raises(tmp_path):
    remote = _remote(tmp_path)
    with pytest.raises(RemoteNotFoundError):
        remote.read("meta.yaml", tmp_path / "x")


def test_delete_is_idempotent(tmp_path):
    remote = _remote(tmp_path)
    remote.delete("nope/thing.send")  # should not raise
    src = tmp_path / "in.bin"
    src.write_bytes(b"x")
    remote.write(src, "a/b.send")
    remote.delete("a/b.send")
    remote.delete("a/b.send")


def test_path_traversal_rejected(tmp_path):
    remote = _remote(tmp_path)
    with pytest.raises(RemoteError):
        remote.write(tmp_path / "in.bin", "../escape")


def test_missing_path_setting():
    with pytest.raises(RemoteError):
        DirRemote({"type": "dir"})
