import json
import logging
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from zotero_pdf_pipeline.models import PaperNeed
from zotero_pdf_pipeline.plugins import load_source

ROOT = Path(__file__).resolve().parents[2]
URL = 'https://www.ablesci.com/assist/detail?id=ABC'

class FakeClient:
    def __init__(self):
        self.requests = []
        self.assists = []
        self.closed = []
        self.mode = 'success'
    def unfinished_count(self):
        return sum(not a['finished'] for a in self.assists)
    def my_assists(self):
        return self.assists
    def close_assist(self, url, note):
        self.closed.append((url, note))
        return self.mode != 'close_fail'
    def fetch_paper(self, paper, dest, *args, resume_detail_url, on_published, **kwargs):
        self.requests.append(resume_detail_url)
        if not resume_detail_url:
            on_published(URL)
        if self.mode in ('give_up', 'close_fail'):
            raise self.error('无人应助')
        if self.mode == 'fail':
            raise self.error('sensitive-email sensitive-password')
        result = dest / 'candidate.pdf'
        result.write_bytes(b'%PDF-test')
        return SimpleNamespace(pdf_path=result)

@pytest.fixture
def setup(monkeypatch, tmp_path):
    source = load_source('ablesci', ROOT / 'plugins')
    module = sys.modules[source.__class__.__module__]
    client = FakeClient()
    client.error = module.AbleSciError
    monkeypatch.setattr(module, '_client', lambda: client)
    monkeypatch.setattr(module, '_STATE_PATH', tmp_path / 'state' / 'ablesci.json')
    monkeypatch.setattr(module, '_PUBLISHED', 0)
    monkeypatch.setenv('ZPP_ABLESCI_MAX_POINTS_PER_RUN', '12')
    return source, module, client

@pytest.fixture
def paper():
    return PaperNeed(item_key='A', doi='10.1234/test', title='A Test Paper')

def test_success(setup, paper, tmp_path):
    source, module, client = setup
    assert source.fetch(paper, tmp_path).read_bytes() == b'%PDF-test'
    assert client.requests == [None]
    assert json.loads(module._STATE_PATH.read_text()) == {}

def test_resume_and_permission(setup, paper, tmp_path):
    source, module, client = setup
    client.mode = 'fail'
    assert source.fetch(paper, tmp_path) is None
    state = json.loads(module._STATE_PATH.read_text())
    assert state[module._state_key(paper)]['detail_url'] == URL
    assert set(state[module._state_key(paper)]) == {'detail_url', 'timestamp'}
    assert stat.S_IMODE(module._STATE_PATH.stat().st_mode) == 0o600
    client.mode = 'success'
    assert source.fetch(paper, tmp_path).exists()
    assert client.requests == [None, URL]

def test_give_up_closes(setup, paper, tmp_path):
    source, module, client = setup
    client.mode = 'give_up'
    assert source.fetch(paper, tmp_path) is None
    assert client.closed == [(URL, '暂时无人应助')]
    assert json.loads(module._STATE_PATH.read_text()) == {}

def test_failed_close_preserves_state(setup, paper, tmp_path):
    source, module, client = setup
    client.mode = 'close_fail'
    assert source.fetch(paper, tmp_path) is None
    assert json.loads(module._STATE_PATH.read_text())

def test_other_unfinished_not_touched(setup, paper, tmp_path):
    source, module, client = setup
    client.assists = [{'id': 'OTHER', 'title': 'Other paper', 'finished': False}]
    assert source.fetch(paper, tmp_path) is None
    assert not client.requests and not client.closed

def test_matching_unfinished_takeover(setup, paper, tmp_path):
    source, module, client = setup
    client.assists = [{'id': 'ABC', 'title': 'A Test Paper', 'finished': False}]
    assert source.fetch(paper, tmp_path).exists()
    assert client.requests == [URL]

def test_budget_blocks_second_publish(setup, paper, tmp_path):
    source, module, client = setup
    assert source.fetch(paper, tmp_path)
    other = PaperNeed(item_key='B', doi='10.1234/other', title='Other')
    assert source.fetch(other, tmp_path) is None
    assert client.requests == [None]

def test_redaction(setup, paper, tmp_path, caplog):
    source, module, client = setup
    client.mode = 'fail'
    with caplog.at_level(logging.DEBUG):
        assert source.fetch(paper, tmp_path) is None
    assert 'sensitive-email' not in caplog.text
    assert 'sensitive-password' not in caplog.text

def test_real_loader():
    assert load_source('ablesci', ROOT / 'plugins').name == 'ablesci'
    assert load_source('scihub', ROOT / 'plugins').name == 'scihub'

def test_insecure_state_filesystem_blocks_publication(setup, paper, tmp_path, monkeypatch):
    source, module, client = setup
    monkeypatch.setattr(module.stat, 'S_IMODE', lambda mode: 0o777)
    assert source.fetch(paper, tmp_path) is None
    assert client.requests == []


def test_conflicting_doi_prevents_takeover(setup, paper, tmp_path):
    source, _, client = setup
    client.assists = [dict(id='ABC', title=paper.title, doi='10.1234/other', finished=False)]
    assert source.fetch(paper, tmp_path) is None
    assert not client.requests and not client.closed


def test_ablesci_preserves_existing_staged_file(setup, paper, tmp_path):
    from zotero_pdf_pipeline.plugins import fetch_plugin

    original = tmp_path / 'candidate.pdf'
    original.write_bytes(b'original')
    assert fetch_plugin('ablesci', ROOT / 'plugins', paper, tmp_path)
    assert original.read_bytes() == b'original'
