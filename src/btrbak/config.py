"""Load and validate profile + auth configuration.

Profile files live in ``/etc/btrbak/profiles.d/<name>.yaml``; auth credentials
live in ``/etc/btrbak/auth.yaml``.
"""

import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from . import timespan
from .remotes import create_remote
from .util import (
    AGE_MISSING_ERROR,
    age_recipient_error,
    is_nested,
    is_subvolume,
    same_device,
    which,
)

CONFIG_DIR = Path("/etc/btrbak/profiles.d")
AUTH_PATH = Path("/etc/btrbak/auth.yaml")
GROUP_PATH = Path("/etc/btrbak/groups.yaml")
DEFAULT_TMPDIR = Path("/var/tmp/btrbak")

# Profile names become path components under `dest` (`<dest>/<profile>/<id>`)
# and under every remote (`<profile>/<id>.send`), so they are restricted to a
# conservative, filesystem-safe character set with no separators at all.
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# The SUBVOL selector is the config file's own stem, so it gets exactly the
# same treatment: a selector is one path component inside CONFIG_DIR, never a
# path. Without this, `btrbak list /etc/other/config` or `btrbak list
# ../../../tmp/x/y` would concatenate out of CONFIG_DIR and load an arbitrary
# file as a profile.
SUBVOL_NAME_RE = PROFILE_NAME_RE

_NAME_HINT = (
    "use letters, digits, '.', '_' or '-' and start with a letter or digit"
)


def _validate_subvol_name(subvol) -> None:
    """Reject a SUBVOL selector that is not one safe path component."""
    if not _is_safe_name(subvol):
        raise ConfigError(
            f"invalid subvol {subvol!r}: a SUBVOL is a single name, not a path; "
            f"{_NAME_HINT}"
        )


class ConfigError(Exception):
    """Raised for invalid configuration."""


@dataclass
class RemoteSpec:
    id: str
    type: str
    settings: dict


@dataclass
class Profile:
    name: str
    freq_full: int
    freq_incr: int
    keep: int
    remotes: list[RemoteSpec]


@dataclass
class Config:
    name: str
    path: Path
    src: Path
    dest: Path
    tmpdir: Path
    compression: dict | None
    encryption: dict | None
    profiles: dict[str, Profile]


# --- loading ---------------------------------------------------------------


def load_auth(path=None) -> dict:
    """Load and return ``auth.yaml``; ``path`` defaults to the module constant."""
    path = Path(path) if path is not None else Path(AUTH_PATH)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}")
    if not isinstance(data, dict):
        raise ConfigError(f"auth file must be a mapping: {path}")
    return data


def _parse_tmpdir(value) -> Path:
    """Return the configured tmpdir, falling back to the default when unset/empty."""
    if not value:
        return DEFAULT_TMPDIR
    return Path(str(value)).expanduser()


def _validate_profile_name(name, path) -> None:
    """Reject profile names that are unsafe as a path component."""
    if not _is_safe_name(name):
        raise ConfigError(
            f"{path}: profile name {name!r} is not allowed; {_NAME_HINT}"
        )


def _is_safe_name(name) -> bool:
    # The pattern already requires an alphanumeric first character, so '.'
    # and '..' can never match.
    return bool(SUBVOL_NAME_RE.match(str(name)))


def _require_absolute(value, label: str, path) -> None:
    """Reject relative paths.

    ``src``/``dest``/``tmpdir`` are resolved against the current working
    directory otherwise, which silently makes behaviour depend on where the
    tool happened to be invoked from (a systemd unit's WorkingDirectory).
    """
    if not Path(value).expanduser().is_absolute():
        raise ConfigError(f"{path}: '{label}' must be an absolute path: {value}")


