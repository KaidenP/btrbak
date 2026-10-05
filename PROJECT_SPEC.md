# PROJECT_SPEC — btrbak

A root-only, Python CLI for managing btrfs read-only snapshots and shipping
incremental offsite backups via `btrfs send`.

---

## 1. Key decisions (resolved during design)

| Topic | Decision |
|---|---|
| Backup model | `btrfs send` to files; incremental streams depend on a parent chain |
| Retention | **Dependency-preserving (Option C)**: each profile declares a full-backup cadence; a snapshot/backup is deleted only when older than `keep` **and** nothing depends on it |
| Local vs remote | Local `dest` keeps the **snapshot subvolumes**; the `btrfs send` files are transient (staged in `tmpdir`, deleted after upload). Remote stores the send files + manifest. Both sides are pruned under the same `keep` policy. `remotes` is optional — a profile with no remotes manages local snapshots only |
| Subvolume scope | One `src` subvolume per config file; nested subvolumes are **not** recursed into |
| Snapshot type | Read-only (`btrfs subvolume snapshot -r`) |
| Offsite transport | Pluggable remote interface; v1 implements only the `dir` remote (a local filesystem path) |
| Remote interface | Path-based: `read`, `write`, `delete`, `list`, `validate`. `btrbak` owns the layout |
| Compression | **xz (LZMA)** via Python stdlib `lzma` (v1: single algorithm) |
| Encryption | **age** via the `age` CLI (v1: single algorithm) |
| Scheduling | External timer/systemd; `btrbak run` reviews config and performs due work |
| CLI | Single `btrbak` command; refuses to run unless `euid == 0` |

---

## 2. Goals

- Create and manage read-only btrfs snapshots of a subvolume.
- Produce **full** and **incremental** offsite backups using `btrfs send`.
- Guarantee restorability under pruning by tracking parent dependencies and
  only deleting snapshots/backups that nothing depends on.
- Configure everything via YAML profile files.
- Keep the remote/offsite layer pluggable so future backends (`s3`, `sftp`,
  `rclone`, …) can be added as modules under `src/remotes`.

## 3. Non-goals (v1)

- No recursion into nested subvolumes (a profile covers exactly one subvolume).
- No writable snapshots / local rollback management (read-only only).
- No built-in scheduler (external timer invokes the tool).
- Only the `dir` remote; only `xz` compression; only `age` encryption.
- No remote-side deduplication or `btrfs receive` on the offsite target.

---

## 4. Architecture overview

```
external timer
      │  btrbak run [SUBVOL] [PROFILE] [--force]
      ▼
┌──────────────────────────────────────────────────────────┐
│ config loader        profiles.d/<name>.yaml + auth.yaml  │
│ manifest             <dest>/meta.yaml (source of truth)  │
│ snapshot manager     btrfs subvolume snapshot -r         │
│ send engine          btrfs send [-p PARENT] SNAP         │
│   └ pipeline         → xz (optional) → age (optional)    │
│ remote layer         read/write/delete/list/validate     │
│ retention            leaf-first, dependency-preserving   │
│ restore              replay full + incrementals          │
└──────────────────────────────────────────────────────────┘
      │
      ▼
   <dest>/<profile>/<snapshot-id>/   (ro subvolume, local)
   <remote>/<profile>/<snapshot-id>.send
   <remote>/meta.yaml
```

Data flow for a backup:

1. Sync the profile config (`config.yaml`) to every configured remote
   (conflict-checked; see §9).
2. Create a read-only snapshot under `dest`.
3. `btrfs send` it (with `-p` parent for incrementals) into a staged file in `tmpdir`.
4. Optionally compress (xz), optionally encrypt (age), compute `sha256`.
5. Upload the final file to every configured remote.
6. Record the snapshot + dependency in `meta.yaml`; write it locally and to every remote.
7. Delete the staged file.
8. Prune (see §9).

---

## 5. Project layout

