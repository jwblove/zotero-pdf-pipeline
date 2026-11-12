"""Local Zotero pagination, without connecting to Zotero."""

import pytest

from zotero_pdf_pipeline.zotero_local import ZoteroLocal, ZoteroLocalError


def rows(count, item_type="journalArticle"):
    return [
        {"key": f"K{i:07d}", "data": {"key": f"K{i:07d}", "itemType": item_type}}
        for i in range(count)
    ]


def fake_local(monkeypatch, responses):
    local = ZoteroLocal.__new__(ZoteroLocal)
    calls = []

    def get(path, params=None):
        calls.append((path, params))
        # As in the local API, offset is ignored; only start moves the page.
        start = params.get("start", 0)
        return responses[path][start:start + params["limit"]]

    monkeypatch.setattr(local, "_get", get)
    return local, calls


def test_collection_reads_all_170_unique_items(monkeypatch):
    expected = rows(170)
    local, calls = fake_local(monkeypatch, {"/collections/C/items": expected})

    result = local.items_in_collection("C")

    assert result == expected
    assert len({row["key"] for row in result}) == 170
    assert [params for _, params in calls] == [
        {"limit": 100, "start": 0}, {"limit": 100, "start": 100}
    ]


@pytest.mark.parametrize("method,path,page_size", [
    ("items_in_collection", "/collections/C/items", 100),
    ("attachments_of", "/items/C/children", 25),
])
def test_repeated_full_page_raises(monkeypatch, method, path, page_size):
    local = ZoteroLocal.__new__(ZoteroLocal)
    calls = []
    page = rows(page_size)

    def get(actual_path, params):
        assert actual_path == path
        calls.append(params)
        return page

    monkeypatch.setattr(local, "_get", get)
    with pytest.raises(ZoteroLocalError, match="Repeated .* page"):
        getattr(local, method)("C")
    assert [params["start"] for params in calls] == [0, page_size]


def test_children_include_pdf_after_first_page_and_filter_other_types(monkeypatch):
    children = rows(25, "note") + rows(1, "attachment")
    children[-1]["data"]["contentType"] = "application/pdf"
    local, calls = fake_local(monkeypatch, {"/items/C/children": children})

    assert local.attachments_of("C") == children[25:]
    assert [params["start"] for _, params in calls] == [0, 25]


@pytest.mark.parametrize("method,invalid", [
    ("items_in_collection", {}),
    ("items_in_collection", ["invalid"]),
    ("attachments_of", {"data": {}}),
    ("attachments_of", [{"data": []}]),
])
def test_invalid_page_raises(monkeypatch, method, invalid):
    local = ZoteroLocal.__new__(ZoteroLocal)
    monkeypatch.setattr(local, "_get", lambda path, params: invalid)
    with pytest.raises(ZoteroLocalError, match="Invalid .* response"):
        getattr(local, method)("C")
