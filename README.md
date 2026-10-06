# btrbak

A root-only Python CLI for managing btrfs read-only snapshots and shipping
incremental offsite backups via `btrfs send`.

See `PROJECT_SPEC.md` for the full design. This implementation is a work in
progress tracking that spec.

## Installation

### From the `.deb`

To download the latest release and install it in one go (requires `curl`):

```
curl -sL "$(curl -sL https://api.github.com/repos/KaidenP/btrbak/releases/latest | grep -o 'https://[^"]*\.deb' | head -1)" -o /tmp/btrbak.deb && sudo apt-get install -y /tmp/btrbak.deb && rm -f /tmp/btrbak.deb
```

Build the package yourself with `./debian/build-deb.sh` or download a release
artifact, then install it with apt so the dependencies are resolved
automatically:

```
sudo apt-get install ./btrbak_1.0.1-1_all.deb
```

Or, if you already have `python3`, `python3-yaml`, and `btrfs-progs` installed:

```
sudo dpkg -i btrbak_1.0.1-1_all.deb
```

The package installs:

- `/usr/bin/btrbak`
- `btrbak(1)` and `btrbak-profiles(5)` manpages
- bash and zsh completions
- a `btrbak.service` / `btrbak.timer` systemd pair, with the timer enabled by default
- example configuration under `/usr/share/doc/btrbak/examples/`

After installing, create your configuration from the examples:

```
sudo mkdir -p /etc/btrbak/profiles.d
sudo cp /usr/share/doc/btrbak/examples/profiles.d/example.yaml \
    /etc/btrbak/profiles.d/<subvol>.yaml
# Only needed if a profile references an auth key:
sudo cp /usr/share/doc/btrbak/examples/auth.yaml /etc/btrbak/auth.yaml
sudo chmod 600 /etc/btrbak/auth.yaml
```

Edit the copied file(s) to match your source subvolume, snapshot destination,
remote(s), encryption, and schedule, then validate:

```
sudo btrbak config check
```

The timer runs `btrbak run` hourly. It is skipped cleanly while
`/etc/btrbak/profiles.d` is empty, so a fresh install won't fail until you add
at least one profile. Inspect it with:

```
systemctl status btrbak.timer
systemctl list-timers btrbak.timer
```

Disable or re-enable the timer with:

```
sudo systemctl disable --now btrbak.timer
sudo systemctl enable --now btrbak.timer
```

### From source

For development, install in editable mode:

```
python -m pip install -e .
```

To install the command without the Debian package:

```
python -m pip install .
```

Source lives in the `btrbak` package under `src/`.

## Commands

```
btrbak config check
btrbak run [SUBVOL] [PROFILE] [--force] [--force-config] [--full] [--dry-run]
btrbak verify [SUBVOL] [PROFILE]
btrbak list [SUBVOL] [PROFILE]
btrbak restore SUBVOL PROFILE SNAPSHOT_ID TARGET
btrbak forget SUBVOL PROFILE SNAPSHOT_ID
```

## Configuration

- `/etc/btrbak/profiles.d/<name>.yaml` — one file per source subvolume.
- `/etc/btrbak/auth.yaml` — remote credentials referenced by `auth:` keys.

`src`, `dest` and `tmpdir` must be absolute paths, and profile names must match
`[A-Za-z0-9][A-Za-z0-9._-]*` (they are used as path components under `dest` and
on each remote). The `SUBVOL` command-line selector is held to the same rule, so
it can only ever name a file inside `profiles.d` — `btrbak list /some/other/path`
is a config error, not a way to read an arbitrary file. An `age` recipient that
names a recipients *file*, and `encryption.identity`, must be absolute for the
same reason; inline `age1...` keys are fine. Run `sudo btrbak config check` to
validate everything, including that every `age` recipient actually works.

`config check` reports **every** profile file it can, not just the first one it
cannot parse, so a single pass shows all outstanding problems. `run`, `list`
and `verify` likewise report a file that fails to load and carry on with the
rest.

Encryption needs recipients to *write* a backup and the identity file to
*read* one back, so `config check` warns when `encryption.identity` is unset:
nothing breaks operationally, but nothing encrypted under that profile can ever
be recovered.

## Removing a profile

Deleting a profile from a config file stops btrbak managing it entirely — its
snapshots under `<dest>/<profile>/`, its objects on each remote, and its entry
in `meta.yaml` are all left alone, and nothing will ever prune them.
`run`, `list` and `verify` therefore report it as `ORPHANED PROFILE`, and
`verify` counts it in its summary line, so the strand cannot pass unnoticed.
Cleaning up is manual:

```
btrfs subvolume delete <dest>/<profile>/<id>     # for each snapshot
rm <remote>/<profile>/<id>.send                  # for each snapshot
# then drop the profile's entry from <dest>/meta.yaml
```

