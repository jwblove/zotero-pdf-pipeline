"""Bounded PDF parsing and title-window comparison; unverifiable files fail closed."""
from __future__ import annotations
import difflib
import json
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from .models import normalize_doi
from .safe_files import DEFAULT_MAX_PDF_BYTES, regular_file

try:
    from pypdf import PdfReader  # required dependency; retained for availability checks
    _HAS_PYPDF = True
except ImportError:
    _HAS_PYPDF = False

MIN_TITLE_CHARS = 8
MIN_TEXT_CHARS = 20
MIN_AUTHOR_TEXT_CHARS = 120
MIN_AUTHOR_TEXT_WORDS = 20
PARSE_TIMEOUT_SECONDS = 10
_STOP = {'a','an','the','of','and','in','on','for','from','with','to','by','at','as','is','we','this'}

@dataclass
class CheckResult:
    ok: bool
    reason: str = ''
    pages: int = 0
    similarity: float = -1.0
    size: int = 0

def is_pdf_magic(path):
    try:
        if path.is_symlink(): return False
        with path.open('rb') as stream: return stream.read(5) == b'%PDF-'
    except OSError: return False

def _normal(text):
    text = unicodedata.normalize('NFKD', text or '').casefold()
    text = ''.join(c for c in text if not unicodedata.combining(c))
    text = text.replace('θ', 'theta').replace('æ','ae').replace('œ','oe')
    return ''.join(c if c.isalnum() else ' ' for c in text)

def normalize_title(text):
    return ''.join(_normal(text).split())

def doi_in_text(doi, text):
    expected = normalize_doi(doi)
    if not re.fullmatch(r'10\.\d{4,9}/\S+', expected): return False
    return any(normalize_doi(match.group()) == expected for match in
               re.finditer(r'\b10\.\d{4,9}/[^\s<>"\]]+', text, re.I))

def _author_normal(text):
    # PDF accent positioning may insert a space: Sin´ ead -> Sinead.
    text = re.sub(r"(?<=\w)[´`ˊˋˆ˜¨˚ˇ˘˙¸˛\u0300-\u036f]\s*(?=\w)", '', text)
    text = re.sub(r"(?<=\w)['’ʼ]\s*(?=\w)", '', text)
    return _normal(unicodedata.normalize('NFKC', text)).replace('ı', 'i')

def _surname_present(name, text):
    parts = _author_normal(name).split()
    if not parts: return False
    # Single Latin initials cannot identify an author; a CJK surname can.
    if len(''.join(parts)) < 2:
        return bool(re.fullmatch(r'[\u3400-\u9fff]', name) and name in text)
    pattern = r'\b' + r'\s+'.join(re.escape(part) for part in parts) + r'\b'
    # Keep the ordinary normalization too: a real accent at a word boundary
    # must not make the spacing-artifact repair join two separate names.
    return any(re.search(pattern, normalized) for normalized in (_author_normal(text), _normal(text)))

def _institutional_name(name):
    return bool(re.search(r'\b(?:collaboration|consortium|university|institute|institution|committee|'
        r'organization|organisation|team|group|society|laborator(?:y|ies)|agency|academy|council|'
        r'cent(?:er|re)|foundation|association|hospital|ministry|corporation)\b', name, re.I)
        or name.strip().upper() in {'CMS','ATLAS','LHCB','ALICE','WHO','UNESCO','UNICEF'})

def _author_header(text):
    page = text.split('\f', 1)[0]
    return '\n'.join(line.strip()[:2000] for line in page.splitlines() if line.strip()).splitlines()[:60]

def _collaboration_banner(lines):
    for line in lines:
        if re.match(r'^(?:The\s+)?(?:CMS|ATLAS|ALICE|LHCb)\b', line, re.I): return True
        if len(line.split()) <= 12 and re.search(r'\bcollaboration\b', line, re.I): return True
    return False

def _authors_missing(text, authors, score, institutional, inbox_matched):
    if not authors or institutional or inbox_matched or score >= .95: return False
    if any(_institutional_name(name) for name in authors): return False
    lines = _author_header(text)
    front = '\n'.join(lines)
    if (len(normalize_title(front)) < MIN_AUTHOR_TEXT_CHARS
            or len(_author_normal(front).split()) < MIN_AUTHOR_TEXT_WORDS
            or _collaboration_banner(lines)):
        return False
    searchable = [name for name in authors if len(normalize_title(name)) >= 2
                  or re.fullmatch(r'[\u3400-\u9fff]', name)]
    return bool(searchable) and not any(_surname_present(name, front) for name in searchable)

def _tokens(text):
    result = set()
    for token in _normal(text).split():
        if token in _STOP or len(token) < 2: continue
        if token.startswith('visualiz'): token = 'visualiz'
        elif len(token) > 5 and token.endswith('s'): token = token[:-1]
        result.add(token)
    return result

def _clean_lines(text):
    # Header patterns are anchored; never discard a title merely containing a journal word.
    text = re.sub(r'arxiv\s*:\s*\S+\s*(?:\[[^\]]+\])?\s*(?:\d{1,2}\s+[A-Za-z]+\s+\d{4})?', '', text, flags=re.I)
    lines = []
    for line in text.splitlines()[:80]:
        line = line.strip()
        if not line: continue
        if re.match(r'^(?:physical review (?:letters|[a-z])\b|journal of\b|nature (?:physics|materials|communications)\b|preprint\b|copyright\b|©|received\b|accepted\b|published\b|https?://|doi\s*:|\d+\s*$)', line, re.I): continue
        lines.append(line[:2000])
        if len(lines) >= 40: break
    return lines

