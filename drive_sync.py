#!/usr/bin/env python3
"""
drive_sync.py  —  Compare, sync, and watch a local folder against Google Drive.

Usage:
    python drive_sync.py compare              # Show what's different (added / deleted / MODIFIED)
    python drive_sync.py push                 # LOCAL is source of truth: upload missing, delete Drive extras
    python drive_sync.py pull                 # DRIVE is source of truth: download missing, delete local extras
    python drive_sync.py sync                 # Interactively fix differences (choose per side)
    python drive_sync.py watch                # Auto-upload new/changed files
    python drive_sync.py watch --daemon       # Run watcher in background
    python drive_sync.py watch --stop         # Stop background watcher

Requirements:
    pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client watchdog
"""

import os, sys, io, time, signal, logging, argparse, hashlib, functools, threading
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# ── Load config from .env ────────────────────────────────────────────────────
def _load_env():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        # Already configured through the real environment (CI, tests, shell export)?
        # Then a missing .env is not an error.
        if os.environ.get("LOCAL_FOLDER") and os.environ.get("DRIVE_FOLDER_ID"):
            return
        print(f"ERROR: .env file not found at {env_path}")
        print("Create a .env file with:\n  LOCAL_FOLDER=/path/to/your/folder\n  DRIVE_FOLDER_ID=your_drive_folder_id")
        sys.exit(1)
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

_load_env()

LOCAL_FOLDER    = os.environ.get("LOCAL_FOLDER", "")
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "")

if not LOCAL_FOLDER or not DRIVE_FOLDER_ID:
    print("ERROR: LOCAL_FOLDER and DRIVE_FOLDER_ID must be set in your .env file.")
    sys.exit(1)
# ─────────────────────────────────────────────────────────────────────────────

SCOPES           = ["https://www.googleapis.com/auth/drive"]
CREDENTIALS_FILE = str(Path(__file__).parent / "credentials.json")
TOKEN_FILE       = str(Path(__file__).parent / "token.json")
LOG_FILE         = str(Path(__file__).parent / "watcher.log")
PID_FILE         = str(Path(__file__).parent / "watcher.pid")
IGNORE_NAMES     = {".DS_Store", "Thumbs.db", ".git"}
IGNORE_EXTS      = {".tmp", ".swp", ".part"}

# Drive listing tuning
PAGE_SIZE         = 1000   # files.list maximum; the default of 100 costs 10x the round trips
PARENTS_PER_QUERY = 25     # folder ids OR-ed into a single q, to widen each round trip
LIST_MAX_WORKERS  = 8      # small pool: we are latency-bound, not CPU-bound
DRIVE_FIELDS      = ("nextPageToken, files(id, name, mimeType, parents, "
                     "md5Checksum, size, modifiedTime)")
FOLDER_MIME       = "application/vnd.google-apps.folder"
NATIVE_MIME_PFX   = "application/vnd.google-apps."   # Docs/Sheets/Slides: no md5, no byte size
MTIME_TOLERANCE   = 2.0    # seconds, for the mtime fallback only


# ── Auth ─────────────────────────────────────────────────────────────────────

_CREDS         = None                 # set by get_service(); see _worker_http()
_WORKER_LOCAL  = threading.local()


def get_service():
    global _CREDS
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    _CREDS = creds
    return build("drive", "v3", credentials=creds)


def _worker_http():
    """Per-thread authorized Http for the parallel lister.

    httplib2 (what google-api-python-client rides on) is NOT thread-safe, and a
    `service` object shares one Http instance. So each worker thread gets its
    own, passed explicitly to `execute(http=...)`. Returns None when there are
    no real credentials -- unit tests drive a mock service -- in which case
    execute() is called plainly.
    """
    if _CREDS is None:
        return None
    http = getattr(_WORKER_LOCAL, "http", None)
    if http is None:
        import google_auth_httplib2
        from googleapiclient.http import build_http
        http = google_auth_httplib2.AuthorizedHttp(_CREDS, http=build_http())
        _WORKER_LOCAL.http = http
    return http