```
btrbak/
├── pyproject.toml            # packaging; console script `btrbak`
├── README.md
├── PROJECT_SPEC.md
├── src/
│   ├── cli.py                # argument parsing, root check, dispatch
│   ├── config.py             # profile + auth.yaml loading/validation
│   ├── timespan.py           # "1d", "2w", "1mo" parsing
│   ├── snapshot.py           # create/delete ro snapshots
│   ├── send.py               # btrfs send + compress/encrypt pipeline
│   ├── manifest.py           # meta.yaml read/write/atomic update
│   ├── retention.py          # dependency-aware pruning
│   ├── restore.py            # chain replay via btrfs receive
│   ├── util.py               # checksum, locking, subprocess, fs helpers
│   └── remotes/
│       ├── __init__.py       # remote registry (type string → class)
│       ├── base.py           # Remote abstract base class
│       └── dir.py            # DirRemote (local filesystem path)
└── tests/
    ├── test_timespan.py
    ├── test_config.py
    ├── test_retention.py
    ├── test_manifest.py
    └── integration/
        └── test_btrfs.py        # runs against a btrfs loopback image
```

Remote modules live in `src/remotes` as required. A future remote (e.g. `s3`)
adds `src/remotes/s3.py` and registers a `type` string in `src/remotes/__init__.py`.

---

## 6. Configuration

### 6.1 Profile files

Location: `/etc/btrbak/profiles.d/<name>.yaml`

One file = one source subvolume + its profiles. The filename stem (`<name>`) is
the **SUBVOL** selector used on the CLI. Config files may use either `.yaml`
or `.yml`; when both exist for the same stem, `.yaml` takes precedence.

`src`, `dest` and `tmpdir` **must be absolute paths**. They are resolved once,
at load time, rather than against the process working directory, so behaviour
does not depend on where the tool happened to be invoked from (a systemd unit's
`WorkingDirectory`). `~` is expanded.

Profile names become path components — `<dest>/<profile>/<snapshot-id>` locally
and `<profile>/<snapshot-id>.send` on every remote — so they are restricted to
`[A-Za-z0-9][A-Za-z0-9._-]*`. A name containing a path separator, or equal to
`.`/`..`, is a config error.

```yaml
# /etc/btrbak/profiles.d/root.yaml
src: /mnt/data                      # absolute path to the btrfs subvolume to back up
dest: /mnt/data/.snapshots          # local root for snapshots; MUST be on the same btrfs fs as src
tmpdir: /var/tmp/btrbak             # optional; staging area for send files (default: /var/tmp/btrbak)

compression:                        # optional; omit for no compression
  algorithm: xz                     # v1: xz (only; future: zstd, gzip, none)
  level: 6                          # lzma preset 0–9 (default 6)

encryption:                         # optional; omit for no encryption
  algorithm: age                    # v1: age (only; future: gpg, none)
  recipients:                       # required when encryption enabled
    - age1qxy...                    # inline age public key, or a path to a recipients file (one key per line)
  identity: /root/.config/age/btrbak.key   # optional; private key path, required for restore

profiles:                           # required; at least one
  daily:
    freq:
      full: 7d                      # full-backup cadence (timespan)
      incr: 1d                      # incremental cadence (timespan)
    keep: 30d                       # retention window (timespan)
    remotes:                        # optional; omit for a local-only profile
      - name: offsite               # optional; stable id (auto-derived if omitted)
        type: dir
        path: /mnt/offsite          # remote-specific settings
        # auth: mykey               # optional; references a key in auth.yaml
  apt:                              # manual-only profile (e.g. triggered by an apt hook)
    freq:
      full: -1                      # -1 = never due automatically
      incr: -1
    keep: 90d
    remotes:
      - name: offsite
        type: dir
        path: /mnt/offsite
  local-only:                       # snapshot management only, no offsite backup
    freq:
      full: 1d                      # take a snapshot daily
      incr: -1
    keep: 14d
    # remotes omitted → local-only
```

### 6.2 auth.yaml

Location: `/etc/btrbak/auth.yaml`

```yaml
# Keys referenced by `auth: <key>` in remote entries.
# Values are opaque to btrbak and passed through to the remote module.
mykey:
  access_key: "..."
  secret: "..."
```

When a remote has `auth: <key>`, the `auth` key is **replaced** by
`auth.yaml[<key>]` before settings are passed to the remote module. Inline
settings are otherwise passed through unchanged.

```yaml
# remote entry
- type: sftp
  host: backup.example.com
  auth: mykey
```

with `auth.yaml`:

