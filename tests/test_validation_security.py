"""Realistic title headers and fail-closed bounded PDF inspection."""
import json
from types import SimpleNamespace
import pytest
from zotero_pdf_pipeline import validate as v, pdf_worker as worker

@pytest.fixture
def pdf(tmp_path):
    path=tmp_path/'candidate.pdf';path.write_bytes(b'%PDF-test')
    return path

def check(monkeypatch,pdf,text,title,**kwargs):
    monkeypatch.setattr(v,'pdf_pages_and_text',lambda path:(2,text))
    return v.check_pdf(pdf,expected_title=title,min_bytes=1,**kwargs)

def test_arxiv_header_and_theta_title(monkeypatch,pdf):
    title='The Cosmological Constant from a Quantum Gravitational θ-Vacua'
    text='arXiv:2506.14886v1 [gr-qc] 17 Jun 2025 '+title+'\nA. Researcher\nAbstract\nWe investigate gravitational vacua.'
    result=check(monkeypatch,pdf,text,title)
    assert result.ok and result.similarity>=.55

def test_published_and_preprint_wording_dashes_and_banner(monkeypatch,pdf):
    title='Visualization of the Zhang–Rice singlet, electron orbitals and correlations'
    text='PHYSICAL REVIEW LETTERS 135, 123456 (2025)\narXiv:2506.12345v1 [cond-mat.str-el] 17 Jun 2025\nVisualizing the Zhang-Rice singlet, molecular orbitals\nand correlations\nAlex Martin and B. Lee\nAbstract\nWe image quantum states in materials.'
    assert check(monkeypatch,pdf,text,title).ok

def test_unicode_normalization():
    assert v.normalize_title('Café – ﬁelds / θ')==v.normalize_title('Cafe - fields theta')

@pytest.mark.parametrize('text', ['a', 'a\n'*80, 'arXiv:2506.12345v1 [gr-qc] 17 Jun 2025\na'])
def test_single_letter_never_passes(monkeypatch,pdf,text):
    assert not check(monkeypatch,pdf,text,'A study of gravitational quantum fields').ok

def test_unrelated_even_with_doi_and_author(monkeypatch,pdf):
    text='Economic development of agricultural markets\nLee\n10.1234/test\nLongitudinal surveys of grain prices and household income.'
    assert not check(monkeypatch,pdf,text,'Chiral molecular intercalation superlattices',expected_doi='10.1234/test',expected_authors=['Lee']).ok

def test_supporting_evidence_rescues_only_borderline(monkeypatch,pdf):
    title='Quantum chiral molecular intercalation superlattices'
    text='Molecular intercalation and electronic order in layered solids\nLee\n10.1234/test\nAn investigation of layer structure.'
    # Same title score, with and without independent identity evidence.
    score=v.title_similarity(title,text)
    assert .35<=score<.9
    assert not check(monkeypatch,pdf,text,title,similarity_threshold=.9).ok
    assert check(monkeypatch,pdf,text,title,similarity_threshold=.9,expected_authors=['Lee']).ok

def test_blank_scan_requires_explicit_inbox_match(monkeypatch,pdf):
    assert not check(monkeypatch,pdf,'','A long paper title').ok
    allowed=check(monkeypatch,pdf,'','A long paper title',allow_scanned=True)
    assert allowed.ok and '扫描件' in allowed.reason

def test_parser_exception_cannot_escape(monkeypatch,pdf):
    def fail(path): raise RuntimeError('private parser content')
    monkeypatch.setattr(v,'pdf_pages_and_text',fail)
    result=v.check_pdf(pdf,expected_title='A long paper title',min_bytes=1)
    assert not result.ok and 'private' not in result.reason

def test_worker_all_pages_extraction_failed(monkeypatch):
    class Page:
        def extract_text(self): raise ValueError('private text')
    class Reader:
        root_object={'/Pages':{'/Count':3}}
        pages=[Page(),Page(),Page()]
    monkeypatch.setattr('pypdf.PdfReader',lambda *a,**k: Reader())
    assert worker.inspect_pdf('unused')=={'error':'text extraction failed'}

def test_worker_caps_pages_inspected(monkeypatch):
    calls=[]
    class Page:
        def extract_text(self): calls.append(1); return 'Readable paper title'
    class Reader:
        root_object={'/Pages':{'/Count':100}}
        pages=[Page() for _ in range(100)]
    monkeypatch.setattr('pypdf.PdfReader',lambda *a,**k: Reader())
    assert worker.inspect_pdf('unused')['pages']==100 and len(calls)==2

def test_worker_rejects_absurd_declared_page_count(monkeypatch):
    class Reader:
        root_object={'/Pages':{'/Count':worker.MAX_PAGES+1}}
        @property
        def pages(self): pytest.fail('must reject before walking tree')
    monkeypatch.setattr('pypdf.PdfReader',lambda *a,**k: Reader())
    assert 'error' in worker.inspect_pdf('unused')

def test_subprocess_timeout_is_failure(monkeypatch,pdf):
    def run(*args,**kwargs):
        assert kwargs['timeout']==v.PARSE_TIMEOUT_SECONDS
        raise v.subprocess.TimeoutExpired('worker',10)
    monkeypatch.setattr(v.subprocess,'run',run)
    assert v.pdf_pages_and_text(pdf)==(0,'')

def test_real_worker_reads_blank_pdf_in_subprocess(tmp_path):
    from pypdf import PdfWriter
    path=tmp_path/'blank.pdf'
    writer=PdfWriter();writer.add_blank_page(width=72,height=72)
    with path.open('wb') as stream: writer.write(stream)
    assert v.pdf_pages_and_text(path)==(1,'')

@pytest.mark.parametrize('text',['10.1234/test-extra','prefix10.1234/test','10.1234/testing'])
def test_doi_support_is_exact_not_prefix(text):
    assert not v.doi_in_text('10.1234/test',text)

def test_doi_support_accepts_punctuation_and_case():
    assert v.doi_in_text('10.1234/test','DOI: https://doi.org/10.1234/TEST).')
    assert not v.doi_in_text('a','a')

def test_worker_supports_legacy_reader_root(monkeypatch):
    class Page:
        def extract_text(self):return 'A complete readable paper title'
    class LegacyReader:
        trailer={'/Root':{'/Pages':{'/Count':1}}}
        pages=[Page()]
    monkeypatch.setattr('pypdf.PdfReader',lambda *a,**k:LegacyReader())
    assert worker.inspect_pdf('unused')['pages']==1

def test_parser_subprocess_does_not_inherit_source_credentials(monkeypatch,pdf):
    monkeypatch.setenv('CORE_API_KEY','private-source-key')
    def run(*args,**kwargs):
        assert 'CORE_API_KEY' not in kwargs['env']
        return SimpleNamespace(returncode=0,stdout=json.dumps({'pages':1,'text':'Paper title and readable text'}).encode())
    monkeypatch.setattr(v.subprocess,'run',run)
    assert v.pdf_pages_and_text(pdf)[0]==1
