"""Fix #1 -- round trips: flat/BFS listing, pagination, orphans, multi-parent."""
import pytest

import drive_sync
from fake_drive import FakeDrive, blob, folder, legacy_list_drive_files, FOLDER_MIME


def representative_tree():
    """root/
         a.txt
         b.bin
         docs/            docs/notes.md, docs/spec.pdf
           img/           docs/img/logo.png
         empty/
         deep/l1/l2/      deep/l1/l2/leaf.txt
    """
    return [
        blob("f-a", "a.txt", ["ROOT"]),
        blob("f-b", "b.bin", ["ROOT"]),
        folder("d-docs", "docs", ["ROOT"]),
        blob("f-notes", "notes.md", ["d-docs"]),
        blob("f-spec", "spec.pdf", ["d-docs"]),
        folder("d-img", "img", ["d-docs"]),
        blob("f-logo", "logo.png", ["d-img"]),
        folder("d-empty", "empty", ["ROOT"]),
        folder("d-l1", "l1", ["ROOT"]),
        folder("d-l1x", "deep", ["ROOT"]),
        folder("d-l2", "l2", ["d-l1x"]),
        blob("f-leaf", "leaf.txt", ["d-l2"]),
    ]


EXPECTED = {
    "a.txt": "f-a",
    "b.bin": "f-b",
    "docs/notes.md": "f-notes",
    "docs/spec.pdf": "f-spec",
    "docs/img/logo.png": "f-logo",
    "deep/l2/leaf.txt": "f-leaf",
}


def test_matches_legacy_recursive_walk():
    """The whole point: identical {path: id} map, far fewer round trips."""
    new = drive_sync.list_drive_files(FakeDrive(representative_tree()), "ROOT")
    old = legacy_list_drive_files(FakeDrive(representative_tree()), "ROOT")
    assert new == old == EXPECTED


def test_return_contract_is_path_to_id():
    out = drive_sync.list_drive_files(FakeDrive(representative_tree()), "ROOT")
    assert isinstance(out, dict)
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in out.items())


def test_prefix_argument_still_honoured():
    out = drive_sync.list_drive_files(FakeDrive(representative_tree()), "ROOT", "backup/")
    assert out["backup/a.txt"] == "f-a"
    assert out["backup/docs/img/logo.png"] == "f-logo"


def test_page_size_is_api_maximum():
    svc = FakeDrive(representative_tree())
    drive_sync.list_drive_files(svc, "ROOT")
    assert svc.list_calls, "no calls made"
    assert all(c.get("pageSize") == 1000 for c in svc.list_calls), \
        "files.list must request the API maximum, not the default 100"


def test_fields_mask_carries_content_signals():
    svc = FakeDrive(representative_tree())
    drive_sync.list_drive_files(svc, "ROOT")
    for c in svc.list_calls:
        for want in ("id", "name", "mimeType", "parents",
                     "md5Checksum", "size", "modifiedTime"):
            assert want in c["fields"], f"{want} missing from fields mask"


def test_round_trips_collapse_versus_recursion():
    """Wide, flat tree: 40 sibling folders each holding a file.

    Old walk: 1 call for the root + 1 per folder = 41 sequential round trips.
    New walk: one call per depth level, parents OR-ed together and chunked.
    """
    items = []
    for i in range(40):
        items.append(folder(f"d{i}", f"dir{i}", ["ROOT"]))
        items.append(blob(f"f{i}", f"file{i}.txt", [f"d{i}"]))

    new_svc, old_svc = FakeDrive(items), FakeDrive(items)
    assert drive_sync.list_drive_files(new_svc, "ROOT") == \
        legacy_list_drive_files(old_svc, "ROOT")

    assert len(old_svc.list_calls) == 41
    # level 0 (root) = 1 call, level 1 = ceil(40 / PARENTS_PER_QUERY) = 2 calls
    assert len(new_svc.list_calls) == 3
    assert len(new_svc.list_calls) < len(old_svc.list_calls) / 10


def test_deep_chain_costs_one_call_per_level():
    items, parent = [], "ROOT"
    for i in range(6):
        items.append(folder(f"d{i}", f"lvl{i}", [parent]))
        parent = f"d{i}"
    items.append(blob("f", "bottom.txt", [parent]))

    svc = FakeDrive(items)
    out = drive_sync.list_drive_files(svc, "ROOT")
    assert out == {"lvl0/lvl1/lvl2/lvl3/lvl4/lvl5/bottom.txt": "f"}
    assert len(svc.list_calls) == 7   # 6 folder levels + the leaf level


