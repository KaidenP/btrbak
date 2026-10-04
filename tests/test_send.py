import send


def test_compress_decompress_roundtrip(tmp_path):
    src = tmp_path / "data.bin"
    src.write_bytes(b"hello btrbak " * 1000)
    compressed = tmp_path / "data.xz"
    restored = tmp_path / "data.out"

    send.compress_file(src, compressed, preset=6)
    send.decompress_file(compressed, restored)

    assert restored.read_bytes() == src.read_bytes()
