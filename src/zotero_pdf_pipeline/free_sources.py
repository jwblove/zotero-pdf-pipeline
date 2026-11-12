"""免费 PDF 源（OpenAlex / Unpaywall / arXiv）及可审计的逐源诊断。

成功返回 ResolvedFile；单源失败抛 FreeSourceError，try_all 负责记录并继续。
诊断不包含响应正文、下载 token、URL 查询串或代理/账号凭据。
"""
from __future__ import annotations

import logging
import os
import re
from html.parser import HTMLParser
from urllib.parse import urljoin
import time
from urllib.parse import quote, unquote
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .public_http import (make_public_client as make_client, request as public_request,
    PublicHTTPError, GENERIC_UA, MAX_HTML_BYTES, _rate_limit, _LAST_REQUEST, _RATE_SECONDS)
from .safe_files import DEFAULT_MAX_PDF_BYTES, regular_file, safe_key, write_bytes, copy_file
from .models import PaperNeed, ResolvedFile, Source
from .naming import build_filename

LOG = logging.getLogger("pipeline.free")
OPENALEX_API = "https://api.openalex.org/works/doi:{doi}"
UNPAYWALL_API = "https://api.unpaywall.org/v2/{doi}"
_CONTACT_EMAIL = ""
_MAX_PDF_BYTES = DEFAULT_MAX_PDF_BYTES

def _user_agent() -> str:
    return GENERIC_UA + (f" (contact: {_CONTACT_EMAIL})" if _CONTACT_EMAIL else "")


class FreeSourceError(RuntimeError):
    """已脱敏、可记录的单源失败原因（不表示论文本身没有开放版本）。"""


def _request(url: str, timeout: int, **kwargs) -> httpx.Response:
    try:
        return public_request(url, timeout, client_factory=make_client, rate_limit=_rate_limit, **kwargs)
    except PublicHTTPError as exc:
        raise FreeSourceError(str(exc)) from exc


def _query_json(url: str, timeout: int, **kwargs) -> dict:
    try:
        response = _request(url, timeout, headers={"User-Agent": _user_agent()}, **kwargs)
    except FreeSourceError as exc:
        if str(exc) == "HTTP 404":
            raise FreeSourceError("HTTP 404（API 查询无记录）") from exc
        raise
    try:
        data = response.json()
    except ValueError as exc:
        raise FreeSourceError("API 返回无效 JSON") from exc
    if not isinstance(data, dict):
        raise FreeSourceError("API 返回非对象 JSON")
    return data


def _download(url: str, dest: Path, timeout: int = 45, referer: str | None = None) -> Path:
    """只有 PDF 魔数通过才落盘；HTTP/内容/磁盘错误交由源诊断记录。"""
    headers = {"User-Agent": GENERIC_UA}
    if referer:
        headers["Referer"] = referer
    response = _request(url, timeout, headers=headers, max_bytes=_MAX_PDF_BYTES)
    if not response.content.startswith(b"%PDF-"):
        raise FreeSourceError(f"响应不是 PDF（{len(response.content)} 字节）")
    try:
        dest = write_bytes(dest.parent, dest.name, response.content, max_bytes=_MAX_PDF_BYTES)
    except (OSError, ValueError) as exc:
        raise FreeSourceError(f"PDF 写盘失败（{type(exc).__name__}）") from exc
    LOG.info("免费源下载成功：%s（%d 字节）", dest.name, len(response.content))
    return dest


class _CitationMeta(HTMLParser):
    def __init__(self):
        super().__init__()
        self.pdf_url = ""
    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag.lower() == "meta" and fields.get("name", "").lower() == "citation_pdf_url":
            self.pdf_url = fields.get("content", "")

def _landing_pdf(url: str, timeout: int) -> str:
    response = _request(url, timeout, headers={"User-Agent": GENERIC_UA}, max_bytes=MAX_HTML_BYTES)
    parser = _CitationMeta()
    parser.feed(response.text)
    pdf = urljoin(str(response.url), parser.pdf_url) if parser.pdf_url else ""
    if urlsplit(pdf).scheme != "https":
        raise FreeSourceError("落地页未提供有效 PDF 链接")
    return pdf

