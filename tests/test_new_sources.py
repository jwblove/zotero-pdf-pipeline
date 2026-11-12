"""Mocked source discovery, migrations and metadata flow."""
import sqlite3
from dataclasses import replace
from pathlib import Path
import httpx
import pytest
from zotero_pdf_pipeline import cli, free_sources as free
from zotero_pdf_pipeline.models import PaperNeed, Stage
from zotero_pdf_pipeline.state import Store, _DDL
from zotero_pdf_pipeline.zotero_local import _extract_pmcid

def mock_http(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(free, 'make_client', lambda **kw: httpx.Client(transport=transport, trust_env=False, **kw))
    monkeypatch.setattr(free, '_rate_limit', lambda url: None)

def test_crossref_matches_title_author_year_without_mutation(monkeypatch):
    seen=[]
    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={'message': {'items': [
            {'title':['Different title'], 'DOI':'10.1234/wrong', 'author':[{'family':'Lee'}], 'published':{'date-parts':[[2024]]}},
            {'title':['A carefully matched research title'], 'DOI':'10.1234/Match', 'author':[{'family':'Lee'}], 'published':{'date-parts':[[2024]]}}]}})
    mock_http(monkeypatch, handler)
    paper = PaperNeed('K1', 'A carefully matched research title', year='2024', authors=['Lee'])
    assert free.lookup_crossref(paper, 5, .8, 'research@institute.invalid') == '10.1234/match'
    assert paper.doi == ''
    assert seen[0].url.params['query.author'] == 'Lee'
    assert seen[0].url.params['mailto'] == 'research@institute.invalid'

@pytest.mark.parametrize('field,value', [('year','2023'), ('authors',['Smith']), ('title','Unrelated title')])
def test_crossref_rejects_mismatch(monkeypatch, field, value):
    mock_http(monkeypatch, lambda req: httpx.Response(200, json={'message':{'items':[{'title':['A matching title'], 'DOI':'10.1234/x', 'author':[{'family':'Lee'}], 'published':{'date-parts':[[2024]]}}]}}))
    paper = replace(PaperNeed('K','A matching title',year='2024',authors=['Lee']), **{field:value})
    with pytest.raises(free.FreeSourceError): free.lookup_crossref(paper,5,.8,'mail@institute.invalid')

def test_crossref_missing_contact_skips(monkeypatch):
    monkeypatch.setattr(free, '_request', lambda *a, **k: pytest.fail('unexpected HTTP'))
    with pytest.raises(free.FreeSourceError, match='邮箱'): free.lookup_crossref(PaperNeed('K','Title'), 5, .8, '')

def test_pmcid_from_extra_and_direct_oa(monkeypatch, tmp_path):
    assert _extract_pmcid({'extra':'PMCID: pmc12345'}) == 'PMC12345'
    requests=[]
    def handler(req):
        requests.append(req)
        if req.url.host == 'europepmc.org': return httpx.Response(200, content=b'%PDF-data')
        return httpx.Response(200, json={'resultList':{'result':[{'pmcid':'PMC12345','isOpenAccess':'Y','license':'cc-by'}]}})
    mock_http(monkeypatch, handler)
    result=free.try_all(PaperNeed('K1','Test',pmcid='PMC12345'), tmp_path, ['europe_pmc'], False)
    assert result and result.license == 'cc-by' and result.oa_status == 'open'
    assert requests[0].url.params['query'] == 'EXT_ID:PMC12345'
    assert requests[1].url.path.endswith('/PMC12345')

def test_pmcid_closed_no_direct_download(monkeypatch, tmp_path):
    calls=[]
    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={'resultList':{'result':[{'pmcid':'PMC12345','isOpenAccess':'N'}]}})
    mock_http(monkeypatch, handler)
    assert free.try_all(PaperNeed('K','Title',pmcid='PMC12345'),tmp_path,['europe_pmc'],False) is None
    assert len(calls)==1

@pytest.mark.parametrize('source', ['openalex','unpaywall'])
def test_landing_meta_and_license(monkeypatch, tmp_path, source):
    def handler(req):
        if req.url.host == 'landing.invalid':
            return httpx.Response(200, text='<meta content="/article.pdf" name="citation_pdf_url">')
        if req.url.path == '/article.pdf': return httpx.Response(200, content=b'%PDF-data')
        if source == 'openalex':
            return httpx.Response(200, json={'open_access':{'oa_status':'gold'}, 'best_oa_location':{'landing_page_url':'https://landing.invalid/page', 'license':'cc-by'}})
        return httpx.Response(200, json={'oa_status':'green','best_oa_location':{'url_for_landing_page':'https://landing.invalid/page','license':'cc-by-nc'}})
    # The resolved relative URL remains on landing.invalid.
    def corrected(req):
        if req.url.host == 'landing.invalid' and req.url.path == '/article.pdf': return httpx.Response(200,content=b'%PDF-data')
        return handler(req)
    mock_http(monkeypatch, corrected)
    result=free.try_all(PaperNeed('K','Test','10.1234/test'),tmp_path,[source],False,contact_email='mail@institute.invalid')
    assert result and result.license.startswith('cc-by') and result.oa_status in ('gold','green')

def test_landing_non_pdf_rejected(monkeypatch, tmp_path):
    def handler(req):
        if req.url.host == 'api.openalex.org': return httpx.Response(200,json={'best_oa_location':{'landing_page_url':'https://landing.invalid/page'}})
        if req.url.path == '/page': return httpx.Response(200,text='<meta name="citation_pdf_url" content="/x">')
        return httpx.Response(200,text='<html>not a pdf</html>')
    mock_http(monkeypatch, handler)
    assert free.try_all(PaperNeed('K','Test','10.1234/test'),tmp_path,['openalex'],False,contact_email='mail@institute.invalid') is None

