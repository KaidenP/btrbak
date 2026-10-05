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
