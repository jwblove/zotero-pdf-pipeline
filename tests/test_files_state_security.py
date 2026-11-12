"""Confinement, atomic state, run locking, inbox and report regression tests."""
import concurrent.futures
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import pytest
from zotero_pdf_pipeline import cli, safe_files as files, free_sources as free, validate
from zotero_pdf_pipeline.attach import to_windows_path
from zotero_pdf_pipeline.config import load_config, FetchConfig, ConfigError
from zotero_pdf_pipeline.models import PaperNeed, Stage, ResolvedFile, Source
from zotero_pdf_pipeline.naming import build_filename, sanitize_filename
from zotero_pdf_pipeline.quarantine import quarantine_pdf
from zotero_pdf_pipeline.state import Store, RunLock, RunLockedError, _DDL

@pytest.mark.parametrize('key', ['../escape','a/b','a\\b','..','/absolute','', 'a'*65])
def test_unsafe_item_keys_rejected(key):
    with pytest.raises(ValueError): PaperNeed(key,'Title')

def test_exclusive_atomic_publication_and_symlink_refusal(tmp_path):
    source=tmp_path/'original.pdf';source.write_bytes(b'original')
    root=tmp_path/'staging'
    one=files.write_bytes(root,'x.pdf',b'%PDF-one')
    two=files.write_bytes(root,'x.pdf',b'%PDF-two')
    assert one!=two and one.read_bytes()==b'%PDF-one'
    (root/'link.pdf').symlink_to(source)
    with pytest.raises(ValueError): files.write_bytes(root,'link.pdf',b'overwrite')
    with pytest.raises(ValueError): files.write_bytes(root,'../escape.pdf',b'x')
    assert source.read_bytes()==b'original'
    assert not list(root.glob('.partial-*'))

def test_failed_large_write_leaves_no_partial_file(tmp_path):
    with pytest.raises(ValueError): files.publish(tmp_path,'big.pdf',[b'ab',b'cd'],max_bytes=3)
    assert not list(tmp_path.iterdir())

def test_copy_rejects_symlink_and_quarantine_root_escape(tmp_path):
    staging=tmp_path/'staging';staging.mkdir()
    outside=tmp_path/'outside';outside.mkdir()
    source=staging/'bad.pdf';source.write_bytes(b'bad')
    link=staging/'link.pdf';link.symlink_to(source)
    with pytest.raises(ValueError): files.copy_file(link,outside,'bad.pdf')
    (tmp_path/'staging-rejected').symlink_to(outside,target_is_directory=True)
    with pytest.raises(ValueError): quarantine_pdf(source,staging,'K')
    assert source.exists() and not list(outside.iterdir())

@pytest.mark.parametrize('name',['CON','nul.pdf','PRN','COM1','LPT9.txt','AUX','COM¹.pdf'])
def test_windows_device_names(name):
    assert sanitize_filename(name).startswith('_')

def test_all_filename_parts_fit_final_byte_limit():
    for authors in (['中'*300], ['x'*225], ['A','B']):
        name=build_filename(authors,'2026','长标题'*300)
        assert len(name.encode())<=230 and name.endswith('.pdf')

@pytest.mark.parametrize('original,expected', [
    ('/mnt/c/Users/alice/../paper.pdf','C:\\Users\\paper.pdf'),
    ('D:\\folder\\..\\paper.pdf','D:\\paper.pdf')])
def test_path_conversion_normalizes_parents(original,expected):
    assert to_windows_path(original)==expected

@pytest.mark.parametrize('path',['/tmp/paper.pdf','relative.pdf','/mnt/c/../../tmp/paper.pdf','C:relative.pdf'])
def test_unsupported_attachment_path_rejected(path):
    with pytest.raises(ValueError): to_windows_path(path)

def test_inbox_only_regular_bounded_pdf_files(monkeypatch,tmp_path):
    inbox=tmp_path/'inbox';inbox.mkdir();staging=tmp_path/'staging'
    regular=inbox/'good.PDF';regular.write_bytes(b'%PDF-test')
    (inbox/'bad.pdf').symlink_to(regular)
    (inbox/'folder.pdf').mkdir()
    (inbox/'not.txt').write_bytes(b'%PDF-test')
    (inbox/'oversized.pdf').write_bytes(b'x'*100)
    monkeypatch.setattr(free,'_MAX_PDF_BYTES',20)
    inspected=[]
    def parse(path): inspected.append(path.name); return 1,'Unique research article title'
    monkeypatch.setattr(validate,'pdf_pages_and_text',parse)
    result=free.try_inbox(PaperNeed('K','Unique research article title'),staging,inbox,.55)
    assert result.inbox_matched and inspected==['good.PDF']
    assert regular.exists()