```yaml
mykey:
  username: btrbak
  password: "..."
```

yields settings passed to the module:

```yaml
type: sftp
host: backup.example.com
auth:
  username: btrbak
  password: "..."
```

### 6.3 Timespan format

`<integer><unit>`, no spaces, e.g. `1d`, `12h`, `2w`, `1mo`.

`freq.full` and `freq.incr` additionally accept `-1`, meaning "never due
automatically" (manual-only). `keep` must always be a positive timespan.

| Unit | Meaning |
|---|---|
| `s` | seconds |
| `min` | minutes |
| `h` | hours |
| `d` | days (24 h) |
| `w` | weeks (7 d) |
| `mo` | months (30 d, fixed approximation) |
| `y` | years (365 d, fixed approximation) |

### 6.4 Config validation (`btrbak config check`)

Structural checks run while the config is **loaded**, and are reported as
`config error` (exit `2`) by every command:

- `src`, `dest` and (when set) `tmpdir` are absolute paths.
- Profile names match `[A-Za-z0-9][A-Za-z0-9._-]*` (see §6.1).
- Timespans parse; `keep` is a positive timespan, never `-1` (§6.3).

Deeper checks run in `validate()`:

- All files parse as valid YAML; required fields present and correctly typed.
- `src` exists, is a btrfs subvolume (`btrfs subvolume show` succeeds).
- `dest` exists or is creatable, is a **directory** (not a file), and is on the
  **same btrfs filesystem** as `src` (compared by filesystem UUID, because
  btrfs subvolumes report distinct `st_dev` values). `dest` must **not** resolve
  to `src` itself — snapshotting a subvolume into itself is rejected as an error,
  not merely warned about.
- `tmpdir` exists or is creatable, is a **directory** (not a file), and is
  writable.
- `profiles` non-empty; `freq.full` and `freq.incr` are each `-1` ("never") or a
  positive timespan; `keep` is a positive timespan.
- Warnings (not errors): `dest` nested inside `src`; `keep` < `freq.incr`. The
  nesting warning is reported once per run (§12) — `config check` prints it from
  validation, while `run` prints it from its own nesting guard so it can be
  combined with the 5 s grace period (§13).
- `remotes` is optional (omit for a local-only profile). When present: each
  remote `type` is registered, `name` (if given) is unique within the profile,
  `auth` keys resolve in `auth.yaml`, and `remote.validate()` passes.
- If `encryption` is enabled: `recipients` present, the `age` binary is found,
  and **every recipient is validated by asking `age` to encrypt a throwaway
  payload** — the same code path used when a backup is sent. A recipients file
  is validated with `-R`, an inline key with `-r`. This rejects keys that merely
  start with `age1` but are malformed (bad checksum, mixed case, truncated) at
  `config check` time rather than after a snapshot subvolume has already been
  created. Public keys are not secret, so `age`'s message is surfaced verbatim.
- If `compression` is enabled: valid algorithm/level.

---

## 7. Layouts

### 7.1 Local `dest` (snapshots only)

`dest` is on the same btrfs filesystem as `src`. Each snapshot is a read-only
subvolume named by its snapshot id.

```
<dest>/
├── meta.yaml                  # manifest (source of truth)
├── .btrbak.lock               # advisory lock file (flock)
└── <profile>/
    └── <snapshot-id>/         # ro btrfs snapshot subvolume
```

### 7.2 Remote store (`dir` remote)

The remote holds send files, a copy of the backup settings, and a mirror of the
manifest. `btrbak` owns the logical layout; the remote maps logical paths onto
its backing store.

```
<remote-root>/
├── meta.yaml
├── config.yaml                 # copy of /etc/btrbak/profiles.d/<name>.yaml
└── <profile>/
    └── <snapshot-id>.send      # final send stream (post compression/encryption)
```

### 7.3 Snapshot id

Format: `YYYYmmddTHHMMSSZ` (UTC, second precision), e.g. `20251004T154300Z`.
Sortable, filesystem-safe, deterministic. If a collision occurs in the same
profile (two runs within one second), append `-1`, `-2`, ….

---

## 8. Manifest — `meta.yaml`

`<dest>/meta.yaml` is the authoritative state. After every change it is written
atomically and uploaded to every remote as `meta.yaml` so the remote is
self-describing for disaster recovery.

