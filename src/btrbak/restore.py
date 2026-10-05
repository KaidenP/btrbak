"""Restore: replay a full + incremental send chain into a btrfs target."""

import sys
from pathlib import Path

from . import manifest
from . import send
from .remotes import create_remote
from .util import BtrbakError, is_btrfs, is_subvolume, scratch_dir, sha256_file, subvolume_uuid


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
    root = manifest.get_snapshot(meta, profile_name, chain[0])
    if root is not None and root.get("type") == "incr":
        raise BtrbakError(
            f"chain root {chain[0]} is incremental; expected a full backup"
        )
    return chain


def pick_remote(snapshot: dict, remote_map: dict):
    for upload in snapshot.get("uploads", []):
        if upload.get("status") == "complete" and upload.get("remote") in remote_map:
            return remote_map[upload["remote"]]
    return None


def codec_for_snapshot(snap: dict, config) -> tuple:
    """Return the ``(compression, encryption)`` settings for a snapshot.

    Prefers the snapshot's own recorded settings and falls back to the
    profile-level values from the config file for manifests written before
    per-snapshot codec storage was introduced.
    """
    return (
        snap.get("compression", config.compression),
        snap.get("encryption", config.encryption),
    )


def _resume_point(target: Path, chain: list[str], meta: dict, profile_name: str) -> int:
    """Return the index of the first link in *chain* that still needs receiving.

    ``btrfs receive`` creates one subvolume per stream, named after the
    snapshot id, directly inside the target. A restore interrupted part way
    through therefore leaves a prefix of the chain already present, and
    replaying it again would fail with ``File exists``. When the prefix is
    intact, the already-received links are skipped so the restore resumes.

    Presence alone is not enough: an unrelated subvolume that merely occupies a
    snapshot id would be skipped and the remaining links replayed on top of it,
    yielding a target that looks restored but holds the wrong data. Each
    present link therefore has to be the subvolume the manifest recorded for
    that snapshot id -- ``btrfs receive`` reports the sent subvolume's UUID,
    so a correctly received link matches exactly. Manifests written before
    UUIDs were recorded carry no ``uuid`` and fall back to the name-only check.

    Matching the UUID also means a half-received link cannot be mistaken for a
    finished one: ``btrfs receive`` builds each stream into a temporary
    subvolume and renames it into place only on success, so an interrupted
    receive leaves nothing at the snapshot id for this to match.

    Raises :class:`BtrbakError` when the target holds a non-subvolume entry
    under a snapshot id, or a subvolume with a different UUID, since either
    would block ``btrfs receive`` or silently invalidate the chain.
    """
    for index, sid in enumerate(chain):
        entry = target / sid
        if not entry.exists():
            return index
        if not is_subvolume(entry):
            raise BtrbakError(
                f"restore target already contains {entry} which is not a btrfs "
                "subvolume; remove it before restoring"
            )
        expected = (manifest.get_snapshot(meta, profile_name, sid) or {}).get("uuid")
        if not expected:
            continue
        actual = subvolume_uuid(entry)
        if actual != expected:
            raise BtrbakError(
                f"restore target already contains {entry} with UUID {actual or 'unknown'}, "
                f"but snapshot {sid} is {expected}; remove it before restoring"
            )
    return len(chain)


def restore(config, profile_name, snapshot_id, target, meta, tmpdir) -> None:
    target = Path(target)
    if target.exists() and not target.is_dir():
        raise BtrbakError(f"restore target exists and is not a directory: {target}")
    if not is_btrfs(target):
        raise BtrbakError(f"restore target must be on a btrfs filesystem: {target}")

    if manifest.get_snapshot(meta, profile_name, snapshot_id) is None:
        raise BtrbakError(f"snapshot not found: {profile_name}/{snapshot_id}")

    chain = build_chain(meta, profile_name, snapshot_id)
    remote_map = {
        spec.id: create_remote(spec) for spec in config.profiles[profile_name].remotes
    }

    # Validate the whole chain, and the resume point, before creating the
    # target: a rejected restore must never leave an empty directory behind.
    for sid in chain:
        snap = manifest.get_snapshot(meta, profile_name, sid)
        if snap.get("type") == "local":
            raise BtrbakError(
                f"snapshot {sid} is local-only and has no offsite copy; "
                "restore it from the local dest instead"
            )
        if pick_remote(snap, remote_map) is None:
            raise BtrbakError(f"no complete remote upload for snapshot {sid}")

    resume_at = _resume_point(target, chain, meta, profile_name)
    if resume_at:
        print(
            f"resuming restore: {resume_at} of {len(chain)} link(s) already "
            f"present in {target}",
            file=sys.stderr,
        )
    # Create the target only once it is known to be usable, so that a failed
    # validation never leaves an empty directory tree behind.
    target.mkdir(parents=True, exist_ok=True)

    for sid in chain[resume_at:]:
        snap = manifest.get_snapshot(meta, profile_name, sid)
        remote = pick_remote(snap, remote_map)

        compression, encryption = codec_for_snapshot(snap, config)
        tmpfile = tmpdir / f"{sid}.send"
        try:
            remote.read(snap["file"], tmpfile)
            if sha256_file(tmpfile) != snap.get("sha256"):
                raise BtrbakError(f"checksum mismatch for snapshot {sid}")
            if tmpfile.stat().st_size != snap.get("size"):
                raise BtrbakError(f"size mismatch for snapshot {sid}")

            send.restore_stream(tmpfile, target, compression, encryption)
        finally:
            tmpfile.unlink(missing_ok=True)


def run_restore(config, profile_name, snapshot_id, target) -> None:
    if profile_name not in config.profiles:
        raise BtrbakError(f"unknown profile: {profile_name}")

    with scratch_dir(config.tmpdir / config.name / "restore") as tmpdir:
        meta = load_meta_for_restore(config, profile_name, tmpdir)
        restore(config, profile_name, snapshot_id, target, meta, tmpdir)


def load_meta_for_restore(config, profile_name, tmpdir) -> dict:
    local = config.dest / "meta.yaml"
    if local.exists():
        return manifest.load(local)

    profile = config.profiles[profile_name]
    if not profile.remotes:
        raise BtrbakError("no remotes configured and no local manifest")

    errors = []
    for spec in profile.remotes:
        remote = create_remote(spec)
        tmp = tmpdir / "meta.yaml"
        try:
            remote.read("meta.yaml", tmp)
            return manifest.load(tmp)
        except Exception as exc:  # noqa: BLE001 - try the next remote
            errors.append(f"{spec.id}: {exc}")
            continue
        finally:
            tmp.unlink(missing_ok=True)
    detail = "; ".join(errors) if errors else "no remotes returned a manifest"
    raise BtrbakError(
        f"could not download a valid meta.yaml from any remote: {detail}"
    )