def test_scanned_inbox_explicit_filename_match(monkeypatch,tmp_path):
    inbox=tmp_path/'inbox';inbox.mkdir()
    (inbox/'Unique research article title.pdf').write_bytes(b'%PDF-test')
    monkeypatch.setattr(validate,'pdf_pages_and_text',lambda path:(1,''))
    assert free.try_inbox(PaperNeed('K','Unique research article title'),tmp_path/'staging',inbox,.55).inbox_matched

@pytest.mark.parametrize('maximum',[0,-1,1024**3+1,True])
def test_config_rejects_invalid_pdf_cap(maximum):
    with pytest.raises(ConfigError): FetchConfig(max_pdf_bytes=maximum)

def test_migrations_and_upserts_are_atomic_across_connections(tmp_path):
    db=tmp_path/'legacy.db'
    with sqlite3.connect(db) as conn:
        conn.executescript(_DDL.replace('    license         TEXT,\n','').replace('    oa_status       TEXT,\n',''))
    def worker(index):
        store=Store(db)
        try:
            for _ in range(5): store.upsert_need(PaperNeed('K','Updated title',authors=['Lee']))
        finally: store.close()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(worker,range(2)))
    store=Store(db)
    try:
        assert len(store.all_rows())==1
        store.mark('K',Stage.ATTACHED,attachment_key='A')
        store.upsert_need(PaperNeed('K','New title'))
        assert store.get('K')['attachment_key']=='A' and store.get_stage('K')=='attached'
    finally: store.close()

def test_lock_excludes_another_command_and_recovers_stale_file(tmp_path):
    (tmp_path/'pipeline.lock').write_text('999999999')
    with RunLock(tmp_path):
        with pytest.raises(RunLockedError):
            with RunLock(tmp_path): pass
    with RunLock(tmp_path): pass

def test_report_escapes_all_metadata(tmp_path):
    store=Store(tmp_path/'state.db')
    attack='![image](https://host.invalid/pixel) | <b>bold</b>\n# title'
    try:
        store.upsert_need(PaperNeed('K',attack))
        store.mark('K',Stage.FAILED,source=attack,license=attack,oa_status=attack,note=attack)
        cfg=SimpleNamespace(paths=SimpleNamespace(report_dir=tmp_path,archive_dir=tmp_path),zotero=SimpleNamespace(collection_key='C'))
        assert cli.cmd_report(cfg,store)==0
        output=next(tmp_path.glob('*-报告.md')).read_text()
        assert '![' not in output and '<b>' not in output and '\n# title' not in output
        assert '&#124;' in output and '&#91;' in output
    finally: store.close()

def test_valid_download_is_reused_without_sources(monkeypatch,tmp_path):
    config=tmp_path/'settings.toml';config.write_text('[zotero]\ncollection_key="C"\n[paths]\narchive_dir="archive"\n')
    cfg=load_config(config)
    class Local:
        def __init__(self,*a): pass
        def items_without_attachment(self,key): return [PaperNeed('K','Unique research article title')]
        def creators_of(self,key): return ['Lee']
        def close(self): pass
    monkeypatch.setattr(cli,'ZoteroLocal',Local)
    monkeypatch.setattr(cli,'try_free_sources',lambda *a,**k: pytest.fail('must reuse validated file'))
    monkeypatch.setattr(cli,'check_pdf',lambda *a,**k: SimpleNamespace(ok=True))
    path=cfg.paths.archive_dir/'paper.pdf';path.write_bytes(b'%PDF-test')
    store=Store(cfg.paths.state_db)
    try:
        store.upsert_need(PaperNeed('K','Unique research article title'))
        store.mark('K',Stage.DOWNLOADED,file_path=str(path),source='openalex',license='cc-by')
        assert cli.cmd_fetch(cfg,store,['K'],free_only=True)==0
        row=store.get('K')
        assert row['stage']=='downloaded' and row['license']=='cc-by' and row['attempts']==0
    finally: store.close()