## Restoring

```
sudo btrbak restore SUBVOL PROFILE SNAPSHOT_ID TARGET
```

`TARGET` must be on btrfs and is created only after the whole chain validates —
including the first remaining link's `sha256`/`size`, which is fetched and
checked before the directory exists. A restore rejected for any reason,
including a corrupt or unreachable offsite object, therefore never leaves an
empty directory tree behind. The one thing that guarantee does not cover is an
object that is corrupt *and* faithfully recorded, i.e. a `meta.yaml` whose own
`sha256`/`size` describe the damaged bytes; that is caught by the codec one step
later, reported as a clean `error:` with the target resumable.

A restore interrupted part way through can simply be re-run into the same
target: the links already received are detected and skipped, so the chain
resumes instead of failing with `File exists`. Detection is by subvolume UUID,
not name alone, so an unrelated subvolume sitting at a snapshot id is rejected
instead of silently treated as part of the chain.

## Forgetting a stuck snapshot

```
sudo btrbak forget SUBVOL PROFILE SNAPSHOT_ID
```

A snapshot whose local subvolume is gone while its uploads never all completed
cannot be re-sent, so every `run` retries it and exits 1 forever; the retry
warning names the exact `forget` command. `forget` drops the entry from
`meta.yaml` (and best-effort deletes any partial remote object, which also
finishes a stuck `local_deleted` remote-delete retry). It refuses to touch an
entry whose local subvolume still exists, or one that other snapshots depend
on as their parent.

## Staging privacy

Everything btrbak stages under `tmpdir` (send streams, decrypted restore
intermediates, verify downloads) is created `0600` inside `0700` directories —
a staged stream is an entire filesystem, and a restore's decrypted intermediate
is plaintext even for an encrypted profile, so none of it is ever
world-readable in `/var/tmp`.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | success |
| `1` | runtime error (including a differing remote `config.yaml`) |
| `2` | config/validation error |
| `130` | interrupted (Ctrl-C) |

`verify` treats two manifest states as normal resting points rather than
drift: a snapshot whose remotes were all removed (recorded `committed: true`
so retention prunes it by age), and one awaiting a remote delete retry
(`local_deleted`). Both are reported informationally, not as failures.

`run` trusts the manifest and never re-checks an object it already recorded as
uploaded, so a remote object that rots or is tampered with is only ever caught
by `verify` — and is never repaired by `run`. Run `verify` on a schedule
alongside `run`; without it, silent offsite corruption goes unnoticed.

## Packaging

Build a binary `.deb` without needing debhelper or dh-python:

```
./debian/build-deb.sh
```

The script produces `dist/btrbak_1.0.1-1_all.deb`. It installs:

- `/usr/bin/btrbak`
- the `btrbak` package into `/usr/lib/python3/dist-packages/btrbak`
- `btrbak(1)` and `btrbak-profiles(5)` manpages
- bash completion at `/usr/share/bash-completion/completions/btrbak`
- zsh completion at `/usr/share/zsh/vendor-completions/_btrbak`
- an empty `/etc/btrbak/profiles.d/` configuration directory

The package depends on `python3`, `python3-yaml`, and `btrfs-progs`, and
recommends `age` for encrypted backups.

Man pages are generated at build time with `pandoc` (install it with
`sudo apt-get install pandoc`) from the committed Markdown source in
`docs/btrbak.1.md` and `docs/btrbak-profiles.5.md`, so the Markdown
documentation and the installed man pages share a single source.
Completion sources live in `completions/`.

A GitHub Actions workflow (`.github/workflows/build-deb.yml`) builds the
`.deb` on every push and pull request and uploads it as a build artifact.

## Examples and scheduled runs

The package installs example configuration under
`/usr/share/doc/btrbak/examples/`:

- `profiles.d/example.yaml` — a sample source-subvolume profile file
- `auth.yaml` — a sample remote-credentials file

Copy and edit these into `/etc/btrbak/` before running.

A systemd timer is installed and enabled by default:

```
systemctl status btrbak.timer
systemctl list-timers btrbak.timer
```

`btrbak.timer` runs `/usr/bin/btrbak run` hourly. The service skips cleanly
while `/etc/btrbak/profiles.d` is empty, so a fresh install does not fail
until you add at least one profile. Disable or re-enable it with:

```
sudo systemctl disable --now btrbak.timer
sudo systemctl enable --now btrbak.timer
```

## Development

Install the project in editable mode to expose the `btrbak` command:

```
python -m pip install -e .
```

Source lives in the `btrbak` package under `src/`, so an installed copy occupies
a single top-level `btrbak/` directory.

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
