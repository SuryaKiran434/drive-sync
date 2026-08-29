# drive_sync

A single-file Python tool to compare, sync, and watch a local folder against a
Google Drive folder — bidirectionally, with full subfolder support, parallel
Drive listing, and content-based change detection.

---

## Contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Running locally](#running-locally)
- [Commands](#commands)
- [How listing is made fast](#how-listing-is-made-fast)
- [How modified files are detected](#how-modified-files-are-detected)
- [Tests](#tests)
- [Continuous integration](#continuous-integration)
- [Security](#security)

---

## What it does

Point it at a local folder and a Drive folder and it will tell you — or fix —
exactly how the two differ. It classifies every file into one of three buckets:

| Bucket | Meaning |
|---|---|
| `only_local` | present locally, missing on Drive |
| `only_drive` | present on Drive, missing locally |
| `modified` | present on **both** sides, but the bytes differ |

That third bucket is the important one. Earlier versions compared the two sides
by **relative path only**, so a file that existed on both ends was considered
"in sync" forever — an edit on either side was silently never transferred. The
tool detected additions and deletions and nothing else. It now compares content.

---

## Architecture

```
                    ┌──────────────────────────────────────┐
                    │  credentials.json  (OAuth client)    │
                    │  token.json        (cached grant)    │
                    └──────────────┬───────────────────────┘
                                   │ get_service()
                                   ▼
                        ┌────────────────────┐
                        │  Drive v3 service  │
                        └─────────┬──────────┘
                                  │
      ┌───────────────────────────┴────────────────────────────┐
      │  list_drive_files_meta()  —  parallel level-order BFS   │
      │                                                        │
      │   level N folders ──► chunk by PARENTS_PER_QUERY (25)   │
      │        │                                               │
      │        ├─► _list_children()  ┐                         │
      │        ├─► _list_children()  ├─ ThreadPoolExecutor(8)   │
      │        └─► _list_children()  ┘  each thread: its own    │
      │                                 AuthorizedHttp          │
      │        │  pageSize=1000, fields include md5Checksum,    │
      │        │  size, modifiedTime, parents                   │
      │        ▼                                               │
      │   paths rebuilt client-side from `parents` ──► level N+1│
      └───────────────────────────┬────────────────────────────┘
                                  │  {rel_path: drive_item}
                                  ▼
   Path(LOCAL_FOLDER).rglob("*") ──►  {rel_path}
                                  │
                                  ▼
                       ┌──────────────────────┐
                       │       scan()         │
                       │  three-way diff      │
                       ├──────────────────────┤
                       │ only_local  (set)    │
                       │ only_drive  (set)    │
                       │ changed     (dict) ◄─┼── detect_changes()
                       │ skipped     (list)   │   size → md5 → mtime
                       └──────────┬───────────┘
                                  │  SyncState
      ┌──────────┬────────────────┼────────────────┬───────────┐
      ▼          ▼                ▼                ▼           ▼
   compare     push             pull             sync        watch
  (read-only) local wins     Drive wins      ask per bucket  watchdog
```

Everything lives in one module, `drive_sync.py`:

| Layer | Functions |
|---|---|
| Auth | `get_service()`, `_worker_http()` |
| Drive listing | `_list_children()`, `list_drive_files_meta()`, `list_drive_files()` |
| Drive mutation | `get_or_create_folder()`, `ensure_drive_path()`, `upload()`, `download()`, `trash_on_drive()` |
| Local scan | `list_local_files()`, `should_ignore()`, `filter_files()`, `file_md5()` |
| Diff | `detect_changes()`, `scan()`, `SyncState` |
| Commands | `cmd_compare()`, `cmd_push()`, `cmd_pull()`, `cmd_sync()`, `cmd_watch()` / `run_watcher()` |

---

## Project structure

```
drive-sync/
├── drive_sync.py            # The entire tool — one module
├── requirements.txt         # Runtime dependencies
├── pytest.ini               # testpaths = tests
├── env.example              # Template for your .env  (copy, don't edit in place)
├── .env                     # Your local config              (never commit)
├── credentials.json         # OAuth client from Google Cloud (never commit)
├── token.json               # Written after first consent     (never commit)
├── tests/                   # 57 tests, fully mocked — no network, no credentials
│   ├── conftest.py
│   ├── fake_drive.py        # In-memory stand-in for the Drive v3 service
│   ├── test_listing.py
│   ├── test_change_detection.py
│   ├── test_commands.py
│   └── test_folder_cache.py
├── .github/
│   ├── dependabot.yml       # Weekly pip + github-actions updates
│   └── workflows/
│       ├── ci.yml           # "Tests (Python)" — required on main
│       └── slack-notify.yml
├── .gitignore
└── README.md
```

---

## Running locally

### 1. Prerequisites

- **Python 3.12** (what CI runs)
- A Google account with the folder you want to sync

### 2. Clone and create a virtualenv

```bash
git clone https://github.com/SuryaKiran434/drive-sync.git
cd drive-sync

python3.12 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

`requirements.txt` covers the Drive client, OAuth and `python-dotenv`. Two
extras are only needed for optional paths:

```bash
pip install watchdog     # required by `watch` only
pip install pytest       # required to run the test suite only
```

### 4. Get OAuth credentials from Google Cloud Console

One-time setup:

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create
   a project (or reuse one).
2. **APIs & Services → Library** → enable **Google Drive API**.
3. **APIs & Services → OAuth consent screen** → choose **External**, fill in an
   app name, and add your own Google address under **Test users**.
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID** →
   application type **Desktop app**.
5. Download the JSON and save it as **`credentials.json` in the repository
   root** (the same directory as `drive_sync.py`).

### 5. First run: browser consent produces `token.json`

The first command you run opens a browser window asking you to sign in and grant
Drive access. On success the tool writes **`token.json`** next to
`drive_sync.py` and reuses it from then on — later runs are non-interactive.

If you ever hit `403 insufficient permissions`, the cached token was minted with
the wrong scope. Delete it and re-consent:

```bash
rm token.json
python drive_sync.py compare
```

### 6. Configure `.env`

Copy the template and fill in your own values:

```bash
cp env.example .env
```

| Variable | Meaning |
|---|---|
| `LOCAL_FOLDER` | Absolute path to the local folder to sync |
| `DRIVE_FOLDER_ID` | The Drive folder id — the last path segment of the folder's URL |

```env
# Placeholders — replace with your own values
LOCAL_FOLDER=/path/to/your/local/folder
DRIVE_FOLDER_ID=your_folder_id_here
```

To find the folder id, open the folder in Drive and read it off the URL:

```
https://drive.google.com/drive/u/0/folders/<DRIVE_FOLDER_ID>
                                            ^^^^^^^^^^^^^^^
```

Both variables are mandatory; the tool exits with an error if either is missing.
They may also come from the real environment (CI, `export`) instead of a `.env`
file.

### 7. Run a command

```bash
python drive_sync.py compare
```

### 8. Run the tests

```bash
python -m pytest
```

---

## Commands

### `compare` — read-only, changes nothing

```bash
python drive_sync.py compare
```

Prints, without touching either side:

- files only on local (not backed up to Drive)
- files only on Drive (not present locally)
- **modified** files — present on both sides with differing contents, annotated
  with the reason (`size` / `md5` / `mtime`) and the direction the change should
  travel
- Google-native files that could not be compared
- an extension breakdown per side, and totals

### `push` — local is the source of truth

```bash
python drive_sync.py push
```

Makes Drive mirror local: uploads local-only files, **re-uploads files whose
local contents changed**, and trashes Drive-only files. Shows a full preview and
asks for confirmation first.

### `pull` — Drive is the source of truth

```bash
python drive_sync.py pull
```

Makes local mirror Drive: downloads Drive-only files, **overwrites local files
whose Drive copy changed**, deletes local-only files, and cleans up the empty
directories left behind. Previews and confirms first.

### `sync` — interactive, decide per bucket

```bash
python drive_sync.py sync
```

- local-only files → upload all / pick file by file / skip
- Drive-only files → download all / pick file by file / skip
- modified on both sides → **newer wins** / **local wins** / **Drive wins** /
  pick file by file / skip

### `watch` — mirror local changes as they happen

```bash
python drive_sync.py watch            # foreground, Ctrl+C to stop
python drive_sync.py watch --daemon   # background, logs to watcher.log
python drive_sync.py watch --stop     # stop the background watcher
```

Watches the local tree with `watchdog` and reflects creates, modifies, moves and
deletes onto Drive. A `modified` event whose bytes already match Drive is
skipped, so the burst of save events most editors emit does not re-upload the
same content over and over.

### Command reference

| Command | Source of truth | Uploads | Downloads | Deletes |
|---|---|---|---|---|
| `compare` | — | No | No | No |
| `push` | Local | Missing + modified | No | Drive extras → Trash |
| `pull` | Drive | No | Missing + modified | Local extras (permanent) |
| `sync` | You decide | If chosen | If chosen | No |
| `watch` | Local (ongoing) | On change | No | On local delete → Trash |

---

## How listing is made fast

Listing used to be a recursive walk: one blocking `files.list` call per folder,
at the API default page size of 100. A 40-folder tree cost **41 round trips**,
serially. It now costs **3**.

Two changes did it.

**1. Fewer, wider queries.** Every folder on a BFS level is OR-ed into a single
query — up to `PARENTS_PER_QUERY = 25` parents per `q` — at
`PAGE_SIZE = 1000`, the API maximum. A 2000-file folder is 2 pages instead of
20.

**2. Levels are fetched in parallel.** Chunks for a level are dispatched across
a `ThreadPoolExecutor` with `LIST_MAX_WORKERS = 8`. The pool is deliberately
small: this workload is latency-bound, not CPU-bound. Cost drops from one
round trip per *folder* to one round trip per *depth level*.

The listing request asks for `id, name, mimeType, parents, md5Checksum, size,
modifiedTime`, so the hierarchy is rebuilt client-side from each item's
`parents` field and change detection gets its inputs for free — no extra calls
are made to resolve paths.

### Why not one flat drive-wide query?

Because Drive's query language **has no "descendant of" operator**. `'X' in
parents` matches **direct children only**. There is therefore no single query
that means "everything under this folder". The only flat option is
`trashed = false` across the entire drive, filtered client-side — which is
correct, but downloads the user's *whole Drive* in order to sync one folder,
and is unbounded in cost. Level-order BFS keeps the work proportional to the
subtree actually being synced.

### Thread safety

`httplib2` — which `google-api-python-client` rides on — is **not thread-safe**,
and a `service` object shares a single `Http` instance across all callers.
Handing that object to eight worker threads corrupts responses.

So `_worker_http()` builds a **per-thread** `AuthorizedHttp` in
`threading.local()` and each request is issued as `req.execute(http=...)`. When
there are no real credentials (the test suite drives a mock service) it returns
`None` and `execute()` is called plainly.

### Folder-id cache

`get_or_create_folder()` is memoised with `functools.lru_cache(maxsize=4096)`.
It previously used a `cache={}` mutable default argument, which bound once at
import time, lived for the entire process, and would happily hand back the id of
a folder that had since been deleted. The `lru_cache` is bounded and every
command calls `get_or_create_folder.cache_clear()` at start-up.

---

## How modified files are detected

`detect_changes()` walks the files present on both sides and applies an
rsync-style ladder, cheapest signal first:

| Rung | Signal | Cost | Notes |
|---|---|---|---|
| 1 | `size` | free | Already in the listing response; eliminates almost every pair |
| 2 | `md5Checksum` | local hash | Only for size-equal survivors; the local MD5 is computed lazily, per survivor |
| 3 | `modifiedTime` | free | Fallback only, for files Drive has no MD5 for; `MTIME_TOLERANCE` is 2s |

Each changed file is recorded with its reason, both sizes, both mtimes, and
`local_newer` — which is what lets `sync` offer a direction-aware "newer wins".
When Drive reports no timestamp at all, `local_newer` defaults to `True` so the
tool errs toward preserving local rather than clobbering it.

**Google-native files** (Docs, Sheets, Slides, Forms — any
`application/vnd.google-apps.*`) carry neither an MD5 nor a meaningful byte
size, and cannot be round-tripped as bytes. They are reported as *skipped /
not comparable* rather than compared, and are never modified.

Modified files are wired through every command: `compare` reports them, `push`
re-uploads them, `pull` re-downloads them, `sync` prompts for a direction, and
`watch` uses the same comparison to suppress no-op save events.

---

## Ignored files

Skipped on both sides:

- `.DS_Store`, `Thumbs.db`, `.git`
- `.tmp`, `.swp`, `.part`

---

## Tests

**57 tests**, all fully mocked — the suite never touches the network and needs
no `credentials.json`, no `token.json` and no `.env`.

```bash
pip install pytest
python -m pytest
```

| File | Tests | Covers |
|---|---|---|
| `tests/test_listing.py` | 18 | BFS levels, chunking, pagination, multi-parent and orphan handling, cycle guard |
| `tests/test_change_detection.py` | 17 | The size → md5 → mtime ladder, Google-native skips, `local_newer` |
| `tests/test_commands.py` | 15 | `compare` / `push` / `pull` / `sync` / `watch` behaviour |
| `tests/test_folder_cache.py` | 7 | `get_or_create_folder` memoisation and `cache_clear()` |

`tests/fake_drive.py` is an in-memory stand-in for the Drive v3 service, so the
same code path runs in tests as in production.

---

## Continuous integration

`.github/workflows/ci.yml` runs the suite on Python 3.12 for every push to
`main` and every pull request. The job is named **`Tests (Python)`** and is a
**required status check on `main`** — a pull request cannot merge until it
passes.

`.github/dependabot.yml` opens weekly dependency PRs (max 5 open at a time) for
both `pip` (root `requirements.txt`) and `github-actions`.

---

## Security

**Never commit these — they are already in `.gitignore`, keep it that way:**

| File | Why |
|---|---|
| `.env` | Contains your local paths and Drive folder id |
| `credentials.json` | Your OAuth **client secret** from Google Cloud |
| `token.json` | A live access + refresh token for your Drive account |

`token.json` in particular is a bearer credential for the full
`https://www.googleapis.com/auth/drive` scope. If one ever lands in a commit,
revoke the client in Google Cloud Console and delete the local token — rotating
is the only fix, since git history keeps the old value.

Commit `env.example` (placeholders only), never `.env`.

---

## Notes

- Deletions from `push`, `pull` and `watch` move Drive files to **Trash**, which
  is recoverable for 30 days.
- Local deletions performed by `pull` are **permanent**. Read the preview before
  confirming.
- Subfolder structure is preserved; missing Drive subfolders are created on
  upload.
- Files are matched by relative path, then compared by **content** (size, then
  MD5, then modification time).
- The project folder can be moved anywhere — all paths come from `.env` at
  runtime.
