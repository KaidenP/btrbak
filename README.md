# btrbak

A root-only Python CLI for managing btrfs read-only snapshots and shipping
incremental offsite backups via `btrfs send`.

See `PROJECT_SPEC.md` for the full design. This implementation is a work in
progress tracking that spec.

## Commands

```
btrbak config check
btrbak run [SUBVOL] [PROFILE] [--force] [--force-config] [--full] [--dry-run]
btrbak verify [SUBVOL] [PROFILE]
btrbak list [SUBVOL] [PROFILE]
btrbak restore SUBVOL PROFILE SNAPSHOT_ID TARGET
```

## Configuration

- `/etc/btrbak/profiles.d/<name>.yaml` — one file per source subvolume.
- `/etc/btrbak/auth.yaml` — remote credentials referenced by `auth:` keys.

## Development

```
python -m pytest
```

End-to-end tests against a real btrfs filesystem (loopback mount) are planned
but not wired up yet; the current suite covers parsing, config, retention,
manifest, remote mapping, and the compression codec.