def load_config(path, auth: dict) -> Config:
    path = Path(path)
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}")
    if not isinstance(data, dict):
        raise ConfigError(f"config must be a mapping: {path}")

    src = data.get("src")
    dest = data.get("dest")
    if not src or not dest:
        raise ConfigError(f"{path}: 'src' and 'dest' are required")
    _require_absolute(src, "src", path)
    _require_absolute(dest, "dest", path)
    if data.get("tmpdir"):
        _require_absolute(data["tmpdir"], "tmpdir", path)

    profiles_raw = data.get("profiles")
    if not isinstance(profiles_raw, dict) or not profiles_raw:
        raise ConfigError(f"{path}: 'profiles' must be a non-empty mapping")

    profiles = {}
    for pname, praw in profiles_raw.items():
        if not isinstance(praw, dict):
            raise ConfigError(f"{path}: profile {pname!r} must be a mapping")
        _validate_profile_name(pname, path)
        profiles[pname] = _parse_profile(pname, praw, auth, path)

    return Config(
        name=path.stem,
        path=path,
        src=Path(src).expanduser(),
        dest=Path(dest).expanduser(),
        tmpdir=_parse_tmpdir(data.get("tmpdir")),
        compression=_normalize_compression(data.get("compression"), path),
        encryption=_normalize_encryption(data.get("encryption"), path),
        profiles=profiles,
    )


def discover_config_paths(subvol=None) -> list[Path]:
    """Return every config path in scope, without parsing any of them."""
    if subvol:
        return [config_path_for_subvol(subvol)]
    if not CONFIG_DIR.is_dir():
        raise ConfigError(f"config directory not found: {CONFIG_DIR}")
    # Prefer <name>.yaml over <name>.yml when both exist for the same subvol.
    by_stem = {}
    for path in CONFIG_DIR.glob("*.yaml"):
        by_stem[path.stem] = path
    for path in CONFIG_DIR.glob("*.yml"):
        by_stem.setdefault(path.stem, path)
    paths = sorted(by_stem.values(), key=lambda p: p.name)
    if not paths:
        raise ConfigError(f"no config files found in {CONFIG_DIR}")
    return paths


def discover_configs_tolerant(subvol=None) -> list[tuple[Path, Config | None, ConfigError | None]]:
    """Load every config in scope, reporting per-file failures instead of raising.

    Returns one ``(path, config, error)`` triple per config file, with exactly
    one of the last two set. Discovery itself (a missing config dir, no files,
    or an unknown SUBVOL) is still fatal -- there is nothing to iterate -- but
    one unparseable profile file no longer hides the state of every other one.

    This matters most for ``config check``, whose entire purpose is to report
    all outstanding problems at once, and for a timer-driven ``run``, where a
    single bad profile should not stop backups for the others.
    """
    paths = discover_config_paths(subvol)
    try:
        auth = load_auth()
    except ConfigError as exc:
        auth, auth_error = {}, exc
    else:
        auth_error = None

    results = []
    for path in paths:
        if auth_error is not None:
            results.append((path, None, auth_error))
            continue
        try:
            results.append((path, load_config(path, auth), None))
        except ConfigError as exc:
            results.append((path, None, exc))
    return results


def config_path_for_subvol(subvol) -> Path:
    """Resolve a SUBVOL selector to a config file (``.yaml`` or ``.yml``).

    The selector is validated as a single safe path component first, so it
    can only ever name a file directly inside :data:`CONFIG_DIR` -- it cannot
    carry a separator, an absolute path or a ``..`` sequence that would read a
    config from somewhere else entirely.
    """
    _validate_subvol_name(subvol)
    for ext in (".yaml", ".yml"):
        path = CONFIG_DIR / f"{subvol}{ext}"
        if path.is_file():
            return path
        if path.exists():
            # Without this, a directory named <subvol>.yaml would surface as
            # an IsADirectoryError runtime error (exit 1) instead of the
            # config error (exit 2) it is.
            raise ConfigError(
                f"config path for subvol {subvol!r} is not a regular file: {path}"
            )
    raise ConfigError(f"no config file for subvol {subvol!r} in {CONFIG_DIR}")


def filter_profiles(config: Config, name: str | None) -> Config:
    """Return *config* restricted to profile *name* (unchanged when *name* is None)."""
    if not name:
        return config
    if name not in config.profiles:
        raise ConfigError(f"unknown profile: {name!r}")
    return replace(config, profiles={name: config.profiles[name]})


def select_profiles(config: Config, name: str | None) -> Config | None:
    """Return *config* restricted to profile *name*, or ``None`` when absent.

    Unlike :func:`filter_profiles`, this is a tolerant selector used by CLI
    commands that iterate over multiple config files: a config without the
    requested profile is skipped rather than treated as an error.
    """
    if not name:
        return config
    if name not in config.profiles:
        return None
    return replace(config, profiles={name: config.profiles[name]})