```yaml
version: 1
profiles:
  daily:
    src: /mnt/data
    snapshots:                  # ordered oldest → newest
      - id: "20251004T154300Z"
        created: 1760000000      # unix epoch seconds (snapshot creation time)
        type: full               # full | incr | local (local-only)
        parent: null             # snapshot id of the send parent (null for full)
        compression: { algorithm: xz, level: 6 }   # null when uncompressed
        encryption: { algorithm: age, recipients: [age1qxy...], identity: /root/.config/age/btrbak.key }  # null when unencrypted
        file: "daily/20251004T154300Z.send"   # logical remote path
        sha256: "deadbeef..."
        size: 123456789          # bytes of the final uploaded file
        uploads:                 # one entry per configured remote
          - remote: offsite      # stable remote id (name, or derived hash)
            status: complete     # pending | complete | failed
      - id: "20251005T154300Z"
        created: 1760086200
        type: incr
        parent: "20251004T154300Z"
        compression: { algorithm: xz, level: 6 }
        encryption: { algorithm: age, recipients: [age1qxy...], identity: /root/.config/age/btrbak.key }
        file: "daily/20251005T154300Z.send"
        sha256: "..."
        size: 45678
        uploads:
          - remote: offsite
            status: complete
```

Each snapshot records the `compression` and `encryption` settings used to
produce its send file (both null for a local-only snapshot, which has no send
file). Restore reads these per-snapshot settings so a later config change never
changes how an existing backup is decoded.

A snapshot is **committed** when every configured remote has a complete
`uploads[].status == complete` record. A local-only snapshot has no uploads
and is committed immediately. For `type: local`, `file`, `sha256`, and `size`
are null and `uploads` is empty.

When a remote is added to a profile, every existing remote-backed snapshot
gains an upload record for it (initially `failed`) and is retried on the next
`run` until the new remote has a complete copy. When a remote is removed from
a profile, its upload records are dropped from existing snapshots. If a
remote-backed snapshot is then left with no upload records (all of its remotes
were removed), it is recorded with `committed: true` so it is no longer
retried and is pruned by age/dependency like a local snapshot; any offsite
copies on the removed remote are no longer managed. The `committed` key is
optional and defaults to false (absent).

A snapshot may carry an optional `local_deleted: true` flag. It is set only
when pruning deleted the local subvolume but a remote `delete` failed, so the
entry is retained for a later remote-delete retry. Such a snapshot is still
considered committed for retention purposes, but it is **never** chosen as a
`send` parent (its local subvolume no longer exists).

The `remote` field is a **stable id**: the remote's `name` if set, otherwise a
hash of its `type` plus non-secret settings. It does not depend on list order,
so reordering remotes in the config never corrupts the manifest.

### 8.1 Manifest validation

`<dest>/meta.yaml` is read from disk and from remote copies, including copies
that may have been hand-edited or truncated by a partial write from another
tool. `load()` therefore validates the structure before any command touches it
and raises a clean `error: ...` (exit `1`) rather than letting a bad field
surface as a traceback deep inside a command:

- `version` must equal `1`; `profiles` must be a mapping; each profile entry
  must be a mapping.
- Each snapshot must be a mapping with a **non-empty string `id`**, unique
  within its profile (`id` is the key for retention planning, the dependency
  tree and chain building).
- `parent` must be a string or `null`; `uploads` must be a list of mappings.
- A missing `snapshots` list is normalised to `[]`.

A missing or non-numeric `created` is *not* fatal: it degrades to `0` (oldest
possible) so a stray value cannot corrupt due-date and retention arithmetic.

---

## 9. Run algorithm

`btrbak run [SUBVOL] [PROFILE] [--force] [--force-config] [--full]`

- No args → process every config file, every profile.
- `SUBVOL` → only the config file `/etc/btrbak/profiles.d/<SUBVOL>.yaml`.
- `PROFILE` → only that profile within the selected config(s). When
  `PROFILE` is given without `SUBVOL`, config files that do not define that
  profile are skipped; if none define it, the command fails with a config error.
- `--force` → create a backup even if nothing is due (used for manual profiles,
  e.g. an apt hook). Type selection is described in §9.1.
