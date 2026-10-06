"""Tests for the Google Drive remote backend.

These tests never talk to Google. They exercise the path-to-ID mapping, folder
creation, query escaping and error semantics against an in-memory fake of the
tiny slice of the Drive API the backend uses. Upload/download are stubbed
because they wrap the real ``googleapiclient`` media objects.
"""

from pathlib import Path

import pytest

from btrbak.remotes import REGISTRY, create_remote
from btrbak.remotes.base import RemoteError, RemoteNotFoundError
from btrbak.remotes.gdrive import (
    FOLDER_MIME,
    GdriveRemote,
    _authorized_http,
    _list,
)

ROOT_ID = "0123456789abcdefghijklmnopqrstuv"  # 32 chars, ID-shaped


# --- fake Drive API --------------------------------------------------------


class _Request:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _NotFound(Exception):
    def __init__(self, file_id):
        super().__init__(f"404: {file_id}")
        self.resp = _Resp(404)


class _Resp:
    def __init__(self, status):
        self.status = status


def _item(fid, name, mime, parent, content):
    return {
        "id": fid,
        "name": name,
        "mimeType": mime,
        "parents": [] if parent is None else [parent],
        "trashed": False,
        "content": content,
    }


def _q_field(q, key):
    idx = q.find(key)
    if idx < 0:
        return None
    rest = q[idx + len(key):]
    assert rest.startswith("'")
    out = []
    i = 1
    while i < len(rest):
        ch = rest[i]
        if ch == "\\":
            out.append(rest[i + 1])
            i += 2
        elif ch == "'":
            break
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _q_parent(q):
    idx = q.find("' in parents")
    if idx < 0:
        return None
    start = q.rfind("'", 0, idx)
    return q[start + 1:idx]


def _match(item, q):
    name = _q_field(q, "name = ")
    if name is not None and item["name"] != name:
        return False
    parent = _q_parent(q)
    if parent is not None and parent not in item["parents"]:
        return False
    mime_eq = _q_field(q, "mimeType = ")
    if mime_eq is not None and item["mimeType"] != mime_eq:
        return False
    mime_ne = _q_field(q, "mimeType != ")
    if mime_ne is not None and item["mimeType"] == mime_ne:
        return False
    if "trashed = false" in q and item["trashed"]:
        return False
    return True


class _Files:
    def __init__(self, drive):
        self.drive = drive

    def list(self, q, fields=None, pageSize=None, corpora=None, pageToken=None):
        def run():
            matching = [
                {"id": i["id"], "name": i["name"], "mimeType": i["mimeType"]}
                for i in self.drive.items.values()
                if _match(i, q)
            ]
            size = pageSize if pageSize is not None else len(matching)
            start = int(pageToken) if pageToken is not None else 0
            page = matching[start:start + size]
            result: dict = {"files": page}
            if start + size < len(matching):
                result["nextPageToken"] = str(start + size)
            return result

        return _Request(run)

    def get(self, fileId, fields=None):
        def run():
            item = self.drive.items.get(fileId)
            if item is None:
                raise _NotFound(fileId)
            return {
                "id": item["id"],
                "name": item["name"],
                "mimeType": item["mimeType"],
                "trashed": item["trashed"],
            }

        return _Request(run)

    def create(self, body, fields=None, media_body=None):
        def run():
            assert media_body is None, "unexpected media upload in fake create"
            parent = body.get("parents", [None])[0]
            fid = self.drive.add(parent, body["name"], body["mimeType"])
            return {"id": fid}

        return _Request(run)

    def delete(self, fileId):
        def run():
            self.drive.items.pop(fileId, None)
            return None

        return _Request(run)


class FakeDrive:
    def __init__(self):
        self.items = {}
        self._next = 0

    def add_root(self, folder_id):
        self.items[folder_id] = _item(folder_id, "Root", FOLDER_MIME, None, None)
        return folder_id

    def add(self, parent, name, mime, content=None):
        self._next += 1
        fid = f"id-{self._next}"
        self.items[fid] = _item(fid, name, mime, parent, content)
        return fid

    def files(self):
        return _Files(self)


# --- fixtures ---------------------------------------------------------------


@pytest.fixture
def drive():
    fake = FakeDrive()
    fake.add_root(ROOT_ID)
    return fake


@pytest.fixture
def make_remote(tmp_path):
    def _make(fake, folder=ROOT_ID):
        creds = tmp_path / "service-account.json"
        creds.write_text("{}")
        creds.chmod(0o600)
        return GdriveRemote(
            {"type": "gdrive", "folder": folder, "auth": str(creds)},
            service=fake,
        )

    return _make