def select_profile_names(config: Config, names) -> Config:
    """Return *config* restricted to *names*, or unchanged when *names* is empty."""
    if not names:
        return config
    return replace(config, profiles={name: config.profiles[name] for name in names})


def load_groups(path=None) -> dict[str, list[tuple[str, str | None]]]:
    """Load and return snapshot groups from ``groups.yaml``.

    A missing file is an empty group set, not an error: the shipped default has
    empty ``apt`` and ``boot`` groups so the installed hooks are no-ops until
    the admin lists members.
    """
    path = Path(path) if path is not None else Path(GROUP_PATH)
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"groups file must be a mapping: {path}")

    groups = {}
    for gname, members in data.items():
        _validate_group_name(gname, path)
        if not isinstance(members, list):
            raise ConfigError(f"{path}: group {gname!r} must be a list")
        parsed = []
        for member in members:
            parsed.append(_parse_group_member(member, path, gname))
        groups[str(gname)] = parsed
    return groups


def _validate_group_name(name, path) -> None:
    if not _is_safe_name(name):
        raise ConfigError(
            f"{path}: group name {name!r} is not allowed; {_NAME_HINT}"
        )


def _parse_group_member(member, path, group) -> tuple[str, str | None]:
    """Parse one ``SUBVOL`` or ``SUBVOL:PROFILE`` group member."""
    if not isinstance(member, str):
        raise ConfigError(
            f"{path}: group {group!r}: member must be a string: {member!r}"
        )
    text = member.strip()
    if not text:
        raise ConfigError(f"{path}: group {group!r}: empty member")

    subvol, sep, profile = text.partition(":")
    if not subvol:
        raise ConfigError(f"{path}: group {group!r}: member {member!r} has no SUBVOL")
    if sep and not profile:
        raise ConfigError(
            f"{path}: group {group!r}: member {member!r} has no PROFILE after ':'"
        )
    if not _is_safe_name(subvol):
        raise ConfigError(
            f"{path}: group {group!r}: invalid SUBVOL {subvol!r}; {_NAME_HINT}"
        )
    if sep and not _is_safe_name(profile):
        raise ConfigError(
            f"{path}: group {group!r}: invalid PROFILE {profile!r}; {_NAME_HINT}"
        )
    return (subvol, profile if sep else None)


def _parse_profile(name, praw, auth, path) -> Profile:
    freq = praw.get("freq")
    if not isinstance(freq, dict):
        raise ConfigError(f"{path}: profile {name!r}: 'freq' is required")
    try:
        freq_full = timespan.parse(freq.get("full"))
        freq_incr = timespan.parse(freq.get("incr"))
        keep = timespan.parse(praw.get("keep"))
    except ValueError as exc:
        raise ConfigError(f"{path}: profile {name!r}: {exc}")

    if timespan.is_never(keep):
        raise ConfigError(f"{path}: profile {name!r}: 'keep' cannot be -1")

    remotes_raw = praw.get("remotes") or []
    if not isinstance(remotes_raw, list):
        raise ConfigError(f"{path}: profile {name!r}: 'remotes' must be a list")

    remotes = []
    seen = set()
    for entry in remotes_raw:
        spec = _parse_remote(entry, auth, path, name)
        if spec.id in seen:
            raise ConfigError(f"{path}: profile {name!r}: duplicate remote id {spec.id!r}")
        seen.add(spec.id)
        remotes.append(spec)

    return Profile(
        name=name,
        freq_full=freq_full,
        freq_incr=freq_incr,
        keep=keep,
        remotes=remotes,
    )


def _parse_remote(entry, auth, path, profile) -> RemoteSpec:
    if not isinstance(entry, dict):
        raise ConfigError(f"{path}: profile {profile!r}: remote must be a mapping")

    resolved = dict(entry)
    rtype = resolved.get("type")
    if not rtype:
        raise ConfigError(f"{path}: profile {profile!r}: remote requires 'type'")

    if "auth" in resolved:
        key = resolved["auth"]
        if key not in auth:
            raise ConfigError(
                f"{path}: profile {profile!r}: auth key {key!r} not found in auth.yaml"
            )
        resolved["auth"] = auth[key]

    name = resolved.get("name")
    if name is None:
        canonical = {k: v for k, v in resolved.items() if k not in ("auth", "name")}
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
        rid = f"hash:{digest}"
    else:
        rid = str(name)

    return RemoteSpec(id=rid, type=str(rtype), settings=resolved)


