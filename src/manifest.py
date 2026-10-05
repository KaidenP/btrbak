"""Read/write the ``meta.yaml`` manifest (dependency DAG + upload state)."""

from pathlib import Path

import yaml

from util import BtrbakError, atomic_write_text

VERSION = 1


def default() -> dict:
    return {"version": VERSION, "profiles": {}}


def load(path) -> dict:
    path = Path(path)
    if not path.exists():
        return default()
    try:
        with open(path) as handle:
            data = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise BtrbakError(f"invalid meta.yaml: {exc}")
    if not isinstance(data, dict):
        raise BtrbakError("meta.yaml must be a mapping")
    if data.get("version") != VERSION:
        raise BtrbakError(f"unsupported meta.yaml version: {data.get('version')!r}")
    data.setdefault("profiles", {})
    return data


def save(path, meta: dict) -> None:
    text = yaml.safe_dump(meta, sort_keys=False, default_flow_style=False)
    atomic_write_text(path, text)


def profile(meta: dict, name: str) -> dict:
    return meta["profiles"].setdefault(name, {"snapshots": []})


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


def committed(snapshot: dict) -> bool:
    if snapshot.get("type") == "local":
        return True
    uploads = snapshot.get("uploads", [])
    return bool(uploads) and all(upload.get("status") == "complete" for upload in uploads)


def last_committed(meta: dict, name: str) -> dict | None:
    for snap in reversed(snapshots(meta, name)):
        if committed(snap):
            return snap
    return None


def last_full_committed(meta: dict, name: str) -> dict | None:
    for snap in reversed(snapshots(meta, name)):
        if snap.get("type") == "full" and committed(snap):
            return snap
    return None


def children(meta: dict, name: str, snapshot_id: str) -> list:
    return [snap for snap in snapshots(meta, name) if snap.get("parent") == snapshot_id]
