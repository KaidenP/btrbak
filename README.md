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

`src`, `dest` and `tmpdir` must be absolute paths, and profile names must match
`[A-Za-z0-9][A-Za-z0-9._-]*` (they are used as path components under `dest` and
on each remote). Run `sudo btrbak config check` to validate everything,
including that every `age` recipient actually works.

## Restoring

```
sudo btrbak restore SUBVOL PROFILE SNAPSHOT_ID TARGET
```

`TARGET` must be on btrfs and is created only after the whole chain validates.
A restore interrupted part way through can simply be re-run into the same
target: the links already received are detected and skipped, so the chain
resumes instead of failing with `File exists`.

## Development

Install the project in editable mode to expose the `btrbak` command:

```
python -m pip install -e .
```

Run the test suite:

```
python -m pytest
```

End-to-end tests against a real btrfs filesystem (loopback mount) live in
`tests/integration/test_btrfs.py` and run automatically as root; they skip when
not running as root or when btrfs tooling is unavailable. Run them explicitly
with:

```
sudo python -m pytest tests/integration/test_btrfs.py
```
