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
: Required. A positive integer: the number of backups to retain. The `keep`
  most recent snapshots are kept together with every snapshot on their parent
  chains, so a retained incremental is always restorable. `-1` keeps
  everything forever.

`remotes`
: Optional list of remote mappings; defaults to an empty list (local snapshots
  only).

# REMOTE SETTINGS

`type`
: Required. Remote backend type. Version 1 supports `dir` and `gdrive`.

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

For the `gdrive` backend:

`folder`
: Required. The Google Drive folder that acts as the remote root: either its
  ID (the long string after `/folders/` in the Drive URL) or a slash-separated
  path of folder names (e.g. `btrbak/test`). Each component of a path is
  created at the previous level if it does not exist, so `btrbak/test` becomes
  a folder `btrbak` at the top level of "My Drive" containing `test`. A
  component that matches several folders is rejected (use the ID instead).

`auth`
: Required. Key into `/etc/btrbak/auth.yaml` whose value is the absolute path
  to an OAuth credentials file produced by `btrbak gdrive authorize` (it holds
  the client id/secret and a refresh token). The file should be `0600`.

btrbak maps its logical paths (`meta.yaml`, `config.yaml`,
`<profile>/<snapshot-id>.send`) onto a folder tree under that Drive folder,
creating the root and intermediate profile folders on demand. Uploads are
resumable and replace the previous object only once complete.

# TIMESPANS

Timespans are strings such as `1d`, `12h`, `2w`, `1mo`, or `1y`. Supported
units are `s`, `min`, `h`, `d`, `w`, `mo`, and `y`. A bare positive integer is
accepted as a number of seconds. The sentinel `-1` means "never" and is
accepted only for `freq.full` and `freq.incr` (the profile `keep` key is not a
timespan; it is an integer backup count, or `-1` to keep forever).

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
    keep: 30
    remotes:
      - type: dir
        name: offsite
        path: /mnt/offsite
```

# GROUPS

Snapshot groups are defined in `/etc/btrbak/groups.yaml`. A group is a list
of `SUBVOL` or `SUBVOL:PROFILE` members, where `SUBVOL` is the stem of a file
in `/etc/btrbak/profiles.d`.

```
apt:
  - root
  - var:weekly
boot:
  - root
  - var
```

A bare `SUBVOL` member selects every profile in that file; a
`SUBVOL:PROFILE` member selects a single profile. Group names follow the same
safe-name rule as profile names. `btrbak run --group NAME` runs a group, and
an empty group is a no-op.

# GOOGLE DRIVE SETUP

The `gdrive` backend authenticates with your personal Google account via OAuth:

1. In Google Cloud Console, create a project, enable the Google Drive API,
   configure the OAuth consent screen (External, add yourself as a test user),
   and create an OAuth client ID of type **Desktop app**; download its JSON.
2. Run `btrbak gdrive authorize --client-secret <client.json> --token
   /etc/btrbak/gdrive-token.json` and sign in when prompted. On a headless
   server, pass `--console` and paste the printed code back.
3. Reference the token file from `/etc/btrbak/auth.yaml`:

   ```
   gdrive: /etc/btrbak/gdrive-token.json
   ```

4. Add the remote to a profile (`type: gdrive`, `folder: <id>`, `auth:
   gdrive`) and run `btrbak config check` to confirm access.

# FILES

`/etc/btrbak/profiles.d/*.yaml`
: Profile configuration files.

`/etc/btrbak/auth.yaml`
: Remote credentials referenced by `auth` keys.

`/etc/btrbak/groups.yaml`
: Snapshot group definitions.

# SEE ALSO

**btrbak**(1), **age**(1)