def _download_candidates(paper: PaperNeed, staging: Path, timeout: int,
                         candidates: list[tuple], source: Source) -> ResolvedFile:
    if not candidates:
        raise FreeSourceError("有元数据记录，但未提供开放 PDF 链接")
    failures = []
    dest = staging / build_filename(_authors_of(paper), paper.year, paper.title)
    for index, candidate in enumerate(candidates, 1):
        url, landing = candidate[:2]
        license_value = str(candidate[2] or "") if len(candidate) > 2 else ""
        oa_status = str(candidate[3] or "") if len(candidate) > 3 else ""
        try:
            if not url:
                url = _landing_pdf(landing, timeout)
            result = _download(url, dest, timeout, referer=landing or None)
        except FreeSourceError as exc:
            failures.append(f"候选{index}：{exc}")
            continue
        return ResolvedFile(paper=paper, file_path=result, source=source, url=url,
                            bytes_size=result.stat().st_size, note=f"{source.value} 免费源",
                            license=license_value[:250], oa_status=oa_status[:100])
    raise FreeSourceError("PDF 下载均失败；" + "；".join(failures))

def try_openalex(paper: PaperNeed, staging: Path, timeout: int = 45) -> ResolvedFile:
    if not paper.doi: raise FreeSourceError("跳过：条目缺 DOI")
    if not _CONTACT_EMAIL: raise FreeSourceError("跳过：未配置联系邮箱")
    data = _query_json(OPENALEX_API.format(doi=quote(paper.doi, safe="/")), timeout, params={"mailto": _CONTACT_EMAIL})
    best = data.get("best_oa_location") or {}
    status = (data.get("open_access") or {}).get("oa_status") or ""
    candidates = []
    for location in [best, *(data.get("locations") or [])]:
        if not isinstance(location, dict): continue
        url, landing = location.get("pdf_url") or "", location.get("landing_page_url") or ""
        if url or (landing and (location is best or location.get("is_oa") is True)):
            candidates.append((url, landing, location.get("license") or "", status))
            if url and landing and (location is best or location.get("is_oa") is True):
                candidates.append(("", landing, location.get("license") or "", status))
    return _download_candidates(paper, staging, timeout, candidates, Source.OPENALEX)

def try_unpaywall(paper: PaperNeed, staging: Path, timeout: int = 45) -> ResolvedFile:
    if not paper.doi: raise FreeSourceError("跳过：条目缺 DOI")
    if not _CONTACT_EMAIL: raise FreeSourceError("跳过：未配置联系邮箱")
    data = _query_json(UNPAYWALL_API.format(doi=quote(paper.doi, safe="/")), timeout, params={"email": _CONTACT_EMAIL})
    candidates = []
    for location in [data.get("best_oa_location") or {}, *(data.get("oa_locations") or [])]:
        if not isinstance(location, dict): continue
        url, landing = location.get("url_for_pdf") or "", location.get("url_for_landing_page") or ""
        if url or landing:
            candidates.append((url, landing, location.get("license") or "", data.get("oa_status") or ""))
            if url and landing:
                candidates.append(("", landing, location.get("license") or "", data.get("oa_status") or ""))
    return _download_candidates(paper, staging, timeout, candidates, Source.UNPAYWALL)

def lookup_crossref(paper: PaperNeed, timeout: int, threshold: float, contact_email: str) -> str:
    """Return a matching DOI without modifying the input or Zotero."""
    from .validate import title_similarity
    global _CONTACT_EMAIL
    _CONTACT_EMAIL = contact_email.strip()
    if not contact_email: raise FreeSourceError("跳过 Crossref：未配置联系邮箱")
    if not paper.title: raise FreeSourceError("跳过 Crossref：条目缺标题")
    params = {"query.bibliographic": paper.title, "rows": 5, "mailto": contact_email}
    if paper.authors and paper.authors[0]: params["query.author"] = paper.authors[0]
    data = _query_json("https://api.crossref.org/works", timeout, params=params)
    candidates = (data.get("message") or {}).get("items") or []
    for item in candidates:
        titles = item.get("title") or []
        if not titles or title_similarity(paper.title, titles[0]) < threshold: continue
        if paper.authors and paper.authors[0]:
            authors = item.get("author") or []
            if not any(paper.authors[0].casefold() in str(a.get("family") or a.get("name") or "").casefold() for a in authors): continue
        if paper.year:
            date = item.get("published") or item.get("issued") or {}
            parts = date.get("date-parts") or []
            if not parts or not parts[0] or str(parts[0][0]) != paper.year: continue
        doi = str(item.get("DOI") or "").strip().lower()
        if re.fullmatch(r"10\.\d{4,9}/\S+", doi): return doi
    raise FreeSourceError("Crossref 未找到可靠匹配")

