from btrbak import send


def test_compress_decompress_roundtrip(tmp_path):
    src = tmp_path / "data.bin"
    src.write_bytes(b"hello btrbak " * 1000)
    compressed = tmp_path / "data.xz"
    restored = tmp_path / "data.out"

    send.compress_file(src, compressed, preset=6)
    send.decompress_file(compressed, restored)

    assert restored.read_bytes() == src.read_bytes()


def test_age_recipient_file_uses_recipients_flag(tmp_path, monkeypatch):
    recipient_file = tmp_path / "recipients.txt"
    recipient_file.write_text("age1abc\n")

    captured = {}

    def fake_run(cmd, stdout=None, stdin=None, check=True):
        if cmd and cmd[0] == "btrfs":
            if stdout is not None:
                stdout.write(b"fake stream")
            return None
        captured["age_cmd"] = cmd
        return None

    monkeypatch.setattr(send, "run", fake_run)

    encryption = {"algorithm": "age", "recipients": [str(recipient_file)], "identity": "/key"}
    snap = tmp_path / "snap"
    snap.mkdir()
    out = tmp_path / "out.send"

    send.send_snapshot(snap, None, out, encryption=encryption)

    assert "-R" in captured["age_cmd"]
    assert str(recipient_file) in captured["age_cmd"]


# --- corrupt xz streams must not escape as LZMAError ------------------------
#
# lzma.LZMAError is not a BtrbakError/OSError, so before it was translated it
# reached the CLI's top level and printed a raw traceback instead of the
# one-line `error:` every other failure produces (§12).


