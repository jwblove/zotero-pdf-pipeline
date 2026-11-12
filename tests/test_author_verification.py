"""Wrong DOI/title-prefix regression and conservative first-page author evidence."""
from pathlib import Path
from types import SimpleNamespace
import pytest
from zotero_pdf_pipeline import validate as v, pdf_worker, cli
from zotero_pdf_pipeline.config import load_config
from zotero_pdf_pipeline.models import PaperNeed, ResolvedFile, Source, Stage, AttachResult
from zotero_pdf_pipeline.state import Store
from zotero_pdf_pipeline.zotero_local import ZoteroLocal

EXPECTED = 'Overlooked Solid Electrolyte Interphase Destabilization by Dissolved Transition Metals in Lithium Batteries'
ACTUAL = 'Overlooked Solid Electrolyte Interphase Destabilization Induced by Mechanical Stress in Sodium Batteries'
AUTHORS = ['Davidson','Gates','Wells']
TEXT = (ACTUAL+'\nWang, Fang, Niu\nSee also 10.1234/wrong\nAbstract\n'
    'We report systematic measurements of sodium battery interfaces under mechanical stress. '
    'Experiments demonstrate interfacial changes during repeated charging, with extensive microscopy '
    'and spectroscopy measurements across different cycling conditions and electrode structures.')


@pytest.fixture
def pdf(tmp_path):
    path=tmp_path/'paper.pdf';path.write_bytes(b'%PDF-test-data')
    return path


def check(monkeypatch,pdf,text=TEXT,authors=AUTHORS,**kwargs):
    monkeypatch.setattr(v,'pdf_pages_and_text',lambda path:(2,text))
    return v.check_pdf(pdf,expected_title=EXPECTED,expected_doi='10.1234/wrong',
        expected_authors=authors,min_bytes=1,**kwargs)


def test_wrong_doi_and_shared_title_prefix_need_manual_review(monkeypatch,pdf):
    assert v.doi_in_text('10.1234/wrong',TEXT)
    result=check(monkeypatch,pdf)
    assert .55 <= result.similarity < .95
    assert not result.ok and 'needs manual review' in result.reason
    assert 'author names not found on first page' in result.reason


def test_first_page_labeled_doi_allows_metadata_discrepancy_with_warning(monkeypatch,pdf):
    text = TEXT.replace('See also 10.1234/wrong', 'DOI: 10.1234/wrong')
    result = check(monkeypatch,pdf,text)
    assert result.ok and '人工核对元数据' in result.reason


def test_labeled_doi_after_extraction_blank_lines(monkeypatch,pdf):
    text = TEXT + '\n'*70 + 'DOI: 10.1234/wrong\n'
    assert check(monkeypatch,pdf,text).ok


def test_first_page_doi_url_allows_different_title_and_authors(monkeypatch,pdf):
    text = 'Unrelated published title\nOther Author\nhttps://doi.org/10.1234/wrong\n' + 'Research data and methods. '*20
    result = check(monkeypatch,pdf,text)
    assert result.ok and '人工核对元数据' in result.reason


def test_doi_on_second_page_does_not_override_author_mismatch(monkeypatch,pdf):
    result = check(monkeypatch,pdf,TEXT+'\fDOI: 10.1234/wrong')
    assert not result.ok


def test_bare_doi_reference_does_not_override_author_mismatch(monkeypatch,pdf):
    assert not check(monkeypatch,pdf).ok


def test_conflicting_first_page_doi_rejected_even_when_title_matches(monkeypatch,pdf):
    text = EXPECTED+'\nDavidson\nDOI: 10.1234/another\n' + 'Research methods and findings. '*20
    result = check(monkeypatch,pdf,text)
    assert not result.ok and 'DOI 不一致' in result.reason


def test_multiple_first_page_dois_require_manual_review(monkeypatch,pdf):
    text = EXPECTED+'\nDavidson\nDOI: 10.1234/wrong\nDOI: 10.1234/another\n' + 'Research findings. '*20
    result = check(monkeypatch,pdf,text)
    assert not result.ok and '多个 DOI' in result.reason


