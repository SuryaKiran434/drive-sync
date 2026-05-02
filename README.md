# drive_sync

A single-file Python tool to compare, sync, and watch a local folder against a Google Drive folder — bidirectionally, with full subfolder support.

---

## Project Structure

```
drive-sync/
├── drive_sync.py       # The entire tool — one file
├── .env                # Your local config (never commit this)
├── credentials.json    # OAuth credentials from Google Cloud Console (never commit this)
├── token.json          # Auto-generated after first login (never commit this)
├── .gitignore          # Excludes sensitive files from version control
└── README.md
```

---

## Requirements

### Python packages

```bash
pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client watchdog
```

### Google Cloud setup (one-time)

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (or use an existing one)
3. Navigate to **APIs & Services → Library** and enable **Google Drive API**
4. Go to **APIs & Services → OAuth consent screen**
   - Choose **External**, fill in the app name
   - Under **Test users**, add your Gmail address
5. Go to **APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client ID**
   - Application type: **Desktop App**
   - Download the JSON file and save it as `credentials.json` in the project folder

### Finding your Drive Folder ID

Open your Google Drive folder in the browser. The URL looks like:

```
https://drive.google.com/drive/u/0/folders/<YOUR_FOLDER_ID>
```

The last segment after `/folders/` is your folder ID.

---

## Configuration

Create a `.env` file in the project folder (copy from `.env.example`):

```bash
cp .env.example .env
```

Then fill in your values:

```env
LOCAL_FOLDER=/path/to/your/local/folder
DRIVE_FOLDER_ID=your_drive_folder_id_here
```

> ⚠️ Never commit `.env`, `credentials.json`, or `token.json` to version control. They are listed in `.gitignore`.

---

## .env.example

```env
# Path to the local folder you want to sync
LOCAL_FOLDER=/path/to/your/folder

# Google Drive folder ID (from the URL of your Drive folder)
DRIVE_FOLDER_ID=your_folder_id_here
```

---

## .gitignore

```
.env
credentials.json
token.json
watcher.log
watcher.pid
__pycache__/
*.pyc
```

---

## Authentication

The first time you run any command, a browser window will open asking you to sign in with your Google account and grant access. After that, a `token.json` file is saved locally and reused automatically.

> If you see a `403 insufficient permissions` error, delete `token.json` and re-run. This happens when the cached token was created with the wrong scope.

```bash
rm token.json
python drive_sync.py <command>
```

---

## Commands

### `compare` — See what's different

```bash
python drive_sync.py compare
```

Scans both sides and prints a summary without making any changes:

- Files only on local (not backed up to Drive)
- Files only on Drive (not present locally)
- File extension breakdown for each side
- Total counts

---

### `push` — Local is source of truth

```bash
python drive_sync.py push
```

Makes Drive mirror your local folder:

- Uploads files that exist locally but not on Drive
- Deletes (moves to Trash) files on Drive that don't exist locally
- Shows a full preview and asks for confirmation before doing anything

Use this after cleaning up your local folder and wanting Drive to match exactly.

---

### `pull` — Drive is source of truth

```bash
python drive_sync.py pull
```

Makes your local folder mirror Drive:

- Downloads files from Drive that don't exist locally
- Deletes local files that don't exist on Drive
- Cleans up empty local folders left behind
- Shows a full preview and asks for confirmation before doing anything

Use this when Drive has been updated from another device.

---

### `sync` — Interactive, choose per side

```bash
python drive_sync.py sync
```

Gives you full control over each side independently:

- For local-only files: upload all, pick file by file, or skip
- For Drive-only files: download all, pick file by file, or skip

---

### `watch` — Auto-upload on file changes

```bash
python drive_sync.py watch                # foreground (Ctrl+C to stop)
python drive_sync.py watch --daemon       # background
python drive_sync.py watch --stop         # stop background watcher
```

Monitors your local folder in real time. Any file added, modified, moved, or deleted locally is automatically reflected on Drive. Logs written to `watcher.log` in daemon mode.

---

## Command Reference

| Command | Source of truth | Uploads | Downloads | Deletes |
|---|---|---|---|---|
| `compare` | — | No | No | No |
| `push` | Local | ✅ Missing files | No | ✅ Drive extras → Trash |
| `pull` | Drive | No | ✅ Missing files | ✅ Local extras (permanent) |
| `sync` | You decide | ✅ If chosen | ✅ If chosen | No |
| `watch` | Local (ongoing) | ✅ On change | No | ✅ On local delete → Trash |

---

## Ignored Files

Automatically skipped on both sides:

- `.DS_Store` (macOS metadata)
- `Thumbs.db` (Windows thumbnails)
- `.git` (version control)
- `.tmp`, `.swp`, `.part` (temporary files)

---

## Moving the Project

To move the project folder to a new location:

```bash
mv /old/path/drive-sync /new/path/drive-sync
```

No code changes needed — paths are read from `.env` at runtime.

---

## Notes

- Deletions via `push`, `pull`, and `watch` move files to **Google Drive Trash** — recoverable within 30 days.
- Local deletions via `pull` are **permanent**. Review the preview carefully before confirming.
- Subfolder structure is fully preserved. Missing Drive subfolders are created automatically during upload.
- Files are matched by **relative path and filename** only — not by content or modification date.
