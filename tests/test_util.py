import subprocess

import pytest

import util


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


def test_age_recipient_kind(tmp_path):
    keyfile = tmp_path / "recipients.txt"
    keyfile.write_text("age1abc\n")
    assert util.age_recipient_kind(str(keyfile)) == "file"
    assert util.age_recipient_kind("age1abc") == "key"
    assert util.age_recipient_kind("not-a-key.txt") == "unknown"
