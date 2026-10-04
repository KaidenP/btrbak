"""CLI entrypoint for btrbak."""

import argparse
import os
import sys
import time

import config as config_mod
import manifest
import restore as restore_mod
import retention
import send
import snapshot
import timespan
import util
from remotes import create_remote
from remotes.base import RemoteError, RemoteNotFoundError


def main(argv=None) -> int:
    if os.geteuid() != 0:
        print("btrbak: must be run as root", file=sys.stderr)
        return 1

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except config_mod.ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except (util.BtrbakError, RemoteError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="btrbak", description="btrfs snapshot and offsite backup utility"
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    sub = parser.add_subparsers(dest="command", required=True)

    p_config = sub.add_parser("config", help="configuration commands")
    config_sub = p_config.add_subparsers(dest="config_command", required=True)
    p_check = config_sub.add_parser("check", help="validate configuration")
    p_check.set_defaults(func=cmd_config_check)

    p_run = sub.add_parser("run", help="create due snapshots/backups and prune")
    p_run.add_argument("subvol", nargs="?")
    p_run.add_argument("profile", nargs="?")
    p_run.add_argument("--force", action="store_true")
    p_run.add_argument("--force-config", action="store_true")
    p_run.add_argument("--full", action="store_true")
    p_run.add_argument("-n", "--dry-run", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_verify = sub.add_parser("verify", help="check remote files against the manifest")
    p_verify.add_argument("subvol", nargs="?")
    p_verify.add_argument("profile", nargs="?")
    p_verify.set_defaults(func=cmd_verify)

    p_list = sub.add_parser("list", help="list snapshots and backups")
    p_list.add_argument("subvol", nargs="?")
    p_list.add_argument("profile", nargs="?")
    p_list.set_defaults(func=cmd_list)

    p_restore = sub.add_parser("restore", help="restore a snapshot chain to a target")
    p_restore.add_argument("subvol")
    p_restore.add_argument("profile")
    p_restore.add_argument("snapshot_id")
    p_restore.add_argument("target")
    p_restore.set_defaults(func=cmd_restore)

    return parser


# --- run -------------------------------------------------------------------


def cmd_run(args) -> int:
    configs = config_mod.discover_configs(args.subvol)
    for cfg in configs:
        run_config(
            cfg,
            args.profile,
            args.force,
            args.force_config,
            args.full,
            args.dry_run,
        )
    return 0


def run_config(cfg, profile_filter, force, force_config, full, dry_run) -> None:
    errors, warnings = config_mod.validate(cfg)
    if errors:
        raise config_mod.ConfigError("\n".join(errors))
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    if config_mod.is_nested(cfg.dest, cfg.src):
        if dry_run:
            print("warning: dest is nested inside src; would wait 5s", file=sys.stderr)
        else:
            print(
                "warning: dest is nested inside src; Ctrl-C within 5s to abort",
                file=sys.stderr,
            )
            time.sleep(5)

    if dry_run:
        meta = manifest.load(cfg.dest / "meta.yaml")
        for pname, profile in cfg.profiles.items():
            if profile_filter and pname != profile_filter:
                continue
            due, stype, parent = compute_plan(cfg, profile, meta, util.now(), force, full)
            if due:
                print(
                    f"[dry-run] {cfg.name}/{pname}: would create {stype} snapshot"
                    f" (parent={parent or '-'})"
                )
            else:
                print(f"[dry-run] {cfg.name}/{pname}: nothing due")
        return

    cfg.dest.mkdir(parents=True, exist_ok=True)
    with util.exclusive_lock(cfg.dest / ".btrbak.lock"):
        clean_tmpdir(cfg)
        meta_path = cfg.dest / "meta.yaml"
        meta = manifest.load(meta_path)
        ensure_profile_meta(meta, cfg)
        _all_remotes, by_profile = collect_remotes(cfg)
        sync_settings(cfg, _all_remotes, force_config)

        for pname, profile in cfg.profiles.items():
            if profile_filter and pname != profile_filter:
                continue
            run_profile(cfg, profile, meta, by_profile[pname], force, full)
            manifest.save(meta_path, meta)
            push_manifest(meta_path, by_profile)

        prune(cfg, meta, by_profile)
        manifest.save(meta_path, meta)
        push_manifest(meta_path, by_profile)


def compute_plan(cfg, profile, meta, now_ts, force=False, full=False):
    """Return ``(due, type, parent_id)`` for the next backup."""
    pname = profile.name
    last = manifest.last_committed(meta, pname)

    if not profile.remotes:
        due = False
        if last is None:
            due = True
        else:
            candidates = [
                value
                for value in (profile.freq_full, profile.freq_incr)
                if not timespan.is_never(value)
            ]
            due = bool(candidates) and (now_ts - last["created"] >= min(candidates))
        if due or force or full:
            return True, "local", None
        return False, None, None

    last_full = manifest.last_full_committed(meta, pname)
    full_due = (not timespan.is_never(profile.freq_full)) and (
        last_full is None or now_ts - last_full["created"] >= profile.freq_full
    )
    incr_due = (
        (not timespan.is_never(profile.freq_incr))
        and (not full_due)
        and (last is not None and now_ts - last["created"] >= profile.freq_incr)
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


def run_profile(cfg, profile, meta, remotes, force, full) -> None:
    pname = profile.name
    for snap in manifest.snapshots(meta, pname):
        if not manifest.committed(snap):
            retry_upload(cfg, profile, snap, remotes)

    now_ts = util.now()
    due, stype, parent_id = compute_plan(cfg, profile, meta, now_ts, force, full)
    if not due:
        return

    snap_id = unique_snapshot_id(util.snapshot_id(now_ts), cfg.dest / pname)
    snap_path = cfg.dest / pname / snap_id
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot.create_ro_snapshot(cfg.src, snap_path)

    entry = {
        "id": snap_id,
        "created": now_ts,
        "type": stype,
        "parent": parent_id,
        "file": f"{pname}/{snap_id}.send" if remotes else None,
        "sha256": None,
        "size": None,
        "uploads": [],
    }
    manifest.add_snapshot(meta, pname, entry)

    if remotes:
        perform_send_upload(cfg, profile, entry, snap_path, parent_id, remotes)


def perform_send_upload(cfg, profile, entry, snap_path, parent_id, remotes) -> None:
    pname = profile.name
    staged = cfg.tmpdir / cfg.name / pname / f"{entry['id']}.send"
    staged.parent.mkdir(parents=True, exist_ok=True)
    parent_path = cfg.dest / pname / parent_id if parent_id else None
    send.send_snapshot(snap_path, parent_path, staged, cfg.compression, cfg.encryption)
    entry["sha256"] = util.sha256_file(staged)
    entry["size"] = staged.stat().st_size

    for spec, remote in remotes:
        status = "complete"
        try:
            remote.write(staged, entry["file"])
        except Exception as exc:  # noqa: BLE001 - record per-remote failure
            print(f"upload to {spec.id} failed: {exc}", file=sys.stderr)
            status = "failed"
        entry["uploads"].append({"remote": spec.id, "status": status})
    staged.unlink(missing_ok=True)


def retry_upload(cfg, profile, snap, remotes) -> None:
    if not remotes or not snap.get("file"):
        return
    pname = profile.name
    snap_path = cfg.dest / pname / snap["id"]
    if not snap_path.exists():
        print(f"snapshot {snap['id']} missing locally; cannot retry", file=sys.stderr)
        return

    staged = cfg.tmpdir / cfg.name / pname / f"{snap['id']}.send"
    staged.parent.mkdir(parents=True, exist_ok=True)
    parent_path = cfg.dest / pname / snap["parent"] if snap.get("parent") else None
    send.send_snapshot(snap_path, parent_path, staged, cfg.compression, cfg.encryption)
    snap["sha256"] = util.sha256_file(staged)
    snap["size"] = staged.stat().st_size

    uploads = snap.setdefault("uploads", [])
    for spec, remote in remotes:
        existing = next((u for u in uploads if u["remote"] == spec.id), None)
        if existing and existing["status"] == "complete":
            continue
        status = "complete"
        try:
            remote.write(staged, snap["file"])
        except Exception as exc:  # noqa: BLE001
            print(f"upload to {spec.id} failed: {exc}", file=sys.stderr)
            status = "failed"
        if existing:
            existing["status"] = status
        else:
            uploads.append({"remote": spec.id, "status": status})
    staged.unlink(missing_ok=True)


def prune(cfg, meta, by_profile) -> None:
    now_ts = util.now()
    for pname, profile in cfg.profiles.items():
        for sid in retention.plan_prune(meta, pname, profile.keep, now_ts):
            snap = manifest.get_snapshot(meta, pname, sid)
            snap_path = cfg.dest / pname / sid
            if snap_path.exists():
                snapshot.delete_snapshot(snap_path)
            if snap and snap.get("file"):
                for spec, remote in by_profile.get(pname, []):
                    try:
                        remote.delete(snap["file"])
                    except RemoteNotFoundError:
                        pass
                    except Exception as exc:  # noqa: BLE001
                        print(f"remote delete on {spec.id} failed: {exc}", file=sys.stderr)
            manifest.remove_snapshot(meta, pname, sid)


# --- helpers ---------------------------------------------------------------


def collect_remotes(cfg):
    all_remotes = {}
    by_profile = {}
    for pname, profile in cfg.profiles.items():
        entries = []
        for spec in profile.remotes:
            if spec.id not in all_remotes:
                all_remotes[spec.id] = (spec, create_remote(spec))
            entries.append(all_remotes[spec.id])
        by_profile[pname] = entries
    return all_remotes, by_profile


def sync_settings(cfg, all_remotes, force_config) -> None:
    local_bytes = cfg.path.read_bytes()
    for rid, (spec, remote) in all_remotes.items():
        tmp = cfg.tmpdir / cfg.name / "config.yaml.check"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        try:
            try:
                remote.read("config.yaml", tmp)
            except RemoteNotFoundError:
                remote.write(cfg.path, "config.yaml")
                continue
            remote_bytes = tmp.read_bytes()
        finally:
            tmp.unlink(missing_ok=True)

        if remote_bytes == local_bytes:
            continue
        if not force_config:
            raise config_mod.ConfigError(
                f"remote {rid} already has a different config.yaml; "
                "use --force-config to overwrite"
            )
        remote.write(cfg.path, "config.yaml")


def push_manifest(meta_path, by_profile) -> None:
    seen = set()
    for entries in by_profile.values():
        for spec, remote in entries:
            if spec.id in seen:
                continue
            seen.add(spec.id)
            remote.write(meta_path, "meta.yaml")


def ensure_profile_meta(meta, cfg) -> None:
    for pname in cfg.profiles:
        entry = manifest.profile(meta, pname)
        entry.setdefault("src", str(cfg.src))
        entry.setdefault("compression", cfg.compression)
        entry.setdefault("encryption", cfg.encryption)
        entry.setdefault("snapshots", [])


def unique_snapshot_id(base, profile_dir) -> str:
    candidate = base
    index = 1
    while (profile_dir / candidate).exists():
        index += 1
        candidate = f"{base}-{index}"
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


# --- other commands --------------------------------------------------------


def cmd_config_check(args) -> int:
    try:
        configs = config_mod.discover_configs()
    except config_mod.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
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
    for cfg in configs:
        meta = manifest.load(cfg.dest / "meta.yaml")
        for pname in cfg.profiles:
            if args.profile and pname != args.profile:
                continue
            print(f"{cfg.name}/{pname}:")
            for snap in manifest.snapshots(meta, pname):
                age = util.now() - snap.get("created", 0)
                uploads = ",".join(
                    f"{u['remote']}={u['status']}" for u in snap.get("uploads", [])
                )
                print(
                    f"  {snap['id']}  type={snap.get('type')}  age={age}s  "
                    f"parent={snap.get('parent') or '-'}  uploads=[{uploads}]"
                )
    return 0


def cmd_verify(args) -> int:
    configs = config_mod.discover_configs(args.subvol)
    failures = 0
    for cfg in configs:
        meta = manifest.load(cfg.dest / "meta.yaml")
        _all_remotes, by_profile = collect_remotes(cfg)
        for pname in cfg.profiles:
            if args.profile and pname != args.profile:
                continue
            lookup = {spec.id: (spec, remote) for spec, remote in by_profile[pname]}
            for snap in manifest.snapshots(meta, pname):
                if snap.get("type") == "local":
                    if not (cfg.dest / pname / snap["id"]).exists():
                        print(f"MISSING local snapshot {cfg.name}/{pname}/{snap['id']}")
                        failures += 1
                    continue
                if snap.get("parent") and manifest.get_snapshot(
                    meta, pname, snap["parent"]
                ) is None:
                    print(
                        f"BROKEN CHAIN {cfg.name}/{pname}/{snap['id']}: "
                        f"parent {snap['parent']} missing"
                    )
                    failures += 1
                for upload in snap.get("uploads", []):
                    if upload.get("status") != "complete":
                        print(
                            f"INCOMPLETE {cfg.name}/{pname}/{snap['id']} "
                            f"on remote {upload.get('remote')}"
                        )
                        failures += 1
                        continue
                    if upload.get("remote") not in lookup:
                        print(
                            f"UNKNOWN REMOTE {cfg.name}/{pname}/{snap['id']} "
                            f"remote {upload.get('remote')}"
                        )
                        failures += 1
                        continue
                    spec, remote = lookup[upload["remote"]]
                    tmp = cfg.tmpdir / cfg.name / "verify" / f"{snap['id']}.send"
                    tmp.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        remote.read(snap["file"], tmp)
                        if util.sha256_file(tmp) != snap.get("sha256"):
                            print(
                                f"CORRUPT {cfg.name}/{pname}/{snap['id']} "
                                f"on remote {spec.id}"
                            )
                            failures += 1
                        elif tmp.stat().st_size != snap.get("size"):
                            print(
                                f"SIZE MISMATCH {cfg.name}/{pname}/{snap['id']} "
                                f"on remote {spec.id}"
                            )
                            failures += 1
                    except RemoteNotFoundError:
                        print(
                            f"MISSING {cfg.name}/{pname}/{snap['id']} "
                            f"on remote {spec.id}"
                        )
                        failures += 1
                    except Exception as exc:  # noqa: BLE001
                        print(
                            f"ERROR {cfg.name}/{pname}/{snap['id']} "
                            f"on remote {spec.id}: {exc}"
                        )
                        failures += 1
                    finally:
                        tmp.unlink(missing_ok=True)
        if failures == 0:
            print(f"{cfg.name}: ok")
    return 1 if failures else 0


def cmd_restore(args) -> int:
    auth = config_mod.load_auth()
    path = config_mod.CONFIG_DIR / f"{args.subvol}.yaml"
    if not path.exists():
        raise config_mod.ConfigError(f"no config file: {path}")
    cfg = config_mod.load_config(path, auth)
    restore_mod.run_restore(cfg, args.profile, args.snapshot_id, args.target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