def remote_identity(spec) -> str:
    """Return a stable identity for a remote endpoint.

    Two remotes collapse to the same identity only when their non-secret
    resolved settings match. ``auth`` is excluded (it is credential material
    and is not part of endpoint identity), while ``name`` is intentionally
    included so two otherwise-identical endpoints with different stable names
    never alias each other.
    """
    settings = {k: v for k, v in spec.settings.items() if k != "auth"}
    return json.dumps(settings, sort_keys=True, default=str)


def _normalize_compression(compression, path) -> dict | None:
    if compression is None:
        return None
    if not isinstance(compression, dict):
        raise ConfigError(f"{path}: 'compression' must be a mapping")
    algo = compression.get("algorithm", "xz")
    if algo != "xz":
        raise ConfigError(f"{path}: unsupported compression algorithm {algo!r} (v1: xz)")
    level = compression.get("level", 6)
    if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 9:
        raise ConfigError(f"{path}: compression level must be an integer 0-9")
    return {"algorithm": "xz", "level": level}


def _normalize_encryption(encryption, path) -> dict | None:
    if encryption is None:
        return None
    if not isinstance(encryption, dict):
        raise ConfigError(f"{path}: 'encryption' must be a mapping")
    algo = encryption.get("algorithm", "age")
    if algo != "age":
        raise ConfigError(f"{path}: unsupported encryption algorithm {algo!r} (v1: age)")
    recipients = encryption.get("recipients")
    if not isinstance(recipients, list) or not recipients:
        raise ConfigError(f"{path}: encryption requires a non-empty 'recipients' list")
    identity = encryption.get("identity")
    if identity:
        _require_absolute(identity, "encryption.identity", path)
    for recipient in recipients:
        _require_absolute_recipient(recipient, path)
    return {
        "algorithm": "age",
        "recipients": [str(r) for r in recipients],
        "identity": str(identity) if identity else None,
    }


def _require_absolute_recipient(recipient, path) -> None:
    """Reject a relative age recipients-file path.

    A recipient is either an inline ``age1...`` key or the path to a recipients
    file. ``send`` resolves a file recipient with ``os.path.exists`` and then
    passes it straight to ``age -R``, so a relative one would silently resolve
    against the invoking process's working directory -- the same class of bug
    as a relative ``src``/``dest``/``tmpdir``, which are rejected outright.
    """
    text = str(recipient)
    if text.startswith("age1"):
        return
    if not Path(text).expanduser().is_absolute():
        raise ConfigError(
            f"{path}: age recipient {text!r} is neither an inline 'age1...' key "
            "nor an absolute path to a recipients file"
        )


# --- validation ------------------------------------------------------------


def _config_uses_auth(config: Config) -> bool:
    """Return True when any configured remote references an ``auth`` key."""
    return any(
        "auth" in remote.settings
        for profile in config.profiles.values()
        for remote in profile.remotes
    )