# ── Drive helpers ─────────────────────────────────────────────────────────────

def _list_children(service, parent_ids):
    """One paginated files.list covering several parents at once.

    Returns (parent_ids, items). pageSize is pinned to the API maximum, so a
    2000-file folder costs 2 round trips instead of the 20 that the default
    page size of 100 would cost.
    """
    clause = " or ".join(f"'{pid}' in parents" for pid in parent_ids)
    q = f"({clause}) and trashed = false"
    items, page_token = [], None
    while True:
        req = service.files().list(
            q=q,
            spaces="drive",
            fields=DRIVE_FIELDS,
            pageSize=PAGE_SIZE,
            pageToken=page_token,
        )
        http = _worker_http()
        resp = req.execute(http=http) if http is not None else req.execute()
        items.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return parent_ids, items


def list_drive_files_meta(service, folder_id, prefix=""):
    """List the whole subtree under `folder_id`. Returns {relative_path: item}.

    Why this shape rather than one flat query:
    Drive's query language has no "descendant of" operator -- `'X' in parents`
    matches DIRECT children only. A single flat query therefore CANNOT be scoped
    to the configured root; the only flat option is to pull every file the token
    can see (`trashed = false` over the entire drive) and filter client-side,
    which is correct but downloads the user's whole Drive to sync one folder,
    and is unbounded in cost. So we walk level by level instead:

      * every folder on a level is OR-ed into as few queries as possible
        (PARENTS_PER_QUERY per query, pageSize=1000 per page), and
      * those queries run concurrently in a small thread pool.

    Cost drops from one blocking round trip per folder (50 folders ~ 50 x 300ms
    serial) to one round trip per *depth level*, issued in parallel. The
    hierarchy itself is still reconstructed client-side from each item's
    `parents` field -- no extra calls are made to resolve paths.
    """
    out = {}
    frontier = {folder_id: prefix}          # folder id -> its relative path prefix
    seen_folders = {folder_id}              # id-level dedupe: cycle / re-entry guard

    with ThreadPoolExecutor(max_workers=LIST_MAX_WORKERS) as pool:
        while frontier:
            ids = list(frontier)
            chunks = [ids[i:i + PARENTS_PER_QUERY]
                      for i in range(0, len(ids), PARENTS_PER_QUERY)]
            results = list(pool.map(lambda c: _list_children(service, c), chunks))

            next_frontier = {}
            for batch_parents, items in results:
                for item in items:
                    parents = item.get("parents")
                    if not parents and len(batch_parents) == 1:
                        # `parents` elided by the API/mock: with a single-parent
                        # query we still know where it came from.
                        parents = list(batch_parents)
                    for pid in parents or []:
                        base = frontier.get(pid)
                        if base is None:
                            # Parent lives outside the synced root (orphan, or a
                            # second parent elsewhere in the drive) -- skip it,
                            # do not crash and do not leak it into the result.
                            continue
                        rel = f"{base}{item['name']}"
                        if item.get("mimeType") == FOLDER_MIME:
                            if item["id"] in seen_folders:
                                continue
                            seen_folders.add(item["id"])
                            next_frontier[item["id"]] = rel + "/"
                        else:
                            # A multi-parent FILE legitimately appears at every
                            # in-scope path, exactly as the old recursive walk did.
                            out[rel] = item
            frontier = next_frontier
    return out


def list_drive_files(service, folder_id, prefix=""):
    """Recursively list all files. Returns {relative_path: file_id}."""
    return {rel: item["id"]
            for rel, item in list_drive_files_meta(service, folder_id, prefix).items()}


