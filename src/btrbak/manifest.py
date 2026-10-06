"""Read/write the ``meta.yaml`` manifest (dependency DAG + upload state)."""

import re
from pathlib import Path

import yaml

from .util import BtrbakError, atomic_write_text

VERSION = 1

#: Upper bound on manifest size. The manifest is untrusted input (on the
#: disaster-recovery path it is downloaded from a remote) and ``yaml`` has no
#: nesting/alias/size limit of its own, so refuse implausibly large files
#: before parsing rather than let a corrupt or hostile file exhaust memory.
MAX_MANIFEST_BYTES = 16 * 1024 * 1024


def default() -> dict:
    return {"version": VERSION, "profiles": {}}


def load(path) -> dict:
    """Load *path*, returning :func:`default` for a missing/blank file.

    Callers that intend to mutate the result must hold the profile's exclusive
    lock for the whole load→mutate→:func:`save` cycle.
    """
    path = Path(path)
    if not path.exists():
        return default()
    try:
        size = path.stat().st_size
        if size > MAX_MANIFEST_BYTES:
            raise BtrbakError(
                f"meta.yaml is {size} bytes; refusing to load a manifest larger "
                f"than {MAX_MANIFEST_BYTES} bytes"
            )
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise BtrbakError(f"invalid meta.yaml: {exc}")
    except (UnicodeDecodeError, OSError) as exc:
        # A non-UTF-8 or unreadable manifest must surface as a one-line error,
        # not a traceback (e.g. after a partial/binary write).
        raise BtrbakError(f"cannot read meta.yaml: {exc}")
    if not data:
        # An empty (or blank/comment-only) file is a not-yet-initialised
        # manifest, exactly like a missing one; `touch <dest>/meta.yaml` must
        # not strand `run` on a "version: None" error.
        return default()
    if not isinstance(data, dict):
        raise BtrbakError("meta.yaml must be a mapping")
    # ``is`` (not ``==``) so a hand-edited ``version: 1.0`` or ``true`` (both
    # compare equal to ``1`` in Python) is rejected rather than accepted.
    if data.get("version") is not VERSION:
        raise BtrbakError(f"unsupported meta.yaml version: {data.get('version')!r}")
    profiles = data.get("profiles")
    if profiles is None:
        profiles = {}
        data["profiles"] = profiles
    if not isinstance(profiles, dict):
        raise BtrbakError("meta.yaml 'profiles' must be a mapping")
    for name, entry in profiles.items():
        if not isinstance(entry, dict):
            raise BtrbakError(f"meta.yaml profile {name!r} must be a mapping")
        snapshots = entry.get("snapshots")
        if snapshots is None:
            entry["snapshots"] = []
            continue
        if not isinstance(snapshots, list):
            raise BtrbakError(
                f"meta.yaml profile {name!r} 'snapshots' must be a list"
            )
        _validate_snapshots(name, snapshots)
    return data


def _validate_snapshots(profile_name: str, snapshots: list) -> None:
    """Reject snapshot entries that are not usable.

    ``id`` is mandatory, safe and unique: retention planning, the dependency
    tree and restore chain building all key off it, so a malformed entry (e.g.
    from a hand-edited manifest) must fail loudly here rather than as a
    ``KeyError`` deep inside a command.
    """
    seen = set()
    for index, snap in enumerate(snapshots):
        where = f"meta.yaml profile {profile_name!r} snapshot #{index}"
        if not isinstance(snap, dict):
            raise BtrbakError(f"{where} must be a mapping")
        sid = snap.get("id")
        if not isinstance(sid, str) or not sid:
            raise BtrbakError(f"{where} must have a non-empty string 'id'")
        _validate_id(sid, where)
        if sid in seen:
            raise BtrbakError(
                f"meta.yaml profile {profile_name!r} has duplicate snapshot id {sid!r}"
            )
        seen.add(sid)
        parent = snap.get("parent")
        if parent is not None and not isinstance(parent, str):
            raise BtrbakError(f"{where} ({sid}) 'parent' must be a string or null")
        stype = snap.get("type")
        if stype is not None and stype not in ("full", "incr", "local"):
            raise BtrbakError(
                f"{where} ({sid}) 'type' must be one of 'full', 'incr', 'local', "
                f"not {stype!r}"
            )
        # Validate scalar fields whose types the rest of the code relies on,
        # so a malformed entry fails here with a clear message instead of as an
        # AttributeError/TypeError deep inside a backup or restore.
        _validate_scalar(snap, "created", where, sid, int)
        _validate_scalar(snap, "size", where, sid, int)
        _validate_scalar(snap, "committed", where, sid, bool)
        _validate_scalar(snap, "local_deleted", where, sid, bool)
        _validate_scalar(snap, "file", where, sid, str)
        _validate_scalar(snap, "sha256", where, sid, str)
        uploads = snap.get("uploads", [])
        if uploads is None:
            uploads = []
            snap["uploads"] = uploads
        if not isinstance(uploads, list):
            raise BtrbakError(f"{where} ({sid}) 'uploads' must be a list")
        for upload in uploads:
            if not isinstance(upload, dict):
                raise BtrbakError(f"{where} ({sid}) each upload must be a mapping")
            for ukey in ("remote", "status"):
                if (
                    ukey in upload
                    and upload[ukey] is not None
                    and not isinstance(upload[ukey], str)
                ):
                    raise BtrbakError(f"{where} ({sid}) upload {ukey!r} must be a string")
        for key in ("compression", "encryption"):
            if key in snap:
                snap[key] = _validate_codec(where, sid, key, snap[key])