def test_scanned_inbox_explicit_doi_filename(monkeypatch,tmp_path):
    inbox=tmp_path/'inbox';inbox.mkdir()
    (inbox/'10.1234_test.pdf').write_bytes(b'%PDF-test')
    monkeypatch.setattr(validate,'pdf_pages_and_text',lambda path:(1,''))
    assert free.try_inbox(PaperNeed('K','Different title','10.1234/test'),tmp_path/'staging',inbox,.55).inbox_matched

@pytest.mark.parametrize('command',['doctor','scan','fetch','attach','run','report'])
def test_configured_commands_create_state_and_lock(monkeypatch,tmp_path,command):
    config=tmp_path/'settings.toml'
    config.write_text('[zotero]\ncollection_key="C"\n[paths]\narchive_dir="archive"\n')
    monkeypatch.setattr(cli,'cmd_'+command,lambda *a,**k:0)
    args=['--config',str(config),command]
    if command in ('fetch','attach','run'): args+=['--only','K']
    assert cli.main(args)==0
    assert (tmp_path/'data/pipeline.db').is_file() and (tmp_path/'data/pipeline.lock').is_file()
    assert all((tmp_path/p).is_dir() for p in ('archive','data/inbox','data/reports','data/staging'))

def test_help_does_not_load_configuration(monkeypatch):
    monkeypatch.setattr(cli,'load_config',lambda *a:pytest.fail('help must not create config/state'))
    with pytest.raises(SystemExit) as exc: cli.main(['--help'])
    assert exc.value.code==0

def test_doctor_missing_parser_is_blocking(monkeypatch,tmp_path,capsys):
    import builtins
    config=tmp_path/'settings.toml';config.write_text('[zotero]\ncollection_key="C"\n[paths]\narchive_dir="archive"\n')
    cfg=load_config(config)
    class Local:
        def __init__(self,*a):pass
        def ping(self):return True
        def collection_name(self,key):return 'Collection'
        def items_without_attachment(self,key):return []
        def close(self):pass
    class Mcp:
        def __init__(self,*a):pass
        def initialize(self):return {}
        def call_tool(self,*a):return []
    original=builtins.__import__
    def imports(name,*args,**kwargs):
        if name=='pypdf':raise ImportError('missing')
        return original(name,*args,**kwargs)
    monkeypatch.setattr(cli,'ZoteroLocal',Local);monkeypatch.setattr(cli,'McpClient',Mcp)
    monkeypatch.setattr(builtins,'__import__',imports)
    assert cli.cmd_doctor(cfg)==1
    output=capsys.readouterr().out
    assert '无法验证 PDF' in output and '将跳过' not in output

def test_invalid_cached_download_attempts_quarantine_before_retry(monkeypatch,tmp_path):
    config=tmp_path/'settings.toml';config.write_text('[zotero]\ncollection_key="C"\n[paths]\narchive_dir="archive"\n')
    cfg=load_config(config)
    class Local:
        def __init__(self,*a):pass
        def items_without_attachment(self,key):return [PaperNeed('K','A detailed research title')]
        def creators_of(self,key):return []
        def close(self):pass
    monkeypatch.setattr(cli,'ZoteroLocal',Local)
    monkeypatch.setattr(cli,'check_pdf',lambda *a,**k:SimpleNamespace(ok=False))
    retried=[]
    monkeypatch.setattr(cli,'try_free_sources',lambda *a,**k:retried.append(True))
    path=cfg.paths.archive_dir/'bad.pdf';path.write_bytes(b'not a PDF')
    store=Store(cfg.paths.state_db)
    try:
        store.upsert_need(PaperNeed('K','A detailed research title'))
        store.mark('K',Stage.DOWNLOADED,file_path=str(path))
        assert cli.cmd_fetch(cfg,store,['K'],free_only=True)==1 and retried
        assert store.get_stage('K')=='failed'
        assert Path(store.get('K')['file_path']).parent.name=='staging-rejected'
        assert path.exists()  # Existing archives are copied, never removed.
    finally:store.close()

@pytest.mark.parametrize('value',['true','1.5','"100"'])
def test_config_pdf_cap_requires_toml_integer(tmp_path,value):
    path=tmp_path/'settings.toml'
    path.write_text('[paths]\narchive_dir="archive"\n[fetch]\nmax_pdf_bytes='+value+'\n')
    with pytest.raises(ConfigError):load_config(path)