@functools.lru_cache(maxsize=4096)
def get_or_create_folder(service, name, parent_id):
    """Resolve (or create) a Drive folder, memoised.

    Bounded by lru_cache and clearable via `get_or_create_folder.cache_clear()`
    -- unlike the old `cache={}` default argument, which bound once at import,
    lived for the whole process and would happily serve the id of a folder that
    had since been deleted. Every command clears it at start-up.
    """
    q = (f"'{parent_id}' in parents and name='{name}' and "
         f"mimeType='{FOLDER_MIME}' and trashed=false")
    items = service.files().list(q=q, fields="files(id)").execute().get("files", [])
    return items[0]["id"] if items else service.files().create(
        body={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]},
        fields="id"
    ).execute()["id"]


def ensure_drive_path(service, rel_path):
    parts = Path(rel_path).parts[:-1]
    current = DRIVE_FOLDER_ID
    for part in parts:
        current = get_or_create_folder(service, part, current)
    return current


def upload(service, local_abs, rel_path, drive_index=None):
    parent = ensure_drive_path(service, rel_path)
    media  = MediaFileUpload(str(local_abs), resumable=True)
    existing = (drive_index or {}).get(rel_path)
    if existing:
        service.files().update(fileId=existing, media_body=media).execute()
        print(f"  ✅ Updated:   {rel_path}")
    else:
        result = service.files().create(
            body={"name": Path(rel_path).name, "parents": [parent]},
            media_body=media, fields="id"
        ).execute()
        if drive_index is not None:
            drive_index[rel_path] = result["id"]
        print(f"  ✅ Uploaded:  {rel_path}")


def download(service, file_id, rel_path):
    dest = Path(LOCAL_FOLDER) / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    dl  = MediaIoBaseDownload(buf, service.files().get_media(fileId=file_id))
    done = False
    while not done:
        _, done = dl.next_chunk()
    dest.write_bytes(buf.getvalue())
    print(f"  ✅ Downloaded: {rel_path}")


def trash_on_drive(service, rel_path, drive_index):
    fid = drive_index.get(rel_path)
    if fid:
        service.files().update(fileId=fid, body={"trashed": True}).execute()
        del drive_index[rel_path]
        print(f"  🗑  Trashed:   {rel_path}")


# ── Local helpers ─────────────────────────────────────────────────────────────

def list_local_files():
    base = Path(LOCAL_FOLDER)
    return {str(p.relative_to(base)) for p in base.rglob("*") if p.is_file()}


def should_ignore(path):
    p = Path(path)
    return p.name in IGNORE_NAMES or p.suffix.lower() in IGNORE_EXTS


def filter_files(files):
    return {f for f in files if not should_ignore(f)}