def _pair(a, b):
    na, nb = normalize_title(a), normalize_title(b)
    if min(len(na), len(nb)) < MIN_TITLE_CHARS: return 0.0
    sequence = difflib.SequenceMatcher(None, na, nb, autojunk=False).ratio()
    ta, tb = _tokens(a), _tokens(b)
    common = ta & tb
    token_score = 2*len(common)/max(1,len(ta)+len(tb)) if len(common) >= 2 else 0.0
    # Only full expected-title containment is allowed; a fragment of the title is insufficient.
    if len(na) >= 16 and len(ta) >= 3 and na in nb: return 1.0
    return max(sequence, token_score)

def title_similarity(a, b):
    if len(normalize_title(a)) < MIN_TITLE_CHARS: return 0.0
    a = a[:512]
    lines = _clean_lines(b[:32768])
    best = 0.0
    for start in range(len(lines)):
        for length in range(1, min(6,len(lines)-start)+1):
            best = max(best, _pair(a, ' '.join(lines[start:start+length])))
    # A whole page can be emitted as one line. Compare bounded word windows too.
    words = ' '.join(lines).split()[:400]
    wanted = max(3,len(a.split()))
    for width in sorted({max(3,wanted-3), wanted, wanted+3, wanted*2}):
        for start in range(max(1,len(words)-width+1)):
            best = max(best, _pair(a, ' '.join(words[start:start+width])))
    return best

def pdf_pages_and_text(path, max_pages=2):
    """Inspect at most two pages in a CPU/memory/time-limited subprocess.

    (0, '') means parse/extraction failure or resource limit. Positive pages with
    empty text means a readable scanned PDF; it is not silently accepted.
    """
    if not _HAS_PYPDF or not regular_file(Path(path), 1024**3): return 0, ''
    # The parser needs import paths, not source credentials/proxy settings.
    environment = {'PYTHONPATH': str(Path(__file__).resolve().parents[1]), 'PYTHONIOENCODING': 'utf-8'}
    if os.name == 'nt' and os.environ.get('SystemRoot'):
        environment['SystemRoot'] = os.environ['SystemRoot']
    try:
        result = subprocess.run([sys.executable, '-m', 'zotero_pdf_pipeline.pdf_worker', str(path)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=PARSE_TIMEOUT_SECONDS,
            env=environment, check=False)
        if result.returncode or len(result.stdout)>256*1024: return 0, ''
        data = json.loads(result.stdout)
        if not isinstance(data,dict) or data.get('error'): return 0, ''
        return int(data['pages']), str(data['text'])[:32768]
    except Exception:
        return 0, ''

def check_pdf(path, expected_title='', expected_doi='', min_bytes=20480, similarity_threshold=.55,
              *, expected_authors=None, max_bytes=DEFAULT_MAX_PDF_BYTES, allow_scanned=False,
              institutional_authors=False, inbox_matched=False):
    """Authors/DOI rescue only borderline title matches, never unrelated/short text."""
    try:
        return _check(Path(path),expected_title,expected_doi,min_bytes,similarity_threshold,
                      expected_authors or [],max_bytes,allow_scanned,institutional_authors,inbox_matched)
    except Exception:
        return CheckResult(False, 'PDF 校验异常，需人工检查')

def _check(path,title,doi,min_bytes,threshold,authors,max_bytes,allow_scanned,institutional,inbox_matched):
    if not regular_file(path,max_bytes): return CheckResult(False,'文件缺失、非普通文件、符号链接或超过大小上限')
    size=path.stat().st_size
    if size<min_bytes: return CheckResult(False,'文件过小',size=size)
    if not is_pdf_magic(path): return CheckResult(False,'文件头不是 %PDF-',size=size)
    if not _HAS_PYPDF: return CheckResult(False,'缺少 pypdf，无法验证 PDF',size=size)
    pages,text=pdf_pages_and_text(path)
    if pages<=0: return CheckResult(False,'PDF 解析或文本提取失败/超时，需人工检查',size=size)
    compact=normalize_title(text)
    if not compact:
        if allow_scanned: return CheckResult(True,'扫描件无文本；仅凭收件箱中显式 DOI/标题匹配确认',pages=pages,size=size)
        return CheckResult(False,'扫描件无文本，无法自动核验，需人工检查',pages=pages,size=size)
    if len(compact)<MIN_TEXT_CHARS: return CheckResult(False,'提取文本过少，无法核验标题',pages=pages,size=size)
    score=title_similarity(title,text) if title else 0.0
    doi_match=doi_in_text(doi, text)
    surname=authors[0] if authors else ''
    author_match=_surname_present(surname, text)
    shared=_tokens(title)&_tokens(' '.join(_clean_lines(text)))
    supported=score>=.35 and len(shared)>=2 and len(shared)/max(1,len(_tokens(title)))>=.4 and (doi_match or author_match)
    if title and score>=threshold: reason='标题窗口匹配通过'
    elif title and supported: reason='边界标题匹配由 DOI/第一作者证据支持'
    elif not title and doi_match: reason='无期望标题；精确 DOI 匹配通过'
    else: return CheckResult(False,f'标题相似度不足（{score:.2f} < {threshold}），需人工检查',pages,score,size)
    if _authors_missing(text, authors, score, institutional, inbox_matched):
        return CheckResult(False,'需人工复核（needs manual review）：author names not found on first page（首页未找到条目作者姓氏）',pages,score,size)
    return CheckResult(True,reason,pages,score,size)