def try_arxiv(paper: PaperNeed, staging: Path, timeout: int = 45) -> ResolvedFile:
    if not paper.title:
        raise FreeSourceError("跳过：条目缺标题")
    query = paper.title.strip().strip('"')[:180]
    response = _request("https://export.arxiv.org/api/query", timeout,
                        params={"search_query": f'ti:"{query}"', "max_results": 3},
                        headers={"User-Agent": _user_agent()})
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise FreeSourceError("API 返回无效 Atom XML") from exc
    namespace = {"a": "http://www.w3.org/2005/Atom"}
    entry = root.find("a:entry", namespace)
    if entry is None:
        raise FreeSourceError("标题检索无候选预印本")
    identifier = entry.findtext("a:id", default="", namespaces=namespace)
    if "/abs/" not in identifier:
        raise FreeSourceError("API 候选记录缺有效 arXiv ID")
    arxiv_id = identifier.split("/abs/", 1)[1]
    return _download_candidates(paper, staging, timeout,
                                [(f"https://arxiv.org/pdf/{arxiv_id}", "")], Source.ARXIV)


def _authors_of(paper: PaperNeed) -> list[str]:
    return list(getattr(paper, "authors", []) or [])


def try_semantic_scholar(paper: PaperNeed, staging: Path, timeout: int = 45) -> ResolvedFile:
    if not paper.doi:
        raise FreeSourceError("跳过：条目缺 DOI")
    data = _query_json("https://api.semanticscholar.org/graph/v1/paper/DOI:" + quote(paper.doi, safe=""), timeout, params={"fields": "openAccessPdf"})
    pdf = data.get("openAccessPdf") or {}
    url = pdf.get("url") if isinstance(pdf, dict) else None
    return _download_candidates(paper, staging, timeout, [(url, "")] if url else [], Source.SEMANTIC_SCHOLAR)

def try_europe_pmc(paper: PaperNeed, staging: Path, timeout: int = 45) -> ResolvedFile:
    pmcid = paper.pmcid.upper()
    if pmcid and not re.fullmatch(r"PMC[0-9]+", pmcid):
        raise FreeSourceError("PMCID 格式无效")
    if not paper.doi and not pmcid:
        raise FreeSourceError("跳过：条目缺 DOI 和 PMCID")
    query = f'EXT_ID:{pmcid}' if pmcid else f'DOI:"{paper.doi}"'
    data = _query_json("https://www.ebi.ac.uk/europepmc/webservices/rest/search", timeout,
                       params={"query": query, "format": "json", "resultType": "core"})
    results = (data.get("resultList") or {}).get("result") or []
    candidates = []
    for item in results:
        if pmcid and str(item.get("pmcid", "")).upper() != pmcid: continue
        if not pmcid and str(item.get("doi", "")).lower() != paper.doi.lower(): continue
        status = "open" if str(item.get("isOpenAccess", "")).upper() == "Y" else ""
        license_value = item.get("license") or ""
        for entry in (item.get("fullTextUrlList") or {}).get("fullTextUrl") or []:
            if entry.get("availability", "").lower() not in ("open access", "free") or not entry.get("url"):
                continue
            style = entry.get("documentStyle", "").lower()
            if style == "pdf":
                candidates.append((entry["url"], "", license_value, status))
            elif style == "html":
                candidates.append(("", entry["url"], license_value, status))
        if pmcid and status == "open":
            candidates.append((f"https://europepmc.org/articles/{pmcid}?pdf=render", "", license_value, status))
    return _download_candidates(paper, staging, timeout, candidates, Source.EUROPE_PMC)

