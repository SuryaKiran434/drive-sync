"""A mock Google Drive `service` object. No network, ever.

Mimics the slice of the Drive v3 API that drive_sync uses:
  service.files().list(q=..., pageSize=..., pageToken=..., fields=...).execute()
  service.files().create(body=..., fields=...).execute()
  service.files().update(fileId=..., body=...).execute()

It honours `pageSize` (defaulting to the API's real default of 100, so tests can
prove the tool asks for 1000), understands `'<id>' in parents` clauses joined by
`or`, plus the `name=` / `mimeType=` / `trashed=` predicates, and records every
call so tests can count round trips.
"""
import re
import threading

FOLDER_MIME = "application/vnd.google-apps.folder"
_PARENT_RE = re.compile(r"'([^']+)' in parents")
_NAME_RE = re.compile(r"name\s*=\s*'([^']*)'")
_MIME_RE = re.compile(r"mimeType\s*=\s*'([^']*)'")


def folder(fid, name, parents):
    return {"id": fid, "name": name, "mimeType": FOLDER_MIME, "parents": list(parents)}


def blob(fid, name, parents, size=10, md5=None, modified="2026-01-01T00:00:00.000Z",
         mime="application/octet-stream"):
    item = {"id": fid, "name": name, "mimeType": mime, "parents": list(parents),
            "size": str(size), "modifiedTime": modified}
    if md5 is not None:
        item["md5Checksum"] = md5
    return item


def gdoc(fid, name, parents, modified="2026-01-01T00:00:00.000Z"):
    """A Google Docs-native file: no md5Checksum, no byte size."""
    return {"id": fid, "name": name, "parents": list(parents),
            "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": modified}


class _Request:
    def __init__(self, fn):
        self._fn = fn

    def execute(self, http=None):
        return self._fn()


class FakeDrive:
    def __init__(self, items, page_size_cap=None):
        self.items = {it["id"]: dict(it) for it in items}
        self.list_calls = []       # every kwargs dict passed to files().list()
        self.created = []
        self.updated = []
        self.page_size_cap = page_size_cap   # force smaller pages, to test paging
        self._lock = threading.Lock()

    # -- api surface -----------------------------------------------------
    def files(self):
        return self

    def list(self, **kwargs):
        with self._lock:
            self.list_calls.append(dict(kwargs))
        return _Request(lambda: self._do_list(kwargs))

    def create(self, body=None, fields=None, media_body=None):
        def run():
            fid = f"new-{len(self.created) + 1}"
            item = dict(body or {})
            item["id"] = fid
            self.items[fid] = item
            self.created.append(item)
            return {"id": fid}
        return _Request(run)

    def update(self, fileId=None, body=None, media_body=None):
        def run():
            self.updated.append((fileId, body))
            if body:
                self.items.get(fileId, {}).update(body)
            return {"id": fileId}
        return _Request(run)

    # -- query engine ----------------------------------------------------
    def _matches(self, item, q):
        if item.get("trashed"):
            return False
        parents = _PARENT_RE.findall(q)
        if parents and not (set(item.get("parents") or []) & set(parents)):
            return False
        name = _NAME_RE.search(q)
        if name and item.get("name") != name.group(1):
            return False
        mime = _MIME_RE.search(q)
        if mime and item.get("mimeType") != mime.group(1):
            return False
        return True

    def _do_list(self, kwargs):
        q = kwargs.get("q", "")
        hits = [it for it in self.items.values() if self._matches(it, q)]
        hits.sort(key=lambda it: it["id"])

        # The real API defaults to 100 when pageSize is omitted.
        size = kwargs.get("pageSize") or 100
        if self.page_size_cap:
            size = min(size, self.page_size_cap)

        start = int(kwargs.get("pageToken") or 0)
        page = hits[start:start + size]
        resp = {"files": [dict(it) for it in page]}
        if start + size < len(hits):
            resp["nextPageToken"] = str(start + size)
        return resp


def legacy_list_drive_files(service, folder_id, prefix=""):
    """The ORIGINAL recursive implementation, kept verbatim as an oracle.

    New flat/BFS listing must agree with it on {relative_path: file_id}.
    """
    files, page_token = {}, None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            spaces="drive",
            fields="nextPageToken, files(id, name, mimeType)",
            pageToken=page_token,
        ).execute()
        for item in resp.get("files", []):
            rel = f"{prefix}{item['name']}" if prefix else item["name"]
            if item["mimeType"] == FOLDER_MIME:
                files.update(legacy_list_drive_files(service, item["id"], rel + "/"))
            else:
                files[rel] = item["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files
