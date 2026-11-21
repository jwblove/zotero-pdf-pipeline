from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from zotero_pdf_pipeline.models import PaperNeed
from zotero_pdf_pipeline.plugins import load_source

ROOT = Path(__file__).resolve().parents[2]

@pytest.fixture
def plugin(monkeypatch):
    source = load_source('scihub', ROOT / 'plugins')
    module = sys.modules[source.__class__.__module__]
    monkeypatch.setenv('ZPP_SCIHUB_BASE_URL', 'https://mirror.invalid')
    monkeypatch.setenv('ZPP_SCIHUB_BROWSER', '1')
    monkeypatch.setattr(module, '_throttle', lambda: True)
    return source, module

class FakePage:
    url = 'https://mirror.invalid/10.1234/test'
    def __init__(self, html): self.html = html
    def goto(self, *args, **kwargs): pass
    def content(self): return self.html
    def wait_for_timeout(self, ms): pass

class FakeContext:
    def __init__(self, html, body):
        self.pages = [FakePage(html)]
        self.request = self
        self.body = body
        self.urls = []
    def route(self, *args): pass
    def get(self, url, **kwargs):
        self.urls.append(url)
        return SimpleNamespace(status=200, body=lambda: self.body)
    def close(self): pass

class FakePlaywright:
    def __init__(self, context):
        self.chromium = self
        self.context = context
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def launch_persistent_context(self, *args, **kwargs): return self.context

def setup_browser(monkeypatch, module, tmp_path, html, body):
    context = FakeContext(html, body)
    monkeypatch.setattr(module, '_browser_engine', lambda: lambda: FakePlaywright(context))
    monkeypatch.setattr(module, '_browser_executable', lambda p: (p.chromium, '/fake/chromium'))
    monkeypatch.setattr(module.Path, 'home', lambda: tmp_path)
    return context

def paper(key='A'):
    return PaperNeed(item_key=key, doi='10.1234/test', title='Test')

def test_browser_success(plugin, monkeypatch, tmp_path, assert_private):
    source, module = plugin
    browser = setup_browser(monkeypatch, module, tmp_path, '<iframe src="/pdf"></iframe>', b'%PDF-test')
    dest = tmp_path / 'destination'
    result = source.fetch(paper(), dest)
    assert result.parent == dest and result.read_bytes() == b'%PDF-test'
    assert browser.urls == ['https://mirror.invalid/pdf']
    assert_private(tmp_path / '.local/state/zotero-pipeline/scihub-profile')

def test_still_challenged(plugin, monkeypatch, tmp_path, caplog):
    source, module = plugin
    setup_browser(monkeypatch, module, tmp_path, '<altcha-widget></altcha-widget>', b'%PDF-test')
    clock = iter([0, 0, 31])
    monkeypatch.setattr(module.time, 'monotonic', lambda: next(clock, 31))
    assert source.fetch(paper(), tmp_path / 'dest') is None
    assert 'challenged or requires human verification' in caplog.text

def test_no_browser(plugin, monkeypatch, tmp_path, caplog):
    source, module = plugin
    monkeypatch.setattr(module, '_browser_engine', lambda: lambda: FakePlaywright(None))
    monkeypatch.setattr(module, '_browser_executable', lambda p: None)
    assert source.fetch(paper(), tmp_path / 'dest') is None
    assert 'no Chromium or Firefox browser executable found' in caplog.text

def test_non_pdf_rejected(plugin, monkeypatch, tmp_path):
    source, module = plugin
    setup_browser(monkeypatch, module, tmp_path, '<embed src="/file">', b'not a PDF')
    assert source.fetch(paper(), tmp_path / 'dest') is None
    assert not (tmp_path / 'dest').exists()

def test_destination_confined(plugin, monkeypatch, tmp_path):
    source, module = plugin
    setup_browser(monkeypatch, module, tmp_path, '<embed src="/file">', b'%PDF-test')
    dest = tmp_path / 'dest'
    dest.mkdir()
    outside = tmp_path / 'outside'
    outside.write_bytes(b'untouched')
    (dest / '___escape-candidate.pdf').symlink_to(outside)
    # Raw metadata exercises the plugin boundary; PaperNeed rejects this key earlier.
    unsafe_item = SimpleNamespace(item_key='../escape', doi='10.1234/test', title='Test')
    assert source.fetch(unsafe_item, dest) is None
    assert outside.read_bytes() == b'untouched'

def test_download_anchor_after_invalid_candidates(plugin, monkeypatch, tmp_path):
    source, module = plugin
    html = ('<meta name><iframe></iframe><embed src="https://[bad">'
            '<a href="/">Home</a><a download href="/viewer?file=%2Fdownload">PDF</a>')
    browser = setup_browser(monkeypatch, module, tmp_path, html, b'%PDF-test')
    assert source.fetch(paper(), tmp_path / 'dest').read_bytes() == b'%PDF-test'
    assert browser.urls == ['https://mirror.invalid/download']


@pytest.mark.parametrize('mode', ['browser', 'http-direct', 'http-link'])
def test_pdf_with_captcha_title(plugin, monkeypatch, tmp_path, mode):
    from io import BytesIO
    from pypdf import PdfWriter

    source, module = plugin
    doc = PdfWriter()
    doc.add_blank_page(width=72, height=72)
    doc.add_metadata({'/Title': 'Research on CAPTCHA'})
    stream = BytesIO()
    doc.write(stream)
    pdf = stream.getvalue()
    html = '<embed src="/file">'
    if mode == 'browser':
        setup_browser(monkeypatch, module, tmp_path, html, pdf)
    else:
        monkeypatch.delenv('ZPP_SCIHUB_BROWSER')
        bodies = iter([html.encode(), pdf] if mode == 'http-link' else [pdf])
        def request(client, url):
            return SimpleNamespace(content=next(bodies), text=html, url=url)
        monkeypatch.setattr(module, '_request', request)
    assert source.fetch(paper(), tmp_path / 'dest').read_bytes() == pdf


def test_incomplete_write_preserves_existing_candidate(plugin, monkeypatch, tmp_path):
    _, module = plugin
    original = module._save(b'%PDF-original', 'A', tmp_path)
    fdopen = module.os.fdopen

    def failing_writer(fd, mode):
        stream = fdopen(fd, mode)
        def fail(data):
            stream.raw.write(data[:3])
            raise OSError('simulated disk full')
        stream.write = fail
        return stream

    monkeypatch.setattr(module.os, 'fdopen', failing_writer)
    with pytest.raises(OSError, match='disk full'):
        module._save(b'%PDF-replacement', 'A', tmp_path)
    assert original.read_bytes() == b'%PDF-original'
    assert list(tmp_path.iterdir()) == [original]
