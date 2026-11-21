import json
import logging
import stat
import sys
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from zotero_pdf_pipeline.models import PaperNeed
from zotero_pdf_pipeline.plugins import load_source


ROOT = Path(__file__).resolve().parents[2]
DETAIL = "https://example.invalid/detail?id=123"


@pytest.fixture
def case(tmp_path, monkeypatch):
    source = load_source("ablesci", ROOT / "plugins")
    module = sys.modules[source.__class__.__module__]

    class FakeClient:
        email = "fake-email-secret"
        password = "fake-password-secret"
        base = "https://example.invalid"
        timeout = 1

        def __init__(self):
            self.channels = []
            self.fetches = 0
            self.opener = SimpleNamespace(open=lambda *a, **kw: BytesIO(b"not a pdf"))

        def unfinished_count(self):
            return 0

        def _request(self, *args, **kwargs):
            config = {"hashid": "fake", "normalServers": [{"id": "2"}]}
            return BytesIO(("const config = " + json.dumps(config) + ";").encode())

        def _request_download_token(self, token_url, hashid, server_id, referer, channel="normal"):
            self.channels.append(channel)
            return {"host": "https://example.invalid/file", "token": "fake-token-secret"}

        def _is_allowed_host(self, *args):
            return True

        download_file = module.AbleSciClient.download_file

        def fetch_paper(self, paper, dest, *args, resume_detail_url, on_published, **kwargs):
            self.fetches += 1
            if not resume_detail_url:
                on_published(DETAIL)
            for _ in range(2):
                try:
                    self.download_file(DETAIL, {"file_id": "fake"}, dest / "test.pdf")
                except module.AbleSciError:
                    pass
            raise module.AbleSciError("failed")

    client = FakeClient()
    monkeypatch.setattr(module, "_client", lambda: client)
    monkeypatch.setattr(module, "_CLIENT", client)
    monkeypatch.setattr(module, "_STATE_PATH", tmp_path / "state" / "ablesci.json")
    monkeypatch.setattr(module, "_PUBLISHED", 0)
    monkeypatch.setenv("ZPP_ABLESCI_MAX_POINTS_PER_RUN", "12")
    paper = PaperNeed(item_key="A", doi="10.1234/test", title="Test")
    return source, module, client, paper


def test_retry_reservation_and_resume(case, tmp_path, caplog):
    source, module, client, paper = case
    with caplog.at_level(logging.DEBUG):
        assert source.fetch(paper, tmp_path) is None
        state = json.loads(module._STATE_PATH.read_text())
        assert state[module._state_key(paper)]["highspeed_attempt_reserved"] is True
        assert stat.S_IMODE(module._STATE_PATH.stat().st_mode) == 0o600
        assert client.channels == ["highspeed", "normal", "normal"]
        assert "_request_download_token" not in vars(client)
        assert source.fetch(paper, tmp_path) is None
    assert client.channels == ["highspeed", "normal", "normal", "normal", "normal"]
    for secret in (client.email, client.password, "fake-token-secret"):
        assert secret not in module._STATE_PATH.read_text()
        assert secret not in caplog.text


def test_existing_reservation_skips_highspeed(case, tmp_path):
    source, module, client, paper = case
    module._write_state({module._state_key(paper): {
        "detail_url": DETAIL, "highspeed_attempt_reserved": True,
    }})
    assert source.fetch(paper, tmp_path) is None
    assert client.channels == ["normal", "normal"]


def test_reservation_write_failure_stops_before_request(case, tmp_path, monkeypatch):
    source, module, client, paper = case
    write = module._write_state

    def fail_reservation(state):
        if state.get(module._state_key(paper), {}).get("highspeed_attempt_reserved"):
            raise OSError("disk error")
        write(state)

    monkeypatch.setattr(module, "_write_state", fail_reservation)
    assert source.fetch(paper, tmp_path) is None
    assert client.channels == []
    assert "_request_download_token" not in vars(client)


def test_reservations_do_not_leak_between_papers(case, tmp_path, monkeypatch):
    source, module, client, paper = case
    monkeypatch.setenv("ZPP_ABLESCI_MAX_POINTS_PER_RUN", "24")
    assert source.fetch(paper, tmp_path) is None
    other = PaperNeed(item_key="B", doi="10.1234/other", title="Other")
    assert source.fetch(other, tmp_path) is None
    assert client.channels.count("highspeed") == 2
    assert client.channels.count("normal") == 4
    state = json.loads(module._STATE_PATH.read_text())
    assert state[module._state_key(paper)]["highspeed_attempt_reserved"] is True
    assert state[module._state_key(other)]["highspeed_attempt_reserved"] is True


@pytest.mark.parametrize("budget, expected_fetches", [(12, 1), (11, 0)])
def test_publication_budget(case, tmp_path, monkeypatch, budget, expected_fetches):
    source, module, client, paper = case
    monkeypatch.setenv("ZPP_ABLESCI_MAX_POINTS_PER_RUN", str(budget))
    assert source.fetch(paper, tmp_path) is None
    assert client.fetches == expected_fetches