- `--full` → create a full (parentless) backup now; implies `--force`. Ignored
  for local-only profiles.
- `--force-config` → overwrite a differing `config.yaml` already present on a
  remote (see step 4).

Per config file:

1. **Lock** — acquire an exclusive `flock` on `<dest>/.btrbak.lock`. Exit if
   already held (another run in progress).
2. **Clean tmpdir** — delete staging files under `<tmpdir>/<SUBVOL>/` older than
   24 h. Safe because the lock is held.
3. **Load** local `meta.yaml` (or initialize an empty one).
4. **Sync settings** — for every unique remote across the selected profiles:
   - remote has no `config.yaml` → upload it;
   - remote `config.yaml` identical to local → skip;
   - remote `config.yaml` differs → **error** (exit `1`) unless `--force-config`,
     in which case overwrite.
   `auth.yaml` is never uploaded.
5. For each selected profile, run the profile pipeline (§9.1).
6. Run pruning (§9.2).
7. Release the lock.

### 9.1 Profile pipeline

Due rules (times measured against **committed** backups only):

- `freq.full = -1` → *scheduled* full backups are never due automatically.
- `freq.incr = -1` → incremental backups are never due automatically.
- `full_due` = (the profile has no committed backup and at least one of
  `freq.full`/`freq.incr` is not `-1`) **or** ((`freq.full != -1`) and
  (no full exists, or `now − last_full.created ≥ freq.full`)).
  The first clause bootstraps the dependency chain: an automatically-scheduled
  profile always creates its root full on first run, even when `freq.full = -1`.
- `incr_due` = (`freq.incr != -1`) and (not `full_due`) and
  `now − last_backup.created ≥ freq.incr` (where `last_backup` is the most
  recent committed full **or** incremental).
- If neither is due and `--force` is not set → skip creation.

When creating:

1. Pick type:
   - `full` if `--full` is passed;
   - else `full` if `full_due`;
   - else `full` if the profile has no committed backup yet (a chain needs a
     root, e.g. the first manual run);
   - else `incr`.
   A local-only profile (no remotes) records `type: local` instead.
   Parent (for `incr`) = the most recent committed backup in the profile.
2. Create snapshot:
   `btrfs subvolume snapshot -r <src> <dest>/<profile>/<snapshot-id>`.
3. If the profile has remotes:
   - Send (`-p <parent>` for incrementals), streaming through optional `xz` and
     `age`, into `<tmpdir>/<SUBVOL>/<profile>/<snapshot-id>.send`.
   - Compute `sha256` and size of the final file.
   - For each remote: `remote.write(<tmpdir>/<SUBVOL>/<profile>/<snapshot-id>.send,
     "<profile>/<snapshot-id>.send")`; mark that upload `complete`, or `failed`
     if it errors.
   - Delete the staged file from `tmpdir`.
4. Append the snapshot entry to `meta.yaml`; write it locally, then upload to
   every remote (none for a local-only profile).

Retry behavior: a snapshot whose uploads are not all `complete` is retried on
subsequent runs (re-send + re-upload). It is never pruned while incomplete.
Local-only snapshots have no uploads and are committed immediately.

### 9.2 Pruning (dependency-preserving retention)

Run every invocation, even when no new snapshot was created.

A snapshot is deleted **if all** of the following hold:

1. `now − created ≥ keep` (older than the retention window).
2. No other snapshot in the profile lists it as `parent` (no dependents).
3. All of its uploads are `complete`, or it is marked `committed: true`
   (a local-only snapshot has no uploads, so this is trivially satisfied —
   local-only snapshots prune by age only).

Deletion cascades leaf-first: repeat the scan until a pass deletes nothing
(because deleting a leaf may make its parent deletable). For each deleted
snapshot:

1. `btrfs subvolume delete <dest>/<profile>/<id>` (local subvolume).
2. `remote.delete("<profile>/<id>.send")` on every remote (skipped for a
   local-only profile).
3. Remove the entry from `meta.yaml` and rewrite it locally. Once pruning
   completes, re-upload `meta.yaml` to every remote; if pruning aborts, the
   local manifest is still persisted so it never refers to deleted subvolumes.

