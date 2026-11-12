"""Offline safety and source integration tests."""
from pathlib import Path
from types import SimpleNamespace
import httpx
import pytest
from zotero_pdf_pipeline import cli, free_sources as free
from zotero_pdf_pipeline.attach import Attacher, to_windows_path
from zotero_pdf_pipeline.config import load_config
from zotero_pdf_pipeline.models import PaperNeed, ResolvedFile, Source
from zotero_pdf_pipeline.state import Store

@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('[zotero]\ncollection_key="COLLECTION"\n[paths]\narchive_dir="archive"\n[plugins]\nenabled=["fake"]\n')
    config = load_config(path)
    config.plugins.directory = tmp_path / 'plugins'
    config.plugins.directory.mkdir()
    config.fetch.source_order = []
    return config

class FakeLocal:
    def __init__(self, *args): pass
    def items_without_attachment(self, key):
        return [PaperNeed('K1', 'A test paper', '10.1234/test'), PaperNeed('OTHER', 'Other paper')]
    def creators_of(self, key): return ['Test']
    def close(self): pass

@pytest.mark.parametrize('command', ['fetch','attach','run'])
def test_only_required(command):
    with pytest.raises(SystemExit): cli.build_parser().parse_args([command])
    with pytest.raises(SystemExit): cli.build_parser().parse_args([command, '--only'])

@pytest.mark.parametrize('command', ['fetch','run'])
def test_free_only_rejects_plugin_flag(command):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([command, '--only', 'K1', '--free-only', '--plugin', 'fake'])

def test_free_only_never_loads_plugins(monkeypatch, cfg):
    monkeypatch.setattr(cli, 'ZoteroLocal', FakeLocal)
    (cfg.plugins.directory / 'fake.py').write_text('raise RuntimeError("must not load")')
    store = Store(cfg.paths.state_db)
    try:
        assert cli.cmd_fetch(cfg, store, ['K1'], free_only=True) == 1
        assert store.get('OTHER') is None
    finally: store.close()

def test_plugin_enabled_explicitly_and_validated(monkeypatch, cfg):
    monkeypatch.setattr(cli, 'ZoteroLocal', FakeLocal)
    (cfg.plugins.directory / 'fake.py').write_text('''from pathlib import Path\nclass Fake:\n name="fake"\n def fetch(self, item_meta, dest_dir):\n  p=dest_dir / "candidate.pdf"\n  p.write_bytes(b"<html>bad</html>")\n  return p\nsource=Fake()\n''')
    store = Store(cfg.paths.state_db)
    try:
        assert cli.cmd_fetch(cfg, store, ['K1'], plugin_names=['fake']) == 1
        row = store.get('K1')
        assert row['stage'] == 'failed'
        assert Path(row['file_path']).parent.name == 'staging-rejected'
        assert not list(cfg.paths.archive_dir.iterdir())
        assert store.get('OTHER') is None
    finally: store.close()

def test_plugin_refuses_not_enabled(monkeypatch, cfg):
    monkeypatch.setattr(cli, 'ZoteroLocal', FakeLocal)
    store = Store(cfg.paths.state_db)
    try: assert cli.cmd_fetch(cfg, store, ['K1'], plugin_names=['unknown']) == 2
    finally: store.close()

def test_inbox_matches_unique_doi_and_preserves_original(monkeypatch, cfg):
    path = cfg.paths.inbox_dir / 'paper.pdf'; path.write_bytes(b'%PDF-mock')
    monkeypatch.setattr('zotero_pdf_pipeline.validate.pdf_pages_and_text', lambda p: (1, '10.1234/test'))
    result = free.try_inbox(PaperNeed('K1', 'A paper', '10.1234/test'), cfg.paths.staging_dir, cfg.paths.inbox_dir, .55)
    assert result.source == Source.INBOX and result.file_path.read_bytes() == path.read_bytes()
    (cfg.paths.inbox_dir / 'second.pdf').write_bytes(b'%PDF-mock')
    with pytest.raises(free.FreeSourceError):
        free.try_inbox(PaperNeed('K1', 'A paper', '10.1234/test'), cfg.paths.staging_dir, cfg.paths.inbox_dir, .55)

def test_inbox_title_match(monkeypatch, cfg):
    (cfg.paths.inbox_dir / 'paper.pdf').write_bytes(b'%PDF-mock')
    monkeypatch.setattr('zotero_pdf_pipeline.validate.pdf_pages_and_text', lambda p: (1, 'The title of a test paper'))
    assert free.try_inbox(PaperNeed('K1', 'The title of a test paper'), cfg.paths.staging_dir, cfg.paths.inbox_dir, .55)