@pytest.mark.parametrize('surname',AUTHORS)
def test_any_item_author_in_first_sixty_lines_is_support(monkeypatch,pdf,surname):
    text='\n'.join([ACTUAL]+['Further measurements of electrode structures and interfaces']*58+[surname])
    assert check(monkeypatch,pdf,text).ok


def test_author_on_second_page_cannot_mask_first_page_mismatch(monkeypatch,pdf):
    assert not check(monkeypatch,pdf,TEXT+'\fDavidson, Gates, Wells\nReferences').ok


def test_author_outside_first_sixty_lines_does_not_mask_mismatch(monkeypatch,pdf):
    text='\n'.join([ACTUAL]+['Further measurements of electrode structures and interfaces']*59+['Davidson'])
    assert not check(monkeypatch,pdf,text).ok


@pytest.mark.parametrize('name,printed',[
    ('Sinéad','Sin´ ead'), ('García','GARCIA'), ('Östberg','Ostberg'),
    ('O’Neill',"O'Neill"), ('García-Márquez','Garcia–Marquez'), ('Ｓｍｉｔｈ','Smith')])
def test_author_unicode_accents_and_pdf_accent_spacing(monkeypatch,pdf,name,printed):
    assert check(monkeypatch,pdf,TEXT+'\n'+printed,authors=[name]).ok


def test_short_surname_is_not_matched_inside_another_word(monkeypatch,pdf):
    result=check(monkeypatch,pdf,TEXT+'\nLithium compounds were studied.',authors=['Li'])
    assert not result.ok and 'author names not found' in result.reason


def test_near_exact_title_skips_negative_author_rule(monkeypatch,pdf):
    result=check(monkeypatch,pdf,TEXT.replace(ACTUAL,EXPECTED))
    assert result.ok and result.similarity >= .95


def test_insufficient_first_page_text_skips_negative_author_rule(monkeypatch,pdf):
    assert check(monkeypatch,pdf,ACTUAL+'\nWang').ok
    assert check(monkeypatch,pdf,ACTUAL+'\nWang\f'+TEXT).ok


def test_no_item_authors_skips_negative_author_rule(monkeypatch,pdf):
    assert check(monkeypatch,pdf,authors=[]).ok


def test_explicit_inbox_match_skips_negative_author_rule(monkeypatch,pdf):
    assert check(monkeypatch,pdf,inbox_matched=True).ok
    assert not check(monkeypatch,pdf,allow_scanned=True).ok


@pytest.mark.parametrize('banner',['CMS Collaboration','ATLAS','The ATLAS Collaboration','the cms collaboration','International Detector Collaboration'])
def test_collaboration_banners_do_not_require_person_surname_match(monkeypatch,pdf,banner):
    assert check(monkeypatch,pdf,TEXT+'\n'+banner).ok


@pytest.mark.parametrize('creators',[['CMS Collaboration'],['ATLAS'],['Research Institute'],['International Research Group']])
def test_named_institutional_creators_skip_author_rule(monkeypatch,pdf,creators):
    assert check(monkeypatch,pdf,authors=creators).ok


def test_explicit_institutional_metadata_skips_rule_without_name_heuristic(monkeypatch,pdf):
    assert check(monkeypatch,pdf,authors=['Detector Partnership'],institutional_authors=True).ok


def test_worker_keeps_page_boundaries_when_first_page_fails(monkeypatch):
    class Failed:
        def extract_text(self):raise ValueError('failed')
    class Readable:
        def extract_text(self):return TEXT+'\nDavidson'
    class Reader:
        root_object={'/Pages':{'/Count':2}}
        pages=[Failed(),Readable()]
    monkeypatch.setattr('pypdf.PdfReader',lambda *a,**k:Reader())
    result=pdf_worker.inspect_pdf('unused')
    assert result['text'].startswith('\f') and not v._author_header(result['text'])