@pytest.fixture
def stub_media(monkeypatch):
    def _stub(fake):
        def upload(self, svc, parent, name, local_src):
            content = Path(local_src).read_bytes()
            existing = self._file_id(svc, parent, name)
            if existing is None:
                fake.add(parent, name, "application/octet-stream", content)
            else:
                fake.items[existing]["content"] = content

        def download(self, svc, file_id, local_dest):
            content = fake.items[file_id]["content"]
            Path(local_dest).parent.mkdir(parents=True, exist_ok=True)
            Path(local_dest).write_bytes(content)

        monkeypatch.setattr(GdriveRemote, "_upload", upload)
        monkeypatch.setattr(GdriveRemote, "_download", download)

    return _stub


# --- tests ------------------------------------------------------------------


def test_registry_maps_gdrive():
    assert REGISTRY["gdrive"] is GdriveRemote


def test_create_remote_constructs_gdrive(tmp_path):
    creds = tmp_path / "sa.json"
    creds.write_text("{}")
    creds.chmod(0o600)
    spec = type(
        "Spec",
        (),
        {"type": "gdrive", "settings": {"folder": ROOT_ID, "auth": str(creds)}},
    )()
    assert isinstance(create_remote(spec), GdriveRemote)


def test_write_read_roundtrip(drive, make_remote, stub_media, tmp_path):
    stub_media(drive)
    remote = make_remote(drive)

    src = tmp_path / "in.bin"
    src.write_bytes(b"hello btrbak")
    remote.write(src, "root/daily/20251004T000000Z.send")

    dst = tmp_path / "out.bin"
    remote.read("root/daily/20251004T000000Z.send", dst)
    assert dst.read_bytes() == b"hello btrbak"


def test_write_overwrites_existing_file(drive, make_remote, stub_media, tmp_path):
    stub_media(drive)
    remote = make_remote(drive)

    first = tmp_path / "first.bin"
    first.write_bytes(b"old")
    second = tmp_path / "second.bin"
    second.write_bytes(b"new")
    remote.write(first, "meta.yaml")
    remote.write(second, "meta.yaml")

    dst = tmp_path / "out.bin"
    remote.read("meta.yaml", dst)
    assert dst.read_bytes() == b"new"


def test_delete_is_idempotent(drive, make_remote, stub_media, tmp_path):
    stub_media(drive)
    remote = make_remote(drive)

    src = tmp_path / "in.bin"
    src.write_bytes(b"x")
    remote.write(src, "a/b.send")

    remote.delete("a/b.send")
    remote.delete("a/b.send")  # missing is not an error

    with pytest.raises(RemoteNotFoundError):
        remote.read("a/b.send", tmp_path / "out.bin")


def test_read_missing_file_raises(drive, make_remote, stub_media):
    stub_media(drive)
    remote = make_remote(drive)
    with pytest.raises(RemoteNotFoundError):
        remote.read("meta.yaml", Path("/tmp/never-written"))


def test_read_missing_parent_folder_raises(drive, make_remote, stub_media):
    stub_media(drive)
    remote = make_remote(drive)
    with pytest.raises(RemoteNotFoundError):
        remote.read("no/such/folder/file.send", Path("/tmp/never-written"))


def test_path_traversal_rejected(drive, make_remote, stub_media, tmp_path):
    stub_media(drive)
    remote = make_remote(drive)
    with pytest.raises(RemoteError):
        remote.write(tmp_path / "in.bin", "../escape")


def test_absolute_path_rejected(drive, make_remote, stub_media, tmp_path):
    stub_media(drive)
    remote = make_remote(drive)
    with pytest.raises(RemoteError):
        remote.write(tmp_path / "in.bin", "/meta.yaml")


def test_missing_folder_setting():
    with pytest.raises(RemoteError):
        GdriveRemote({"type": "gdrive", "auth": "/somewhere"})


def test_missing_auth_setting():
    with pytest.raises(RemoteError):
        GdriveRemote({"type": "gdrive", "folder": ROOT_ID})


def test_missing_credentials_file():
    with pytest.raises(RemoteError):
        GdriveRemote({"type": "gdrive", "folder": ROOT_ID, "auth": "/no/such/file.json"})


def test_multiple_files_with_same_name_rejected(drive, make_remote, stub_media, tmp_path):
    stub_media(drive)
    drive.add(ROOT_ID, "dup.send", "application/octet-stream", b"a")
    drive.add(ROOT_ID, "dup.send", "application/octet-stream", b"b")
    remote = make_remote(drive)
    with pytest.raises(RemoteError):
        remote.read("dup.send", tmp_path / "out.bin")


def test_query_escaping(drive, make_remote, stub_media, tmp_path):
    stub_media(drive)
    drive.add(ROOT_ID, "it's.send", "application/octet-stream", b"data")
    remote = make_remote(drive)
    remote.read("it's.send", tmp_path / "out.bin")
    assert (tmp_path / "out.bin").read_bytes() == b"data"


def test_validate_resolves_root_by_id(drive, make_remote):
    remote = make_remote(drive, folder=ROOT_ID)
    assert remote.validate() is None


def test_validate_root_id_not_found_raises(drive, make_remote):
    # 34 chars: definitely ID-shaped, so a 404 must raise (never be
    # recreated as a folder name).
    missing = "zyxwvutsrqponmlkjihgfedcba98765432"
    remote = make_remote(drive, folder=missing)
    with pytest.raises(RemoteError):
        remote.validate()