def test_decompress_file_reports_a_corrupt_stream_as_btrbak_error(tmp_path):
    import pytest

    from btrbak.util import BtrbakError

    src = tmp_path / "data.bin"
    src.write_bytes(b"payload " * 100_000)
    compressed = tmp_path / "data.xz"
    send.compress_file(src, compressed, preset=1)

    corrupt = bytearray(compressed.read_bytes())
    # Well past the 12-byte container header, so the stream parses and then
    # fails to decode rather than being rejected as "not an xz file".
    corrupt[len(corrupt) // 2] ^= 0xFF
    corrupt_path = tmp_path / "corrupt.xz"
    corrupt_path.write_bytes(bytes(corrupt))

    with pytest.raises(BtrbakError, match="xz decompression failed"):
        send.decompress_file(corrupt_path, tmp_path / "out.bin")


def test_decompress_file_rejects_garbage_as_btrbak_error(tmp_path):
    """A non-xz payload raises EOFError rather than LZMAError; both translate."""
    import pytest

    from btrbak.util import BtrbakError

    garbage = tmp_path / "garbage.xz"
    garbage.write_bytes(b"\x00" * 4096)
    with pytest.raises(BtrbakError, match="xz (decompression failed|stream is truncated)"):
        send.decompress_file(garbage, tmp_path / "out.bin")


def test_restore_stream_reports_a_corrupt_stream_as_btrbak_error(tmp_path):
    import pytest

    from btrbak.util import BtrbakError

    src = tmp_path / "data.bin"
    src.write_bytes(b"payload " * 100_000)
    compressed = tmp_path / "data.xz"
    send.compress_file(src, compressed, preset=1)
    corrupt = bytearray(compressed.read_bytes())
    corrupt[len(corrupt) // 2] ^= 0xFF
    compressed.write_bytes(bytes(corrupt))

    with pytest.raises(BtrbakError, match="xz decompression failed"):
        send.restore_stream(
            compressed, tmp_path, compression={"algorithm": "xz", "level": 1}
        )


def test_codec_level_rejects_a_nonnumeric_level():
    import pytest

    from btrbak.util import BtrbakError

    with pytest.raises(BtrbakError, match="invalid xz compression level"):
        send._codec_level({"algorithm": "xz", "level": "six"})


def test_codec_level_accepts_a_quoted_level():
    assert send._codec_level({"algorithm": "xz", "level": "3"}) == 3
    assert send._codec_level({"algorithm": "xz"}) == 6


def test_send_snapshot_refuses_to_encrypt_without_a_recipient(tmp_path, monkeypatch):
    import pytest

    from btrbak.util import BtrbakError

    snap = tmp_path / "snap"
    snap.mkdir()
    out = tmp_path / "out.send"

    def fake_run(cmd, stdout=None, stdin=None, check=True):
        if stdout is not None:
            stdout.write(b"stream")
        return None

    monkeypatch.setattr(send, "run", fake_run)

    with pytest.raises(BtrbakError, match="no age recipient"):
        send.send_snapshot(
            snap, None, out, encryption={"algorithm": "age", "recipients": []}
        )


# --- staged files must never be world-readable --------------------------------
#
# A staged send stream is an entire filesystem; a restore's `.dec` intermediate
# is decrypted plaintext. Everything send.py creates must be 0600, and the
# directories it creates 0700.


def _fake_btrfs_run(monkeypatch):
    from btrbak import send as send_mod

    def fake_run(cmd, stdout=None, stdin=None, check=True):
        if cmd and cmd[0] == "btrfs" and stdout is not None:
            stdout.write(b"fake stream")
        return None

    monkeypatch.setattr(send_mod, "run", fake_run)


def test_send_snapshot_plain_stream_is_0600(tmp_path, monkeypatch):
    _fake_btrfs_run(monkeypatch)
    snap = tmp_path / "snap"
    snap.mkdir()
    out = tmp_path / "staging" / "out.send"

    send.send_snapshot(snap, None, out)

    assert out.stat().st_mode & 0o777 == 0o600
    assert out.parent.stat().st_mode & 0o777 == 0o700


def test_send_snapshot_xz_stream_is_0600(tmp_path, monkeypatch):
    _fake_btrfs_run(monkeypatch)
    snap = tmp_path / "snap"
    snap.mkdir()
    out = tmp_path / "staging" / "out.send"

    send.send_snapshot(snap, None, out, compression={"algorithm": "xz", "level": 1})

    assert out.stat().st_mode & 0o777 == 0o600
    # No intermediates left behind
    assert sorted(p.name for p in out.parent.iterdir()) == ["out.send"]


def test_compress_and_decompress_outputs_are_0600(tmp_path):
    src = tmp_path / "data.bin"
    src.write_bytes(b"payload " * 1000)
    compressed = tmp_path / "data.xz"
    restored = tmp_path / "data.out"

    send.compress_file(src, compressed, preset=1)
    send.decompress_file(compressed, restored)

    assert compressed.stat().st_mode & 0o777 == 0o600
    assert restored.stat().st_mode & 0o777 == 0o600
    assert restored.read_bytes() == src.read_bytes()


def test_age_output_is_created_under_a_restrictive_umask(tmp_path, monkeypatch):
    """`age -o` creates its output 0666&~umask; send.py must tighten the umask."""
    import os

    captured = {}

    def fake_run(cmd, stdout=None, stdin=None, check=True):
        if cmd[0] == "btrfs" and stdout is not None:
            stdout.write(b"fake stream")
        elif cmd[0] == "age":
            current = os.umask(0o022)
            os.umask(current)
            captured["umask"] = current
            out = cmd[cmd.index("-o") + 1]
            # created under the active umask, exactly like age itself does
            fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
            os.close(fd)
        return None

    monkeypatch.setattr(send, "run", fake_run)
    monkeypatch.setattr(send, "age_recipient_kind", lambda recipient: "key")

    snap = tmp_path / "snap"
    snap.mkdir()
    out = tmp_path / "out.send"
    encryption = {"algorithm": "age", "recipients": ["age1abc"], "identity": None}

    before = os.umask(0o022)
    os.umask(before)
    send.send_snapshot(snap, None, out, encryption=encryption)

    assert captured["umask"] == 0o077
    # the caller's umask must be restored, and the output private
    after = os.umask(before)
    assert after == before
    assert out.stat().st_mode & 0o777 == 0o600