def validate(config: Config, check_remotes=True, check_nesting=True):
    """Return ``(errors, warnings)`` for a config.

    ``check_nesting`` is disabled by ``run``, which reports the nesting
    condition itself so that a run prints it exactly once (§13).
    """
    errors: list[str] = []
    warnings: list[str] = []

    if _same_path(config.dest, config.src):
        errors.append(f"dest must not be the same path as src: {config.dest}")

    btrfs_available = which("btrfs")
    if not btrfs_available:
        errors.append("the 'btrfs' binary was not found (install btrfs-progs)")

    if not config.src.exists():
        errors.append(f"src does not exist: {config.src}")
    elif btrfs_available and not is_subvolume(config.src):
        errors.append(f"src is not a btrfs subvolume: {config.src}")
    elif btrfs_available:
        if config.dest.exists():
            if not same_device(config.src, config.dest):
                errors.append(
                    f"dest must be on the same btrfs filesystem as src: {config.dest}"
                )
        else:
            parent = _nearest_existing(config.dest)
            if parent is None:
                errors.append(f"dest parent cannot be resolved: {config.dest}")
            elif not same_device(config.src, parent):
                errors.append(
                    f"dest must be on the same btrfs filesystem as src: {config.dest}"
                )
        if check_nesting and is_nested(config.dest, config.src):
            warnings.append(
                f"dest is nested inside src ({config.dest}); snapshots may be picked up as nested subvolumes"
            )

    _check_creatable(config.tmpdir, "tmpdir", errors)
    _check_creatable(config.dest, "dest", errors)
    if _config_uses_auth(config):
        _check_permissions(AUTH_PATH, "auth.yaml", warnings)

    for profile in config.profiles.values():
        if not timespan.is_never(profile.freq_incr) and profile.keep < profile.freq_incr:
            warnings.append(
                f"profile {profile.name}: keep ({profile.keep}s) is shorter than "
                f"freq.incr ({profile.freq_incr}s); backups may be pruned immediately"
            )
        for remote in profile.remotes:
            try:
                instance = create_remote(remote)
                if check_remotes:
                    instance.validate()
            except Exception as exc:  # noqa: BLE001 - surface any remote error
                errors.append(f"profile {profile.name}: remote {remote.id}: {exc}")
            if remote.type == "gdrive" and isinstance(remote.settings.get("auth"), str):
                _check_permissions(
                    remote.settings["auth"], "gdrive credentials file", warnings
                )

    if config.encryption and config.encryption["algorithm"] == "age":
        age_available = which("age")
        if not age_available:
            errors.append("encryption is enabled but the 'age' binary was not found")
        if config.encryption["identity"]:
            _check_permissions(
                config.encryption["identity"], "age identity file", warnings
            )
        else:
            # Sending needs only recipients; restoring needs the private
            # identity, so a profile without one backs up cleanly and can
            # never be restored anywhere. Worth saying out loud now rather
            # than discovering it during a recovery.
            warnings.append(
                "encryption is enabled but 'encryption.identity' is not set; "
                "backups will be written but can never be restored"
            )
        for recipient in config.encryption["recipients"]:
            problem = age_recipient_error(recipient)
            # A missing `age` binary has already been reported once above;
            # don't echo the same fact once per recipient.
            if problem is not None and not (
                not age_available and problem == AGE_MISSING_ERROR
            ):
                errors.append(f"age recipient is invalid: {recipient!r}: {problem}")

    return errors, warnings


def validate_remote_config(config: Config, profile_name: str | None = None) -> list[str]:
    """Return structural remote-configuration errors for *config*.

    This is intentionally lighter than :func:`validate`: it does not require
    ``src``/``dest``/``tmpdir`` to exist or be on the same filesystem, so it is
    safe for ``verify`` and disaster-recovery ``restore`` runs. It only checks
    that every remote spec can be instantiated (registered type, required
    settings present).
    """
    errors: list[str] = []
    profiles = config.profiles
    if profile_name is not None:
        if profile_name not in profiles:
            return [f"unknown profile: {profile_name!r}"]
        profiles = {profile_name: profiles[profile_name]}
    for profile in profiles.values():
        for remote in profile.remotes:
            try:
                create_remote(remote)
            except Exception as exc:  # noqa: BLE001 - surface any remote error
                errors.append(f"profile {profile.name}: remote {remote.id}: {exc}")
    return errors


def _nearest_existing(path: Path) -> Path | None:
    path = Path(path)
    while not path.exists():
        if path.parent == path:
            return None
        path = path.parent
    return path


def _same_path(a, b) -> bool:
    """Return True when two paths resolve to the same location."""
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return Path(a) == Path(b)


def _check_creatable(path: Path, label: str, errors: list[str]) -> None:
    if path.exists():
        if not path.is_dir():
            errors.append(f"{label} is not a directory: {path}")
        elif not os.access(path, os.W_OK):
            errors.append(f"{label} is not writable: {path}")
    else:
        parent = _nearest_existing(path)
        if parent is None or not os.access(parent, os.W_OK):
            errors.append(f"{label} is not creatable: {path}")


def _check_permissions(path, label: str, warnings: list[str]) -> None:
    """Warn when a sensitive file is not ``0600``."""
    path = Path(path)
    if not path.exists():
        return
    mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        warnings.append(f"{label} should be 0600 but is {oct(mode)[2:]}: {path}")
