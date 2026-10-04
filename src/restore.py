"""Restore: replay a full + incremental send chain into a btrfs target."""

from pathlib import Path

import manifest
import send
from remotes import create_remote
from remotes.base import RemoteNotFoundError
from util import BtrbakError, is_btrfs, sha256_file


def build_chain(meta: dict, profile_name: str, snapshot_id: str) -> list[str]:
    """Return the root→target chain of snapshot ids for *snapshot_id*."""
    chain = []
    current = snapshot_id
    seen = set()
    while current is not None:
        if current in seen:
            raise BtrbakError("dependency cycle in manifest")
        seen.add(current)
        chain.append(current)
        snap = manifest.get_snapshot(meta, profile_name, current)
        if snap is None:
            raise BtrbakError(f"missing snapshot in chain: {current}")
        current = snap.get("parent")
    chain.reverse()
    return chain


def pick_remote(snapshot: dict, remote_map: dict):
    for upload in snapshot.get("uploads", []):
        if upload.get("status") == "complete" and upload.get("remote") in remote_map:
            return remote_map[upload["remote"]]
    return None


def restore(config, profile_name, snapshot_id, target, meta, tmpdir) -> None:
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    if not is_btrfs(target):
        raise BtrbakError(f"restore target must be on a btrfs filesystem: {target}")

    if manifest.get_snapshot(meta, profile_name, snapshot_id) is None:
        raise BtrbakError(f"snapshot not found: {profile_name}/{snapshot_id}")

    chain = build_chain(meta, profile_name, snapshot_id)
    profile_meta = manifest.profile(meta, profile_name)
    compression = profile_meta.get("compression")
    encryption = profile_meta.get("encryption")
    remote_map = {
        spec.id: create_remote(spec) for spec in config.profiles[profile_name].remotes
    }

    for sid in chain:
        snap = manifest.get_snapshot(meta, profile_name, sid)
        remote = pick_remote(snap, remote_map)
        if remote is None:
            raise BtrbakError(f"no complete remote upload for snapshot {sid}")

        tmpfile = tmpdir / f"{sid}.send"
        remote.read(snap["file"], tmpfile)
        if sha256_file(tmpfile) != snap.get("sha256"):
            raise BtrbakError(f"checksum mismatch for snapshot {sid}")
        if tmpfile.stat().st_size != snap.get("size"):
            raise BtrbakError(f"size mismatch for snapshot {sid}")

        send.restore_stream(tmpfile, target, compression, encryption)
        tmpfile.unlink(missing_ok=True)


def run_restore(config, profile_name, snapshot_id, target) -> None:
    if profile_name not in config.profiles:
        raise BtrbakError(f"unknown profile: {profile_name}")

    tmpdir = config.tmpdir / config.name / "restore"
    tmpdir.mkdir(parents=True, exist_ok=True)
    meta = load_meta_for_restore(config, profile_name, tmpdir)
    restore(config, profile_name, snapshot_id, target, meta, tmpdir)


def load_meta_for_restore(config, profile_name, tmpdir) -> dict:
    local = config.dest / "meta.yaml"
    if local.exists():
        return manifest.load(local)

    profile = config.profiles[profile_name]
    if not profile.remotes:
        raise BtrbakError("no remotes configured and no local manifest")

    for spec in profile.remotes:
        remote = create_remote(spec)
        try:
            tmp = tmpdir / "meta.yaml"
            remote.read("meta.yaml", tmp)
            return manifest.load(tmp)
        except RemoteNotFoundError:
            continue
    raise BtrbakError("could not download meta.yaml from any remote")
