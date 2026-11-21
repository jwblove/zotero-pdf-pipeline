import importlib.util
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

spec = importlib.util.spec_from_file_location(
    "test_local_scidownl_adapter", Path(__file__).resolve().parents[2] / "plugins/scidownl.py"
)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)
BASE = "https://mirror.example"
PAPER = SimpleNamespace(doi="10.1234/paper", item_key="ABCDEFGH")


@pytest.fixture
def source(monkeypatch):
    monkeypatch.delenv("ZPP_SCIHUB_BASE_URLS", raising=False)
    monkeypatch.setenv("ZPP_SCIHUB_BASE_URL", BASE)
    monkeypatch.setattr(adapter.time, "sleep", lambda _: None)
    return adapter.Source()


def simulate(monkeypatch, routes):
    calls = []
    def request(url, **kwargs):
        calls.append(url)
        assert kwargs["_retry"] is False
        content = routes[url]
        if isinstance(content, Exception):
            raise content
        return httpx.Response(200, request=httpx.Request("GET", url), content=content)
    monkeypatch.setattr(adapter, "request", request)
    return calls


def test_resolve_pdf_element(monkeypatch, source, tmp_path):
    calls = simulate(monkeypatch, {BASE + "/10.1234/paper": b'<embed id="pdf" src="/paper.pdf#page=1">', BASE + "/paper.pdf": b"%PDF-1.5"})
    assert source.fetch(PAPER, tmp_path).read_bytes() == b"%PDF-1.5"
    assert calls == [BASE + "/10.1234/paper", BASE + "/paper.pdf"]


@pytest.mark.parametrize("body", [b'<iframe id="pdf" src="https://other.example/a.pdf">', b'<p>no PDF</p>'])
def test_no_download(monkeypatch, source, tmp_path, body):
    calls = simulate(monkeypatch, {BASE + "/10.1234/paper": body})
    assert source.fetch(PAPER, tmp_path) is None
    assert len(calls) == 1 and not list(tmp_path.iterdir())


@pytest.mark.parametrize("response", [b'<div class="cf-challenge"></div>', adapter.PublicHTTPError("HTTP 429; host cooldown active")])
def test_stop_for_remaining_batch(monkeypatch, source, tmp_path, response):
    calls = simulate(monkeypatch, {BASE + "/10.1234/paper": response})
    assert source.fetch(PAPER, tmp_path) is None
    assert source.fetch(PAPER, tmp_path) is None
    assert len(calls) == 1 and not list(tmp_path.iterdir())


def test_reject_html_result(monkeypatch, source, tmp_path):
    simulate(monkeypatch, {BASE + "/10.1234/paper": b'<iframe id="pdf" src="/paper.pdf">', BASE + "/paper.pdf": b"<html>not a PDF</html>"})
    assert source.fetch(PAPER, tmp_path) is None
    assert not list(tmp_path.iterdir())


def test_invalid_mirror_list_does_not_fall_back_to_defaults(monkeypatch, source, tmp_path):
    monkeypatch.setenv("ZPP_SCIHUB_BASE_URLS", "http://invalid.example,https://invalid.example/?query=yes")
    calls = simulate(monkeypatch, {})
    assert source.fetch(PAPER, tmp_path) is None
    assert not calls


def test_default_mirrors(monkeypatch, source):
    monkeypatch.delenv("ZPP_SCIHUB_BASE_URL")
    assert source._mirrors() == ("https://sci-hub.ru", "https://sci-hub.su")


def test_fallback_after_missing_pdf(monkeypatch, source, tmp_path):
    backup = "https://backup.example"
    monkeypatch.setenv("ZPP_SCIHUB_BASE_URLS", f"{BASE},{BASE}/duplicate,{backup}")
    calls = simulate(monkeypatch, {
        BASE + "/10.1234/paper": b"<p>not found</p>",
        backup + "/10.1234/paper": b"%PDF-1.5",
    })
    assert source.fetch(PAPER, tmp_path).read_bytes() == b"%PDF-1.5"
    assert calls == [BASE + "/10.1234/paper", backup + "/10.1234/paper"]


@pytest.mark.parametrize("response", [
    b'<div class="cf-challenge"></div>',
    adapter.PublicHTTPError("HTTP 403"),
])
def test_protection_never_falls_back(monkeypatch, source, tmp_path, response):
    monkeypatch.setenv("ZPP_SCIHUB_BASE_URLS", f"{BASE},https://backup.example")
    calls = simulate(monkeypatch, {BASE + "/10.1234/paper": response})
    assert source.fetch(PAPER, tmp_path) is None
    assert calls == [BASE + "/10.1234/paper"]