If a remote `delete` fails after the local subvolume was removed, the entry is
kept in `meta.yaml` and marked `local_deleted: true` (see §8) so the remote
delete is retried on the next run without the snapshot ever being reused as a
`send` parent. If the local `btrfs subvolume delete` itself fails, pruning
aborts for that profile with both the local subvolume and the manifest entry
intact.

The source subvolume `src` is never deleted; only snapshots under `dest`.

Invariants (why this is safe):

- A parent is always older than its children, so children become eligible for
  deletion before (or with) their parents, and are deleted first.
- A snapshot is never deleted while a retained snapshot depends on it.
- A snapshot is never deleted while its offsite copies are missing/incomplete.

---

## 10. Restore

`btrbak restore SUBVOL PROFILE SNAPSHOT_ID TARGET`

1. Verify `TARGET` is on a btrfs filesystem.
2. Load config + manifest; locate `SNAPSHOT_ID`.
3. Build the chain: follow `parent` links from the target snapshot back to the
   full (root), then reverse to get root → … → target order.
4. Validate the **whole** chain up front: no link may be `local`-only or lack a
   complete remote upload. `TARGET` is created only after every check passes, so
   a rejected restore never leaves an empty directory tree behind.
5. Determine the resume point (§10.2).
6. For each remaining snapshot in chain order:
   - Download `<profile>/<id>.send` from a remote (prefer a `complete` upload).
   - Verify the downloaded `sha256` and `size` against the manifest.
   - Decrypt (if encrypted) using the snapshot's recorded `encryption.identity`;
     decompress (if compressed) using the snapshot's recorded `compression`.
   - `btrfs receive <TARGET>` to replay the stream.
7. The restored data ends up as a received subvolume under `TARGET`.

`btrfs receive` matches incremental parents by the subvolume UUID embedded in
the send stream, so replaying in order into the same target is sufficient.

### 10.1 Resumable restore

`btrfs receive` creates one subvolume per stream, named after the snapshot id,
directly inside `TARGET`. A restore interrupted part way through — a dropped
network connection on a long chain, say — therefore leaves a **prefix** of the
chain already received.

Re-running `restore` into the same target detects that prefix and skips those
links, reporting `resuming restore: N of M link(s) already present`, so the
restore completes instead of failing with `creating subvolume … File exists`.

The resume point is the index of the first link in the chain that is not
present in `TARGET`. A link counts as present only when it is a real btrfs
subvolume; if `TARGET` holds a non-subvolume entry (an empty directory, say)
under a snapshot id, the restore stops with an explicit error rather than
letting `btrfs receive` fail cryptically. Because snapshot ids are per profile,
restoring two profiles that produced the same id into one target is rejected by
that check — use separate targets.

Only a contiguous prefix is resumed. A gap (link 1 restored, link 3 restored,
link 2 missing) is not repaired automatically, because skipping a parent would
silently invalidate the chain; `restore` fails at the missing link instead.

### 10.2 Disaster recovery (restore from remote only)

To restore on a fresh host that has no local state:

1. Copy `<remote>/config.yaml` to `/etc/btrbak/profiles.d/<SUBVOL>.yaml`.
2. Provide `auth.yaml` (remote credentials) and the age identity file if the
   profile was encrypted.
3. Run `btrbak restore SUBVOL PROFILE SNAPSHOT_ID TARGET`.

`restore` performs light validation (parse config, resolve the profile and
remotes, check for `age`/`xz` as needed). It does **not** require `src`,
`dest`, `tmpdir`, or the same-filesystem checks. If local `<dest>/meta.yaml`
is missing, `restore` downloads `meta.yaml` from a configured remote before
building the chain.

The target is validated before it is created — it must either not exist or be a
directory, and must live on a btrfs filesystem. Only then is it created, so a
rejected target never leaves an empty directory tree behind.

Notes:

- Restore requires `age` (and the identity file) only if encryption was used.
- Restore targets must be btrfs (documented and enforced).
- Local-only snapshots have no send files and cannot be restored from a remote;
  they are recoverable only from the local `dest`.

---

## 11. Remote interface

Location: `src/remotes/base.py`. `btrbak` owns all logical paths; remotes map
them to their backing store. Logical paths used by btrbak:

- `meta.yaml`
- `config.yaml`
- `<profile>/<snapshot-id>.send`