#: Snapshot ids are used verbatim as directory names and remote object names;
#: require a plain, single-component name so a hostile or hand-edited manifest
#: cannot smuggle a path separator, ``.``/``..`` or an absolute path into them.
_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _validate_id(sid: str, where: str) -> None:
    if not _ID_RE.match(sid):
        raise BtrbakError(
            f"{where} has unsafe snapshot id {sid!r} (must start with an "
            "alphanumeric and contain only [A-Za-z0-9._-])"
        )


def _validate_scalar(snap: dict, key: str, where: str, sid: str, expected, *, positive: bool = False) -> None:
    if key not in snap or snap[key] is None:
        return
    value = snap[key]
    if expected is int:
        valid = isinstance(value, int) and not isinstance(value, bool)
    else:
        valid = isinstance(value, expected)
    if not valid:
        raise BtrbakError(f"{where} ({sid}) {key!r} must be a {expected.__name__}")
    if positive and expected is int and value <= 0:
        raise BtrbakError(f"{where} ({sid}) {key!r} must be positive")


def _validate_codec(where: str, sid: str, key: str, value):
    """Validate a per-snapshot ``compression``/``encryption`` record.

    These are recorded per snapshot precisely so a profile's codec can change
    without breaking the chains already written, and ``restore`` feeds them
    straight back into :mod:`send`, which indexes them with ``.get()``. A
    manifest is untrusted input -- on the disaster-recovery path it is
    downloaded from a remote -- so a string or list where a mapping belongs
    would otherwise reach ``send`` and escape as an ``AttributeError``
    traceback rather than a one-line ``error:`` (§8.1).

    ``None`` is preserved: it means "no codec", which is how a local-only
    snapshot and any manifest written before per-snapshot codecs existed are
    recorded.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise BtrbakError(
            f"{where} ({sid}) {key!r} must be a mapping or null, "
            f"not {type(value).__name__}"
        )
    algorithm = value.get("algorithm")
    if not isinstance(algorithm, str) or not algorithm:
        raise BtrbakError(
            f"{where} ({sid}) {key!r} must have a non-empty string 'algorithm'"
        )
    if key == "compression":
        level = value.get("level", 6)
        # send.send_snapshot coerces the level with int(); a non-numeric value
        # here would raise ValueError from inside a backup or a restore.
        if isinstance(level, bool) or not isinstance(level, int):
            if not (isinstance(level, str) and level.lstrip("+-").isdigit()):
                raise BtrbakError(
                    f"{where} ({sid}) 'compression.level' must be an integer, "
                    f"not {level!r}"
                )
        # lzma presets are 0-9; anything else fails inside send at runtime.
        if not 0 <= int(level) <= 9:
            raise BtrbakError(
                f"{where} ({sid}) 'compression.level' must be between 0 and 9, "
                f"not {level!r}"
            )
    else:
        recipients = value.get("recipients")
        if recipients is not None and (
            not isinstance(recipients, list)
            or not all(isinstance(r, str) for r in recipients)
        ):
            raise BtrbakError(
                f"{where} ({sid}) 'encryption.recipients' must be a list of strings"
            )
        identity = value.get("identity")
        if identity is not None and not isinstance(identity, str):
            raise BtrbakError(
                f"{where} ({sid}) 'encryption.identity' must be a string or null"
            )
    return value


def save(path, meta: dict) -> None:
    """Write *meta* to *path*.

    Callers must hold the profile's exclusive lock (``util.exclusive_lock``)
    before calling this: save is not atomic with respect to a concurrent
    read-modify-write cycle, and every command that mutates the manifest
    already serialises itself on that lock.
    """
    text = yaml.safe_dump(meta, sort_keys=False, default_flow_style=False)
    atomic_write_text(path, text)


def profile(meta: dict, name: str) -> dict:
    """Return (creating if needed) the profile entry for *name*.

    ``src`` is seeded as ``None`` so it serialises before ``snapshots``; the
    caller fills in the real value.
    """
    return meta["profiles"].setdefault(name, {"src": None, "snapshots": []})


def snapshots(meta: dict, name: str) -> list:
    entry = meta["profiles"].get(name)
    if not entry:
        return []
    return entry.get("snapshots", [])


def snapshots_by_id(meta: dict, name: str) -> dict[str, dict]:
    """Return a ``{snapshot id: snapshot}`` index for a profile.

    Several commands walk a profile's snapshots while looking up entries by
    id (``verify``'s parent check, ``restore``'s chain walk, ``prune``).
    :func:`get_snapshot` is a linear scan, so doing that once per id turns
    those walks quadratic for large manifests; callers that need repeated
    lookups build this index once instead.
    """
    by_id: dict[str, dict] = {}
    for snap in snapshots(meta, name):
        sid = snap.get("id")
        if not sid:
            continue
        if sid in by_id:
            # ``load`` already rejects duplicates, so this only fires on an
            # unvalidated in-memory manifest; fail loudly rather than silently
            # let the last entry shadow an earlier one.
            raise BtrbakError(f"duplicate snapshot id {sid!r} in profile {name!r}")
        by_id[sid] = snap
    return by_id


def get_snapshot(meta: dict, name: str, snapshot_id: str) -> dict | None:
    for snap in snapshots(meta, name):
        if snap.get("id") == snapshot_id:
            return snap
    return None


def add_snapshot(meta: dict, name: str, entry: dict) -> None:
    profile(meta, name).setdefault("snapshots", []).append(entry)


def remove_snapshot(meta: dict, name: str, snapshot_id: str) -> None:
    entry = profile(meta, name)
    snaps = entry.get("snapshots", [])
    removed = next((snap for snap in snaps if snap.get("id") == snapshot_id), None)
    entry["snapshots"] = [
        snap for snap in snaps if snap.get("id") != snapshot_id
    ]
    if removed is not None:
        # Re-graft any dependent onto the removed entry's parent so the chain
        # stays valid: ``btrfs send -p`` accepts any ancestor, so parenting a
        # child onto its (former) grandparent yields a restorable stream and
        # avoids a dangling ``parent`` reference to a snapshot that no longer
        # exists.
        new_parent = removed.get("parent")
        for snap in entry["snapshots"]:
            if snap.get("parent") == snapshot_id:
                snap["parent"] = new_parent


def created(snapshot: dict) -> int:
    """Return a snapshot's ``created`` epoch, tolerating a missing/bad value.

    Retention and due-date arithmetic all key off this field, so a
    non-numeric value must degrade to ``0`` (oldest possible) rather than
    raising deep inside a command.
    """
    value = snapshot.get("created", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def committed(snapshot: dict) -> bool:
    # ``is True`` (not truthiness): a hand-edited ``committed: "false"`` string
    # is truthy and used to silently flip a snapshot's state.
    if snapshot.get("committed") is True:
        return True
    if snapshot.get("local_deleted") is True:
        return True
    if snapshot.get("type") == "local":
        return True
    uploads = snapshot.get("uploads", [])
    return bool(uploads) and all(upload.get("status") == "complete" for upload in uploads)


def last_committed(meta: dict, name: str) -> dict | None:
    eligible = [
        snap
        for snap in snapshots(meta, name)
        if snap.get("local_deleted") is not True and committed(snap)
    ]
    return max(eligible, key=created, default=None)


def last_remote_committed(meta: dict, name: str) -> dict | None:
    """Return the newest committed snapshot that has an offsite copy.

    A snapshot only qualifies if it records a ``file``: that excludes
    ``type: local`` snapshots and entries whose remotes were all removed. Both
    are committed as far as retention is concerned, but neither can serve as a
    ``btrfs send -p`` parent for a chain that has to be restorable offsite --
    parenting onto one produces an ``incr`` whose chain root can never be
    received, which is what happens when remotes are added to a profile that
    already had local-only snapshots.
    """
    eligible = [
        snap
        for snap in snapshots(meta, name)
        if snap.get("local_deleted") is not True
        and snap.get("file")
        and committed(snap)
    ]
    return max(eligible, key=created, default=None)


def last_full_committed(meta: dict, name: str) -> dict | None:
    # A full only counts for the full-backup cadence if it has an offsite copy
    # (a ``file``): a local-only full can never be the root of a restorable
    # chain, so measuring the cadence against it would suppress a needed
    # remote full (mirrors ``last_remote_committed``).
    eligible = [
        snap
        for snap in snapshots(meta, name)
        if snap.get("local_deleted") is not True
        and snap.get("file")
        and snap.get("type") == "full"
        and committed(snap)
    ]
    return max(eligible, key=created, default=None)
