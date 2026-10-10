---
title: BTRBAK
section: 1
date: October 2025
header: btrbak manual
---

# NAME

btrbak - btrfs snapshot and offsite backup utility

# SYNOPSIS

```
btrbak [-v | --verbose] command [options] [arguments]
btrbak config check
btrbak run [SUBVOL] [PROFILE] [--force] [--force-config] [--full] [--dry-run]
btrbak verify [SUBVOL] [PROFILE]
btrbak list [SUBVOL] [PROFILE]
btrbak restore SUBVOL PROFILE SNAPSHOT_ID TARGET
btrbak forget SUBVOL PROFILE SNAPSHOT_ID
```

# DESCRIPTION

**btrbak** creates read-only btrfs snapshots of a source subvolume, ships
incremental offsite backups with **btrfs send**, verifies the remote objects
against a local manifest, and prunes expired snapshots by age.

The program must be run as **root**. It discovers configuration files in
`/etc/btrbak/profiles.d`, one YAML file per source subvolume. Configuration
format and validation rules are documented in **btrbak-profiles**(5).

The special `-1` timespan means "never" and may be used for `freq.full` and
`freq.incr`. A profile's `keep` is not a timespan: it is an integer backup
count, or `-1` to keep everything; see **btrbak-profiles**(5).

# OPTIONS

`-v, --verbose`
: Increase verbosity. May be given once or twice and may appear either before
  or after the subcommand.

# COMMANDS

## config check

Validate every profile file in `/etc/btrbak/profiles.d`, or the single file
named by `SUBVOL` when one is given. It reports every configuration problem it
can find in a single pass, including that each remote is reachable and that
each configured **age**(1) recipient actually works. Exits 2 when any problem
is found.

## run

Create due snapshots and backups, retry incomplete uploads, and prune expired
snapshots for the named `SUBVOL` (optional), optionally limited to `PROFILE`
(optional). When `SUBVOL` is omitted, every profile file is processed; when
`PROFILE` is omitted, every profile in each file is processed.

`-g, --group NAME`
: Run the snapshot group `NAME` from `/etc/btrbak/groups.yaml` instead of a
  `SUBVOL`/`PROFILE` selection. Members are `SUBVOL` or `SUBVOL:PROFILE`
  entries; see **btrbak-profiles**(5). Cannot be combined with `SUBVOL` or
  `PROFILE`. An empty group is a no-op and prints a warning.

`--force`
: Create a snapshot even when nothing is due.

`--force-config`
: Overwrite a remote `config.yaml` that already differs from the local profile
  file. Without this option, a differing remote config aborts the run.

`--full`
: Force a full snapshot (rather than an incremental) when one is created.

`-n, --dry-run`
: Print what would be done without creating snapshots, uploading data, or
  pruning anything. Remote access is limited to a read-only check of each
  remote's `config.yaml`.

## verify

Check the remote objects recorded in the local `meta.yaml` against their
recorded checksums and sizes, and report missing, corrupt, or incomplete
backups. A missing local manifest is fetched from the first configured remote
that has one, when possible. `SUBVOL` and `PROFILE` are optional selectors with
the same meaning as in **run**.

## list

List the snapshots recorded in `meta.yaml`, with their type, age, parent, and
per-remote upload state. Orphaned profiles (recorded in `meta.yaml` but no
longer configured) are reported separately.

## restore

Restore the snapshot chain ending at `SNAPSHOT_ID` to `TARGET`. `TARGET` must
be on a btrfs filesystem and is created only after the whole chain validates.
A restore interrupted part way through can be re-run into the same target;
links already received are detected by subvolume UUID and skipped.

## forget

Drop a stuck snapshot entry from `meta.yaml`. This is the escape hatch for a
snapshot whose local subvolume is gone while its uploads never completed, so
that **run** would otherwise retry it forever. It refuses to forget a snapshot
whose local subvolume still exists, or one that another snapshot depends on as
its parent. Remote objects for the entry are deleted on a best-effort basis.

# CONFIGURATION

Profile files use the format documented in **btrbak-profiles**(5). Each file
describes one source subvolume (`src`), a snapshot destination on the same
btrfs filesystem (`dest`), optional compression and encryption settings, and
one or more named profiles.

Remote credentials are kept separately in `/etc/btrbak/auth.yaml` and
referenced from profiles with an `auth` key; that file should be readable only
by root.

# FILES

`/etc/btrbak/profiles.d/*.yaml`
: One configuration file per source subvolume.

`/etc/btrbak/auth.yaml`
: Optional remote credentials referenced by `auth` keys.

`<dest>/meta.yaml`
: Local manifest; the authoritative record of snapshots and remote uploads.

`<dest>/<profile>/<snapshot-id>`
: Local read-only snapshots.

`/var/tmp/btrbak`
: Default staging directory (configurable per profile file with `tmpdir`).

# EXIT STATUS

`0`
: Success.

`1`
: Runtime error, including a differing remote `config.yaml`, a failed upload,
  or a failed verify.

`2`
: Configuration or validation error.

`130`
: Interrupted (Ctrl-C).

# SEE ALSO

**btrbak-profiles**(5), **btrfs**(8), **btrfs-send**(8),
**btrfs-subvolume**(8), **age**(1)
