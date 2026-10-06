"""Read/write the ``meta.yaml`` manifest (dependency DAG + upload state)."""

from pathlib import Path

import yaml

from .util import BtrbakError, atomic_write_text

VERSION = 1


def default() -> dict:
    return {"version": VERSION, "profiles": {}}


def load(path) -> dict:
    path = Path(path)
    if not path.exists():
        return default()
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise BtrbakError(f"invalid meta.yaml: {exc}")
    if not isinstance(data, dict):
        raise BtrbakError("meta.yaml must be a mapping")
    if data.get("version") != VERSION:
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

    ``id`` is mandatory and unique: retention planning, the dependency tree and
    restore chain building all key off it, so a malformed entry (e.g. from a
    hand-edited manifest) must fail loudly here rather than as a ``KeyError``
    deep inside a command.
    """
    seen = set()
    for index, snap in enumerate(snapshots):
        where = f"meta.yaml profile {profile_name!r} snapshot #{index}"
        if not isinstance(snap, dict):
            raise BtrbakError(f"{where} must be a mapping")
        sid = snap.get("id")
        if not isinstance(sid, str) or not sid:
            raise BtrbakError(f"{where} must have a non-empty string 'id'")
        if sid in seen:
            raise BtrbakError(
                f"meta.yaml profile {profile_name!r} has duplicate snapshot id {sid!r}"
            )
        seen.add(sid)
        parent = snap.get("parent")
        if parent is not None and not isinstance(parent, str):
            raise BtrbakError(f"{where} ({sid}) 'parent' must be a string or null")
        uploads = snap.get("uploads", [])
        if uploads is None:
            uploads = []
            snap["uploads"] = uploads
        if not isinstance(uploads, list):
            raise BtrbakError(f"{where} ({sid}) 'uploads' must be a list")
        for upload in uploads:
            if not isinstance(upload, dict):
                raise BtrbakError(f"{where} ({sid}) each upload must be a mapping")
        for key in ("compression", "encryption"):
            if key in snap:
                snap[key] = _validate_codec(where, sid, key, snap[key])


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


def get_snapshot(meta: dict, name: str, snapshot_id: str) -> dict | None:
    for snap in snapshots(meta, name):
        if snap.get("id") == snapshot_id:
            return snap
    return None


def add_snapshot(meta: dict, name: str, entry: dict) -> None:
    profile(meta, name).setdefault("snapshots", []).append(entry)


def remove_snapshot(meta: dict, name: str, snapshot_id: str) -> None:
    entry = profile(meta, name)
    entry["snapshots"] = [
        snap for snap in entry.get("snapshots", []) if snap.get("id") != snapshot_id
    ]


def created(snapshot: dict) -> int:
    """Return a snapshot's ``created`` epoch, tolerating a missing/bad value.

    Retention and due-date arithmetic all key off this field, so a
    non-numeric value must degrade to ``0`` (oldest possible) rather than
    raising deep inside a command.
    """
    value = snapshot.get("created", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def committed(snapshot: dict) -> bool:
    if snapshot.get("committed"):
        return True
    if snapshot.get("local_deleted"):
        return True
    if snapshot.get("type") == "local":
        return True
    uploads = snapshot.get("uploads", [])
    return bool(uploads) and all(upload.get("status") == "complete" for upload in uploads)


def last_committed(meta: dict, name: str) -> dict | None:
    for snap in reversed(snapshots(meta, name)):
        if snap.get("local_deleted"):
            continue
        if committed(snap):
            return snap
    return None


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
    for snap in reversed(snapshots(meta, name)):
        if snap.get("local_deleted") or not snap.get("file"):
            continue
        if committed(snap):
            return snap
    return None


def last_full_committed(meta: dict, name: str) -> dict | None:
    for snap in reversed(snapshots(meta, name)):
        if snap.get("local_deleted"):
            continue
        if snap.get("type") == "full" and committed(snap):
            return snap
    return None
