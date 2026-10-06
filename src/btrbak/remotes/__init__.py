"""Remote registry: maps a ``type`` string to a Remote class."""

from .base import Remote, RemoteError
from .dir import DirRemote
from .gdrive import GdriveRemote

__all__ = ["REGISTRY", "Remote", "create_remote"]

REGISTRY = {
    "dir": DirRemote,
    "gdrive": GdriveRemote,
}


def create_remote(spec) -> Remote:
    """Instantiate a remote from a resolved ``RemoteSpec``."""
    cls = REGISTRY.get(spec.type)
    if cls is None:
        raise RemoteError(f"unknown remote type: {spec.type!r}")
    return cls(spec.settings)