def test_validate_resolves_root_by_name(drive, make_remote):
    drive.add(ROOT_ID, "my-backups", FOLDER_MIME)
    remote = make_remote(drive, folder="my-backups")
    assert remote.validate() is None


def test_validate_creates_missing_root_by_name(drive, make_remote):
    remote = make_remote(drive, folder="no-such-folder")
    assert remote.validate() is None
    matches = [i for i in drive.items.values() if i["name"] == "no-such-folder"]
    assert len(matches) == 1
    assert matches[0]["mimeType"] == FOLDER_MIME
    assert matches[0]["parents"] == []  # created at the top level of My Drive


def test_validate_ambiguous_root(drive, make_remote):
    drive.add(ROOT_ID, "dup", FOLDER_MIME)
    drive.add(ROOT_ID, "dup", FOLDER_MIME)
    remote = make_remote(drive, folder="dup")
    with pytest.raises(RemoteError):
        remote.validate()


def test_validate_creates_full_folder_tree(drive, make_remote):
    remote = make_remote(drive, folder="btrbak/test")
    assert remote.validate() is None
    top = [i for i in drive.items.values() if i["name"] == "btrbak"]
    assert len(top) == 1
    assert top[0]["parents"] == []  # created at the top level of My Drive
    child = [i for i in drive.items.values() if i["name"] == "test"]
    assert len(child) == 1
    assert child[0]["parents"] == [top[0]["id"]]
    assert remote._root_folder_id() == child[0]["id"]


def test_validate_reuses_existing_folder_tree(drive, make_remote):
    top_id = drive.add(None, "btrbak", FOLDER_MIME)
    child_id = drive.add(top_id, "test", FOLDER_MIME)
    remote = make_remote(drive, folder="btrbak/test")
    assert remote.validate() is None
    assert len([i for i in drive.items.values() if i["name"] == "btrbak"]) == 1
    assert len([i for i in drive.items.values() if i["name"] == "test"]) == 1
    assert remote._root_folder_id() == child_id


def test_validate_folder_path_empty_segment_rejected(drive, make_remote):
    remote = make_remote(drive, folder="btrbak//test")
    with pytest.raises(RemoteError):
        remote.validate()


def test_validate_folder_path_dot_segment_rejected(drive, make_remote):
    remote = make_remote(drive, folder="btrbak/../test")
    with pytest.raises(RemoteError):
        remote.validate()


def test_validate_ambiguous_intermediate_folder_rejected(drive, make_remote):
    top_id = drive.add(None, "btrbak", FOLDER_MIME)
    drive.add(top_id, "test", FOLDER_MIME)
    drive.add(top_id, "test", FOLDER_MIME)
    remote = make_remote(drive, folder="btrbak/test")
    with pytest.raises(RemoteError):
        remote.validate()


def test_authorized_http_disables_redirect_following():
    # httplib2 follows 308 redirects by default, which breaks Drive's
    # resumable upload of any multi-chunk file (>8 MiB): the per-chunk
    # "308 Resume Incomplete" has no Location header, so httplib2 raises
    # RedirectMissingLocation. The upload code reads the Range header itself.
    authorized = _authorized_http(object())
    assert authorized.follow_redirects is False


def test_list_paginates_beyond_1000(drive):
    # A broad listing must walk every page (pageSize=1000): without the
    # nextPageToken loop, >1000 matches would silently truncate.
    for _ in range(2500):
        drive.add(ROOT_ID, "file.send", "application/octet-stream", b"x")
    items = _list(drive, "name = 'file.send'", corpora="user")
    assert len(items) == 2500


def test_credentials_file_wrong_mode_rejected(tmp_path):
    creds = tmp_path / "sa.json"
    creds.write_text("{}")
    creds.chmod(0o644)
    with pytest.raises(RemoteError):
        GdriveRemote(
            {"type": "gdrive", "folder": ROOT_ID, "auth": str(creds)}
        )


def test_download_creates_0600(monkeypatch, make_remote, tmp_path):
    import googleapiclient.http

    class _FakeDownloader:
        def __init__(self, fd, request, chunksize=None):
            self._fd = fd

        def next_chunk(self):
            self._fd.write(b"downloaded")
            return None, True  # (status, done)

    class _FakeSvc:
        class _Files:
            def get_media(self, fileId):
                return object()

        def files(self):
            return self._Files()

    monkeypatch.setattr(
        googleapiclient.http, "MediaIoBaseDownload", _FakeDownloader
    )

    fake = FakeDrive()
    fake.add_root(ROOT_ID)
    remote = make_remote(fake)
    dest = tmp_path / "out.bin"
    remote._download(_FakeSvc(), "whatever", dest)

    assert dest.read_bytes() == b"downloaded"
    assert (dest.stat().st_mode & 0o777) == 0o600
