"""End-to-end smoke tests over the real commands, against the mock Drive.

Nothing here touches the network; `get_service` is replaced with a FakeDrive and
`LOCAL_FOLDER` / `DRIVE_FOLDER_ID` point at a tmp dir.
"""
import hashlib
import os

import pytest

import drive_sync
from fake_drive import FakeDrive, blob, folder, gdoc


def md5_of(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


DRIVE_EPOCH = drive_sync._rfc3339_to_epoch("2026-01-01T00:00:00.000Z")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """local + Drive laid out so every category is populated:

      same.txt      identical both sides            -> in sync
      edited.txt    same path, different bytes      -> MODIFIED  (the bug)
      onlylocal.txt local only                      -> upload
      docs/x.md     Drive only                      -> download
      Plan          Google-native on Drive          -> skipped
    """
    (tmp_path / "docs").mkdir()
    (tmp_path / "same.txt").write_bytes(b"same")
    (tmp_path / "edited.txt").write_bytes(b"NEW CONTENT, LONGER")
    (tmp_path / "onlylocal.txt").write_bytes(b"fresh")
    (tmp_path / "Plan").write_bytes(b"placeholder")
    os.utime(tmp_path / "edited.txt", (DRIVE_EPOCH + 3600, DRIVE_EPOCH + 3600))

    svc = FakeDrive([
        blob("i-same", "same.txt", ["ROOT"], size=4, md5=md5_of(b"same")),
        blob("i-edit", "edited.txt", ["ROOT"], size=3, md5=md5_of(b"old")),
        folder("d-docs", "docs", ["ROOT"]),
        blob("i-x", "x.md", ["d-docs"], size=2, md5=md5_of(b"hi")),
        gdoc("i-plan", "Plan", ["ROOT"]),
    ])

    monkeypatch.setattr(drive_sync, "LOCAL_FOLDER", str(tmp_path))
    monkeypatch.setattr(drive_sync, "DRIVE_FOLDER_ID", "ROOT")
    monkeypatch.setattr(drive_sync, "get_service", lambda: svc)
    drive_sync.get_or_create_folder.cache_clear()
    return tmp_path, svc


# ── scan(): the three-way difference ─────────────────────────────────────────

def test_scan_produces_three_categories(world):
    tmp_path, svc = world
    s = drive_sync.scan(svc)

    assert s.only_local == {"onlylocal.txt"}
    assert s.only_drive == {"docs/x.md"}
    assert set(s.changed) == {"edited.txt"}, \
        "a file on both sides with different bytes must be its own category"
    assert s.skipped == ["Plan"]
    assert s.drive_files["edited.txt"] == "i-edit"
    assert s.changed["edited.txt"]["local_newer"] is True


def test_scan_ignores_junk_files(world):
    tmp_path, svc = world
    (tmp_path / ".DS_Store").write_bytes(b"junk")
    (tmp_path / "work.tmp").write_bytes(b"junk")
    s = drive_sync.scan(svc)
    assert ".DS_Store" not in s.only_local and "work.tmp" not in s.only_local


# ── compare (the diff command) ───────────────────────────────────────────────

def test_compare_smoke(world, capsys):
    drive_sync.cmd_compare()
    out = capsys.readouterr().out

    assert "Only on LOCAL" in out and "onlylocal.txt" in out
    assert "Only on DRIVE" in out and "docs/x.md" in out
    assert "MODIFIED" in out and "edited.txt" in out
    assert "local → Drive" in out
    assert "Modified: 1" in out
    assert "Google Docs-native" in out and "Plan" in out


def test_compare_reports_drive_side_edits_pointing_the_other_way(world, capsys):
    tmp_path, svc = world
    os.utime(tmp_path / "edited.txt", (DRIVE_EPOCH - 3600, DRIVE_EPOCH - 3600))
    drive_sync.cmd_compare()
    out = capsys.readouterr().out
    assert "Drive → local" in out


def test_compare_clean_tree_shows_no_modifications(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.txt").write_bytes(b"a")
    svc = FakeDrive([blob("i", "a.txt", ["ROOT"], size=1, md5=md5_of(b"a"))])
    monkeypatch.setattr(drive_sync, "LOCAL_FOLDER", str(tmp_path))
    monkeypatch.setattr(drive_sync, "DRIVE_FOLDER_ID", "ROOT")
    monkeypatch.setattr(drive_sync, "get_service", lambda: svc)
    drive_sync.cmd_compare()
    out = capsys.readouterr().out
    assert "Modified: 0" in out


# ── push: modified files actually transfer ───────────────────────────────────

def test_push_uploads_modified_file(world, monkeypatch, capsys):
    tmp_path, svc = world
    uploaded, downloaded, trashed = [], [], []
    monkeypatch.setattr(drive_sync, "upload",
                        lambda s, p, rel, idx=None: uploaded.append((rel, idx is not None)))
    monkeypatch.setattr(drive_sync, "download", lambda s, fid, rel: downloaded.append(rel))
    monkeypatch.setattr(drive_sync, "trash_on_drive",
                        lambda s, rel, idx: trashed.append(rel))
    monkeypatch.setattr("builtins.input", lambda *a: "y")

    drive_sync.cmd_push()
    out = capsys.readouterr().out

    assert "Will UPDATE on Drive" in out
    assert ("edited.txt", True) in uploaded, \
        "REGRESSION: a locally modified file must be re-uploaded by push"
    assert ("onlylocal.txt", False) in uploaded
    assert trashed == ["docs/x.md"]
    assert downloaded == []


def test_push_aborts_cleanly(world, monkeypatch, capsys):
    uploaded = []
    monkeypatch.setattr(drive_sync, "upload", lambda *a, **k: uploaded.append(a))
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    drive_sync.cmd_push()
    assert "Aborted." in capsys.readouterr().out
    assert uploaded == []


def test_push_reports_in_sync_when_nothing_differs(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.txt").write_bytes(b"a")
    svc = FakeDrive([blob("i", "a.txt", ["ROOT"], size=1, md5=md5_of(b"a"))])
    monkeypatch.setattr(drive_sync, "LOCAL_FOLDER", str(tmp_path))
    monkeypatch.setattr(drive_sync, "DRIVE_FOLDER_ID", "ROOT")
    monkeypatch.setattr(drive_sync, "get_service", lambda: svc)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    drive_sync.cmd_push()
    assert "already in sync" in capsys.readouterr().out


# ── pull: modified files actually transfer, the other way ────────────────────

def test_pull_downloads_modified_file(world, monkeypatch, capsys):
    tmp_path, svc = world
    downloaded, uploaded = [], []
    monkeypatch.setattr(drive_sync, "download", lambda s, fid, rel: downloaded.append(rel))
    monkeypatch.setattr(drive_sync, "upload", lambda *a, **k: uploaded.append(a))
    monkeypatch.setattr("builtins.input", lambda *a: "y")

    drive_sync.cmd_pull()
    out = capsys.readouterr().out

    assert "Will OVERWRITE locally" in out
    assert "edited.txt" in downloaded, \
        "REGRESSION: a file changed on Drive must be pulled down"
    assert "docs/x.md" in downloaded
    assert uploaded == []
    assert not (tmp_path / "onlylocal.txt").exists()   # deleted, Drive is truth


# ── sync: the interactive third category ─────────────────────────────────────

def _wire(monkeypatch, answers):
    """Feed the interactive prompts a fixed script."""
    seq = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *a: next(seq))


def test_sync_offers_modified_category_and_honours_newer(world, monkeypatch, capsys):
    tmp_path, svc = world
    uploaded, downloaded = [], []
    monkeypatch.setattr(drive_sync, "upload",
                        lambda s, p, rel, idx=None: uploaded.append(rel))
    monkeypatch.setattr(drive_sync, "download", lambda s, fid, rel: downloaded.append(rel))
    # only-local: skip(3), only-drive: skip(3), modified: newer(1)
    _wire(monkeypatch, ["3", "3", "1"])

    drive_sync.cmd_sync()
    out = capsys.readouterr().out

    assert "Modified on BOTH sides" in out
    assert uploaded == ["edited.txt"]     # local mtime is newer
    assert downloaded == []


def test_sync_drive_wins_downloads_modified(world, monkeypatch, capsys):
    uploaded, downloaded = [], []
    monkeypatch.setattr(drive_sync, "upload",
                        lambda s, p, rel, idx=None: uploaded.append(rel))
    monkeypatch.setattr(drive_sync, "download", lambda s, fid, rel: downloaded.append(rel))
    _wire(monkeypatch, ["3", "3", "3"])   # skip, skip, "Download ALL — Drive wins"

    drive_sync.cmd_sync()
    assert downloaded == ["edited.txt"] and uploaded == []


def test_sync_pick_per_file(world, monkeypatch):
    uploaded, downloaded = [], []
    monkeypatch.setattr(drive_sync, "upload",
                        lambda s, p, rel, idx=None: uploaded.append(rel))
    monkeypatch.setattr(drive_sync, "download", lambda s, fid, rel: downloaded.append(rel))
    _wire(monkeypatch, ["3", "3", "4", "d"])   # skip, skip, pick, download it

    drive_sync.cmd_sync()
    assert downloaded == ["edited.txt"] and uploaded == []


def test_sync_can_skip_modified(world, monkeypatch):
    uploaded, downloaded = [], []
    monkeypatch.setattr(drive_sync, "upload",
                        lambda s, p, rel, idx=None: uploaded.append(rel))
    monkeypatch.setattr(drive_sync, "download", lambda s, fid, rel: downloaded.append(rel))
    _wire(monkeypatch, ["3", "3", "5"])        # skip modified

    drive_sync.cmd_sync()
    assert uploaded == [] and downloaded == []


# ── upload() still reaches the right API call ────────────────────────────────

def test_upload_updates_in_place_when_the_file_already_exists(world, tmp_path):
    _, svc = world
    index = {"edited.txt": "i-edit"}
    drive_sync.upload(svc, tmp_path / "edited.txt", "edited.txt", index)
    assert svc.updated and svc.updated[0][0] == "i-edit"
    assert svc.created == []


def test_upload_creates_when_absent(world, tmp_path):
    _, svc = world
    index = {}
    drive_sync.upload(svc, tmp_path / "onlylocal.txt", "onlylocal.txt", index)
    assert svc.created and svc.created[0]["name"] == "onlylocal.txt"
    assert index["onlylocal.txt"] == svc.created[0]["id"]