def test_institutional_creator_metadata_is_persisted_for_attach(monkeypatch,tmp_path):
    local=ZoteroLocal('http://127.0.0.1:23119/api/users/0')
    monkeypatch.setattr(local,'items_in_collection',lambda key:[{'data':{'key':'K','title':EXPECTED,
        'itemType':'journalArticle','creators':[{'creatorType':'author','fieldMode':1,'name':'Detector Partnership'}]}}])
    monkeypatch.setattr(local,'attachments_of',lambda key:[])
    try:paper=local.items_without_attachment('C')[0]
    finally:local.close()
    assert paper.has_institutional_authors
    config=tmp_path/'settings.toml'
    config.write_text('[zotero]\ncollection_key="C"\n[paths]\narchive_dir="archive"\n')
    cfg=load_config(config)
    file=cfg.paths.staging_dir/'paper.pdf';file.write_bytes(b'%PDF-test')
    class Local:
        def __init__(self,*a):pass
        def close(self):pass
    class Attacher:
        def __init__(self,*a,**k):pass
        def attach(self,*a,**k):return AttachResult('K',True,'PDF','PDF confirmed',True)
    observed=[]
    def validation(*a,**kwargs):observed.append(kwargs['institutional_authors']);return SimpleNamespace(ok=True)
    monkeypatch.setattr(cli,'ZoteroLocal',Local)
    monkeypatch.setattr(cli,'McpClient',lambda *a:object())
    monkeypatch.setattr(cli,'Attacher',Attacher)
    monkeypatch.setattr(cli,'check_pdf',validation)
    store=Store(cfg.paths.state_db)
    try:
        store.upsert_need(paper)
        store.mark('K',Stage.DOWNLOADED,file_path=str(file),authors=paper.authors)
        assert store.get('K')['institutional_authors']=='1'
        assert cli.cmd_attach(cfg,store,['K'])==0 and observed==[True]
    finally:store.close()


def test_author_mismatch_quarantined_by_fetch_and_attach_without_import(monkeypatch,tmp_path):
    config=tmp_path/'settings.toml'
    config.write_text('[zotero]\ncollection_key="C"\n[paths]\narchive_dir="archive"\n[fetch]\nsource_order=[]\nmin_pdf_bytes=1\n')
    cfg=load_config(config)
    paper=PaperNeed('K',EXPECTED,'10.1234/wrong',authors=AUTHORS)
    file=cfg.paths.staging_dir/'candidate.pdf';file.write_bytes(b'%PDF-test-data')
    class Local:
        def __init__(self,*a):pass
        def items_without_attachment(self,key):return [paper]
        def creators_of(self,key):return AUTHORS
        def close(self):pass
    class NoImport:
        def __init__(self,*a,**k):pass
        def attach(self,*a,**k):pytest.fail('manual review must block import')
    monkeypatch.setattr(cli,'ZoteroLocal',Local)
    monkeypatch.setattr(cli,'McpClient',lambda *a:object())
    monkeypatch.setattr(cli,'Attacher',NoImport)
    monkeypatch.setattr(v,'pdf_pages_and_text',lambda path:(2,TEXT))
    monkeypatch.setattr(cli,'try_free_sources',lambda *a,**k:ResolvedFile(paper,file,Source.SEMANTIC_SCHOLAR))
    store=Store(cfg.paths.state_db)
    try:
        assert cli.cmd_fetch(cfg,store,['K'],free_only=True)==1
        row=store.get('K')
        assert row['stage']=='failed' and 'author names not found' in row['note']
        assert Path(row['file_path']).parent.name=='staging-rejected'
        # Revalidate a previously downloaded file too, before any import.
        store.mark('K',Stage.DOWNLOADED)
        assert cli.cmd_attach(cfg,store,['K'])==1 and store.get_stage('K')=='failed'
    finally:store.close()

def test_combining_accent_at_real_name_boundary_does_not_join_people(monkeypatch,pdf):
    assert check(monkeypatch,pdf,TEXT+'\nGarcia\u0301 Smith',authors=['García']).ok