def file_md5(path, chunk=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


# ── Change detection ─────────────────────────────────────────────────────────

def is_google_native(item):
    """Docs / Sheets / Slides / Forms: no md5Checksum and no meaningful `size`."""
    return str(item.get("mimeType") or "").startswith(NATIVE_MIME_PFX)


def _rfc3339_to_epoch(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def detect_changes(common, drive_meta, local_root=None):
    """Which files exist on BOTH sides but no longer hold the same bytes?

    A plain set difference over relative paths can only ever see additions and
    deletions -- a path present on both sides falls into neither `only_local`
    nor `only_drive`, so an edit on either side was silently never transferred.
    This is the missing third category.

    rsync-style ladder, cheapest signal first:
      1. size         -- already in the listing response, free, kills most pairs
      2. md5Checksum  -- only for size-equal survivors; the local hash is the
                         only real work and is computed lazily, per survivor
      3. modifiedTime -- fallback for a binary file that Drive has no md5 for

    Google-native files (Docs/Sheets/Slides) carry neither md5 nor a byte size
    and cannot be round-tripped as bytes, so they are reported as skipped rather
    than compared -- and never crash the comparison.

    Returns (changed, skipped): changed maps rel -> info dict carrying the
    reason, both sizes/mtimes, and `local_newer` for direction-aware syncing.
    """
    root = Path(local_root if local_root is not None else LOCAL_FOLDER)
    changed, skipped = {}, []

    for rel in sorted(common):
        item = drive_meta.get(rel)
        if item is None:
            continue
        try:
            st = (root / rel).stat()
        except OSError:
            continue                       # vanished mid-scan; nothing to compare

        if is_google_native(item):
            skipped.append(rel)
            continue

        raw_size = item.get("size")
        try:
            drive_size = int(raw_size) if raw_size is not None else None
        except (TypeError, ValueError):
            drive_size = None
        drive_md5   = item.get("md5Checksum")
        drive_mtime = _rfc3339_to_epoch(item.get("modifiedTime"))

        reason = None
        if drive_size is not None and drive_size != st.st_size:
            reason = "size"                                        # rung 1
        elif drive_md5:
            if file_md5(root / rel) != drive_md5:
                reason = "md5"                                     # rung 2
        elif drive_mtime is not None and abs(drive_mtime - st.st_mtime) > MTIME_TOLERANCE:
            reason = "mtime"                                       # rung 3
        if not reason:
            continue

        changed[rel] = {
            "id":          item.get("id"),
            "reason":      reason,
            "local_size":  st.st_size,
            "drive_size":  drive_size,
            "local_mtime": st.st_mtime,
            "drive_mtime": drive_mtime,
            # No Drive timestamp -> assume local wins rather than clobber local.
            "local_newer": drive_mtime is None or st.st_mtime > drive_mtime,
        }
    return changed, skipped


@dataclass
class SyncState:
    local:       set  = field(default_factory=set)
    drive:       set  = field(default_factory=set)
    drive_files: dict = field(default_factory=dict)   # {rel: file_id}
    drive_meta:  dict = field(default_factory=dict)   # {rel: full Drive item}
    only_local:  set  = field(default_factory=set)
    only_drive:  set  = field(default_factory=set)
    changed:     dict = field(default_factory=dict)   # both sides, bytes differ
    skipped:     list = field(default_factory=list)   # Google-native, not comparable


def scan(service):
    """One local walk + one Drive listing -> the full three-way difference."""
    local      = filter_files(list_local_files())
    drive_meta = {rel: item
                  for rel, item in list_drive_files_meta(service, DRIVE_FOLDER_ID).items()
                  if not should_ignore(rel)}
    drive      = set(drive_meta)
    changed, skipped = detect_changes(local & drive, drive_meta)
    return SyncState(
        local=local,
        drive=drive,
        drive_files={rel: item["id"] for rel, item in drive_meta.items()},
        drive_meta=drive_meta,
        only_local=local - drive,
        only_drive=drive - local,
        changed=changed,
        skipped=skipped,
    )


def _arrow(info):
    return "local → Drive" if info["local_newer"] else "Drive → local"


# ── COMMAND: compare ──────────────────────────────────────────────────────────

def cmd_compare():
    print(f"\nScanning local:  {LOCAL_FOLDER}")
    local = filter_files(list_local_files())
    print(f"  {len(local)} files found")

    print(f"Scanning Drive (folder: {DRIVE_FOLDER_ID}) ...")
    service = get_service()
    get_or_create_folder.cache_clear()
    s = scan(service)
    local, drive = s.local, s.drive
    only_local, only_drive = s.only_local, s.only_drive
    print(f"  {len(drive)} files found")

    def section(title, items):
        print(f"\n{'='*58}\n  {title}  ({len(items)})\n{'='*58}")
        for f in sorted(items): print(f"  {f}")
        if not items: print("  (none)")

    section("Only on LOCAL  →  not backed up to Drive", only_local)
    section("Only on DRIVE  →  not present locally",    only_drive)

    print(f"\n{'='*58}\n  MODIFIED  →  on both sides, contents differ  ({len(s.changed)})\n{'='*58}")
    for f in sorted(s.changed):
        info = s.changed[f]
        print(f"  {f}")
        print(f"      {_arrow(info)}   (differs by {info['reason']}; "
              f"local {info['local_size']}B vs Drive {info['drive_size']}B)")
    if not s.changed: print("  (none)")
    if s.skipped:
        print(f"\n  Not comparable (Google Docs-native, no checksum): {len(s.skipped)}")
        for f in sorted(s.skipped): print(f"    ~  {f}")

    # Extension summary
    def exts(files):
        d = defaultdict(int)
        for f in files: d[Path(f).suffix.lower() or "(none)"] += 1
        return dict(sorted(d.items()))

    le, de = exts(local), exts(drive)
    only_le = set(le) - set(de)
    only_de = set(de) - set(le)

    print(f"\n{'='*58}\n  EXTENSIONS\n{'='*58}")
    print("  Local:")
    for e, n in le.items():
        print(f"    {e:<20} {n:>3}{'  ← local only' if e in only_le else ''}")
    print("  Drive:")
    for e, n in de.items():
        print(f"    {e:<20} {n:>3}{'  ← Drive only' if e in only_de else ''}")

    print(f"\n{'='*58}")
    print(f"  Local: {len(local)}  |  Drive: {len(drive)}  |  Both: {len(local & drive)}")
    print(f"  Missing from Drive: {len(only_local)}  |  Missing locally: {len(only_drive)}"
          f"  |  Modified: {len(s.changed)}")
    print(f"{'='*58}\n")


# ── COMMAND: sync ─────────────────────────────────────────────────────────────

def ask(label, options):
    print(f"\n  {label}")
    for i, (_, desc) in enumerate(options, 1): print(f"    [{i}] {desc}")
    while True:
        c = input("  Choice: ").strip()
        if c.isdigit() and 1 <= int(c) <= len(options):
            return options[int(c)-1][0]
        print("  Invalid, try again.")


def cmd_sync():
    print(f"\nScanning local:  {LOCAL_FOLDER}")
    local = filter_files(list_local_files())

    print(f"Scanning Drive ...")
    service     = get_service()
    get_or_create_folder.cache_clear()
    s           = scan(service)
    drive_files = s.drive_files
    only_local, only_drive = s.only_local, s.only_drive

    print(f"\n  Missing from Drive: {len(only_local)} files")
    print(f"  Missing locally:    {len(only_drive)} files")
    print(f"  Modified both ends: {len(s.changed)} files")

    # Handle local-only
    if only_local:
        action = ask(
            f"Files only on LOCAL ({len(only_local)}) — what to do?",
            [("all",  f"Upload ALL to Drive"),
             ("pick", "Choose file by file"),
             ("skip", "Skip")],
        )
        if action != "skip":
            targets = sorted(only_local)
            if action == "pick":
                targets = [f for f in targets if input(f"  Upload '{f}'? [y/N]: ").strip().lower() == "y"]
            print(f"\n  Uploading {len(targets)} file(s)...")
            for rel in targets:
                try: upload(service, Path(LOCAL_FOLDER) / rel, rel)
                except Exception as e: print(f"  ❌ {rel}: {e}")

    # Handle drive-only
    if only_drive:
        action = ask(
            f"Files only on DRIVE ({len(only_drive)}) — what to do?",
            [("all",  "Download ALL to local"),
             ("pick", "Choose file by file"),
             ("skip", "Skip")],
        )
        if action != "skip":
            targets = sorted(only_drive)
            if action == "pick":
                targets = [f for f in targets if input(f"  Download '{f}'? [y/N]: ").strip().lower() == "y"]
            print(f"\n  Downloading {len(targets)} file(s)...")
            for rel in targets:
                try: download(service, drive_files[rel], rel)
                except Exception as e: print(f"  ❌ {rel}: {e}")

    # Handle files that exist on BOTH sides but whose contents differ
    if s.changed:
        print(f"\n  Files that DIFFER ({len(s.changed)}):")
        for rel in sorted(s.changed):
            info = s.changed[rel]
            print(f"    ↕  {rel}   ({_arrow(info)}, differs by {info['reason']})")
        action = ask(
            f"Modified on BOTH sides ({len(s.changed)}) — what to do?",
            [("newer", "Sync each from whichever side is NEWER"),
             ("up",    "Upload ALL — local wins"),
             ("down",  "Download ALL — Drive wins"),
             ("pick",  "Choose file by file"),
             ("skip",  "Skip")],
        )
        if action != "skip":
            plan = []      # (rel, "up" | "down")
            for rel in sorted(s.changed):
                if action == "newer":
                    plan.append((rel, "up" if s.changed[rel]["local_newer"] else "down"))
                elif action in ("up", "down"):
                    plan.append((rel, action))
                else:
                    c = input(f"  '{rel}' — [u]pload / [d]ownload / skip? [u/d/N]: ").strip().lower()
                    if c == "u":   plan.append((rel, "up"))
                    elif c == "d": plan.append((rel, "down"))
            print(f"\n  Syncing {len(plan)} modified file(s)...")
            for rel, direction in plan:
                try:
                    if direction == "up":
                        upload(service, Path(LOCAL_FOLDER) / rel, rel, drive_files)
                    else:
                        download(service, drive_files[rel], rel)
                except Exception as e:
                    print(f"  ❌ {rel}: {e}")

    if s.skipped:
        print(f"\n  ⚠️  Skipped {len(s.skipped)} Google Docs-native file(s) "
              f"(no checksum to compare).")

    print("\n  ✅ Sync complete!\n")


# ── COMMAND: push (local is source of truth) ─────────────────────────────────

def cmd_push():
    print(f"\nScanning local:  {LOCAL_FOLDER}")
    local = filter_files(list_local_files())

    print(f"Scanning Drive ...")
    service     = get_service()
    get_or_create_folder.cache_clear()
    s           = scan(service)
    local, drive = s.local, s.drive
    drive_files = s.drive_files

    only_local = s.only_local    # need to upload
    only_drive = s.only_drive    # need to delete from Drive
    modified   = sorted(s.changed)  # exist on both sides, local content wins

    print(f"\n  To upload to Drive:    {len(only_local)} files")
    print(f"  To update on Drive:    {len(modified)} files")
    print(f"  To delete from Drive:  {len(only_drive)} files")
    print(f"  Already in sync:       {len(local & drive) - len(modified) - len(s.skipped)} files")

    if not only_local and not only_drive and not modified:
        print("\n  ✅ Everything is already in sync!\n")
        return

    # Preview what will happen
    if only_local:
        print(f"\n  Will UPLOAD ({len(only_local)}):")
        for f in sorted(only_local): print(f"    ↑  {f}")

    if modified:
        print(f"\n  Will UPDATE on Drive ({len(modified)}):")
        for f in modified:
            print(f"    ↻  {f}   (differs by {s.changed[f]['reason']})")

    if only_drive:
        print(f"\n  Will DELETE from Drive ({len(only_drive)}):")
        for f in sorted(only_drive): print(f"    🗑  {f}")

    confirm = input("\n  Proceed? [y/N]: ").strip().lower()
    if confirm != "y":
        print("  Aborted.")
        return

    if only_local:
        print(f"\n  Uploading {len(only_local)} file(s)...")
        for rel in sorted(only_local):
            try: upload(service, Path(LOCAL_FOLDER) / rel, rel)
            except Exception as e: print(f"  ❌ {rel}: {e}")

    if modified:
        print(f"\n  Updating {len(modified)} changed file(s) on Drive...")
        for rel in modified:
            try: upload(service, Path(LOCAL_FOLDER) / rel, rel, drive_files)
            except Exception as e: print(f"  ❌ {rel}: {e}")

    if only_drive:
        print(f"\n  Deleting {len(only_drive)} file(s) from Drive...")
        for rel in sorted(only_drive):
            try: trash_on_drive(service, rel, drive_files)
            except Exception as e: print(f"  ❌ {rel}: {e}")

    if s.skipped:
        print(f"\n  ⚠️  Left {len(s.skipped)} Google Docs-native file(s) untouched.")

    print("\n  ✅ Push complete! Drive now mirrors your local folder.\n")



# ── COMMAND: pull (Drive is source of truth) ─────────────────────────────────

def cmd_pull():
    print(f"\nScanning local:  {LOCAL_FOLDER}")
    local = filter_files(list_local_files())

    print(f"Scanning Drive ...")
    service     = get_service()
    get_or_create_folder.cache_clear()
    s           = scan(service)
    local, drive = s.local, s.drive
    drive_files = s.drive_files

    only_drive = s.only_drive    # need to download
    only_local = s.only_local    # need to delete locally
    modified   = sorted(s.changed)  # exist on both sides, Drive content wins

    print(f"\n  To download from Drive:  {len(only_drive)} files")
    print(f"  To refresh from Drive:   {len(modified)} files")
    print(f"  To delete locally:       {len(only_local)} files")
    print(f"  Already in sync:         {len(local & drive) - len(modified) - len(s.skipped)} files")

    if not only_drive and not only_local and not modified:
        print("\n  ✅ Everything is already in sync!\n")
        return

    # Preview what will happen
    if only_drive:
        print(f"\n  Will DOWNLOAD ({len(only_drive)}):")
        for f in sorted(only_drive): print(f"    ↓  {f}")

    if modified:
        print(f"\n  Will OVERWRITE locally ({len(modified)}):")
        for f in modified:
            print(f"    ↻  {f}   (differs by {s.changed[f]['reason']})")

    if only_local:
        print(f"\n  Will DELETE locally ({len(only_local)}):")
        for f in sorted(only_local): print(f"    🗑  {f}")

    confirm = input("\n  Proceed? [y/N]: ").strip().lower()
    if confirm != "y":
        print("  Aborted.")
        return

    if only_drive:
        print(f"\n  Downloading {len(only_drive)} file(s)...")
        for rel in sorted(only_drive):
            try: download(service, drive_files[rel], rel)
            except Exception as e: print(f"  ❌ {rel}: {e}")

    if modified:
        print(f"\n  Refreshing {len(modified)} changed file(s) from Drive...")
        for rel in modified:
            try: download(service, drive_files[rel], rel)
            except Exception as e: print(f"  ❌ {rel}: {e}")

    if only_local:
        print(f"\n  Deleting {len(only_local)} local file(s)...")
        for rel in sorted(only_local):
            try:
                p = Path(LOCAL_FOLDER) / rel
                p.unlink()
                # Remove empty parent dirs
                for parent in p.parents:
                    if parent == Path(LOCAL_FOLDER): break
                    if parent.is_dir() and not any(parent.iterdir()):
                        parent.rmdir()
                print(f"  🗑  Deleted: {rel}")
            except Exception as e: print(f"  ❌ {rel}: {e}")

    if s.skipped:
        print(f"\n  ⚠️  Left {len(s.skipped)} Google Docs-native file(s) untouched.")

    print("\n  ✅ Pull complete! Local now mirrors your Drive folder.\n")

# ── COMMAND: watch ────────────────────────────────────────────────────────────

def run_watcher():
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        print("ERROR: Run: pip install watchdog")
        sys.exit(1)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger()

    log.info(f"Connecting to Drive...")
    service     = get_service()
    get_or_create_folder.cache_clear()
    drive_meta  = list_drive_files_meta(service, DRIVE_FOLDER_ID)
    drive_index = {rel: item["id"] for rel, item in drive_meta.items()}
    log.info(f"Drive index: {len(drive_index)} files. Watching {LOCAL_FOLDER} ...")

    def unchanged_on_drive(rel):
        """True when Drive already holds these exact bytes (size, then md5).

        Editors fire a burst of `modified` events; without this every save
        re-uploads the whole file. Entries are dropped from `drive_meta` as
        soon as we write to Drive, so a stale signal can never suppress a
        genuine upload.
        """
        item = drive_meta.get(rel)
        if item is None:
            return False
        changed, skipped = detect_changes({rel}, {rel: item})
        return not changed and not skipped

    def push(local_path, rel):
        upload(service, local_path, rel, drive_index)
        drive_meta.pop(rel, None)   # our copy of the content signal is now stale

    class Handler(FileSystemEventHandler):
        def _rel(self, p): return str(Path(p).relative_to(LOCAL_FOLDER))

        def on_created(self, e):
            if e.is_directory or should_ignore(e.src_path): return
            rel = self._rel(e.src_path); time.sleep(0.5)
            push(e.src_path, rel)

        def on_modified(self, e):
            if e.is_directory or should_ignore(e.src_path): return
            rel = self._rel(e.src_path); time.sleep(0.5)
            if unchanged_on_drive(rel):
                return
            push(e.src_path, rel)

        def on_deleted(self, e):
            if e.is_directory or should_ignore(e.src_path): return
            rel = self._rel(e.src_path)
            trash_on_drive(service, rel, drive_index)
            drive_meta.pop(rel, None)

        def on_moved(self, e):
            if e.is_directory: return
            src_rel = self._rel(e.src_path)
            trash_on_drive(service, src_rel, drive_index)
            drive_meta.pop(src_rel, None)
            time.sleep(0.5)
            push(e.dest_path, self._rel(e.dest_path))

    obs = Observer()
    obs.schedule(Handler(), LOCAL_FOLDER, recursive=True)
    obs.start()
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        obs.stop()
    obs.join()


def cmd_watch(daemon=False, stop=False):
    if stop:
        if not os.path.exists(PID_FILE):
            print("Watcher is not running.")
            return
        pid = int(open(PID_FILE).read())
        try:
            os.kill(pid, signal.SIGTERM)
            os.remove(PID_FILE)
            print(f"Stopped watcher (PID {pid}).")
        except ProcessLookupError:
            os.remove(PID_FILE)
            print("Watcher was not running (stale PID removed).")
        return

    if daemon:
        print(f"Starting watcher in background. Logs → {LOG_FILE}")
        if os.fork() > 0: sys.exit(0)
        os.setsid()
        if os.fork() > 0: sys.exit(0)
        lf = open(LOG_FILE, "a")
        os.dup2(lf.fileno(), sys.stdout.fileno())
        os.dup2(lf.fileno(), sys.stderr.fileno())
        open(PID_FILE, "w").write(str(os.getpid()))
        import atexit
        atexit.register(lambda: os.path.exists(PID_FILE) and os.remove(PID_FILE))

    run_watcher()


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Sync local folder ↔ Google Drive")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("compare", help="Show differences")
    sub.add_parser("push",    help="Local is source of truth: upload missing, delete Drive extras")
    sub.add_parser("pull",    help="Drive is source of truth: download missing, delete local extras")
    sub.add_parser("sync",    help="Interactively fix differences (choose per side)")
    w = sub.add_parser("watch", help="Auto-upload new files")
    w.add_argument("--daemon", action="store_true", help="Run in background")
    w.add_argument("--stop",   action="store_true", help="Stop background watcher")

    args = p.parse_args()
    if   args.cmd == "compare": cmd_compare()
    elif args.cmd == "push":    cmd_push()
    elif args.cmd == "pull":    cmd_pull()
    elif args.cmd == "sync":    cmd_sync()
    elif args.cmd == "watch":   cmd_watch(getattr(args, "daemon", False), getattr(args, "stop", False))
    else: p.print_help()
