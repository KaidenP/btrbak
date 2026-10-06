---
title: BTRBAK-PROFILES
section: 5
date: October 2025
header: btrbak manual
---

# NAME

btrbak-profiles - configuration file format for btrbak

# SYNOPSIS

```
/etc/btrbak/profiles.d/*.yaml
```

# DESCRIPTION

Each YAML file in `/etc/btrbak/profiles.d` describes one source btrfs
subvolume and the backup profiles applied to it. The file stem becomes the
`SUBVOL` selector used by **btrbak**(1), so stems must match the profile-name
rule below and can only ever name a file inside that directory.

# TOP-LEVEL KEYS

`src`
: Required. Absolute path to the source btrfs subvolume.

`dest`
: Required. Absolute path to the snapshot destination; it must be on the same
  btrfs filesystem as `src`.

`tmpdir`
: Optional. Absolute path used for staging send streams and downloads. Defaults
  to `/var/tmp/btrbak`.

`compression`
: Optional mapping. Currently supports `algorithm` (must be `xz`, the default)
  and `level` (an integer 0-9; defaults to 6).

`encryption`
: Optional mapping. Currently supports `algorithm` (must be `age`, the
  default), a required non-empty `recipients` list of inline `age1...` public
  keys or absolute paths to recipients files, and an optional `identity`
  absolute path to the **age**(1) private key file needed to restore. When
  `identity` is unset, backups are written but can never be restored.

`profiles`
: Required. A non-empty mapping of profile name to profile settings.

# PROFILE SETTINGS

Each profile is a mapping with the following keys.

`freq`
: Required mapping with `full` and `incr` values. Each is a timespan after
  which a full or incremental backup is due; `-1` means "never" and may be
  used here.

`keep`
: Required. A timespan for which snapshots are retained before pruning. Must
  not be `-1`.

`remotes`
: Optional list of remote mappings; defaults to an empty list (local snapshots
  only).

# REMOTE SETTINGS

`type`
: Required. Remote backend type. Version 1 supports `dir`.

`name`
: Optional stable identifier. When omitted, btrbak derives one from a hash of
  the other settings.

`auth`
: Optional key into `/etc/btrbak/auth.yaml`; the value stored there is merged
  into the remote settings. Used primarily for credential material.

For the `dir` backend:

`path`
: Required. Absolute or expandable path to the local directory that acts as
  the remote root.

# TIMESPANS

Timespans are strings such as `1d`, `12h`, `2w`, `1mo`, or `1y`. Supported
units are `s`, `min`, `h`, `d`, `w`, `mo`, and `y`. A bare positive integer is
accepted as a number of seconds. The sentinel `-1` means "never" and is
accepted only for `freq.full` and `freq.incr`.

# NAME RULES

Profile names and `SUBVOL` selectors must start with a letter or digit and
contain only letters, digits, `.`, `_`, or `-`. They are used as single path
components under `dest` and on each remote.

# EXAMPLE

```
src: /mnt/data
dest: /mnt/backups/data
tmpdir: /var/tmp/btrbak
compression:
  algorithm: xz
  level: 6
encryption:
  algorithm: age
  recipients:
    - age1...
  identity: /root/.config/age/keys.txt
profiles:
  hourly:
    freq:
      full: 1w
      incr: 1h
    keep: 1mo
    remotes:
      - type: dir
        name: offsite
        path: /mnt/offsite
```

# FILES

`/etc/btrbak/profiles.d/*.yaml`
: Profile configuration files.

`/etc/btrbak/auth.yaml`
: Remote credentials referenced by `auth` keys.

# SEE ALSO

**btrbak**(1), **age**(1)
