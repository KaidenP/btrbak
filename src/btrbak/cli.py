"""CLI entrypoint for btrbak."""

import argparse
import os
import sys
import time

from . import config as config_mod
from . import manifest
from . import restore as restore_mod
from . import retention
from . import send
from . import snapshot
from . import timespan
from . import util
from .remotes import create_remote
from .remotes.base import RemoteError, RemoteNotFoundError

VERBOSITY = 0


def _log(level, message) -> None:
    if VERBOSITY >= level:
        print(message, file=sys.stderr)


def main(argv=None) -> int:
    global VERBOSITY
    parser = build_parser()
    args = parser.parse_args(argv)
    VERBOSITY = args.verbose + getattr(args, "verbose_extra", 0)
    if os.geteuid() != 0:
        print("btrbak: must be run as root", file=sys.stderr)
        return 1

    try:
        return args.func(args)
    except KeyboardInterrupt:
        # 130 is the conventional shell status for SIGINT. Handled here so a
        # Ctrl-C during, say, the nesting grace period aborts quietly instead
        # of dumping a traceback.
        print("\nbtrbak: interrupted", file=sys.stderr)
        return 130
    except config_mod.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except (util.BtrbakError, RemoteError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    # `-v` is accepted both before and after the subcommand. The subparser
    # copy suppresses its default so that a value given before the subcommand
    # is not clobbered; the two counts are summed in main().
    global_verbosity = argparse.ArgumentParser(add_help=False)
    global_verbosity.add_argument(
        "-v", "--verbose", action="count", default=0, help="increase verbosity"
    )
    sub_verbosity = argparse.ArgumentParser(add_help=False)
    sub_verbosity.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=argparse.SUPPRESS,
        dest="verbose_extra",
        help="increase verbosity",
    )

    parser = argparse.ArgumentParser(
        prog="btrbak",
        description="btrfs snapshot and offsite backup utility",
        parents=[global_verbosity],
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_config = sub.add_parser(
        "config", help="configuration commands", parents=[sub_verbosity]
    )
    config_sub = p_config.add_subparsers(dest="config_command", required=True)
    p_check = config_sub.add_parser(
        "check", help="validate configuration", parents=[sub_verbosity]
    )
    p_check.set_defaults(func=cmd_config_check)

    p_run = sub.add_parser(
        "run",
        help="create due snapshots/backups and prune",
        parents=[sub_verbosity],
    )
    p_run.add_argument("subvol", nargs="?")
    p_run.add_argument("profile", nargs="?")
    p_run.add_argument("--force", action="store_true")
    p_run.add_argument("--force-config", action="store_true")
    p_run.add_argument("--full", action="store_true")
    p_run.add_argument("-n", "--dry-run", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_verify = sub.add_parser(
        "verify",
        help="check remote files against the manifest",
        parents=[sub_verbosity],
    )
    p_verify.add_argument("subvol", nargs="?")
    p_verify.add_argument("profile", nargs="?")
    p_verify.set_defaults(func=cmd_verify)

    p_list = sub.add_parser(
        "list", help="list snapshots and backups", parents=[sub_verbosity]
    )
    p_list.add_argument("subvol", nargs="?")
    p_list.add_argument("profile", nargs="?")
    p_list.set_defaults(func=cmd_list)

    p_restore = sub.add_parser(
        "restore", help="restore a snapshot chain to a target", parents=[sub_verbosity]
    )
    p_restore.add_argument("subvol")
    p_restore.add_argument("profile")
    p_restore.add_argument("snapshot_id")
    p_restore.add_argument("target")
    p_restore.set_defaults(func=cmd_restore)

    return parser


# --- run -------------------------------------------------------------------


def _discover(subvol):
    """Return ``(config, error)`` for every config file in scope.

    Wraps :func:`config.discover_configs_tolerant` so each command reports a
    config that fails to load and keeps going with the rest, instead of one
    broken profile file masking every other one.
    """
    return config_mod.discover_configs_tolerant(subvol)


def cmd_run(args) -> int:
    selected_configs = []
    config_errors = 0
    for path, cfg, error in _discover(args.subvol):
        if error is not None:
            print(f"config error: {error}", file=sys.stderr)
            config_errors += 1
            continue
        selected = config_mod.select_profiles(cfg, args.profile)
        if selected is None:
            continue
        # The unfiltered profile names travel with the selection so a run can
        # tell an orphaned manifest profile apart from one merely filtered out
        # by PROFILE.
        selected_configs.append((selected, set(cfg.profiles)))
    if args.profile and not selected_configs:
        raise config_mod.ConfigError(f"unknown profile: {args.profile!r}")

    upload_failures = 0
    runtime_failures = 0
    for cfg, configured in selected_configs:
        try:
            upload_failures += run_config(
                cfg,
                None,
                args.force,
                args.force_config,
                args.full,
                args.dry_run,
                configured=configured,
            )
        except config_mod.ConfigError as exc:
            config_errors += 1
            print(f"config error ({cfg.name}): {exc}", file=sys.stderr)
        except (util.BtrbakError, RemoteError, OSError) as exc:
            runtime_failures += 1
            print(f"error ({cfg.name}): {exc}", file=sys.stderr)
    if upload_failures:
        print(
            f"warning: {upload_failures} upload(s) failed; see errors above",
            file=sys.stderr,
        )
    if config_errors:
        return 2
    return 1 if upload_failures or runtime_failures else 0


def run_config(
    cfg, profile_filter, force, force_config, full, dry_run, configured=None
) -> int:
    selected = config_mod.filter_profiles(cfg, profile_filter)
    errors, warnings = config_mod.validate(
        selected, check_remotes=not dry_run, check_nesting=False
    )
    if errors:
        raise config_mod.ConfigError("\n".join(errors))
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    # The nesting warning is emitted here rather than by validate() so a run
    # reports it exactly once, together with the 5s grace period.
    if config_mod.is_nested(selected.dest, selected.src):
        message = (
            f"warning: dest is nested inside src ({selected.dest}); "
            "snapshots may be picked up as nested subvolumes"
        )
        if dry_run:
            print(message, file=sys.stderr)
        else:
            print(f"{message}; Ctrl-C within 5s to abort", file=sys.stderr)
            time.sleep(5)

    configured_profiles = set(cfg.profiles) if configured is None else set(configured)

    if dry_run:
        meta = manifest.load(selected.dest / "meta.yaml")
        _warn_orphans(selected.name, meta, configured_profiles)
        now_ts = util.now()
        for pname, profile in selected.profiles.items():
            due, stype, parent = compute_plan(profile, meta, now_ts, force, full)
            if due:
                print(
                    f"[dry-run] {selected.name}/{pname}: would create {stype} snapshot"
                    f" (parent={parent or '-'})"
                )
            else:
                print(f"[dry-run] {selected.name}/{pname}: nothing due")

        # Uploads that are still outstanding are retried before anything new
        # is created, so a dry run that stayed silent about them would
        # under-report the work a real run does (§12).
        for pname, profile in selected.profiles.items():
            for snap in manifest.snapshots(meta, pname):
                if manifest.committed(snap):
                    continue
                remote_ids = [spec.id for spec in profile.remotes]
                pending = pending_upload_remotes(snap, remote_ids)
                if not pending:
                    continue
                # retry_upload() only re-sends when no remote still holds a
                # copy matching the recorded checksum; otherwise it reuses
                # that copy (age is non-deterministic, so re-sending would
                # change the bytes). The preview says which path it expects.
                reusable = [rid for rid in remote_ids if rid not in pending]
                if reusable:
                    print(
                        f"[dry-run] {selected.name}/{pname}: would retry upload of "
                        f"{snap.get('id')} on {', '.join(pending)}"
                    )
                else:
                    print(
                        f"[dry-run] {selected.name}/{pname}: would re-send and "
                        f"upload {snap.get('id')} to {', '.join(pending)}"
                    )

        dry_run_config_sync(selected, collect_remotes(selected), force_config)

        for pname, profile in selected.profiles.items():
            for sid in retention.plan_prune(meta, pname, profile.keep, now_ts):
                print(f"[dry-run] {selected.name}/{pname}: would prune snapshot {sid}")
        return 0

    selected.dest.mkdir(parents=True, exist_ok=True)
    with util.exclusive_lock(selected.dest / ".btrbak.lock"):
        meta_path = selected.dest / "meta.yaml"
        meta = manifest.load(meta_path)
        _warn_orphans(selected.name, meta, configured_profiles)
        ensure_profile_meta(meta, selected)
        by_profile = collect_remotes(selected)
        sync_settings(selected, unique_remotes(by_profile), force_config)

        # Reap stale staging files only while holding the same advisory lock
        # `verify` and `restore` use, so a long-running restore can never have
        # its staging directory swept out from under it. Contention is benign
        # here: skipping the sweep must not block the backup itself.
        with util.optional_lock(selected.tmpdir / (selected.name + ".lock")) as got:
            if got:
                clean_tmpdir(selected)
            else:
                _log(1, "staging directory is in use by another command; skipping cleanup")

        failures = 0
        for pname, profile in selected.profiles.items():
            try:
                failures += run_profile(selected, profile, meta, by_profile[pname], force, full)
            finally:
                manifest.save(meta_path, meta)
                _push_manifest_best_effort(meta_path, by_profile)

        try:
            prune(selected, meta, by_profile, meta_path)
        except BaseException:
            # Persist any deletions already applied before re-raising so the
            # on-disk manifest never refers to subvolumes that were removed.
            manifest.save(meta_path, meta)
            _push_manifest_best_effort(meta_path, by_profile)
            raise
        manifest.save(meta_path, meta)
        # Best-effort like every other push: the local manifest is
        # authoritative and the remote copy is refreshed on the next run, so a
        # remote hiccup here must not fail an otherwise successful backup.
        _push_manifest_best_effort(meta_path, by_profile)
    return failures


def compute_plan(profile, meta, now_ts, force=False, full=False):
    """Return ``(due, type, parent_id)`` for the next backup."""
    pname = profile.name
    last = manifest.last_committed(meta, pname)

    if not profile.remotes:
        candidates = [
            value
            for value in (profile.freq_full, profile.freq_incr)
            if not timespan.is_never(value)
        ]
        if last is None:
            due = bool(candidates)
        else:
            due = bool(candidates) and (now_ts - manifest.created(last) >= min(candidates))
        if due or force:
            return True, "local", None
        return False, None, None

    last_full = manifest.last_full_committed(meta, pname)
    # Only a snapshot with an offsite copy can be a send parent: parenting an
    # incremental onto a local-only entry yields a chain whose root has no
    # send file, so the whole chain becomes unrestorable. That is exactly the
    # state a profile is in when remotes are added to one that has been
    # snapshotting locally, and it also makes the profile look like it already
    # has a backup when it does not.
    last = manifest.last_remote_committed(meta, pname)
    has_auto = (not timespan.is_never(profile.freq_full)) or (
        not timespan.is_never(profile.freq_incr)
    )
    full_due = (
        (last is None and has_auto)
        or (
            (not timespan.is_never(profile.freq_full))
            and (last_full is None or now_ts - manifest.created(last_full) >= profile.freq_full)
        )
    )
    incr_due = (
        (not timespan.is_never(profile.freq_incr))
        and (not full_due)
        and (last is not None and now_ts - manifest.created(last) >= profile.freq_incr)
    )
    due = full_due or incr_due
    if not (due or force or full):
        return False, None, None

    if full or full_due:
        stype = "full"
    elif last is None:
        stype = "full"
    else:
        stype = "incr"
    parent = last["id"] if stype == "incr" else None
    return True, stype, parent


def run_profile(cfg, profile, meta, remotes, force, full) -> int:
    pname = profile.name
    failures = 0
    current_ids = {spec.id for spec, _ in remotes}
    for snap in manifest.snapshots(meta, pname):
        reconcile_uploads(snap, current_ids)
        if not manifest.committed(snap):
            _log(1, f"{pname}: retrying incomplete snapshot {snap['id']}")
            failures += retry_upload(cfg, profile, snap, remotes)

    now_ts = util.now()
    due, stype, parent_id = compute_plan(profile, meta, now_ts, force, full)
    if not due:
        return failures

    snap_id = unique_snapshot_id(
        util.snapshot_id(now_ts), cfg.dest / pname, meta, pname
    )
    snap_path = cfg.dest / pname / snap_id
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot.create_ro_snapshot(cfg.src, snap_path)

    entry = {
        "id": snap_id,
        "created": now_ts,
        "type": stype,
        "parent": parent_id,
        "uuid": util.subvolume_uuid(snap_path),
        "compression": cfg.compression if remotes else None,
        "encryption": cfg.encryption if remotes else None,
        "file": f"{pname}/{snap_id}.send" if remotes else None,
        "sha256": None,
        "size": None,
        "uploads": [],
    }
    manifest.add_snapshot(meta, pname, entry)

    if remotes:
        failures += perform_send_upload(cfg, profile, entry, snap_path, parent_id, remotes)
    _log(1, f"{pname}: created {stype} snapshot {snap_id}")
    return failures


def _cleanup_staging(staged, root) -> None:
    """Remove a staged send file and the directories it emptied above it.

    The per-profile and per-subvol staging directories exist only to hold the
    file being sent, so a run must not leave them behind for the next
    ``clean_tmpdir`` sweep to discover. Directories that still hold files are
    left alone, and *root* (``tmpdir``) is never removed.
    """
    staged.unlink(missing_ok=True)
    util.prune_empty_dir(staged.parent, root)


def perform_send_upload(cfg, profile, entry, snap_path, parent_id, remotes) -> int:
    pname = profile.name
    staged = cfg.tmpdir / cfg.name / pname / f"{entry['id']}.send"
    util.private_dir(staged.parent)
    try:
        parent_path = cfg.dest / pname / parent_id if parent_id else None
        send.send_snapshot(
            snap_path, parent_path, staged, cfg.compression, cfg.encryption
        )
        entry["sha256"] = util.sha256_file(staged)
        entry["size"] = staged.stat().st_size

        failed = 0
        for spec, remote in remotes:
            status = "complete"
            try:
                remote.write(staged, entry["file"])
            except Exception as exc:  # noqa: BLE001 - record per-remote failure
                print(f"upload to {spec.id} failed: {exc}", file=sys.stderr)
                status = "failed"
                failed += 1
            entry["uploads"].append({"remote": spec.id, "status": status})
        return failed
    finally:
        _cleanup_staging(staged, cfg.tmpdir)


def retry_upload(cfg, profile, snap, remotes) -> int:
    """Bring a snapshot's offsite copies back to the manifest's canonical file.

    When at least one remote already holds a copy matching the recorded
    ``sha256``/``size``, that copy is reused and only the missing remotes are
    filled in (age encryption is non-deterministic, so re-sending would change
    the bytes and invalidate the checksum). Otherwise the snapshot is re-sent
    and every remote is refreshed. Returns the number of uploads that still
    failed.
    """
    if not remotes or not snap.get("file"):
        return 0

    pname = profile.name
    uploads = snap.setdefault("uploads", [])
    by_remote = {spec.id: (spec, remote) for spec, remote in remotes}
    remote_ids = [spec.id for spec, _ in remotes]

    def is_complete(rid):
        return any(
            u.get("remote") == rid and u.get("status") == "complete"
            for u in uploads
        )

    pending = pending_upload_remotes(snap, remote_ids)
    if not pending:
        return 0

    staged = cfg.tmpdir / cfg.name / pname / f"{snap['id']}.send"
    util.private_dir(staged.parent)
    try:
        reuse_existing = False
        for rid, (spec, remote) in by_remote.items():
            if not is_complete(rid):
                continue
            try:
                remote.read(snap["file"], staged)
            except Exception:  # noqa: BLE001 - try the next complete remote
                continue
            if util.sha256_file(staged) == snap.get("sha256") and staged.stat().st_size == snap.get("size"):
                reuse_existing = True
                break

        if not reuse_existing:
            snap_path = cfg.dest / pname / snap["id"]
            if not snap_path.exists():
                print(f"snapshot {snap['id']} missing locally; cannot retry", file=sys.stderr)
                return len(pending)
            parent_path = cfg.dest / pname / snap["parent"] if snap.get("parent") else None
            send.send_snapshot(
                snap_path, parent_path, staged, cfg.compression, cfg.encryption
            )
            snap["sha256"] = util.sha256_file(staged)
            snap["size"] = staged.stat().st_size
            snap["compression"] = cfg.compression
            snap["encryption"] = cfg.encryption

        failed = 0
        for spec, remote in remotes:
            existing = next((u for u in uploads if u.get("remote") == spec.id), None)
            if reuse_existing and existing and existing.get("status") == "complete":
                continue
            status = "complete"
            try:
                remote.write(staged, snap["file"])
            except Exception as exc:  # noqa: BLE001
                print(f"upload to {spec.id} failed: {exc}", file=sys.stderr)
                status = "failed"
                failed += 1
            if existing:
                existing["status"] = status
            else:
                uploads.append({"remote": spec.id, "status": status})
        return failed
    finally:
        _cleanup_staging(staged, cfg.tmpdir)


def reconcile_uploads(snap, current_ids) -> None:
    """Reconcile upload records against the currently configured remotes.

    - Drop records for remotes that were removed.
    - De-duplicate records for a remote, keeping the most favourable status.
    - Add a ``failed`` record for every newly configured remote so existing
      snapshots are backfilled to it on the next retry.
    - Mark a remote-backed snapshot ``committed`` only when no remotes remain
      to upload to (all were removed). A ``local_deleted`` snapshot is never
      backfilled because its local subvolume is already gone.
    """
    uploads = snap.get("uploads", [])
    best = {}
    order = []
    for upload in uploads:
        remote_id = upload.get("remote")
        if remote_id not in current_ids:
            continue
        if remote_id not in best:
            best[remote_id] = upload
            order.append(remote_id)
        elif (
            upload.get("status") == "complete"
            and best[remote_id].get("status") != "complete"
        ):
            best[remote_id] = upload

    if snap.get("type") != "local" and not snap.get("local_deleted"):
        if current_ids:
            for rid in current_ids:
                if rid not in best:
                    best[rid] = {"remote": rid, "status": "failed"}
                    order.append(rid)
            snap.pop("committed", None)
        elif not best:
            snap["committed"] = True

    kept = [best[rid] for rid in order]
    if kept != uploads:
        snap["uploads"] = kept


def prune(cfg, meta, by_profile, meta_path=None) -> None:
    """Delete expired snapshots and persist the manifest after each change.

    ``meta_path``, when provided, is rewritten after every manifest mutation so
    an aborted prune never leaves on-disk state pointing at deleted subvolumes.
    """
    now_ts = util.now()
    for pname, profile in cfg.profiles.items():
        by_id = manifest.snapshots_by_id(meta, pname)
        for sid in retention.plan_prune(meta, pname, profile.keep, now_ts):
            snap = by_id.get(sid)
            snap_path = cfg.dest / pname / sid
            if snap_path.exists():
                snapshot.delete_snapshot(snap_path)
            delete_ok = True
            if snap and snap.get("file"):
                for spec, remote in by_profile.get(pname, []):
                    try:
                        remote.delete(snap["file"])
                    except RemoteNotFoundError:
                        pass
                    except Exception as exc:  # noqa: BLE001
                        print(f"remote delete on {spec.id} failed: {exc}", file=sys.stderr)
                        delete_ok = False
            if not delete_ok:
                # The local subvolume is already gone. Keep the manifest entry
                # so the remote delete is retried on the next run, but flag it
                # so it is never chosen as a future incremental send parent
                # (its local data no longer exists).
                if snap:
                    snap["local_deleted"] = True
                _save_prune_meta(meta_path, meta)
                break
            manifest.remove_snapshot(meta, pname, sid)
            _save_prune_meta(meta_path, meta)


def _save_prune_meta(meta_path, meta) -> None:
    if meta_path is not None:
        manifest.save(meta_path, meta)


# --- helpers ---------------------------------------------------------------


def collect_remotes(cfg):
    """Instantiate each profile's remotes (one instance per spec).

    Instances are intentionally not shared across profiles so two remotes that
    share a ``name`` but differ in settings never alias each other.
    """
    by_profile = {}
    for pname, profile in cfg.profiles.items():
        by_profile[pname] = [(spec, create_remote(spec)) for spec in profile.remotes]
    return by_profile


def unique_remotes(by_profile):
    """Return the distinct remote endpoints across all profiles."""
    seen = set()
    unique = []
    for entries in by_profile.values():
        for spec, remote in entries:
            key = config_mod.remote_identity(spec)
            if key in seen:
                continue
            seen.add(key)
            unique.append((spec, remote))
    return unique


def _config_sync_state(remote, local_bytes, tmp) -> str:
    """Classify how the remote's ``config.yaml`` compares to *local_bytes*.

    Returns ``"missing"`` when the remote has no copy yet, ``"in-sync"`` when
    the bytes are identical, and ``"differs"`` otherwise. *tmp* is a scratch
    path for the download; the caller owns it.
    """
    try:
        remote.read("config.yaml", tmp)
    except RemoteNotFoundError:
        return "missing"
    return "in-sync" if tmp.read_bytes() == local_bytes else "differs"


def sync_settings(cfg, remotes, force_config) -> None:
    local_bytes = cfg.path.read_bytes()
    # scratch_dir removes the staging root again once the comparison file is
    # gone, so a run does not leave an empty directory behind in tmpdir.
    with util.scratch_dir(cfg.tmpdir / cfg.name, cfg.tmpdir) as staging:
        for spec, remote in remotes:
            tmp = staging / "config.yaml.check"
            try:
                state = _config_sync_state(remote, local_bytes, tmp)
            finally:
                tmp.unlink(missing_ok=True)

            if state == "missing" or (state == "differs" and force_config):
                remote.write(cfg.path, "config.yaml")
            elif state == "differs":
                raise util.BtrbakError(
                    f"remote {spec.id} already has a different config.yaml; "
                    "use --force-config to overwrite"
                )


def dry_run_config_sync(cfg, by_profile, force_config) -> None:
    """Report what ``sync_settings`` would do for each remote, without writing.

    This is the only remote access ``--dry-run`` performs and it is strictly
    read-only: the downloaded copy is staged in a temp file that is removed
    again immediately, along with the directory holding it, so a dry run writes
    nothing at all. Reporting the real state matters because a differing remote
    ``config.yaml`` aborts a real run unless ``--force-config`` is given.
    """
    local_bytes = cfg.path.read_bytes()
    with util.scratch_dir(cfg.tmpdir / cfg.name, cfg.tmpdir) as staging:
        tmp = staging / "config.yaml.dry-run"
        for spec, remote in unique_remotes(by_profile):
            try:
                state = _config_sync_state(remote, local_bytes, tmp)
            except Exception as exc:  # noqa: BLE001 - a dry run must not fail hard
                print(
                    f"[dry-run] could not read config.yaml from remote {spec.id}: {exc}",
                    file=sys.stderr,
                )
                continue
            finally:
                tmp.unlink(missing_ok=True)

            if state == "missing":
                print(f"[dry-run] would upload config.yaml to remote {spec.id}")
            elif state == "in-sync":
                print(f"[dry-run] config.yaml already in sync on remote {spec.id}")
            elif force_config:
                print(f"[dry-run] would overwrite differing config.yaml on remote {spec.id}")
            else:
                print(
                    f"[dry-run] remote {spec.id} has a differing config.yaml; "
                    "this run would fail without --force-config",
                    file=sys.stderr,
                )


def push_manifest(meta_path, by_profile) -> None:
    seen = set()
    for entries in by_profile.values():
        for spec, remote in entries:
            key = config_mod.remote_identity(spec)
            if key in seen:
                continue
            seen.add(key)
            remote.write(meta_path, "meta.yaml")


def _push_manifest_best_effort(meta_path, by_profile) -> None:
    try:
        push_manifest(meta_path, by_profile)
    except Exception as exc:  # noqa: BLE001 - keep local state authoritative
        print(f"warning: failed to push manifest to remote: {exc}", file=sys.stderr)


def ensure_profile_meta(meta, cfg) -> None:
    """Ensure every configured profile has an entry recording its ``src``.

    The entry is rebuilt when ``src`` is absent so that it is serialised
    *before* ``snapshots``, keeping ``meta.yaml`` readable and diff-friendly
    even for manifests written by earlier versions.
    """
    for pname in cfg.profiles:
        entry = manifest.profile(meta, pname)
        if entry.get("src"):
            continue
        rest = {k: v for k, v in entry.items() if k != "src"}
        entry.clear()
        entry["src"] = str(cfg.src)
        entry.update(rest)


def orphaned_profiles(meta, configured) -> list[str]:
    """Return the manifest profiles that are no longer configured.

    Nothing prunes, verifies or lists a profile that the config does not
    define, so deleting a profile from its config file silently orphans
    everything that profile left behind: the subvolumes under
    ``<dest>/<profile>/``, the offsite objects, and the ``meta.yaml`` entry
    itself all stay put forever. Reporting them is the only signal that those
    copies now need removing by hand (§12).

    *configured* must be the profile names of the **unfiltered** config: with
    ``btrbak list SUBVOL PROFILE`` every other profile is absent from the
    selection but is not orphaned, and comparing against the selection would
    report all of them.
    """
    recorded = meta.get("profiles") or {}
    return sorted(set(recorded) - set(configured))


def pending_upload_remotes(snap, remote_ids) -> list[str]:
    """Return the remotes *snap* still owes a ``complete`` upload to.

    Shared by ``retry_upload`` and ``--dry-run`` so the preview and the real
    run agree on what is outstanding.
    """
    complete = {
        upload.get("remote")
        for upload in (snap.get("uploads") or [])
        if upload.get("status") == "complete"
    }
    return [rid for rid in remote_ids if rid not in complete]


def _warn_orphans(where, meta, configured) -> None:
    """Surface profiles recorded in the manifest but absent from the config."""
    for pname in orphaned_profiles(meta, configured):
        count = len(manifest.snapshots(meta, pname))
        plural = "snapshot" if count == 1 else "snapshots"
        print(
            f"warning: {where}/{pname}: {count} {plural} recorded in meta.yaml "
            "but the profile is no longer configured; they are no longer pruned "
            "or verified, so remove them by hand or restore the profile",
            file=sys.stderr,
        )


def report_orphans(where, meta, configured) -> int:
    """Print one ``ORPHANED PROFILE`` line per unconfigured manifest profile."""
    orphans = orphaned_profiles(meta, configured)
    for pname in orphans:
        count = len(manifest.snapshots(meta, pname))
        plural = "snapshot" if count == 1 else "snapshots"
        print(
            f"ORPHANED PROFILE {where}/{pname}: {count} {plural} recorded in "
            "meta.yaml but the profile is no longer configured; they are no "
            "longer pruned or verified, so remove them by hand or restore the "
            "profile"
        )
    return len(orphans)


def unique_snapshot_id(base, profile_dir, meta=None, profile=None) -> str:
    """Return a snapshot id not already in use in *profile_dir* or *meta*.

    Two runs within one second collide on the base id. The filesystem alone
    is not enough to detect that, though: a manifest entry can outlive its
    subvolume (``local_deleted`` after a failed remote delete, or a ``dest``
    wiped out of from under btrbak), leaving the id free on disk but still
    recorded. Reusing it would append a duplicate entry and make
    ``meta.yaml`` unloadable, so the manifest is consulted as well.
    """
    taken = set()
    if meta is not None and profile is not None:
        taken = {
            snap["id"]
            for snap in manifest.snapshots(meta, profile)
            if isinstance(snap.get("id"), str)
        }
    candidate = base
    index = 1
    while candidate in taken or (profile_dir / candidate).exists():
        candidate = f"{base}-{index}"
        index += 1
    return candidate


def clean_tmpdir(cfg, max_age=86400) -> None:
    root = cfg.tmpdir / cfg.name
    if not root.exists():
        return
    now_ts = util.now()
    for path in root.rglob("*"):
        if path.is_file():
            try:
                if now_ts - path.stat().st_mtime > max_age:
                    path.unlink()
            except OSError:
                pass
    # Prune now-empty directories leaf-first, keeping the staging root itself.
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass


def _snapshot_depths(meta, pname) -> dict:
    """Return ``{snapshot id: dependency depth}`` for every snapshot in *pname*.

    Depth 0 is a chain root; each parent link adds one. The walk is iterative:
    a chain is only bounded by retention in practice, and a recursive version
    both overflowed the stack on long chains and re-scanned the snapshot list
    for every level (``get_snapshot`` is a linear lookup). Snapshots whose
    parent is missing or cyclic settle at the depth reached, which keeps a
    hand-edited manifest from taking `list` down with a traceback.
    """
    snaps = [snap for snap in manifest.snapshots(meta, pname) if snap.get("id")]
    parents = {snap["id"]: snap.get("parent") for snap in snaps}
    depths: dict = {}
    for snap in snaps:
        sid = snap["id"]
        if sid in depths:
            continue
        # Walk up to the nearest ancestor of known depth, recording the way
        # back down; this keeps every snapshot O(chain length) in total
        # instead of re-walking a shared prefix.
        chain, seen, cursor = [], set(), sid
        while cursor not in depths and cursor not in seen:
            seen.add(cursor)
            chain.append(cursor)
            parent = parents.get(cursor)
            if parent not in parents:
                break
            cursor = parent
        base = depths.get(cursor, -1)
        for node in reversed(chain):
            base += 1
            depths[node] = base
    return depths


# --- other commands --------------------------------------------------------


def cmd_config_check(args) -> int:
    try:
        discovered = _discover(None)
    except config_mod.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    return_code = 0
    for path, cfg, error in discovered:
        if error is not None:
            print(f"{path.stem}:")
            print(f"  error: {error}")
            return_code = 2
            continue
        errors, warnings = config_mod.validate(cfg)
        print(f"{cfg.name}:")
        for warning in warnings:
            print(f"  warning: {warning}")
        for err in errors:
            print(f"  error: {err}")
        if errors:
            return_code = 2
        elif not warnings:
            # A clean config otherwise prints a bare name with nothing under
            # it, which reads like the report was truncated rather than like
            # everything passing.
            print("  ok")
    return return_code


def cmd_list(args) -> int:
    matched = False
    config_errors = 0
    for path, cfg, error in _discover(args.subvol):
        if error is not None:
            print(f"config error: {error}", file=sys.stderr)
            config_errors += 1
            continue
        selected = config_mod.select_profiles(cfg, args.profile)
        if selected is None:
            continue
        matched = True
        meta_path = cfg.dest / "meta.yaml"
        meta = manifest.load(meta_path)
        if not meta_path.exists():
            print(
                f"warning: {cfg.name}: no local manifest at {meta_path}; "
                "listing nothing",
                file=sys.stderr,
            )
        report_orphans(cfg.name, meta, cfg.profiles)
        for pname in selected.profiles:
            snaps = manifest.snapshots(meta, pname)
            count = f" ({len(snaps)} snapshot{'' if len(snaps) == 1 else 's'})" if snaps else ""
            print(f"{cfg.name}/{pname}:" + count)
            depths = _snapshot_depths(meta, pname)
            for snap in snaps:
                depth = depths.get(snap.get("id"), 0)
                age = util.now() - manifest.created(snap)
                uploads = ",".join(
                    f"{u.get('remote') or '?'}={u.get('status') or '?'}"
                    for u in snap.get("uploads", [])
                )
                print(
                    f"{'  ' * (depth + 1)}{snap.get('id')}  type={snap.get('type')}  "
                    f"age={age}s  parent={snap.get('parent') or '-'}  uploads=[{uploads}]"
                )
    if args.profile and not matched:
        raise config_mod.ConfigError(f"unknown profile: {args.profile!r}")
    return 2 if config_errors else 0


def _load_meta_for_verify(cfg):
    """Return ``(meta, used_remote)`` for verification.

    Prefer the local ``dest/meta.yaml``; when it is missing, download a copy
    from the first configured remote that has one. Returns ``(None, False)``
    when no manifest is available anywhere.
    """
    local = cfg.dest / "meta.yaml"
    if local.exists():
        return manifest.load(local), False

    with util.scratch_dir(
        cfg.tmpdir / cfg.name / util.VERIFY_SCRATCH, cfg.tmpdir
    ) as tmpdir:
        seen = set()
        for profile in cfg.profiles.values():
            for spec in profile.remotes:
                key = config_mod.remote_identity(spec)
                if key in seen:
                    continue
                seen.add(key)
                remote = create_remote(spec)
                tmp = tmpdir / "meta.yaml"
                try:
                    remote.read("meta.yaml", tmp)
                    return manifest.load(tmp), True
                except Exception:  # noqa: BLE001 - try the next remote
                    continue
                finally:
                    tmp.unlink(missing_ok=True)
    return None, False


def cmd_verify(args) -> int:
    total_failures = 0
    config_errors = 0
    matched = False
    for path, cfg, error in _discover(args.subvol):
        if error is not None:
            # Report and keep going: one broken config must not hide the
            # verification result of every other config.
            print(f"config error: {error}", file=sys.stderr)
            config_errors += 1
            continue
        selected = config_mod.select_profiles(cfg, args.profile)
        if selected is None:
            continue
        matched = True

        remote_errors = config_mod.validate_remote_config(selected)
        if remote_errors:
            # Report and keep going: one broken config must not hide the
            # verification result of every other config.
            config_errors += 1
            print(
                f"config error ({cfg.name}): {'; '.join(remote_errors)}",
                file=sys.stderr,
            )
            continue

        try:
            total_failures += _verify_config(selected, configured=cfg.profiles)
        except (util.BtrbakError, RemoteError, OSError) as exc:
            print(f"error ({cfg.name}): {exc}", file=sys.stderr)
            total_failures += 1
    if args.profile and not matched:
        raise config_mod.ConfigError(f"unknown profile: {args.profile!r}")
    if config_errors:
        return 2
    return 1 if total_failures else 0


def _verify_config(cfg, configured=None) -> int:
    """Verify one config; return the number of failures found.

    *configured* is the set of profile names in the **unfiltered** config.
    It defaults to ``cfg.profiles`` (correct when no PROFILE filter is in
    play) but must be passed when one is, so that profiles merely filtered
    out are not reported as orphans.
    """
    configured_profiles = set(cfg.profiles) if configured is None else set(configured)
    with util.exclusive_lock(cfg.tmpdir / (cfg.name + ".lock")):
        failures = 0
        meta, used_remote = _load_meta_for_verify(cfg)
        if meta is None:
            print(f"{cfg.name}: no manifest found locally or on any remote")
            return 1
        if used_remote:
            print(
                f"{cfg.name}: local meta.yaml missing; "
                "verifying against a remote copy"
            )
        by_profile = collect_remotes(cfg)
        with util.scratch_dir(
            cfg.tmpdir / cfg.name / util.VERIFY_SCRATCH, cfg.tmpdir
        ) as tmpdir:
            for pname in cfg.profiles:
                lookup = {spec.id: (spec, remote) for spec, remote in by_profile[pname]}
                by_id = manifest.snapshots_by_id(meta, pname)
                for snap in manifest.snapshots(meta, pname):
                    failures += _verify_snapshot(
                        cfg, meta, pname, snap, lookup, by_id, tmpdir
                    )
        # Reported on both paths: a config can be drifting and stranding at
        # once, and the run that finally surfaces the strand is not one to
        # wait for.
        orphans = report_orphans(cfg.name, meta, configured_profiles)
        if failures == 0:
            # An orphan is not a failure (§12): an admin who removed a
            # profile may legitimately keep its data around. It is called out
            # in the summary, though, so `verify` never reads as a clean bill
            # of health while copies nobody prunes sit on disk.
            suffix = f" ({orphans} orphaned profile(s))" if orphans else ""
            print(f"{cfg.name}: ok{suffix}")
        return failures


def _receivable(snap, lookup) -> bool:
    """Return True when ``restore`` could actually replay *snap*.

    A link has to carry a send file and a complete upload on a remote that is
    still configured, because that is exactly what ``restore.pick_remote``
    looks for. Anything weaker would let ``verify`` bless a chain that
    ``restore`` then refuses -- most visibly a parent whose remotes were all
    removed, which retention deliberately keeps (``committed: true``) but
    which can no longer be received from anywhere.
    """
    if snap.get("type") == "local" or not snap.get("file"):
        return False
    return any(
        upload.get("status") == "complete" and upload.get("remote") in lookup
        for upload in snap.get("uploads") or []
    )


def _verify_snapshot(cfg, meta, pname, snap, lookup, by_id, tmpdir) -> int:
    """Verify a single snapshot against its local subvolume and remotes."""
    failures = 0
    sid = snap.get("id")
    where = f"{cfg.name}/{pname}/{sid}"

    if snap.get("local_deleted"):
        # The local subvolume is already gone and the entry survives only so
        # the remote delete can be retried (see §8). Nothing is left to
        # verify: the remotes that did drop their object would be reported
        # MISSING, which is noise about a state btrbak created itself.
        print(f"PENDING REMOTE DELETE {where}: local subvolume removed, awaiting retry")
        return 0

    if snap.get("type") == "local":
        if not (cfg.dest / pname / sid).exists():
            print(f"MISSING local snapshot {where}")
            failures += 1
        return failures

    parent = snap.get("parent")
    if parent:
        parent_snap = by_id.get(parent)
        if parent_snap is None:
            print(f"BROKEN CHAIN {where}: parent {parent} missing")
            failures += 1
        elif parent_snap.get("type") == "local":
            print(
                f"BROKEN CHAIN {where}: parent {parent} is local-only, so the "
                "chain can never be restored offsite"
            )
            failures += 1
        elif not _receivable(parent_snap, lookup):
            print(
                f"BROKEN CHAIN {where}: parent {parent} has no complete upload"
            )
            failures += 1

    uploads = snap.get("uploads") or []
    if not uploads:
        if manifest.committed(snap):
            # Every remote was removed from the profile, so the entry is kept
            # with `committed: true` purely so retention prunes it by age like
            # a local snapshot (§8). There is no offsite copy left to check;
            # calling that a failure would make verify exit 1 on every run for
            # as long as the entry survives.
            print(f"OK {where}: no remotes configured, nothing offsite to verify")
            return failures
        print(f"INCOMPLETE {where}: no uploads recorded")
        return failures + 1

    remote_file = snap.get("file")
    if not remote_file:
        print(f"INCOMPLETE {where}: remote-backed snapshot has no 'file' recorded")
        return failures + 1

    tmp = tmpdir / f"{sid}.send"
    for upload in uploads:
        if upload.get("status") != "complete":
            print(f"INCOMPLETE {where} on remote {upload.get('remote')}")
            failures += 1
            continue
        remote_id = upload.get("remote")
        if remote_id not in lookup:
            print(f"UNKNOWN REMOTE {where} remote {remote_id}")
            failures += 1
            continue
        spec, remote = lookup[remote_id]
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            remote.read(remote_file, tmp)
            if util.sha256_file(tmp) != snap.get("sha256"):
                print(f"CORRUPT {where} on remote {spec.id}")
                failures += 1
            elif tmp.stat().st_size != snap.get("size"):
                print(f"SIZE MISMATCH {where} on remote {spec.id}")
                failures += 1
        except RemoteNotFoundError:
            print(f"MISSING {where} on remote {spec.id}")
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR {where} on remote {spec.id}: {exc}")
            failures += 1
        finally:
            tmp.unlink(missing_ok=True)
    return failures


def cmd_restore(args) -> int:
    auth = config_mod.load_auth()
    path = config_mod.config_path_for_subvol(args.subvol)
    cfg = config_mod.load_config(path, auth)
    remote_errors = config_mod.validate_remote_config(cfg, args.profile)
    if remote_errors:
        raise config_mod.ConfigError("\n".join(remote_errors))
    with util.exclusive_lock(cfg.tmpdir / (cfg.name + ".lock")):
        restore_mod.run_restore(cfg, args.profile, args.snapshot_id, args.target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
