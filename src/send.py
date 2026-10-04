"""``btrfs send`` / ``btrfs receive`` with optional xz + age.

Compression uses the Python stdlib ``lzma`` module (no external dependency);
encryption shells out to the ``age`` binary.
"""

import lzma
import os
import shutil
from pathlib import Path

from util import BtrbakError, run


def _use_xz(compression) -> bool:
    return bool(compression and compression.get("algorithm") == "xz")


def _use_age(encryption) -> bool:
    return bool(encryption and encryption.get("algorithm") == "age")


def send_snapshot(snapshot, parent, out_path, compression=None, encryption=None) -> None:
    """Send a snapshot to *out_path* as a compressed/encrypted stream file.

    *parent* is the local snapshot used as the ``btrfs send -p`` parent for an
    incremental send (``None`` for a full send).
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
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
            with open(out_path, "wb") as handle:
                run(send_cmd, stdout=handle)
            return

        raw = work / (out_path.name + ".raw")
        intermediates.append(raw)
        with open(raw, "wb") as handle:
            run(send_cmd, stdout=handle)
        current = raw

        if use_xz:
            level = int(compression.get("level", 6))
            compressed = work / (out_path.name + ".xz")
            intermediates.append(compressed)
            with open(current, "rb") as source, lzma.open(
                compressed, "wb", format=lzma.FORMAT_XZ, preset=level
            ) as sink:
                shutil.copyfileobj(source, sink)
            current = compressed

        if use_age:
            age_cmd = ["age"]
            for recipient in encryption["recipients"]:
                age_cmd += ["-r", str(recipient)]
            age_cmd += ["-o", str(out_path), str(current)]
            run(age_cmd)
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
            run(["age", "-d", "-i", str(identity), "-o", str(decrypted), str(current)])
            current = decrypted

        if use_xz:
            decompressed = work / (send_file.name + ".decx")
            intermediates.append(decompressed)
            with lzma.open(current, "rb") as source, open(decompressed, "wb") as sink:
                shutil.copyfileobj(source, sink)
            current = decompressed

        with open(current, "rb") as handle:
            run(["btrfs", "receive", str(target)], stdin=handle)
    finally:
        for path in intermediates:
            path.unlink(missing_ok=True)