def mock_http(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(free, 'make_client', lambda **kwargs: httpx.Client(transport=transport, trust_env=False, **kwargs))
    monkeypatch.setattr(free, '_rate_limit', lambda url: None)

@pytest.mark.parametrize('source', ['openalex', 'unpaywall'])
def test_contact_missing_skips_without_network(monkeypatch, tmp_path, source):
    monkeypatch.setattr(free, '_request', lambda *a, **k: pytest.fail('network must not be called'))
    diag=[]
    assert free.try_all(PaperNeed('K1','Test','10.1234/x'), tmp_path, [source], False, diagnostics=diag) is None
    assert '邮箱' in diag[0]

@pytest.mark.parametrize('source,metadata', [
    ('semantic_scholar', {'openAccessPdf': {'url': 'https://pdf.invalid/test'}}),
    ('europe_pmc', {'resultList': {'result': [{'doi': '10.1234/test', 'fullTextUrlList': {'fullTextUrl': [{'documentStyle':'pdf', 'availability':'Open access', 'url':'https://pdf.invalid/test'}]}}]}})])
def test_extra_sources_mocked(monkeypatch, tmp_path, source, metadata):
    def handler(request):
        if request.url.host == 'pdf.invalid': return httpx.Response(200, content=b'%PDF-mock')
        return httpx.Response(200, json=metadata)
    mock_http(monkeypatch, handler)
    result = free.try_all(PaperNeed('K1','Test','10.1234/test'), tmp_path, [source], False)
    assert result and result.file_path.read_bytes() == b'%PDF-mock'

def test_non_pdf_and_redacted_diagnostics(monkeypatch, tmp_path):
    def handler(request):
        if request.url.host == 'api.openalex.org':
            return httpx.Response(200, json={'best_oa_location': {'pdf_url': 'https://pdf.invalid/x?token=secret'}})
        return httpx.Response(200, content=b'<html>no</html>')
    mock_http(monkeypatch, handler)
    diag=[]
    assert free.try_all(PaperNeed('K1','Test','10.1234/test'), tmp_path, ['openalex'], False, diagnostics=diag, contact_email='contact@institute.invalid') is None
    assert '不是 PDF' in diag[0] and 'secret' not in diag[0]

def test_attachment_requires_mcp_readback(monkeypatch, tmp_path):
    monkeypatch.setattr('zotero_pdf_pipeline.attach.to_windows_path', lambda p: 'C:\\test\\' + p.name)
    monkeypatch.setattr('zotero_pdf_pipeline.attach.time.sleep', lambda _: None)
    path=tmp_path/'a.pdf'; path.write_bytes(b'%PDF-mock')
    class Local:
        calls=0
        def attachments_of(self, key):
            self.calls += 1
            return [] if self.calls == 1 else [{'data': {'key': 'A1', 'contentType':'application/pdf', 'linkMode':'linked_file'}}]
    class Mcp:
        def __init__(self, found): self.found = found
        def call_tool(self, name, args):
            if name == 'write_item': return {'attachmentKey': 'A1'}
            assert name == 'get_item_details' and args['itemKey'] == 'K1'
            return {'attachments': [{'key': 'A1'}]} if self.found else {'attachments': []}
    for found in (False, True):
        result = Attacher(Mcp(found), Local()).attach(PaperNeed('K1','Test'), path)
        assert result.verified is found and result.ok is found

def test_windows_path():
    assert to_windows_path('/mnt/c/Users/alice/test.pdf') == 'C:\\Users\\alice\\test.pdf'

def test_plugin_exception_redacted(monkeypatch, cfg, caplog):
    monkeypatch.setattr(cli, 'ZoteroLocal', FakeLocal)
    (cfg.plugins.directory / 'fake.py').write_text('raise RuntimeError("private-token")')
    store=Store(cfg.paths.state_db)
    try:
        assert cli.cmd_fetch(cfg, store, ['K1'], plugin_names=['fake']) == 1
        assert 'private-token' not in caplog.text + store.get('K1')['note']
    finally: store.close()

@pytest.mark.parametrize('source', ['openalex', 'unpaywall'])
def test_contact_and_user_agent_sent(monkeypatch, tmp_path, source):
    requests=[]
    def handler(request):
        requests.append(request)
        if request.url.host == 'pdf.invalid': return httpx.Response(200, content=b'%PDF-mock')
        return httpx.Response(200, json={'best_oa_location': {'pdf_url':'https://pdf.invalid/x', 'url_for_pdf':'https://pdf.invalid/x'}})
    mock_http(monkeypatch, handler)
    assert free.try_all(PaperNeed('K1','Test','10.1234/test'), tmp_path, [source], False, contact_email='contact@institute.invalid')
    assert 'contact@institute.invalid' in requests[0].headers['user-agent']
    assert requests[0].url.params['mailto' if source == 'openalex' else 'email'] == 'contact@institute.invalid'

def test_entry_point_plugin(monkeypatch, tmp_path):
    from zotero_pdf_pipeline import plugins
    class Fake:
        name='fake'
        def fetch(self, item_meta, dest_dir): return None
    class Entry:
        def load(self): return Fake()
    monkeypatch.setattr(plugins.metadata, 'entry_points', lambda **kw: [Entry()])
    assert plugins.load_source('fake', tmp_path).name == 'fake'

def test_rate_limit_arxiv(monkeypatch):
    current=[0.0]; sleeps=[]
    monkeypatch.setattr(free.time, 'monotonic', lambda: current[0])
    def sleep(seconds): sleeps.append(seconds); current[0] += seconds
    monkeypatch.setattr(free.time, 'sleep', sleep)
    free._LAST_REQUEST.clear()
    free._rate_limit('https://export.arxiv.org/api/query')
    free._rate_limit('https://export.arxiv.org/api/query')
    assert sleeps == [3.0]

def test_attach_revalidates_before_mcp(monkeypatch, cfg):
    monkeypatch.setattr(cli, 'ZoteroLocal', FakeLocal)
    monkeypatch.setattr(cli, 'McpClient', lambda *args: object())
    class NoAttach:
        def __init__(self, *a, **k): pass
        def attach(self, *a, **k): pytest.fail('invalid PDF must not reach MCP')
    monkeypatch.setattr(cli, 'Attacher', NoAttach)
    path=cfg.paths.staging_dir/'bad.pdf'; path.write_bytes(b'not a pdf')
    store=Store(cfg.paths.state_db)
    try:
        store.upsert_need(PaperNeed('K1','A test paper'))
        store.mark('K1', cli.Stage.DOWNLOADED, file_path=str(path))
        assert cli.cmd_attach(cfg, store, ['K1']) == 1
        assert store.get('K1')['stage'] == 'failed'
        assert Path(store.get('K1')['file_path']).parent.name == 'staging-rejected'
    finally: store.close()
