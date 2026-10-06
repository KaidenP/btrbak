"""``btrfs send`` / ``btrfs receive`` with optional xz + age.

Compression uses the Python stdlib ``lzma`` module (no external dependency);
encryption shells out to the ``age`` binary.
"""

import lzma
import os
import shutil
from pathlib import Path

from .util import (
    BtrbakError,
    age_recipient_kind,
    open_private,
    private_dir,
    run,
)


def _run_private_output(cmd) -> None:
    """Run *cmd* under a restrictive umask.

    ``age`` creates its ``-o`` output with mode ``0666`` masked only by the
    process umask, so without this a freshly encrypted (or decrypted!) file
    would sit ``0644`` in the staging directory until something chmod'ed it.
    Tightening the umask for the duration of the call closes that window;
    the CLI is single-threaded, so the process-wide setting is safe.
    """
    old = os.umask(0o077)
    try:
        run(cmd)
    finally:
        os.umask(old)


def _use_xz(compression) -> bool:
    return bool(compression and compression.get("algorithm") == "xz")


def _use_age(encryption) -> bool:
    return bool(encryption and encryption.get("algorithm") == "age")


def _codec_level(compression) -> int:
    """Return the xz preset from a ``compression`` record.

    Accepts a numeric string because a manifest written by hand (or by a
    different version) may quote the level; anything else raises
    :class:`BtrbakError` rather than a bare :class:`ValueError`. ``manifest``
    validation already rejects the hopeless cases up front.
    """
    level = compression.get("level", 6)
    try:
        return int(level)
    except (TypeError, ValueError) as exc:
        raise BtrbakError(
            f"invalid xz compression level {level!r}; expected an integer 0-9"
        ) from exc


def compress_file(src, dst, preset: int = 6) -> None:
    try:
        with open(src, "rb") as source, open_private(dst) as raw_sink, lzma.open(
            raw_sink, "wb", format=lzma.FORMAT_XZ, preset=preset
        ) as sink:
            shutil.copyfileobj(source, sink)
    except lzma.LZMAError as exc:
        raise BtrbakError(f"xz compression failed: {exc}") from exc


def decompress_file(src, dst) -> None:
    """Decode an xz stream.

    A stream that is corrupt or truncated raises :class:`lzma.LZMAError` or
    :class:`EOFError`, neither of which is a ``BtrbakError``/``OSError``, so
    both are translated here. Otherwise a bad send file reaches the CLI's top
    level and prints a raw traceback instead of the one-line ``error:`` every
    other failure produces (§12).
    """
    try:
        with lzma.open(src, "rb") as source, open_private(dst) as sink:
            shutil.copyfileobj(source, sink)
    except lzma.LZMAError as exc:
        raise BtrbakError(f"xz decompression failed: {exc}") from exc
    except EOFError as exc:
        raise BtrbakError(
            f"xz stream is truncated: {exc or 'ended before the end-of-stream marker'}"
        ) from exc


def send_snapshot(snapshot, parent, out_path, compression=None, encryption=None) -> None:
    """Send a snapshot to *out_path* as a compressed/encrypted stream file.

    *parent* is the local snapshot used as the ``btrfs send -p`` parent for an
    incremental send (``None`` for a full send).
    """
    out_path = Path(out_path)
    private_dir(out_path.parent)
    work = out_path.parent
    use_xz = _use_xz(compression)
    use_age = _use_age(encryption)

    send_cmd = ["btrfs", "send"]
    if parent:
        send_cmd += ["-p", str(parent)]
    send_cmd.append(str(snapshot))

    intermediates = []
    try:
        if not use_xz and not use_age:
            with open_private(out_path) as handle:
                run(send_cmd, stdout=handle)
            return

        raw = work / (out_path.name + ".raw")
        intermediates.append(raw)
        with open_private(raw) as handle:
            run(send_cmd, stdout=handle)
        current = raw

        if use_xz:
            level = _codec_level(compression)
            compressed = work / (out_path.name + ".xz")
            intermediates.append(compressed)
            compress_file(current, compressed, level)
            current = compressed

        if use_age:
            recipients = encryption.get("recipients") or []
            if not recipients:
                raise BtrbakError(
                    "encryption is enabled but no age recipient is recorded; "
                    "the snapshot cannot be sent"
                )
            age_cmd = ["age"]
            for recipient in recipients:
                kind = age_recipient_kind(recipient)
                if kind == "file":
                    age_cmd += ["-R", str(recipient)]
                elif kind == "key":
                    age_cmd += ["-r", str(recipient)]
                else:
                    raise BtrbakError(
                        "age recipient is neither an existing file nor an "
                        f"inline age1 key: {recipient!r}"
                    )
            age_cmd += ["-o", str(out_path), str(current)]
            _run_private_output(age_cmd)
        else:
            os.replace(current, out_path)
    finally:
        for path in intermediates:
            path.unlink(missing_ok=True)


def restore_stream(send_file, target, compression=None, encryption=None) -> None:
    """Decrypt/decompress *send_file* and replay it into *target* via receive."""
    send_file = Path(send_file)
    work = send_file.parent
    use_xz = _use_xz(compression)
    use_age = _use_age(encryption)
    current = send_file
    intermediates = []

    try:
        if use_age:
            identity = encryption.get("identity")
            if not identity:
                raise BtrbakError("encryption identity is required to restore")
            decrypted = work / (send_file.name + ".dec")
            intermediates.append(decrypted)
            _run_private_output(
                ["age", "-d", "-i", str(identity), "-o", str(decrypted), str(current)]
            )
            current = decrypted

        if use_xz:
            decompressed = work / (send_file.name + ".decx")
            intermediates.append(decompressed)
            decompress_file(current, decompressed)
            current = decompressed

        with open(current, "rb") as handle:
            run(["btrfs", "receive", str(target)], stdin=handle)
    finally:
        for path in intermediates:
            path.unlink(missing_ok=True)