# ── pagination ───────────────────────────────────────────────────────────────

def test_pagination_across_multiple_pages():
    items = [blob(f"f{i:04d}", f"file{i:04d}.txt", ["ROOT"]) for i in range(250)]
    svc = FakeDrive(items, page_size_cap=100)     # force 3 pages
    out = drive_sync.list_drive_files(svc, "ROOT")

    assert len(out) == 250
    assert out["file0000.txt"] == "f0000"
    assert out["file0249.txt"] == "f0249"
    assert len(svc.list_calls) == 3
    assert [c.get("pageToken") for c in svc.list_calls] == [None, "100", "200"]


def test_pagination_inside_a_subfolder():
    items = [folder("d", "bulk", ["ROOT"])]
    items += [blob(f"f{i:04d}", f"f{i:04d}.txt", ["d"]) for i in range(120)]
    svc = FakeDrive(items, page_size_cap=50)
    out = drive_sync.list_drive_files(svc, "ROOT")
    assert len(out) == 120
    assert out["bulk/f0119.txt"] == "f0119"


def test_pagination_agrees_with_legacy_walk():
    items = [folder("d", "bulk", ["ROOT"])]
    items += [blob(f"f{i:04d}", f"f{i:04d}.txt", ["d"]) for i in range(120)]
    assert drive_sync.list_drive_files(FakeDrive(items, page_size_cap=50), "ROOT") == \
        legacy_list_drive_files(FakeDrive(items, page_size_cap=50), "ROOT")


# ── orphans and multi-parent files ───────────────────────────────────────────

def test_orphan_outside_root_is_excluded_not_crashed():
    items = representative_tree() + [
        blob("f-out", "elsewhere.txt", ["SOME-OTHER-FOLDER"]),
        folder("d-out", "otherdir", ["SOME-OTHER-FOLDER"]),
        blob("f-out2", "buried.txt", ["d-out"]),
    ]
    out = drive_sync.list_drive_files(FakeDrive(items), "ROOT")
    assert out == EXPECTED
    assert "elsewhere.txt" not in out


def test_file_with_no_parents_at_all_does_not_crash():
    items = representative_tree() + [
        {"id": "f-orphan", "name": "nowhere.txt",
         "mimeType": "application/octet-stream", "parents": []},
    ]
    # The fake never returns it (no parent matches), but a real API could echo
    # a parentless item back; the lister must simply drop it.
    out = drive_sync.list_drive_files(FakeDrive(items), "ROOT")
    assert out == EXPECTED


def test_multi_parent_file_appears_under_every_in_scope_parent():
    items = representative_tree() + [
        blob("f-multi", "shared.txt", ["ROOT", "d-docs"]),
    ]
    out = drive_sync.list_drive_files(FakeDrive(items), "ROOT")
    assert out["shared.txt"] == "f-multi"
    assert out["docs/shared.txt"] == "f-multi"


def test_multi_parent_with_one_parent_outside_root_keeps_only_the_in_scope_path():
    items = representative_tree() + [
        blob("f-half", "half.txt", ["d-docs", "SOME-OTHER-FOLDER"]),
    ]
    out = drive_sync.list_drive_files(FakeDrive(items), "ROOT")
    assert out["docs/half.txt"] == "f-half"
    assert "half.txt" not in out          # the out-of-scope parent contributes nothing


def test_parent_cycle_terminates():
    """Drive forbids this, but a malformed response must not hang the tool."""
    items = [
        folder("a", "a", ["ROOT", "b"]),
        folder("b", "b", ["a"]),
        blob("f", "x.txt", ["b"]),
    ]
    out = drive_sync.list_drive_files(FakeDrive(items), "ROOT")
    assert out == {"a/b/x.txt": "f"}


def test_trashed_files_are_excluded():
    items = representative_tree()
    items.append(dict(blob("f-trash", "gone.txt", ["ROOT"]), trashed=True))
    assert drive_sync.list_drive_files(FakeDrive(items), "ROOT") == EXPECTED


def test_empty_folder_yields_nothing():
    svc = FakeDrive([folder("d", "empty", ["ROOT"])])
    assert drive_sync.list_drive_files(svc, "ROOT") == {}


def test_meta_listing_exposes_content_signals():
    meta = drive_sync.list_drive_files_meta(FakeDrive(representative_tree()), "ROOT")
    assert set(meta) == set(EXPECTED)
    assert meta["a.txt"]["id"] == "f-a"
    assert "size" in meta["a.txt"] and "modifiedTime" in meta["a.txt"]
