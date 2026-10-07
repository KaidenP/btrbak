"""A remote backed by Google Drive.

Google Drive is an object store, not a filesystem: files are addressed by ID
and names are not unique within a folder. This backend maps btrbak's logical
paths (``meta.yaml``, ``<profile>/<id>.send``) onto a folder tree rooted at a
single Drive folder, creating intermediate folders on demand.

The Google API client is imported lazily so that a btrbak installation that
only uses the ``dir`` backend does not need it installed.
"""

import json
import os
import tempfile
import time
from pathlib import Path

from .base import Remote, RemoteError, RemoteNotFoundError, split_safe_path

FOLDER_MIME = "application/vnd.google-apps.folder"
_SCOPES = ["https://www.googleapis.com/auth/drive"]
_CHUNK = 8 * 1024 * 1024  # 8 MiB
_ID_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
)
_RETRIABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_MAX_RETRIES = 5
_RETRY_BASE_DELAY = 1.0  # seconds
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
        # OAuth refresh tokens are secrets: refuse anything not readable /
        # writable only by its owner (0600).
        try:
            mode = os.stat(self.credentials_path).st_mode
        except OSError as exc:
            raise RemoteError(
                f"cannot stat gdrive credentials file: {self.credentials_path}"
            ) from exc
        if mode & 0o777 != 0o600:
            raise RemoteError(
                f"gdrive credentials file must be mode 0600: "
                f"{self.credentials_path}"
            )

        # ``service`` is only supplied by tests; production builds it lazily so
        # constructing a remote never touches the network (validate() does).
        self._service_obj = service
        self._root_id = None

    def _service(self):
        if self._service_obj is None:
            self._service_obj = _build_service(self.credentials_path)
        return self._service_obj

    def validate(self) -> None:
        # Resolves the root folder against the live API, so an unreachable or
        # misconfigured remote is reported here (as the interface requires),
        # then proves write access with a create-and-delete probe.
        root_id = self._root_folder_id()
        self._write_probe(self._service(), root_id)

    def _write_probe(self, svc, folder_id) -> None:
        """Verify write access to *folder_id* by creating and deleting a file."""
        name = ".btrbak-probe-" + os.urandom(4).hex()
        try:
            created = _run(
                svc.files().create(
                    body={
                        "name": name,
                        "mimeType": "application/octet-stream",
                        "parents": [folder_id],
                    },
                    fields="id",
                )
            )
        except RemoteError as exc:
            raise RemoteError(f"gdrive folder is not writable: {exc}") from exc
        file_id = created.get("id") if isinstance(created, dict) else None
        if file_id is not None:
            try:
                _run(svc.files().delete(fileId=file_id))
            except RemoteError as exc:
                raise RemoteError(f"gdrive folder is not writable: {exc}") from exc

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
        return self._resolve_folder_by_name(svc)

    def _resolve_folder_by_name(self, svc) -> str:
        # ``folder`` may be a path such as "btrbak/test": resolve or create
        # each level in turn, starting from the top level of "My Drive".
        parts = self.folder.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise RemoteError(f"invalid gdrive folder path: {self.folder!r}")
        parent_id = self._ensure_folder(svc, None, parts[0])
        for name in parts[1:]:
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
            # A value that is not *definitely* an ID (a long folder name made
            # of ID-like characters) should fall back to name-based
            # resolution; a definite ID that 404s is a typo or a deleted
            # folder and must not be recreated as a name.
            if not _is_definitely_id(self.folder):
                return self._resolve_folder_by_name(svc)
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
        if parent_id is None:
            # Top level of "My Drive": no ``parents`` constraint. Restrict to
            # folders the user owns (not merely shared with them).
            q = (
                f"name = '{_quote(name)}' and "
                f"mimeType = '{FOLDER_MIME}' and trashed = false and "
                f"'me' in owners"
            )
        else:
            q = (
                f"name = '{_quote(name)}' and '{parent_id}' in parents and "
                f"mimeType = '{FOLDER_MIME}' and trashed = false and "
                f"'me' in owners"
            )
        matches = _list(svc, q, corpora="user")
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
        return matches[0]["id"]

    def _ensure_folder(self, svc, parent_id, name) -> str:
        existing = self._child_folder_id(svc, parent_id, name)
        if existing is not None:
            return existing
        body = {"name": name, "mimeType": FOLDER_MIME}
        if parent_id is not None:
            body["parents"] = [parent_id]
        created = _run(svc.files().create(body=body, fields="id"))
        return created["id"]

    def _file_id(self, svc, parent_id: str, name: str):
        q = (
            f"name = '{_quote(name)}' and '{parent_id}' in parents and "
            f"mimeType = 'application/octet-stream' and trashed = false"
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
        try:
            _run(svc.files().delete(fileId=file_id))
        except RemoteNotFoundError:
            return  # a concurrent delete already removed it

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
                _run(request)
            else:
                body = {"name": name, "parents": [parent_id]}
                request = svc.files().create(
                    body=body, media_body=media, fields="id"
                )
                try:
                    _run(request)
                except Exception:
                    # Drive creates the (empty) file entry immediately on the
                    # create metadata POST, before any content chunk; a failed
                    # upload would otherwise leave a phantom zero-byte file.
                    self._cleanup_phantom_file(svc, parent_id, name)
                    raise

    def _cleanup_phantom_file(self, svc, parent_id: str, name: str) -> None:
        """Best-effort removal of a zero-byte file left by a failed create."""
        try:
            file_id = self._file_id(svc, parent_id, name)
        except RemoteError:
            return
        if file_id is None:
            return
        try:
            svc.files().delete(fileId=file_id).execute()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass

    def _download(self, svc, file_id: str, local_dest: Path) -> None:
        try:
            from googleapiclient.http import MediaIoBaseDownload
        except ImportError as exc:
            raise RemoteError(_DEP_MESSAGE) from exc
        try:
            from googleapiclient.errors import HttpError
            from httplib2 import HttpLib2Error
            _exc_types = (HttpError, HttpLib2Error, OSError, ConnectionError, TimeoutError)
        except ImportError:
            _exc_types = (OSError, ConnectionError, TimeoutError)

        local_dest = Path(local_dest)
        local_dest.parent.mkdir(parents=True, exist_ok=True)
        # Download to a temp file in the destination directory and atomically
        # replace, so a failed download never leaves a partial file. mkstemp
        # creates the temp file (and hence the final file) as 0600: downloads
        # are raw backup data and must never land world-readable.
        fd, tmp_name = tempfile.mkstemp(
            dir=str(local_dest.parent),
            prefix=local_dest.name + ".",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                request = svc.files().get_media(fileId=file_id)
                downloader = MediaIoBaseDownload(
                    handle, request, chunksize=_CHUNK
                )
                done = False
                attempt = 0
                while not done:
                    try:
                        _status, done = downloader.next_chunk()
                        attempt = 0
                    except _exc_types as exc:
                        if _is_retriable(exc) and attempt < _MAX_RETRIES:
                            time.sleep(_retry_delay(exc, attempt))
                            attempt += 1
                            continue
                        if _is_not_found(exc):
                            raise RemoteNotFoundError(str(exc)) from exc
                        raise RemoteError(f"gdrive: {exc}") from exc
            os.replace(tmp_name, local_dest)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


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

    Note: ``follow_redirects = False`` applies to *all* requests through this
    transport, not just resumable uploads. That is fine because no Drive
    endpoint used by this backend requires redirect-following, but it is
    currently required for resumable uploads (see above). The underlying
    transport is built with a bounded 60s timeout so a hung connection cannot
    block a request indefinitely.
    """
    try:
        import httplib2
        from google_auth_httplib2 import AuthorizedHttp
    except ImportError as exc:
        raise RemoteError(_DEP_MESSAGE) from exc

    authorized = AuthorizedHttp(credentials, http=httplib2.Http(timeout=60))
    authorized.follow_redirects = False
    return authorized


def _split_path(remote_path: str) -> list[str]:
    return split_safe_path(remote_path)


def _quote(value: str) -> str:
    """Escape a value for a Drive ``q`` single-quoted string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _looks_like_id(value: str) -> bool:
    # Real Drive IDs are ~28+ chars; short all-ID-char values are far more
    # likely to be folder names.
    return len(value) >= 28 and all(ch in _ID_CHARS for ch in value)


def _is_definitely_id(value: str) -> bool:
    # ~33 chars matches a real Drive ID; a shorter all-ID-char value is
    # ambiguous and should fall back to name resolution on a 404.
    return len(value) >= 33 and all(ch in _ID_CHARS for ch in value)


def _list(svc, q: str, corpora: str | None = None) -> list[dict]:
    items: list[dict] = []
    page_token = None
    while True:
        kwargs: dict = {
            "q": q,
            "fields": "files(id, name), nextPageToken",
            "pageSize": 1000,
        }
        if corpora is not None:
            kwargs["corpora"] = corpora
        if page_token is not None:
            kwargs["pageToken"] = page_token
        resp = _run(svc.files().list(**kwargs))
        items.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return items


def _run(request):
    try:
        from googleapiclient.errors import HttpError
        from httplib2 import HttpLib2Error
        _exc_types = (HttpError, HttpLib2Error, OSError, ConnectionError, TimeoutError)
    except ImportError:
        _exc_types = (OSError, ConnectionError, TimeoutError)

    attempt = 0
    while True:
        try:
            return request.execute()
        except RemoteError:
            raise
        except _exc_types as exc:
            if _is_retriable(exc) and attempt < _MAX_RETRIES:
                time.sleep(_retry_delay(exc, attempt))
                attempt += 1
                continue
            if _is_not_found(exc):
                raise RemoteNotFoundError(str(exc)) from exc
            raise RemoteError(f"gdrive: {exc}") from exc


def _is_retriable(exc) -> bool:
    """True for transient errors worth retrying (rate limit / 5xx / reset)."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status == 403:
        return _is_rate_limited(exc)
    if status in _RETRIABLE_STATUS:
        return True
    return isinstance(exc, (ConnectionError, TimeoutError))


def _is_rate_limited(exc) -> bool:
    """True when a 403 body reports a rate limit (vs. a permanent 403)."""
    content = getattr(exc, "content", None)
    if not content:
        return False
    try:
        data = json.loads(content)
    except (TypeError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    error = data.get("error")
    if not isinstance(error, dict):
        return False
    reason = error.get("reason")
    if isinstance(reason, str) and reason.lower() in (
        "ratelimitexceeded",
        "userratelimitexceeded",
    ):
        return True
    errors = error.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict):
                item_reason = item.get("reason")
                if isinstance(item_reason, str) and item_reason.lower() in (
                    "ratelimitexceeded",
                    "userratelimitexceeded",
                ):
                    return True
    return False


def _retry_after_seconds(exc) -> float | None:
    """Best-effort ``Retry-After`` (seconds) from an HTTP error."""
    resp = getattr(exc, "resp", None)
    headers = getattr(resp, "headers", None)
    value = None
    if isinstance(headers, dict):
        value = headers.get("Retry-After", headers.get("retry-after"))
    if value is None:
        get = getattr(resp, "get", None)
        if callable(get):
            value = get("retry-after")
    if value is None or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _retry_delay(exc, attempt: int) -> float:
    delay = _retry_after_seconds(exc)
    if delay is not None:
        return min(delay, 60.0)
    return min(_RETRY_BASE_DELAY * (2 ** attempt), 60.0)


def _is_not_found(exc) -> bool:
    resp = getattr(exc, "resp", None)
    return getattr(resp, "status", None) == 404
