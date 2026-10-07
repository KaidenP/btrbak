"""Restore: replay a full + incremental send chain into a btrfs target."""

import os
import sys
from pathlib import Path

from . import manifest
from . import send
from .remotes import create_remote
from .util import (
    BtrbakError,
    RESTORE_SCRATCH,
    is_btrfs,
    is_subvolume,
    private_dir,
    scratch_dir,
    sha256_file,
    subvolume_uuid,
)


def build_chain(meta: dict, profile_name: str, snapshot_id: str) -> list[str]:
    """Return the root→target chain of snapshot ids for *snapshot_id*."""
    by_id = manifest.snapshots_by_id(meta, profile_name)
    chain = []
    current = snapshot_id
    seen = set()
    while current is not None:
        if current in seen:
            raise BtrbakError("dependency cycle in manifest")
        seen.add(current)
        chain.append(current)
        snap = by_id.get(current)
        if snap is None:
            raise BtrbakError(f"missing snapshot in chain: {current}")
        current = snap.get("parent")
    chain.reverse()
    root = by_id.get(chain[0])
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

    A gap (a present link after a missing one), a symlink, or a non-subvolume
    entry under a snapshot id all raise :class:`BtrbakError`, since each would
    block ``btrfs receive`` or silently invalidate the chain.
    """
    by_id = manifest.snapshots_by_id(meta, profile_name)
    resume_at = None
    for index, sid in enumerate(chain):
        entry = target / sid
        present = os.path.lexists(entry)
        if present and resume_at is not None:
            raise BtrbakError(
                f"restore target has a gap: {entry} is present but snapshot "
                f"{chain[resume_at]} (earlier in the chain) is missing; "
                "remove stray subvolumes before restoring"
            )
        if not present:
            if resume_at is None:
                resume_at = index
            continue
        if entry.is_symlink():
            raise BtrbakError(
                f"restore target contains {entry} which is a symlink; remove "
                "it before restoring"
            )
        if not is_subvolume(entry):
            raise BtrbakError(
                f"restore target already contains {entry} which is not a btrfs "
                "subvolume; remove it before restoring"
            )
        expected = (by_id.get(sid) or {}).get("uuid")
        if not expected:
            print(
                f"warning: snapshot {sid} has no recorded uuid; resuming on "
                "name only, which may be incorrect",
                file=sys.stderr,
            )
            continue
        actual = subvolume_uuid(entry)
        if actual != expected:
            raise BtrbakError(
                f"restore target already contains {entry} with UUID {actual or 'unknown'}, "
                f"but snapshot {sid} is {expected}; remove it before restoring"
            )
    return resume_at if resume_at is not None else len(chain)


def download_link(remote, snap: dict, tmpfile: Path) -> None:
    """Download one send stream into *tmpfile* and check it against the manifest.

    The local copy is authoritative, so a stream that does not match the
    recorded ``sha256``/``size`` is rejected here rather than handed to
    ``btrfs receive``, which would fail cryptically or (worse) replay
    something the manifest never described.
    """
    remote.read(snap["file"], tmpfile)
    verify_download(snap, tmpfile)


def verify_download(snap: dict, tmpfile: Path) -> None:
    """Check a staged stream against the manifest's recorded hash/size.

    A missing ``sha256`` or ``size`` (older or hand-edited manifest) skips the
    corresponding check rather than reporting a spurious mismatch.
    """
    if snap.get("sha256") is not None and sha256_file(tmpfile) != snap.get("sha256"):
        raise BtrbakError(f"checksum mismatch for snapshot {snap['id']}")
    if snap.get("size") is not None and tmpfile.stat().st_size != snap.get("size"):
        raise BtrbakError(f"size mismatch for snapshot {snap['id']}")


def restore(config, profile_name, snapshot_id, target, meta, tmpdir) -> None:
    target = Path(target)
    if target.exists() and not target.is_dir():
        raise BtrbakError(f"restore target exists and is not a directory: {target}")
    if not is_btrfs(target):
        raise BtrbakError(f"restore target must be on a btrfs filesystem: {target}")

    if manifest.get_snapshot(meta, profile_name, snapshot_id) is None:
        raise BtrbakError(f"snapshot not found: {profile_name}/{snapshot_id}")

    chain = build_chain(meta, profile_name, snapshot_id)
    by_id = manifest.snapshots_by_id(meta, profile_name)
    remote_map = {
        spec.id: create_remote(spec) for spec in config.profiles[profile_name].remotes
    }

    # Validate the links still to be received, and the resume point, before
    # creating the target: a rejected restore must never leave an empty
    # directory behind, and a link already present (per _resume_point) must
    # not block a resume just because its remote upload was removed. Every
    # chain id is guaranteed present in by_id: build_chain just walked the
    # same index and raised on anything missing.
    resume_at = _resume_point(target, chain, meta, profile_name)
    if resume_at:
        print(
            f"resuming restore: {resume_at} of {len(chain)} link(s) already "
            f"present in {target}",
            file=sys.stderr,
        )
    pending = chain[resume_at:]

    for sid in pending:
        snap = by_id[sid]
        if snap.get("type") == "local":
            raise BtrbakError(
                f"snapshot {sid} is local-only and has no offsite copy; "
                "restore it from the local dest instead"
            )
        if not snap.get("file"):
            raise BtrbakError(
                f"snapshot {sid} has no 'file' recorded; cannot restore it"
            )
        if pick_remote(snap, remote_map) is None:
            raise BtrbakError(f"no complete remote upload for snapshot {sid}")

    # Fetch and verify the first remaining link *before* the target exists.
    # Everything checkable from the manifest has been checked above; the one
    # thing left is the object's own integrity, and discovering a corrupt or
    # unreachable stream must not leave an empty TARGET behind. The staged
    # file is then reused for that link, so this costs no extra download.
    staged: Path | None = None
    if pending:
        first = by_id[pending[0]]
        staged = tmpdir / f"{pending[0]}.send"
        try:
            download_link(pick_remote(first, remote_map), first, staged)
        except BaseException:
            staged.unlink(missing_ok=True)
            raise

    # Create the target only once it is known to be usable: every manifest
    # check has passed and the first stream is downloaded and verified. A
    # restore rejected at any earlier point therefore never leaves an empty
    # directory tree behind. The mkdir and the replay both live under the one
    # finally so a mkdir failure still cleans up the staged download.
    try:
        private_dir(target)
        for index, sid in enumerate(pending):
            snap = by_id[sid]
            tmpfile = staged if index == 0 else tmpdir / f"{sid}.send"
            try:
                if index:
                    download_link(pick_remote(snap, remote_map), snap, tmpfile)
                # Re-verify the staged bytes immediately before replay: the
                # check in download_link applied at download time and a
                # concurrent writer to the scratch dir must not substitute a
                # different stream in between (BTR-014).
                verify_download(snap, tmpfile)
                # Narrow the check→receive TOCTOU: _resume_point already
                # skipped links present at startup, but a subvolume can still
                # appear at the link path before btrfs receive runs.
                if os.path.lexists(target / sid):
                    raise BtrbakError(
                        f"restore target already contains {target / sid}; "
                        "remove it before restoring"
                    )
                compression, encryption = codec_for_snapshot(snap, config)
                send.restore_stream(tmpfile, target, compression, encryption)
            finally:
                if index:
                    tmpfile.unlink(missing_ok=True)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def _sweep_stale_scratch(tmpdir: Path) -> None:
    """Remove stale download/decrypt intermediates from a previous run.

    These are only cleaned by ``finally`` blocks, so an unclean termination
    (SIGKILL, power loss) can leave decrypted plaintext behind; sweep it at
    the start of every restore instead of waiting for ``run``'s 24 h tmpdir
    sweeper.
    """
    if not tmpdir.is_dir():
        return
    for pattern in ("*.send", "*.dec", "*.decx"):
        for stale in tmpdir.glob(pattern):
            try:
                stale.unlink()
            except OSError:
                pass


def run_restore(config, profile_name, snapshot_id, target) -> None:
    if profile_name not in config.profiles:
        raise BtrbakError(f"unknown profile: {profile_name}")

    with scratch_dir(
        config.tmpdir / config.name / RESTORE_SCRATCH, config.tmpdir
    ) as tmpdir:
        _sweep_stale_scratch(tmpdir)
        meta = load_meta_for_restore(config, profile_name, tmpdir)
        restore(config, profile_name, snapshot_id, target, meta, tmpdir)


def load_meta_for_restore(config, profile_name, tmpdir) -> dict:
    local = config.dest / "meta.yaml"
    errors = []
    if local.exists():
        try:
            return manifest.load(local)
        except BtrbakError as exc:
            # A corrupt local manifest should not block restore when a remote
            # holds a valid copy (BTR-034); remember the local failure for the
            # final message if no remote copy works either, and tell the admin
            # that a fallback is happening rather than silently ignoring it.
            errors.append(f"local: {exc}")
            print(
                f"warning: local meta.yaml is unusable ({exc}); "
                "trying a remote copy",
                file=sys.stderr,
            )

    profile = config.profiles[profile_name]
    if not profile.remotes:
        if errors:
            raise BtrbakError(
                f"local meta.yaml is unusable and no remotes are configured: "
                f"{errors[0]}"
            )
        raise BtrbakError("no remotes configured and no local manifest")

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