def try_core(paper: PaperNeed, staging: Path, timeout: int = 45) -> ResolvedFile:
    key = os.environ.get("CORE_API_KEY", "").strip()
    if not key: raise FreeSourceError("跳过 CORE：未设置 API 密钥")
    if not paper.doi: raise FreeSourceError("跳过：条目缺 DOI")
    # CORE v3 accepts the key as an authorization header, never as a URL parameter.
    response = _request("https://api.core.ac.uk/v3/search/works", timeout,
        headers={"User-Agent": _user_agent(), "Authorization": f"Bearer {key}"},
        params={"q": f'doi:"{paper.doi}"', "limit": 10})
    try: data = response.json()
    except ValueError as exc: raise FreeSourceError("API 返回无效 JSON") from exc
    results = data.get("results", []) if isinstance(data, dict) else []
    candidates = []
    for item in results:
        if str(item.get("doi") or "").lower() != paper.doi.lower(): continue
        url = item.get("downloadUrl") or ""
        if url: candidates.append((url, ""))
    return _download_candidates(paper, staging, timeout, candidates, Source.CORE)

def try_inbox(paper: PaperNeed, staging: Path, inbox: Path, threshold: float) -> ResolvedFile:
    from .validate import pdf_pages_and_text, title_similarity, doi_in_text
    safe_key(paper.item_key)
    matches = []
    for path in sorted(inbox.iterdir()):
        if path.suffix.lower() != '.pdf' or not regular_file(path, _MAX_PDF_BYTES):
            continue
        pages, text = pdf_pages_and_text(path)
        if pages <= 0:
            continue
        # A user-named scanned PDF may be matched explicitly by its DOI or title.
        evidence = text if text.strip() else unquote(path.stem)
        if not text.strip():
            evidence = re.sub(r'\b(10\.\d{4,9})[_-]', r'\1/', evidence)
        doi_match = doi_in_text(paper.doi, evidence)
        title_match = bool(paper.title and title_similarity(paper.title, evidence[:32768]) >= threshold)
        if doi_match or title_match:
            matches.append(path)
    if len(matches) != 1:
        raise FreeSourceError("收件箱无唯一匹配 PDF")
    dest = copy_file(matches[0], staging, paper.item_key + '-inbox.pdf', max_bytes=_MAX_PDF_BYTES)
    return ResolvedFile(paper, dest, Source.INBOX, inbox_matched=True)

REGISTRY = {"openalex": try_openalex, "unpaywall": try_unpaywall, "arxiv": try_arxiv,
            "semantic_scholar": try_semantic_scholar, "europe_pmc": try_europe_pmc, "core": try_core}

def try_all(paper: PaperNeed, staging: Path, order: list[str], enable_arxiv: bool,
            timeout: int = 45, diagnostics: list[str] | None = None, *, contact_email: str = "",
            inbox_dir: Path | None = None, title_threshold: float = .55,
            allow_credentials: bool = True, max_pdf_bytes: int = DEFAULT_MAX_PDF_BYTES) -> ResolvedFile | None:
    global _CONTACT_EMAIL, _MAX_PDF_BYTES
    safe_key(paper.item_key)
    _CONTACT_EMAIL = contact_email.strip()
    _MAX_PDF_BYTES = max_pdf_bytes
    failures = diagnostics if diagnostics is not None else []
    for name in dict.fromkeys(order):
        if name == "arxiv" and not enable_arxiv:
            continue
        if name == "core" and not allow_credentials:
            failures.append("core: --free-only 跳过需要凭据的来源")
            continue
        try:
            if name == "inbox":
                if inbox_dir is None:
                    raise FreeSourceError("未配置收件箱")
                resolved = try_inbox(paper, staging, inbox_dir, title_threshold)
            elif name in REGISTRY:
                resolved = REGISTRY[name](paper, staging, timeout)
            else:
                raise FreeSourceError("未知源名称（检查 source_order）")
            if resolved:
                return resolved
            reason = "未返回 PDF"
        except FreeSourceError as exc:
            reason = str(exc)
        except Exception as exc:
            reason = f"源处理异常（{type(exc).__name__}）"
        detail = f"{name}: {reason}"
        failures.append(detail)
        LOG.warning("[%s] 免费源失败 %s", paper.item_key, detail)
    return None
