"""Fix #2 -- THE BUG: files present on both sides were never compared.

Before this fix the tool diffed relative PATHS only:
    only_local = local - drive
    only_drive = drive - local
A file living on both sides lands in neither set, so an edit on either side was
invisible and never transferred. These tests pin the regression shut.
"""
import hashlib
import os
import time

import pytest

import drive_sync
from fake_drive import FakeDrive, blob, gdoc


def md5_of(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def write(tmp_path, rel, data: bytes, mtime=None):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if mtime is not None:
        os.utime(p, (mtime, mtime))
    return p


# epoch 1767225600 == 2026-01-01T00:00:00Z, matching fake_drive's default
DRIVE_EPOCH = drive_sync._rfc3339_to_epoch("2026-01-01T00:00:00.000Z")


# ── the regression itself ────────────────────────────────────────────────────

def test_path_only_diff_is_blind_to_edits(tmp_path):
    """Documents the old behaviour that caused the bug."""
    local = {"report.txt"}
    drive = {"report.txt"}
    assert local - drive == set()      # not "only local"
    assert drive - local == set()      # not "only drive"
    # ...and so the edited file was in no category at all. Hence detect_changes.


def test_local_edit_is_detected_as_modified(tmp_path):
    """Direction 1: edited LOCALLY -> must be re-uploaded."""
    data = b"a" * 500
    write(tmp_path, "report.txt", data, mtime=DRIVE_EPOCH + 3600)
    drive_meta = {"report.txt": blob("id1", "report.txt", ["ROOT"],
                                     size=200, md5=md5_of(b"b" * 200))}

    changed, skipped = drive_sync.detect_changes({"report.txt"}, drive_meta, tmp_path)

    assert "report.txt" in changed, "a locally edited file must be classified modified"
    assert changed["report.txt"]["reason"] == "size"
    assert changed["report.txt"]["local_newer"] is True   # -> upload
    assert skipped == []


def test_drive_edit_is_detected_as_modified(tmp_path):
    """Direction 2: edited on DRIVE -> must be pulled down."""
    data = b"a" * 200
    write(tmp_path, "report.txt", data, mtime=DRIVE_EPOCH - 3600)
    drive_meta = {"report.txt": blob("id1", "report.txt", ["ROOT"],
                                     size=999, md5=md5_of(b"z" * 999))}

    changed, _ = drive_sync.detect_changes({"report.txt"}, drive_meta, tmp_path)

    assert "report.txt" in changed
    assert changed["report.txt"]["local_newer"] is False  # -> download
    assert changed["report.txt"]["drive_size"] == 999
    assert changed["report.txt"]["local_size"] == 200


def test_same_name_same_size_different_md5_is_modified(tmp_path):
    """The nastiest case: byte counts match, contents do not."""
    write(tmp_path, "notes.md", b"hello world!!", mtime=DRIVE_EPOCH + 60)
    drive_meta = {"notes.md": blob("id1", "notes.md", ["ROOT"],
                                   size=13, md5=md5_of(b"HELLO WORLD!!"))}

    changed, _ = drive_sync.detect_changes({"notes.md"}, drive_meta, tmp_path)

    assert "notes.md" in changed
    assert changed["notes.md"]["reason"] == "md5"


def test_identical_files_are_not_flagged(tmp_path):
    data = b"identical bytes"
    write(tmp_path, "same.txt", data)
    drive_meta = {"same.txt": blob("id1", "same.txt", ["ROOT"],
                                   size=len(data), md5=md5_of(data))}
    changed, skipped = drive_sync.detect_changes({"same.txt"}, drive_meta, tmp_path)
    assert changed == {} and skipped == []


def test_identical_files_are_not_flagged_even_with_skewed_mtimes(tmp_path):
    """md5 match wins over a wildly different modifiedTime -- no false positives."""
    data = b"identical bytes"
    write(tmp_path, "same.txt", data, mtime=DRIVE_EPOCH + 999999)
    drive_meta = {"same.txt": blob("id1", "same.txt", ["ROOT"],
                                   size=len(data), md5=md5_of(data))}
    assert drive_sync.detect_changes({"same.txt"}, drive_meta, tmp_path)[0] == {}


# ── the rsync-style ladder ───────────────────────────────────────────────────

def test_md5_is_not_computed_when_size_already_differs(tmp_path, monkeypatch):
    """Rung 1 must short-circuit rung 2 -- hashing is the expensive step."""
    write(tmp_path, "big.bin", b"x" * 1000)
    drive_meta = {"big.bin": blob("id1", "big.bin", ["ROOT"], size=1,
                                  md5=md5_of(b"y"))}

    calls = []
    monkeypatch.setattr(drive_sync, "file_md5",
                        lambda *a, **k: calls.append(a) or "deadbeef")

    changed, _ = drive_sync.detect_changes({"big.bin"}, drive_meta, tmp_path)
    assert changed["big.bin"]["reason"] == "size"
    assert calls == [], "md5 must not be computed once size already settles it"


def test_md5_is_computed_only_for_size_equal_survivors(tmp_path):
    for i in range(5):
        write(tmp_path, f"f{i}.bin", b"x" * 10)
    drive_meta = {
        "f0.bin": blob("i0", "f0.bin", ["ROOT"], size=10, md5=md5_of(b"x" * 10)),
        "f1.bin": blob("i1", "f1.bin", ["ROOT"], size=11, md5="nope"),
        "f2.bin": blob("i2", "f2.bin", ["ROOT"], size=99, md5="nope"),
        "f3.bin": blob("i3", "f3.bin", ["ROOT"], size=10, md5="mismatch"),
        "f4.bin": blob("i4", "f4.bin", ["ROOT"], size=10, md5=md5_of(b"x" * 10)),
    }
    real = drive_sync.file_md5
    hashed = []

    def counting(path, *a, **k):
        hashed.append(str(path))
        return real(path, *a, **k)

    orig = drive_sync.file_md5
    drive_sync.file_md5 = counting
    try:
        changed, _ = drive_sync.detect_changes(set(drive_meta), drive_meta, tmp_path)
    finally:
        drive_sync.file_md5 = orig

    assert set(changed) == {"f1.bin", "f2.bin", "f3.bin"}
    assert changed["f1.bin"]["reason"] == "size"
    assert changed["f3.bin"]["reason"] == "md5"
    assert len(hashed) == 3, "only the three size-equal pairs should be hashed"


# ── Google Docs-native files ─────────────────────────────────────────────────

def test_google_native_file_is_skipped_not_crashed(tmp_path):
    """Docs/Sheets/Slides have no md5Checksum and no size -- must not blow up."""
    write(tmp_path, "Plan", b"local placeholder")
    drive_meta = {"Plan": gdoc("gid", "Plan", ["ROOT"])}

    changed, skipped = drive_sync.detect_changes({"Plan"}, drive_meta, tmp_path)

    assert changed == {}
    assert skipped == ["Plan"]


def test_google_native_detection():
    assert drive_sync.is_google_native({"mimeType": "application/vnd.google-apps.spreadsheet"})
    assert drive_sync.is_google_native({"mimeType": "application/vnd.google-apps.folder"})
    assert not drive_sync.is_google_native({"mimeType": "text/plain"})
    assert not drive_sync.is_google_native({})          # missing key -> not native


def test_binary_without_md5_falls_back_to_modified_time(tmp_path):
    """Rung 3: no checksum from Drive, sizes equal -> compare timestamps."""
    write(tmp_path, "odd.bin", b"1234567890", mtime=DRIVE_EPOCH + 7200)
    item = blob("id1", "odd.bin", ["ROOT"], size=10, md5=None)
    changed, skipped = drive_sync.detect_changes({"odd.bin"}, {"odd.bin": item}, tmp_path)
    assert changed["odd.bin"]["reason"] == "mtime"
    assert skipped == []


def test_mtime_fallback_tolerates_small_clock_skew(tmp_path):
    write(tmp_path, "odd.bin", b"1234567890", mtime=DRIVE_EPOCH + 1)
    item = blob("id1", "odd.bin", ["ROOT"], size=10, md5=None)
    assert drive_sync.detect_changes({"odd.bin"}, {"odd.bin": item}, tmp_path)[0] == {}


# ── malformed / hostile metadata ─────────────────────────────────────────────

def test_unparseable_size_falls_through_to_md5(tmp_path):
    write(tmp_path, "x.bin", b"abc")
    item = blob("id1", "x.bin", ["ROOT"], size=3, md5=md5_of(b"different"))
    item["size"] = "not-a-number"
    changed, _ = drive_sync.detect_changes({"x.bin"}, {"x.bin": item}, tmp_path)
    assert changed["x.bin"]["reason"] == "md5"


def test_bad_modified_time_does_not_crash(tmp_path):
    write(tmp_path, "x.bin", b"abc")
    item = blob("id1", "x.bin", ["ROOT"], size=99, md5=None, modified="garbage")
    changed, _ = drive_sync.detect_changes({"x.bin"}, {"x.bin": item}, tmp_path)
    assert changed["x.bin"]["reason"] == "size"
    assert changed["x.bin"]["local_newer"] is True   # no timestamp -> keep local


def test_missing_local_file_is_ignored(tmp_path):
    item = blob("id1", "ghost.txt", ["ROOT"], size=1, md5="x")
    assert drive_sync.detect_changes({"ghost.txt"}, {"ghost.txt": item}, tmp_path) == ({}, [])


def test_missing_drive_entry_is_ignored(tmp_path):
    write(tmp_path, "a.txt", b"a")
    assert drive_sync.detect_changes({"a.txt"}, {}, tmp_path) == ({}, [])


def test_file_md5_matches_hashlib(tmp_path):
    data = os.urandom(3_000_000)     # spans several read chunks
    p = write(tmp_path, "big.bin", data)
    assert drive_sync.file_md5(p) == md5_of(data)
