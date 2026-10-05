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


def cmd_run(args) -> int:
    configs = config_mod.discover_configs(args.subvol)
    selected_configs = []
    for cfg in configs:
        selected = config_mod.select_profiles(cfg, args.profile)
        if selected is None:
            continue
        selected_configs.append(selected)
    if args.profile and not selected_configs:
        raise config_mod.ConfigError(f"unknown profile: {args.profile!r}")

    upload_failures = 0
    runtime_failures = 0
    config_errors = 0
    for cfg in selected_configs:
        try:
            upload_failures += run_config(
                cfg,
                None,
                args.force,
                args.force_config,
                args.full,
                args.dry_run,
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


def run_config(cfg, profile_filter, force, force_config, full, dry_run) -> int:
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

    if dry_run:
        meta = manifest.load(selected.dest / "meta.yaml")
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

        dry_run_config_sync(selected, collect_remotes(selected), force_config)

        for pname, profile in selected.profiles.items():
            for sid in retention.plan_prune(meta, pname, profile.keep, now_ts):
                print(f"[dry-run] {selected.name}/{pname}: would prune snapshot {sid}")
        return 0

    selected.dest.mkdir(parents=True, exist_ok=True)
    with util.exclusive_lock(selected.dest / ".btrbak.lock"):
        meta_path = selected.dest / "meta.yaml"
        meta = manifest.load(meta_path)
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
        push_manifest(meta_path, by_profile)
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

    snap_id = unique_snapshot_id(util.snapshot_id(now_ts), cfg.dest / pname)
    snap_path = cfg.dest / pname / snap_id
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot.create_ro_snapshot(cfg.src, snap_path)

    entry = {
        "id": snap_id,
        "created": now_ts,
        "type": stype,
        "parent": parent_id,
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


def perform_send_upload(cfg, profile, entry, snap_path, parent_id, remotes) -> int:
    pname = profile.name
    staged = cfg.tmpdir / cfg.name / pname / f"{entry['id']}.send"
    staged.parent.mkdir(parents=True, exist_ok=True)
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
        staged.unlink(missing_ok=True)


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

    def is_complete(rid):
        return any(
            u.get("remote") == rid and u.get("status") == "complete"
            for u in uploads
        )

    pending = [rid for rid in by_remote if not is_complete(rid)]
    if not pending:
        return 0

    staged = cfg.tmpdir / cfg.name / pname / f"{snap['id']}.send"
    staged.parent.mkdir(parents=True, exist_ok=True)
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
        staged.unlink(missing_ok=True)


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
        for sid in retention.plan_prune(meta, pname, profile.keep, now_ts):
            snap = manifest.get_snapshot(meta, pname, sid)
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
    for spec, remote in remotes:
        tmp = cfg.tmpdir / cfg.name / "config.yaml.check"
        tmp.parent.mkdir(parents=True, exist_ok=True)
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
    again immediately. Reporting the real state matters because a differing
    remote ``config.yaml`` aborts a real run unless ``--force-config`` is given.
    """
    local_bytes = cfg.path.read_bytes()
    for spec, remote in unique_remotes(by_profile):
        tmp = cfg.tmpdir / cfg.name / "config.yaml.dry-run"
        tmp.parent.mkdir(parents=True, exist_ok=True)
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


def unique_snapshot_id(base, profile_dir) -> str:
    candidate = base
    index = 1
    while (profile_dir / candidate).exists():
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


def _snapshot_depth(meta, pname, snap, cache, seen=None) -> int:
    """Return the dependency depth of *snap* (0 for a root full backup)."""
    if seen is None:
        seen = set()
    sid = snap.get("id")
    if sid in cache:
        return cache[sid]
    if sid in seen:
        return 0
    seen.add(sid)
    depth = 0
    parent_id = snap.get("parent")
    if parent_id:
        parent = manifest.get_snapshot(meta, pname, parent_id)
        if parent is not None:
            depth = _snapshot_depth(meta, pname, parent, cache, seen) + 1
    cache[sid] = depth
    return depth


# --- other commands --------------------------------------------------------


def cmd_config_check(args) -> int:
    try:
        configs = config_mod.discover_configs()
    except config_mod.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    return_code = 0
    for cfg in configs:
        errors, warnings = config_mod.validate(cfg)
        print(f"{cfg.name}:")
        for warning in warnings:
            print(f"  warning: {warning}")
        for error in errors:
            print(f"  error: {error}")
        if errors:
            return_code = 2
    return return_code


def cmd_list(args) -> int:
    configs = config_mod.discover_configs(args.subvol)
    matched = False
    for cfg in configs:
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
        for pname in selected.profiles:
            snaps = manifest.snapshots(meta, pname)
            print(f"{cfg.name}/{pname}:" + (f" ({len(snaps)} snapshots)" if snaps else ""))
            depth_cache = {}
            for snap in snaps:
                depth = _snapshot_depth(meta, pname, snap, depth_cache)
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
    return 0


def _load_meta_for_verify(cfg):
    """Return ``(meta, used_remote)`` for verification.

    Prefer the local ``dest/meta.yaml``; when it is missing, download a copy
    from the first configured remote that has one. Returns ``(None, False)``
    when no manifest is available anywhere.
    """
    local = cfg.dest / "meta.yaml"
    if local.exists():
        return manifest.load(local), False

    tmpdir = cfg.tmpdir / cfg.name / "verify"
    tmpdir.mkdir(parents=True, exist_ok=True)
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
    configs = config_mod.discover_configs(args.subvol)
    total_failures = 0
    config_errors = 0
    matched = False
    for cfg in configs:
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
            total_failures += _verify_config(selected)
        except (util.BtrbakError, RemoteError, OSError) as exc:
            print(f"error ({cfg.name}): {exc}", file=sys.stderr)
            total_failures += 1
    if args.profile and not matched:
        raise config_mod.ConfigError(f"unknown profile: {args.profile!r}")
    if config_errors:
        return 2
    return 1 if total_failures else 0


def _verify_config(cfg) -> int:
    """Verify one config; return the number of failures found."""
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
        for pname in cfg.profiles:
            lookup = {spec.id: (spec, remote) for spec, remote in by_profile[pname]}
            for snap in manifest.snapshots(meta, pname):
                failures += _verify_snapshot(
                    cfg, meta, pname, snap, lookup
                )
        if failures == 0:
            print(f"{cfg.name}: ok")
        return failures


def _verify_snapshot(cfg, meta, pname, snap, lookup) -> int:
    """Verify a single snapshot against its local subvolume and remotes."""
    failures = 0
    sid = snap.get("id")
    where = f"{cfg.name}/{pname}/{sid}"

    if snap.get("type") == "local":
        if not (cfg.dest / pname / sid).exists():
            print(f"MISSING local snapshot {where}")
            failures += 1
        return failures

    parent = snap.get("parent")
    if parent and manifest.get_snapshot(meta, pname, parent) is None:
        print(f"BROKEN CHAIN {where}: parent {parent} missing")
        failures += 1

    uploads = snap.get("uploads") or []
    if not uploads:
        print(f"INCOMPLETE {where}: no uploads recorded")
        return failures + 1

    remote_file = snap.get("file")
    if not remote_file:
        print(f"INCOMPLETE {where}: remote-backed snapshot has no 'file' recorded")
        return failures + 1

    tmp = cfg.tmpdir / cfg.name / "verify" / f"{sid}.send"
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
