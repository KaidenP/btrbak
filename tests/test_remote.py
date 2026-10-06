import os

import pytest

from btrbak.remotes.base import RemoteError, RemoteNotFoundError
from btrbak.remotes.dir import DirRemote


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


_IS_ROOT = os.geteuid() == 0


@pytest.mark.parametrize(
    "case",
    [
        "ok",
        "non-dir",
        "not-writable",
        "missing-parent",
        "missing-non-writable-parent",
        "missing-root-writable-parent",
    ],
)
def test_validate_branches(tmp_path, case):
    root = tmp_path / "offsite"
    if case in ("missing-parent", "missing-non-writable-parent"):
        root = tmp_path / "no-such-subdir" / "offsite"

    chmod_path = None
    if case == "ok":
        root.mkdir()
    elif case == "non-dir":
        root.write_bytes(b"x")
    elif case == "not-writable":
        root.mkdir()
        root.chmod(0o500)
        chmod_path = root
    elif case == "missing-non-writable-parent":
        root.parent.mkdir()
        root.parent.chmod(0o500)
        chmod_path = root.parent
    # "missing-parent" and "missing-root-writable-parent" need no setup:
    # the former's parent is absent, the latter's parent (tmp_path) exists.

    if case in ("not-writable", "missing-non-writable-parent") and _IS_ROOT:
        pytest.skip("writability cannot be tested when running as root")

    try:
        remote = DirRemote({"type": "dir", "path": str(root)})
        if case in (
            "non-dir",
            "not-writable",
            "missing-parent",
            "missing-non-writable-parent",
        ):
            with pytest.raises(RemoteError):
                remote.validate()
        else:
            assert remote.validate() is None
    finally:
        if chmod_path is not None and chmod_path.exists():
            chmod_path.chmod(0o755)


@pytest.mark.parametrize(
    "bad_path",
    ["", ".", "..", "a/../b", "trailing/", "/abs", "  ", "a//b"],
)
def test_invalid_path_shapes_rejected(tmp_path, bad_path):
    remote = _remote(tmp_path)
    src = tmp_path / "in.bin"
    src.write_bytes(b"x")
    with pytest.raises(RemoteError):
        remote.write(src, bad_path)
