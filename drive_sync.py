#!/usr/bin/env python3
"""
drive_sync.py  —  Compare, sync, and watch a local folder against Google Drive.

Usage:
    python drive_sync.py compare              # Show what's different
    python drive_sync.py push                 # LOCAL is source of truth: upload missing, delete Drive extras
    python drive_sync.py pull                 # DRIVE is source of truth: download missing, delete local extras
    python drive_sync.py sync                 # Interactively fix differences (choose per side)
    python drive_sync.py watch                # Auto-upload new/changed files
    python drive_sync.py watch --daemon       # Run watcher in background
    python drive_sync.py watch --stop         # Stop background watcher

Requirements:
    pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client watchdog
"""

import os, sys, io, time, signal, logging, argparse
from pathlib import Path
from collections import defaultdict

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# ── Load config from .env ────────────────────────────────────────────────────
def _load_env():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
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


# ── Auth ─────────────────────────────────────────────────────────────────────

def get_service():
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
    return build("drive", "v3", credentials=creds)


# ── Drive helpers ─────────────────────────────────────────────────────────────

def list_drive_files(service, folder_id, prefix=""):
    """Recursively list all files. Returns {relative_path: file_id}."""
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
            if item["mimeType"] == "application/vnd.google-apps.folder":
                files.update(list_drive_files(service, item["id"], rel + "/"))
            else:
                files[rel] = item["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


def get_or_create_folder(service, name, parent_id, cache={}):
    key = (parent_id, name)
    if key in cache:
        return cache[key]
    q = f"'{parent_id}' in parents and name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    items = service.files().list(q=q, fields="files(id)").execute().get("files", [])
    fid = items[0]["id"] if items else service.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
        fields="id"
    ).execute()["id"]
    cache[key] = fid
    return fid


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


# ── COMMAND: compare ──────────────────────────────────────────────────────────

def cmd_compare():
    print(f"\nScanning local:  {LOCAL_FOLDER}")
    local = filter_files(list_local_files())
    print(f"  {len(local)} files found")

    print(f"Scanning Drive (folder: {DRIVE_FOLDER_ID}) ...")
    service = get_service()
    drive   = filter_files(set(list_drive_files(service, DRIVE_FOLDER_ID).keys()))
    print(f"  {len(drive)} files found")

    only_local = local - drive
    only_drive = drive - local

    def section(title, items):
        print(f"\n{'='*58}\n  {title}  ({len(items)})\n{'='*58}")
        for f in sorted(items): print(f"  {f}")
        if not items: print("  (none)")

    section("Only on LOCAL  →  not backed up to Drive", only_local)
    section("Only on DRIVE  →  not present locally",    only_drive)

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
    print(f"  Missing from Drive: {len(only_local)}  |  Missing locally: {len(only_drive)}")
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
    drive_files = list_drive_files(service, DRIVE_FOLDER_ID)
    drive       = filter_files(set(drive_files.keys()))

    only_local = local - drive
    only_drive = drive - local

    print(f"\n  Missing from Drive: {len(only_local)} files")
    print(f"  Missing locally:    {len(only_drive)} files")

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

    print("\n  ✅ Sync complete!\n")


# ── COMMAND: push (local is source of truth) ─────────────────────────────────

def cmd_push():
    print(f"\nScanning local:  {LOCAL_FOLDER}")
    local = filter_files(list_local_files())

    print(f"Scanning Drive ...")
    service     = get_service()
    drive_files = list_drive_files(service, DRIVE_FOLDER_ID)
    drive       = filter_files(set(drive_files.keys()))

    only_local = local - drive    # need to upload
    only_drive = drive - local    # need to delete from Drive

    print(f"\n  To upload to Drive:    {len(only_local)} files")
    print(f"  To delete from Drive:  {len(only_drive)} files")
    print(f"  Already in sync:       {len(local & drive)} files")

    if not only_local and not only_drive:
        print("\n  ✅ Everything is already in sync!\n")
        return

    # Preview what will happen
    if only_local:
        print(f"\n  Will UPLOAD ({len(only_local)}):")
        for f in sorted(only_local): print(f"    ↑  {f}")

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

    if only_drive:
        print(f"\n  Deleting {len(only_drive)} file(s) from Drive...")
        for rel in sorted(only_drive):
            try: trash_on_drive(service, rel, drive_files)
            except Exception as e: print(f"  ❌ {rel}: {e}")

    print("\n  ✅ Push complete! Drive now mirrors your local folder.\n")



# ── COMMAND: pull (Drive is source of truth) ─────────────────────────────────

def cmd_pull():
    print(f"\nScanning local:  {LOCAL_FOLDER}")
    local = filter_files(list_local_files())

    print(f"Scanning Drive ...")
    service     = get_service()
    drive_files = list_drive_files(service, DRIVE_FOLDER_ID)
    drive       = filter_files(set(drive_files.keys()))

    only_drive = drive - local    # need to download
    only_local = local - drive    # need to delete locally

    print(f"\n  To download from Drive:  {len(only_drive)} files")
    print(f"  To delete locally:       {len(only_local)} files")
    print(f"  Already in sync:         {len(local & drive)} files")

    if not only_drive and not only_local:
        print("\n  ✅ Everything is already in sync!\n")
        return

    # Preview what will happen
    if only_drive:
        print(f"\n  Will DOWNLOAD ({len(only_drive)}):")
        for f in sorted(only_drive): print(f"    ↓  {f}")

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
    drive_index = list_drive_files(service, DRIVE_FOLDER_ID)
    log.info(f"Drive index: {len(drive_index)} files. Watching {LOCAL_FOLDER} ...")

    class Handler(FileSystemEventHandler):
        def _rel(self, p): return str(Path(p).relative_to(LOCAL_FOLDER))

        def on_created(self, e):
            if e.is_directory or should_ignore(e.src_path): return
            rel = self._rel(e.src_path); time.sleep(0.5)
            upload(service, e.src_path, rel, drive_index)

        def on_modified(self, e):
            if e.is_directory or should_ignore(e.src_path): return
            rel = self._rel(e.src_path); time.sleep(0.5)
            upload(service, e.src_path, rel, drive_index)

        def on_deleted(self, e):
            if e.is_directory or should_ignore(e.src_path): return
            trash_on_drive(service, self._rel(e.src_path), drive_index)

        def on_moved(self, e):
            if e.is_directory: return
            trash_on_drive(service, self._rel(e.src_path), drive_index)
            time.sleep(0.5)
            upload(service, e.dest_path, self._rel(e.dest_path), drive_index)

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
