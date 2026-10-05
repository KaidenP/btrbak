"""Load and validate profile + auth configuration.

Profile files live in ``/etc/btrbak/profiles.d/<name>.yaml``; auth credentials
live in ``/etc/btrbak/auth.yaml``.
"""

import hashlib
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

import timespan
from remotes import create_remote
from util import is_nested, is_subvolume, same_device, which

CONFIG_DIR = Path("/etc/btrbak/profiles.d")
AUTH_PATH = Path("/etc/btrbak/auth.yaml")
DEFAULT_TMPDIR = Path("/var/tmp/btrbak")


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


def load_auth(path=AUTH_PATH) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        with open(path) as handle:
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


def load_config(path, auth: dict) -> Config:
    path = Path(path)
    try:
        with open(path) as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}")
    if not isinstance(data, dict):
        raise ConfigError(f"config must be a mapping: {path}")

    src = data.get("src")
    dest = data.get("dest")
    if not src or not dest:
        raise ConfigError(f"{path}: 'src' and 'dest' are required")

    profiles_raw = data.get("profiles")
    if not isinstance(profiles_raw, dict) or not profiles_raw:
        raise ConfigError(f"{path}: 'profiles' must be a non-empty mapping")

    profiles = {}
    for pname, praw in profiles_raw.items():
        if not isinstance(praw, dict):
            raise ConfigError(f"{path}: profile {pname!r} must be a mapping")
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


def discover_configs(subvol=None) -> list[Config]:
    auth = load_auth()
    if subvol:
        return [load_config(config_path_for_subvol(subvol), auth)]

    if not CONFIG_DIR.is_dir():
        raise ConfigError(f"config directory not found: {CONFIG_DIR}")
    paths = sorted(
        list(CONFIG_DIR.glob("*.yaml")) + list(CONFIG_DIR.glob("*.yml"))
    )
    if not paths:
        raise ConfigError(f"no config files found in {CONFIG_DIR}")
    return [load_config(path, auth) for path in paths]


def config_path_for_subvol(subvol) -> Path:
    """Resolve a SUBVOL selector to a config file (``.yaml`` or ``.yml``)."""
    for ext in (".yaml", ".yml"):
        path = CONFIG_DIR / f"{subvol}{ext}"
        if path.exists():
            return path
    raise ConfigError(f"no config file for subvol {subvol!r} in {CONFIG_DIR}")


def filter_profiles(config: Config, name: str | None) -> Config:
    """Return *config* restricted to profile *name* (unchanged when *name* is None)."""
    if not name:
        return config
    if name not in config.profiles:
        raise ConfigError(f"unknown profile: {name!r}")
    return replace(config, profiles={name: config.profiles[name]})


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

    Two remotes collapse to the same identity only when their full resolved
    settings match. The ``name`` (a stable manifest id) is intentionally
    included so distinct remotes never alias each other even when they share a
    name.
    """
    return json.dumps(spec.settings, sort_keys=True, default=str)


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
    return {
        "algorithm": "age",
        "recipients": [str(r) for r in recipients],
        "identity": str(identity) if identity else None,
    }


# --- validation ------------------------------------------------------------


def validate(config: Config, check_remotes=True):
    """Return ``(errors, warnings)`` for a config."""
    errors: list[str] = []
    warnings: list[str] = []

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
        if is_nested(config.dest, config.src):
            warnings.append(
                f"dest is nested inside src ({config.dest}); snapshots may be picked up as nested subvolumes"
            )

    _check_creatable(config.tmpdir, "tmpdir", errors)
    _check_creatable(config.dest, "dest", errors)
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

    if config.encryption and config.encryption["algorithm"] == "age":
        if not which("age"):
            errors.append("encryption is enabled but the 'age' binary was not found")
        if config.encryption["identity"]:
            _check_permissions(
                config.encryption["identity"], "age identity file", warnings
            )

    return errors, warnings


def _nearest_existing(path: Path) -> Path | None:
    path = Path(path)
    while not path.exists():
        if path.parent == path:
            return None
        path = path.parent
    return path


def _check_creatable(path: Path, label: str, errors: list[str]) -> None:
    if path.exists():
        if not os.access(path, os.W_OK):
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
