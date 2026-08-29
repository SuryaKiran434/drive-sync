"""Fix #3 -- get_or_create_folder: lru_cache instead of a mutable default arg."""
import inspect

import drive_sync
from fake_drive import FakeDrive, folder


def setup_function():
    drive_sync.get_or_create_folder.cache_clear()


def test_no_mutable_default_argument():
    sig = inspect.signature(drive_sync.get_or_create_folder.__wrapped__)
    assert list(sig.parameters) == ["service", "name", "parent_id"]
    assert all(p.default is inspect.Parameter.empty for p in sig.parameters.values()), \
        "a mutable default argument would live for the whole process and never clear"


def test_repeated_lookups_hit_the_cache():
    svc = FakeDrive([folder("d-docs", "docs", ["ROOT"])])
    for _ in range(5):
        assert drive_sync.get_or_create_folder(svc, "docs", "ROOT") == "d-docs"
    assert len(svc.list_calls) == 1, "only the first lookup should reach the API"
    assert drive_sync.get_or_create_folder.cache_info().hits == 4


def test_missing_folder_is_created_once():
    svc = FakeDrive([])
    fid = drive_sync.get_or_create_folder(svc, "new", "ROOT")
    assert drive_sync.get_or_create_folder(svc, "new", "ROOT") == fid
    assert len(svc.created) == 1
    assert svc.created[0]["mimeType"] == "application/vnd.google-apps.folder"
    assert svc.created[0]["parents"] == ["ROOT"]


def test_cache_is_keyed_on_parent_and_name():
    svc = FakeDrive([folder("a", "docs", ["ROOT"]), folder("b", "docs", ["OTHER"])])
    assert drive_sync.get_or_create_folder(svc, "docs", "ROOT") == "a"
    assert drive_sync.get_or_create_folder(svc, "docs", "OTHER") == "b"


def test_cache_clear_escape_hatch_re_queries():
    """The whole point of the fix: a folder deleted mid-run can be forgotten."""
    svc = FakeDrive([folder("d-old", "docs", ["ROOT"])])
    assert drive_sync.get_or_create_folder(svc, "docs", "ROOT") == "d-old"

    del svc.items["d-old"]                       # someone deleted it on Drive
    svc.items["d-new"] = folder("d-new", "docs", ["ROOT"])

    assert drive_sync.get_or_create_folder(svc, "docs", "ROOT") == "d-old"  # stale
    drive_sync.get_or_create_folder.cache_clear()
    assert drive_sync.get_or_create_folder(svc, "docs", "ROOT") == "d-new"


def test_cache_is_bounded():
    assert drive_sync.get_or_create_folder.cache_info().maxsize == 4096


def test_ensure_drive_path_walks_and_caches(monkeypatch):
    svc = FakeDrive([folder("d1", "a", ["ROOT"]), folder("d2", "b", ["d1"])])
    monkeypatch.setattr(drive_sync, "DRIVE_FOLDER_ID", "ROOT")
    assert drive_sync.ensure_drive_path(svc, "a/b/file.txt") == "d2"
    calls_after_first = len(svc.list_calls)
    assert drive_sync.ensure_drive_path(svc, "a/b/other.txt") == "d2"
    assert len(svc.list_calls) == calls_after_first, "second walk should be all cache"