```python
class Remote(ABC):
    def __init__(self, settings: dict): ...   # remote entry with `auth` resolved (see §6.2)

    def validate(self) -> None: ...
    # raise RemoteError if unreachable / misconfigured

    def read(self, remote_path: str, local_dest: Path) -> None: ...
    # download remote_path → local_dest (streaming; creates parent dirs)

    def write(self, local_src: Path, remote_path: str) -> None: ...
    # upload local_src → remote_path (streaming; atomic where possible)

    def delete(self, remote_path: str) -> None: ...
    # idempotent delete; missing file is not an error
```

`src/remotes/dir.py` — `DirRemote`:

- `type: dir`
- required setting: `path` (the remote root directory).
- Maps `remote_path` → `<path>/<remote_path>`.
- `write` creates parent directories and writes to a temp file then `os.replace`
  (atomic); `read`/`delete` are direct filesystem operations.

Registry: `src/remotes/__init__.py` maps `type` string → class, e.g.
`{"dir": DirRemote}`. Adding a remote = new module + one registry entry.

Errors: all remote failures raise `RemoteError` (defined in `base.py`); callers
treat upload failures as non-fatal (mark `failed`, retry later) and never prune
on failure.

---

## 12. CLI

```
btrbak config check
btrbak run [SUBVOL] [PROFILE] [--force] [--force-config] [--full] [--dry-run]
btrbak verify [SUBVOL] [PROFILE]
btrbak list [SUBVOL] [PROFILE]
btrbak restore SUBVOL PROFILE SNAPSHOT_ID TARGET
```

- Global: `--verbose/-v`, repeatable. Accepted either before or after the
  subcommand (`btrbak -v run …` and `btrbak run … -v` are equivalent; the two
  counts are summed).
- `--dry-run/-n`: simulation — no lock acquired, no snapshots created, no
  `meta.yaml`/snapshot/config written, and no remote *writes*; prints the actions
  that would be taken. The single exception is a **read-only** fetch of each
  remote's `config.yaml`, staged in a temp file that is removed immediately, so
  that the reported sync state is real: a differing remote copy is reported as
  the run-failing condition it is (or as an overwrite under `--force-config`),
  rather than as a blind "would sync". A remote that cannot be read is reported
  and does not abort the dry run.
- Root check: if `os.geteuid() != 0`, print an error and exit `1` before doing
  anything.
- `run` is the main entrypoint used by the external timer.
- `--force-config` allows overwriting a differing remote `config.yaml` (§9).
- `--full` forces a full (parentless) backup and implies `--force`. For
  local-only profiles it has no effect (use `--force` to trigger a manual
  local snapshot).
- Manual profiles (those with `freq.* = -1`) are invoked explicitly:
  `btrbak run SUBVOL PROFILE --force` (e.g. from an apt hook).
- `verify` checks every remote file against the manifest (existence, size,
  `sha256`) and that the dependency chain is intact; reports drift/failures.
  When the local `<dest>/meta.yaml` is missing, `verify` falls back to a copy
  downloaded from a configured remote; if no manifest is available locally or
  on any remote, it reports an error. Like `run` and `list`, `verify` iterates
  **every** matching config: a broken remote definition in one config is
  reported to stderr and the remaining configs are still verified. Exit `2`
  wins over `1` when both occur, since a config error is the more fundamental
  failure.
- `list` shows each profile's snapshots: id, age, type, parent, upload status,
  and a compact dependency tree, indented by dependency depth and headed with a
  snapshot count. When the local manifest is missing, `list` says so on stderr
  rather than silently printing nothing.
- Exit codes: `0` success; `1` runtime error (including a differing remote
  `config.yaml` that is not overwritten); `2` config/validation error.
- Logging to stderr (levels via `-v`); never log secrets or auth values, and
  subprocess error messages include only the program name (plus stderr), never
  command arguments.
- Expected failures always surface as a one-line `error: …` / `config error: …`
  message plus the documented exit code. Unexpected states (a hand-edited or
  truncated `meta.yaml`, a non-numeric `created`) are rejected or normalised
  while loading (§8.1) rather than escaping as a Python traceback.

---

## 13. Safety & error handling

- All external commands (`btrfs`, `age`) are checked; non-zero exit aborts the
  current operation and prevents pruning.