def test_core_opt_in_key_and_redaction(monkeypatch, tmp_path, caplog):
    monkeypatch.delenv('CORE_API_KEY', raising=False)
    mock_http(monkeypatch, lambda req: pytest.fail('unexpected HTTP'))
    diag=[]
    assert free.try_all(PaperNeed('K','Test','10.1234/test'),tmp_path,['core'],False,diagnostics=diag) is None
    assert '密钥' in diag[0]
    monkeypatch.setenv('CORE_API_KEY','secret-key-123')
    calls=[]
    def handler(req):
        calls.append(req)
        if req.url.host == 'pdf.invalid': return httpx.Response(200,content=b'%PDF-data')
        return httpx.Response(200,json={'results':[{'doi':'10.1234/test','downloadUrl':'https://pdf.invalid/file'}]})
    mock_http(monkeypatch, handler)
    result=free.try_all(PaperNeed('K','Test','10.1234/test'),tmp_path,['core'],False)
    assert result and calls[0].headers['authorization'] == 'Bearer secret-key-123'
    assert 'secret-key-123' not in str(calls[0].url) + caplog.text

def test_state_migration_and_report_preserve_license(tmp_path):
    db=tmp_path/'legacy.db'
    legacy=_DDL.replace('    license         TEXT,\n','').replace('    oa_status       TEXT,\n','')
    with sqlite3.connect(db) as conn:
        conn.executescript(legacy)
        conn.execute("INSERT INTO papers (item_key, stage, title) VALUES ('K', 'pending', 'Title')")
    store=Store(db)
    try:
        assert store.get('K')['license'] is None
        store.mark('K',Stage.DOWNLOADED,license='cc-by',oa_status='gold')
        from types import SimpleNamespace
        cfg=SimpleNamespace(paths=SimpleNamespace(report_dir=tmp_path,archive_dir=tmp_path),zotero=SimpleNamespace(collection_key='C'))
        assert cli.cmd_report(cfg,store)==0
        assert 'cc-by' in next(tmp_path.glob('*-报告.md')).read_text()
        assert store.get('K')['oa_status']=='gold'
    finally: store.close()
    with StoreContext(db) as reopened:
        assert reopened.get('K')['license']=='cc-by'

class StoreContext:
    def __init__(self,path): self.store=Store(path)
    def __enter__(self): return self.store
    def __exit__(self,*args): self.store.close()

def test_free_only_does_not_read_core_key(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs): pytest.fail('credential source must not run')
    monkeypatch.setitem(free.REGISTRY, 'core', forbidden)
    diag=[]
    assert free.try_all(PaperNeed('K','Title','10.1234/x'),tmp_path,['core'],False,
                        diagnostics=diag, allow_credentials=False) is None
    assert 'free-only' in diag[0]

def test_crossref_doi_only_used_for_lookup(monkeypatch, tmp_path):
    from zotero_pdf_pipeline.config import load_config
    from types import SimpleNamespace
    config=tmp_path/'settings.toml'
    config.write_text('[zotero]\ncollection_key="C"\n[paths]\narchive_dir="archive"\n[fetch]\nenable_crossref=true\nsource_order=[]\n')
    cfg=load_config(config)
    cfg.contact_email='contact@institute.invalid'
    original=PaperNeed('K','A long enough title',authors=['Lee'])
    class Local:
        def __init__(self,*a): pass
        def items_without_attachment(self,key): return [original]
        def creators_of(self,key): return ['Lee']
        def close(self): pass
    monkeypatch.setattr(cli,'ZoteroLocal',Local)
    monkeypatch.setattr(cli,'lookup_crossref',lambda *a: '10.1234/found')
    seen=[]
    def source(paper,*args,**kwargs): seen.append(paper.doi); return None
    monkeypatch.setattr(cli,'try_free_sources',source)
    store=Store(cfg.paths.state_db)
    try:
        assert cli.cmd_fetch(cfg,store,['K'],free_only=True)==1
        assert seen==['10.1234/found'] and original.doi=='' and store.get('K')['doi']==''
    finally: store.close()

def test_fetched_license_is_stored(monkeypatch,tmp_path):
    from zotero_pdf_pipeline.config import load_config
    from zotero_pdf_pipeline.models import ResolvedFile, Source
    config=tmp_path/'settings.toml'
    config.write_text('[zotero]\ncollection_key="C"\n[paths]\narchive_dir="archive"\n')
    cfg=load_config(config)
    paper=PaperNeed('K','A paper','10.1234/x')
    class Local:
        def __init__(self,*a): pass
        def items_without_attachment(self,key): return [paper]
        def creators_of(self,key): return []
        def close(self): pass
    monkeypatch.setattr(cli,'ZoteroLocal',Local)
    candidate=cfg.paths.staging_dir/'candidate.pdf'; candidate.write_bytes(b'%PDF-data')
    monkeypatch.setattr(cli,'try_free_sources',lambda *a,**k: ResolvedFile(paper,candidate,Source.OPENALEX,license='cc-by',oa_status='gold'))
    monkeypatch.setattr(cli,'check_pdf',lambda *a,**k: type('Result',(),{'ok':True,'pages':1,'similarity':.9})())
    store=Store(cfg.paths.state_db)
    try:
        assert cli.cmd_fetch(cfg,store,['K'],free_only=True)==0
        assert store.get('K')['license']=='cc-by' and store.get('K')['oa_status']=='gold'
    finally: store.close()
