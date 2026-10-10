# AGENTS.md

Guidance for AI coding agents working in this repository.

## What this is

`btrbak` is a root-only Python CLI for managing btrfs read-only snapshots and
shipping incremental offsite backups via `btrfs send`. It is distributed as a
Debian package plus a systemd timer that runs `btrbak run` hourly. The full
design lives in `PROJECT_SPEC.md`; `README.md` is the user-facing
documentation.

## Layout

- `src/btrbak/` — the package; the CLI entry point is `btrbak.cli:main`
- `tests/` — unit tests; `tests/integration/test_btrfs.py` is the loopback
  btrfs end-to-end suite
- `debian/` — `.deb` packaging (`build-deb.sh`), systemd units, apt hook
- `completions/` — bash/zsh completion sources
- `docs/` — man page Markdown, compiled to `btrbak(1)`/`btrbak-profiles(5)` at
  build time with `pandoc`
- `.github/workflows/build-deb.yml` — build/test/release CI

## Commands

- Editable install: `python -m pip install -e .`
- Unit tests: `python -m pytest` (all green required)
- Integration tests (root, real btrfs loopback): `sudo python -m pytest tests/integration/test_btrfs.py`
- Build the `.deb`: `./debian/build-deb.sh` (requires `pandoc`)
- Validate a config: `sudo btrbak config check`

`pyproject.toml` sets `testpaths = ["tests"]` and `pythonpath = ["src"]`, so
plain `pytest` works without installing the package.

## Code conventions

- Python 3.10+. No runtime dependencies beyond what is listed in
  `pyproject.toml`.
- The tool is root-only by intent (btrfs subvolume operations). Do not add code
  that assumes it can run unprivileged.
- Exit codes are contractual (see README): `0` success, `1` runtime error,
  `2` config/validation error, `130` interrupted. Preserve them.
- `config check` reports **all** problems it can find, not just the first one;
  `run`, `list`, and `verify` report a profile that fails to load and carry on
  with the rest.
- Keep modules independently importable (stated in the `__init__.py`
  docstring); tests import and exercise single units directly.
- Security-sensitive files (`auth.yaml`, gdrive token files) are expected to be
  `0600` and owned by the caller; validation warns otherwise. Keep that.
- Config paths (`src`, `dest`, `tmpdir`, `encryption.identity`, file-based
  `age` recipients) must be absolute. Profile names and the `SUBVOL`
  command-line selector must match `[A-Za-z0-9][A-Za-z0-9._-]*`; they become
  path components under `dest` and on each remote.

## Tests

- Unit tests mock subprocesses via `monkeypatch.setattr(util.subprocess, "run", ...)`
  and assert on the constructed `CompletedProcess`. Follow that pattern rather
  than requiring real binaries.
- Integration tests skip cleanly when not running as root or when btrfs
  tooling is unavailable — those are skips, never failures.

## Version bump and release

This is a multi-file change, and the CI `release` job verifies the tag matches
the package version exactly. To bump the version:

1. `src/btrbak/__init__.py` → `__version__`
2. `pyproject.toml` → `[project] version`
3. Prepend a new entry to `debian/changelog`
4. Update the `.deb` filename references in `README.md` (search for the old
   version string)

Then tag and push both:

```
git tag -a v<VERSION> -m "btrbak <VERSION>"
git push origin master v<VERSION>
```

Existing convention is annotated tags — tagger `Kaiden`, message
`btrbak <VERSION>` (see `git cat-file -p v1.2.1`). Pushing the tag triggers the
`release` job, which builds the `.deb`, checks the tag against `__version__`,
and uploads the artifact to a GitHub release.

## Packaging / systemd gotchas

- Man pages are generated at build time from `docs/*.md` with `pandoc`. If you
  change CLI help text or options, update the man page Markdown too.
- The systemd timer runs `btrbak run` hourly with `Persistent=true` and
  `RandomizedDelaySec=300`. A config error therefore produces a failed unit
  every hour until fixed.
- `btrfs filesystem show` output varies by btrfs-progs version. Since v6.6 the
  label and uuid are printed on the same line (`Label: 'name'  uuid: <fsid>`),
  with the label single-quoted when non-empty and `none` when empty.
  `util.btrfs_fsid()` parses this; if you touch it, keep
  `tests/test_util.py::test_btrfs_fsid_*` green and make sure a label that
  itself contains `uuid:` cannot shadow the real filesystem UUID.
- btrfs subvolumes report distinct `st_dev` values, so "same filesystem"
  checks must go through `util._mount_point` + `util.btrfs_fsid` — never
  compare raw `st_dev`.