- `meta.yaml` updates are atomic: write to `meta.yaml.tmp`, `fsync`, `os.replace`.
- Uploads are verified against the recorded `sha256` (on `restore`, download
  sha is compared before receive).
- Never prune incomplete, failed, or referenced snapshots.
- Per-config `flock` prevents overlapping runs. `verify` and `restore` take a
  separate advisory `flock` under `<tmpdir>/<SUBVOL>.lock` to serialize their
  staging-directory use; they intentionally do not require the `dest` lock so
  disaster-recovery restores work when `dest` is absent or read-only.
- `run` reaps stale staging files under `<tmpdir>/<SUBVOL>/` while holding that
  same `<tmpdir>/<SUBVOL>.lock`, so a long-running restore can never have its
  staging directory swept out from under it. The lock is taken
  **non-blockingly**: a restore in progress skips the sweep (logged at `-v`)
  and the backup proceeds normally.
- Profile names are validated as path components (§6.1), so no config can make
  snapshots or remote objects land outside `dest` / the remote root.
- `src`, `dest` and `tmpdir` must be absolute (§6.1), so behaviour never
  depends on the invoking process's working directory.
- `tmpdir` staging files are removed on success; on failure the snapshot is
  retained and the send is re-attempted next run.
- If `dest` is nested inside `src`, `run` prints a single warning naming both
  paths and waits 5 s (Ctrl-C aborts) before continuing. `config check` reports
  the same condition without the wait.
- A remote `config.yaml` is never silently overwritten; a differing remote copy
  is an error unless `--force-config` is passed.
- `auth.yaml` and the age identity file should be `0600`; the tool warns if not.
- The uploaded `config.yaml` copy contains no secrets as long as credentials
  are referenced via `auth:` keys into the local-only `auth.yaml`, which is
  never uploaded. Inline remote credentials written directly in a profile file
  would be uploaded as part of `config.yaml`; route all secrets through
  `auth.yaml` instead.
- `keep` is measured against snapshot **creation** time.

---

## 14. Dependencies

- Python ≥ 3.10 (Linux only; btrfs is Linux-only).
- `PyYAML`.
- `btrfs-progs` (`btrfs` binary on PATH).
- `age` binary (only required when encryption is enabled).
- `xz` via Python stdlib `lzma` (no external dependency).

---

## 15. Testing

- Unit: timespan parsing, config validation (including profile-name and
  absolute-path enforcement), retention/dependency algorithm, manifest
  read/write and structural validation, remote path mapping, stable remote-id
  derivation, age recipient classification/validation.
- Integration (pytest): create a btrfs loopback image, mount it, and exercise
  the full `run` pipeline — snapshot → send → upload → manifest → prune →
  restore — for full and incremental chains, asserting restored content and
  that dependency-preserving pruning never orphans an incremental. Also covers
  resuming a restore that was interrupted mid-chain, and rejection of a
  non-subvolume entry occupying a snapshot id in the target.
- CLI: root-check, exit-code mapping for every error class, arg parsing (`-v`
  before and after the subcommand), `--dry-run` acquires no lock and writes
  nothing, dry-run `config.yaml` sync reporting for missing/in-sync/differing
  remotes, `verify` covering ok/corrupt/size-mismatch/missing/broken-chain/
  incomplete/unknown-remote plus its continuation across configs, `list`
  snapshot counts and missing-manifest warning, `config check` reporting, and
  the staging-directory lock being skipped rather than fatal when held.

---

## 16. Future work (explicitly out of v1)

- Additional remotes: `s3`, `sftp`, `rclone`, `restic`-style backends.
- Additional compression (`zstd`, `gzip`) and encryption (`gpg`).
- Per-profile `src`/`dest`/compression/encryption overrides (currently file-level).
- Writable snapshots and local rollback.
- Nested-subvolume handling (explicit per-subvolume enumeration).
- Example systemd timer units and distro packaging.
- Namespace the remote layout by config name so one remote root can be shared
  across configs (currently guarded by the `config.yaml` conflict check).
- Remote-side `btrfs receive` (live btrfs target) as an alternative remote type.
- Remote orphan reconciliation (a `Remote.list` primitive) so `verify` can
  report files present on a remote but missing from the manifest.
