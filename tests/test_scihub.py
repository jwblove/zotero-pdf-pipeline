"""Offline tests for the opt-in example source."""
from pathlib import Path
import httpx
import pytest
from zotero_pdf_pipeline import cli, plugins
from zotero_pdf_pipeline.config import load_config
from zotero_pdf_pipeline.models import PaperNeed
from zotero_pdf_pipeline.state import Store

DIRECTORY = Path(__file__).resolve().parents[1] / 'examples' / 'plugins'

@pytest.fixture
def source(monkeypatch):
    plugin = plugins.load_source('scihub', DIRECTORY)
    module = plugins._FILE_MODULES[(DIRECTORY/'scihub.py').resolve()]
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    module._LAST = 0.0
    return plugin, module

def mock_client(monkeypatch, module, handler):
    original = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(module, 'make_public_client', lambda **kw: original(transport=transport, trust_env=False, **kw))

def test_plugin_success_and_shared_throttle(monkeypatch, tmp_path, source):
    plugin, module=source
    monkeypatch.setenv('ZPP_SCIHUB_BASE_URL','https://host.invalid')
    sleeps=[]
    monkeypatch.setattr(module.time,'sleep',lambda n: sleeps.append(n))
    def handler(req):
        if req.url.path == '/pdf': return httpx.Response(200,content=b'%PDF-valid')
        return httpx.Response(200,text='<iframe src="/pdf"></iframe>')
    mock_client(monkeypatch,module,handler)
    result=plugin.fetch(PaperNeed('K','Title','10.1234/test'),tmp_path)
    assert result.parent == tmp_path and result.read_bytes()==b'%PDF-valid'
    assert sleeps and 4.9 <= sleeps[0] <= 5.0
    assert plugins.load_source('scihub', DIRECTORY) is plugin

def test_missing_base_diagnostic(monkeypatch, tmp_path, source, caplog):
    plugin, module=source
    monkeypatch.delenv('ZPP_SCIHUB_BASE_URL',raising=False)
    assert plugin.fetch(PaperNeed('K','Title','10.1234/test'),tmp_path) is None
    assert 'not configured' in caplog.text

def test_non_https_base_rejected(monkeypatch, tmp_path, source):
    plugin, module=source
    monkeypatch.setenv('ZPP_SCIHUB_BASE_URL','http://host.invalid')
    monkeypatch.setattr(module.httpx,'Client',lambda **kw: pytest.fail('unexpected HTTP'))
    assert plugin.fetch(PaperNeed('K','Title','10.1234/test'),tmp_path) is None

@pytest.mark.parametrize('page,pdf',[(b'<html>CAPTCHA challenge</html>',b'%PDF-data'),(b'<embed src="/pdf">',b'<html>not pdf</html>')])
def test_challenge_and_non_pdf_rejected(monkeypatch, tmp_path, source, page, pdf):
    plugin,module=source
    monkeypatch.setenv('ZPP_SCIHUB_BASE_URL','https://host.invalid')
    def handler(req): return httpx.Response(200,content=pdf if req.url.path=='/pdf' else page)
    mock_client(monkeypatch,module,handler)
    assert plugin.fetch(PaperNeed('K','Title','10.1234/test'),tmp_path) is None
    assert list(tmp_path.iterdir()) == []

def test_example_requires_config_and_cli_opt_in(monkeypatch,tmp_path,source):
    plugin,module=source
    monkeypatch.setenv('ZPP_SCIHUB_BASE_URL','https://host.invalid')
    mock_client(monkeypatch,module,lambda req: httpx.Response(200,content=b'%PDF-test'))
    conf=tmp_path/'settings.toml'
    conf.write_text('[zotero]\ncollection_key="C"\n[paths]\narchive_dir="archive"\n[fetch]\nsource_order=[]\n[plugins]\nenabled=[]\n')
    cfg=load_config(conf);cfg.plugins.directory=DIRECTORY
    class Local:
        def __init__(self,*args): pass
        def items_without_attachment(self,key): return [PaperNeed('K','Title','10.1234/test')]
        def creators_of(self,key): return []
        def close(self): pass
    monkeypatch.setattr(cli,'ZoteroLocal',Local)
    monkeypatch.setattr(cli,'check_pdf',lambda *a,**k: type('Result',(),{'ok':True,'pages':1,'similarity':1.0})())
    store=Store(cfg.paths.state_db)
    try:
        assert cli.cmd_fetch(cfg,store,['K'],plugin_names=['scihub']) == 2
        cfg.plugins.enabled=['scihub']
        assert cli.cmd_fetch(cfg,store,['K'],free_only=True) == 1
        assert cli.cmd_fetch(cfg,store,['K'],plugin_names=['scihub']) == 0
        assert store.get('K')['source'] == 'scihub'
        with pytest.raises(ValueError): cli.cmd_fetch(cfg,store,['K'],free_only=True,plugin_names=['scihub'])
    finally: store.close()

def test_retry_after_blocks_later_fetch(monkeypatch, tmp_path, source):
    plugin, module=source
    monkeypatch.setenv('ZPP_SCIHUB_BASE_URL','https://host.invalid')
    now=[100.0]; calls=[]
    monkeypatch.setattr(module.time, 'monotonic', lambda: now[0])
    def handler(req):
        calls.append(req)
        return httpx.Response(429, headers={'Retry-After':'30'})
    mock_client(monkeypatch,module,handler)
    paper=PaperNeed('K','Title','10.1234/test')
    assert plugin.fetch(paper,tmp_path) is None
    now[0]+=10
    assert plugin.fetch(paper,tmp_path) is None and len(calls)==1
    now[0]+=21
    assert plugin.fetch(paper,tmp_path) is None and len(calls)==2

def test_public_dns_guard_applies_to_plugin(monkeypatch,tmp_path,source):
    plugin,module=source
    from zotero_pdf_pipeline import public_http
    monkeypatch.setenv('ZPP_SCIHUB_BASE_URL','https://host.invalid')
    monkeypatch.setattr(public_http,'_resolve',lambda *a:['127.0.0.1'])
    mock_client(monkeypatch,module,lambda req:pytest.fail('private address must not be contacted'))
    assert plugin.fetch(PaperNeed('K','Title','10.1234/test'),tmp_path) is None
    assert not list(tmp_path.iterdir())

def test_candidate_challenge_stops_before_next_link(monkeypatch,tmp_path,source):
    plugin,module=source
    monkeypatch.setenv('ZPP_SCIHUB_BASE_URL','https://host.invalid')
    calls=[]
    def handler(req):
        calls.append(req.url.path)
        if req.url.path=='/first': return httpx.Response(200,text='CAPTCHA challenge')
        if req.url.path=='/second': pytest.fail('must stop on first challenge')
        return httpx.Response(200,text='<embed src="/first"><iframe src="/second"></iframe>')
    mock_client(monkeypatch,module,handler)
    assert plugin.fetch(PaperNeed('K','Title','10.1234/test'),tmp_path) is None
    assert calls==['/10.1234/test','/first']

def test_challenge_service_unavailable_does_not_retry(monkeypatch,tmp_path,source):
    plugin,module=source
    monkeypatch.setenv('ZPP_SCIHUB_BASE_URL','https://host.invalid')
    calls=[]
    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(503,text='CAPTCHA challenge')
    mock_client(monkeypatch,module,handler)
    assert plugin.fetch(PaperNeed('K','Title','10.1234/test'),tmp_path) is None
    assert len(calls)==1 and not list(tmp_path.iterdir())
