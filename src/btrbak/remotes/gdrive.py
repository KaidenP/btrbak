"""A remote backed by Google Drive.

Google Drive is an object store, not a filesystem: files are addressed by ID
and names are not unique within a folder. This backend maps btrbak's logical
paths (``meta.yaml``, ``<profile>/<id>.send``) onto a folder tree rooted at a
single Drive folder, creating intermediate folders on demand.

The Google API client is imported lazily so that a btrbak installation that
only uses the ``dir`` backend does not need it installed.
"""

import os
from pathlib import Path

from .base import Remote, RemoteError, RemoteNotFoundError

FOLDER_MIME = "application/vnd.google-apps.folder"
_SCOPES = ["https://www.googleapis.com/auth/drive"]
_CHUNK = 8 * 1024 * 1024  # 8 MiB
_ID_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)
_DEP_MESSAGE = (
    "the 'gdrive' remote requires the Google API client; install "
    "google-api-python-client and google-auth (Debian: python3-googleapi)"
)


class GdriveRemote(Remote):
    """Maps logical paths onto ``<folder>/<logical-path>`` in Google Drive."""

    def __init__(self, settings: dict, service=None):
        super().__init__(settings)
        folder = settings.get("folder")
        if not folder or not isinstance(folder, str) or not folder.strip():
            raise RemoteError("'gdrive' remote requires a 'folder' setting")
        self.folder = folder.strip()

        credentials = settings.get("auth")
        if not credentials or not isinstance(credentials, str):
            raise RemoteError(
                "'gdrive' remote requires an 'auth' key that resolves to the "
                "path of an OAuth credentials file (see `btrbak gdrive "
                "authorize` and btrbak-profiles(5))"
            )
        self.credentials_path = Path(credentials).expanduser()
        if not self.credentials_path.is_file():
            raise RemoteError(
                f"gdrive credentials file not found: {self.credentials_path}"
            )

        # ``service`` is only supplied by tests; production builds it lazily so
        # constructing a remote never touches the network (validate() does).
        self._service_obj = service
        self._root_id = None
        self._folder_ids = {}

    def _service(self):
        if self._service_obj is None:
            self._service_obj = _build_service(self.credentials_path)
        return self._service_obj

    def validate(self) -> None:
        # Resolves the root folder against the live API, so an unreachable or
        # misconfigured remote is reported here (as the interface requires).
        self._root_folder_id()

    # --- path handling -----------------------------------------------------

    def _root_folder_id(self) -> str:
        if self._root_id is None:
            self._root_id = self._resolve_root_folder(self._service())
        return self._root_id

    def _resolve_root_folder(self, svc) -> str:
        # A Drive folder ID looks like a long opaque token; anything else is a
        # name (or a slash-separated path of names) to search for. Trying
        # files().get() on a name would only yield a 400, so only attempt it
        # for ID-shaped values.
        if _looks_like_id(self.folder):
            return self._resolve_folder_by_id(svc)

        # ``folder`` may be a path such as "btrbak/test": resolve or create
        # each level in turn, starting from the top level of "My Drive".
        parts = self.folder.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise RemoteError(f"invalid gdrive folder path: {self.folder!r}")
        parent_id = None
        for name in parts:
            parent_id = self._ensure_folder(svc, parent_id, name)
        return parent_id

    def _resolve_folder_by_id(self, svc) -> str:
        try:
            meta = (
                svc.files()
                .get(fileId=self.folder, fields="id, mimeType, trashed")
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            if not _is_not_found(exc):
                raise RemoteError(f"gdrive: {exc}") from exc
            meta = None
        if meta is None:
            # A folder ID that does not resolve is almost certainly a
            # typo or a deleted folder; never create a folder named with
            # an opaque ID.
            raise RemoteError(f"gdrive folder not found: {self.folder!r}")
        if meta.get("trashed"):
            raise RemoteError(
                f"gdrive folder is in the trash: {self.folder!r}"
            )
        if meta.get("mimeType") != FOLDER_MIME:
            raise RemoteError(
                f"gdrive 'folder' is not a folder: {self.folder!r}"
            )
        return meta["id"]

    def _child_folder_id(self, svc, parent_id, name):
        key = (parent_id, name)
        cached = self._folder_ids.get(key)
        if cached is not None:
            return cached
        if parent_id is None:
            # Top level of "My Drive": no ``parents`` constraint.
            q = (
                f"name = '{_quote(name)}' and "
                f"mimeType = '{FOLDER_MIME}' and trashed = false"
            )
        else:
            q = (
                f"name = '{_quote(name)}' and '{parent_id}' in parents and "
                f"mimeType = '{FOLDER_MIME}' and trashed = false"
            )
        matches = _list(svc, q)
        if not matches:
            return None
        if len(matches) > 1:
            if parent_id is None:
                raise RemoteError(
                    f"multiple gdrive folders named {name!r}; use the folder ID"
                )
            raise RemoteError(
                f"multiple gdrive folders named {name!r} under {parent_id}"
            )
        self._folder_ids[key] = matches[0]["id"]
        return matches[0]["id"]

    def _ensure_folder(self, svc, parent_id, name) -> str:
        existing = self._child_folder_id(svc, parent_id, name)
        if existing is not None:
            return existing
        body = {"name": name, "mimeType": FOLDER_MIME}
        if parent_id is not None:
            body["parents"] = [parent_id]
        created = _run(svc.files().create(body=body, fields="id"))
        self._folder_ids[(parent_id, name)] = created["id"]
        return created["id"]

    def _file_id(self, svc, parent_id: str, name: str):
        q = (
            f"name = '{_quote(name)}' and '{parent_id}' in parents and "
            f"mimeType != '{FOLDER_MIME}' and trashed = false"
        )
        matches = _list(svc, q)
        if not matches:
            return None
        if len(matches) > 1:
            raise RemoteError(
                f"multiple gdrive files named {name!r} under {parent_id}"
            )
        return matches[0]["id"]

    def _parent_for_path(self, svc, remote_path: str) -> str:
        """Return the folder ID that holds the final path component."""
        parts = _split_path(remote_path)
        parent = self._root_folder_id()
        for name in parts[:-1]:
            folder_id = self._child_folder_id(svc, parent, name)
            if folder_id is None:
                raise RemoteNotFoundError(f"remote path not found: {remote_path}")
            parent = folder_id
        return parent

    # --- Remote interface ---------------------------------------------------

    def read(self, remote_path: str, local_dest: Path) -> None:
        parts = _split_path(remote_path)
        svc = self._service()
        parent = self._parent_for_path(svc, remote_path)
        file_id = self._file_id(svc, parent, parts[-1])
        if file_id is None:
            raise RemoteNotFoundError(f"remote file not found: {remote_path}")
        self._download(svc, file_id, Path(local_dest))

    def write(self, local_src: Path, remote_path: str) -> None:
        parts = _split_path(remote_path)
        svc = self._service()
        parent = self._root_folder_id()
        for name in parts[:-1]:
            parent = self._ensure_folder(svc, parent, name)
        self._upload(svc, parent, parts[-1], Path(local_src))

    def delete(self, remote_path: str) -> None:
        parts = _split_path(remote_path)
        svc = self._service()
        parent = self._root_folder_id()
        for name in parts[:-1]:
            folder_id = self._child_folder_id(svc, parent, name)
            if folder_id is None:
                return  # nothing under a missing folder can need deleting
            parent = folder_id
        file_id = self._file_id(svc, parent, parts[-1])
        if file_id is None:
            return
        _run(svc.files().delete(fileId=file_id))

    # --- media (kept separate so tests can stub the HTTP client) -----------

    def _upload(self, svc, parent_id: str, name: str, local_src: Path) -> None:
        try:
            from googleapiclient.http import MediaIoBaseUpload
        except ImportError as exc:
            raise RemoteError(_DEP_MESSAGE) from exc

        existing = self._file_id(svc, parent_id, name)
        with open(local_src, "rb") as handle:
            media = MediaIoBaseUpload(
                handle,
                mimetype="application/octet-stream",
                chunksize=_CHUNK,
                resumable=True,
            )
            if existing is not None:
                # Updating the existing file by ID keeps the upload "atomic"
                # from the API's perspective: the old content is replaced only
                # once the resumable upload completes.
                request = svc.files().update(
                    fileId=existing, media_body=media, fields="id"
                )
            else:
                body = {"name": name, "parents": [parent_id]}
                request = svc.files().create(
                    body=body, media_body=media, fields="id"
                )
            _run(request)

    def _download(self, svc, file_id: str, local_dest: Path) -> None:
        try:
            from googleapiclient.http import MediaIoBaseDownload
        except ImportError as exc:
            raise RemoteError(_DEP_MESSAGE) from exc

        local_dest = Path(local_dest)
        local_dest.parent.mkdir(parents=True, exist_ok=True)
        # Downloads are raw backup data; never let them land world-readable.
        fd = os.open(str(local_dest), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                request = svc.files().get_media(fileId=file_id)
                downloader = MediaIoBaseDownload(
                    handle, request, chunksize=_CHUNK
                )
                done = False
                while not done:
                    _status, done = downloader.next_chunk()
        except Exception as exc:  # noqa: BLE001
            if _is_not_found(exc):
                raise RemoteNotFoundError(str(exc)) from exc
            raise RemoteError(f"gdrive: {exc}") from exc


# --- helpers ---------------------------------------------------------------


def _build_service(credentials_path: Path):
    try:
        from google.oauth2 import credentials as oauth2_credentials
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RemoteError(_DEP_MESSAGE) from exc

    try:
        credentials = oauth2_credentials.Credentials.from_authorized_user_file(
            str(credentials_path), scopes=_SCOPES
        )
    except (OSError, ValueError) as exc:
        raise RemoteError(
            f"invalid gdrive credentials file {credentials_path}: {exc}"
        ) from exc
    return build(
        "drive", "v3", http=_authorized_http(credentials), cache_discovery=False
    )


def _authorized_http(credentials):
    """Build an authorized httplib2 transport with redirect-following off.

    httplib2 follows redirects by default, which breaks Drive's resumable
    uploads: the API answers each 8 MiB chunk with ``308 Resume Incomplete``
    (which carries no ``Location`` header), so httplib2 raises
    ``RedirectMissingLocation`` on the first chunk of any multi-chunk file.
    The upload code consumes the ``Range`` header itself and never needs to
    follow a redirect, so disabling redirects is both necessary and safe.
    """
    try:
        from google_auth_httplib2 import AuthorizedHttp
    except ImportError as exc:
        raise RemoteError(_DEP_MESSAGE) from exc

    authorized = AuthorizedHttp(credentials)
    authorized.follow_redirects = False
    return authorized


def _split_path(remote_path: str) -> list[str]:
    if not isinstance(remote_path, str) or not remote_path:
        raise RemoteError(f"invalid remote path: {remote_path!r}")
    if remote_path.startswith("/") or remote_path != remote_path.strip():
        raise RemoteError(f"invalid remote path: {remote_path!r}")
    parts = remote_path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise RemoteError(f"invalid remote path: {remote_path!r}")
    return parts


def _quote(value: str) -> str:
    """Escape a value for a Drive ``q`` single-quoted string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _looks_like_id(value: str) -> bool:
    return len(value) >= 20 and all(ch in _ID_CHARS for ch in value)


def _list(svc, q: str) -> list[dict]:
    resp = svc.files().list(q=q, fields="files(id, name)", pageSize=1000).execute()
    return resp.get("files", [])


def _run(request):
    try:
        return request.execute()
    except RemoteError:
        raise
    except Exception as exc:  # noqa: BLE001
        if _is_not_found(exc):
            raise RemoteNotFoundError(str(exc)) from exc
        raise RemoteError(f"gdrive: {exc}") from exc


def _is_not_found(exc) -> bool:
    resp = getattr(exc, "resp", None)
    return getattr(resp, "status", None) == 404
