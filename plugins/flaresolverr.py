"""Local FlareSolverr plugin for Zotero PDF pipeline.

Uses a local FlareSolverr instance (default http://127.0.0.1:8191/v1) to render
protected or JS-dependent landing pages, extracts candidate PDF URLs, and
downloads the PDF via guarded public HTTPS using the matching User-Agent and
domain-scoped cookies.

Does not solve interactive CAPTCHAs, bypass logins, or write outside dest_dir.
"""
from __future__ import annotations

from html.parser import HTMLParser
import ipaddress
import logging
import os
import re
import time
from urllib.parse import parse_qs, quote, urldefrag, urljoin, urlsplit
import zlib

import httpx

from zotero_pdf_pipeline.public_http import (
    GENERIC_UA,
    MAX_HTML_BYTES,
    MAX_REDIRECTS,
    PublicHTTPError,
    _budget,
    _network_deadline,
    clean_referer,
    guard_url,
    make_public_client,
)
from zotero_pdf_pipeline.safe_files import safe_key, write_bytes

_LOG = logging.getLogger(__name__)
_MAX_PDF_BYTES = 50 * 1024 * 1024
_MAX_SOLVER_RESPONSE_BYTES = 16 * 1024 * 1024
_DEFAULT_FLARESOLVERR_URL = "http://127.0.0.1:8191/v1"
_DEFAULT_TARGETS = ("https://doi.org",)
_MAX_TARGETS = 3
_MAX_CANDIDATE_LINKS = 5
_MIN_INTERVAL_SECONDS = 5.0
_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s?#]+")
_SESSION_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_CONTROL_RE = re.compile(r"[\r\n\x00-\x1f\x7f]")
_HARD_CHALLENGE_RE = re.compile(
    r"<title[^>]*>\s*(?:just a moment|attention required|ddos-guard|verify you are human|access denied|sci-hub:\s*are you)"
    r"|cf-challenge|altcha-widget|altcha\.min\.js|ddg-captcha|please verify you are human|verify you are human|are you (?:a|are) robot",
    re.I,
)
_SOFT_CHALLENGE_RE = re.compile(
    r"cdn-cgi/challenge-platform|challenges\.cloudflare\.com"
    r"|g-recaptcha|h-captcha|hcaptcha|cf-turnstile|captcha|type\s*=\s*['\"]password",
    re.I,
)
_DIRECT_PDF_HREF_RE = re.compile(r"(?:\.pdf(?:[?#]|$)|/(?:doi/)?(?:pdf|pdfdirect)(?:/|[?#]|$)|/pdfft(?:[?#]|$)|[?&]file=)", re.I)
_EPDF_HREF_RE = re.compile(r"/(?:doi/)?epdf(?:/|[?#]|$)", re.I)
_NON_PDF_ASSET_RE = re.compile(
    r"\.(?:jpe?g|png|gif|webp|svg|bmp|ico|tiff?|css|js|mjs|zip|rar|7z|tar|gz|mp[34]|avi|mov|webm|ris|bib|enw|csv|txt|xml|json)(?:[?#]|$)",
    re.I,
)
_PII_RE = re.compile(r"/science/article/pii/([A-Za-z0-9]+)", re.I)
_IGNORE_EMBED_RE = re.compile(
    r"googletagmanager|doubleclick|googlesyndication|facebook\.com|twitter\.com"
    r"|youtube\.com|vimeo\.com|cookiebot|onetrust|recaptcha|hcaptcha|turnstile",
    re.I,
)


class _PDFLinkExtractor(HTMLParser):
    """Extract candidate PDF URLs ordered from highest to lowest confidence."""

    def __init__(self) -> None:
        super().__init__()
        self.meta_pdf: list[str] = []
        self.id_pdf: list[str] = []
        self.embedded: list[str] = []
        self.direct_anchors: list[str] = []
        self.secondary_anchors: list[str] = []
        self.has_pdf_viewer_embed = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        data = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta" and data.get("name", "").lower() == "citation_pdf_url":
            if data.get("content"):
                self.meta_pdf.append(data["content"])
            return
        if data.get("id") == "pdf" and data.get("src"):
            self.id_pdf.append(data["src"])
            return
        if tag in ("embed", "iframe"):
            src = data.get("src", "")
            is_pdf_type = data.get("type", "").lower() == "application/pdf"
            if is_pdf_type:
                self.has_pdf_viewer_embed = True
            if src and not _IGNORE_EMBED_RE.search(src) and (
                is_pdf_type or _DIRECT_PDF_HREF_RE.search(src) or _EPDF_HREF_RE.search(src) or tag == "embed" or src.startswith("/")
            ):
                self.embedded.append(src)
        elif tag == "object":
            obj_data = data.get("data", "")
            is_pdf_type = data.get("type", "").lower() == "application/pdf"
            if is_pdf_type:
                self.has_pdf_viewer_embed = True
            if obj_data and not _IGNORE_EMBED_RE.search(obj_data):
                self.embedded.append(obj_data)
        elif tag == "a":
            href = data.get("href", "")
            if not href:
                return
            if _DIRECT_PDF_HREF_RE.search(href):
                self.direct_anchors.append(href)
            elif _EPDF_HREF_RE.search(href):
                self.secondary_anchors.append(href)
            elif "download" in data and not _NON_PDF_ASSET_RE.search(href):
                self.secondary_anchors.append(href)

    def raw_links(self) -> list[str]:
        return self.meta_pdf + self.id_pdf + self.embedded + self.direct_anchors + self.secondary_anchors


def _valid_https(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (
            parts.scheme == "https"
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
        )
    except ValueError:
        return False


def _validate_solver_url(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return _DEFAULT_FLARESOLVERR_URL
    parts = urlsplit(text)
    _ = parts.port  # urllib validates the numeric range on attribute access.
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ValueError("invalid FlareSolverr URL")
    host = parts.hostname.lower().strip("[]")
    if host != "localhost":
        try:
            addr = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("FlareSolverr URL must target loopback") from exc
        if not addr.is_loopback:
            raise ValueError("FlareSolverr URL must target loopback")
    path = parts.path.rstrip("/")
    if not path:
        path = "/v1"
    return f"{parts.scheme}://{parts.netloc}{path}"


def _build_target_url(template: str, doi: str) -> str:
    if "{doi}" in template:
        parts = urlsplit(template)
        if "{doi}" in (parts.query or ""):
            encoded = quote(doi, safe="")
        else:
            encoded = quote(doi, safe="/")
        return template.replace("{doi}", encoded)
    return template.rstrip("/") + "/" + quote(doi, safe="/")


def _candidate_urls(landing_url: str, parser: _PDFLinkExtractor) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    pii_match = _PII_RE.search(urlsplit(landing_url).path)
    landing_pii = pii_match.group(1).upper() if pii_match else None
    for raw in parser.raw_links():
        cleaned = (raw or "").strip()
        if not cleaned or cleaned.startswith(("#", "about:", "data:", "javascript:", "blob:")):
            continue
        try:
            resolved = urldefrag(urljoin(landing_url, cleaned))[0]
            if not _valid_https(resolved):
                continue
            cand_pii_match = _PII_RE.search(urlsplit(resolved).path)
            if landing_pii and cand_pii_match and cand_pii_match.group(1).upper() != landing_pii:
                continue
            viewer_file = parse_qs(urlsplit(resolved).query).get("file")
            if viewer_file and viewer_file[0]:
                unwrapped = urldefrag(urljoin(resolved, viewer_file[0].strip()))[0]
                if _valid_https(unwrapped):
                    resolved = unwrapped
            if _valid_https(resolved) and resolved not in seen:
                seen.add(resolved)
                ordered.append(resolved)
        except ValueError:
            continue
        if len(ordered) >= _MAX_CANDIDATE_LINKS:
            break
    # If Chromium rendered its built-in PDF viewer (or no links were extracted),
    # try fetching the landing URL directly with the cleared cookies & UA.
    landing_clean = urldefrag(landing_url)[0]
    if (
        (
            parser.has_pdf_viewer_embed
            or not ordered
            or _DIRECT_PDF_HREF_RE.search(urlsplit(landing_clean).path)
        )
        and _valid_https(landing_clean)
        and landing_clean not in seen
        and len(ordered) < _MAX_CANDIDATE_LINKS
    ):
        ordered.append(landing_clean)
    return ordered


def _cookie_matches(cookie: dict, target_url: httpx.URL, now: float) -> bool:
    name = cookie.get("name")
    value = cookie.get("value")
    if not isinstance(name, str) or not isinstance(value, str) or not name:
        return False
    if _CONTROL_RE.search(name) or _CONTROL_RE.search(value) or ";" in name or "=" in name or ";" in value:
        return False
    expiry = cookie.get("expiry")
    if expiry is not None:
        try:
            if float(expiry) < now:
                return False
        except (TypeError, ValueError):
            return False
    host = (target_url.host or "").lower().rstrip(".")
    raw_domain = cookie.get("domain")
    if not isinstance(raw_domain, str) or not raw_domain.strip():
        return False
    domain = raw_domain.strip().lower().rstrip(".")
    if domain.startswith("."):
        base_domain = domain.lstrip(".")
        if not base_domain or (host != base_domain and not host.endswith("." + base_domain)):
            return False
    else:
        if host != domain:
            return False
    cookie_path = cookie.get("path") or "/"
    if not isinstance(cookie_path, str) or not cookie_path.startswith("/"):
        cookie_path = "/"
    req_path = target_url.path or "/"
    if cookie_path != "/":
        if req_path != cookie_path:
            if cookie_path.endswith("/"):
                if not req_path.startswith(cookie_path):
                    return False
            elif not req_path.startswith(cookie_path + "/"):
                return False
    return True


def _cookie_header(cookies: list[dict], target_url: httpx.URL, now: float | None = None) -> str:
    if not cookies:
        return ""
    current_time = time.time() if now is None else now
    pairs: list[str] = []
    seen_names: set[str] = set()
    for item in cookies:
        if not isinstance(item, dict):
            continue
        if _cookie_matches(item, target_url, current_time):
            name = item["name"]
            if name not in seen_names:
                seen_names.add(name)
                pairs.append(f"{name}={item['value']}")
    return "; ".join(pairs)


def _sanitize_ua(raw: object) -> str:
    if not isinstance(raw, str):
        return GENERIC_UA
    cleaned = _CONTROL_RE.sub("", raw).strip()
    if not cleaned or len(cleaned) > 512:
        return GENERIC_UA
    return cleaned


def _has_hard_challenge(text: str | bytes) -> bool:
    if isinstance(text, bytes):
        sample = text[:100_000].decode("latin-1", errors="ignore")
    else:
        sample = text[:100_000]
    return bool(_HARD_CHALLENGE_RE.search(sample))


def _has_any_challenge(text: str | bytes) -> bool:
    if isinstance(text, bytes):
        sample = text[:100_000].decode("latin-1", errors="ignore")
    else:
        sample = text[:100_000]
    return bool(_HARD_CHALLENGE_RE.search(sample) or _SOFT_CHALLENGE_RE.search(sample))


def _read_bounded_body(response: httpx.Response, *, max_pdf_bytes: int, max_non_pdf_bytes: int) -> bytes:
    encoding = response.headers.get("Content-Encoding", "").lower().strip()
    if encoding not in ("", "identity", "gzip", "deflate"):
        raise PublicHTTPError("unsupported content encoding")
    raw_len = response.headers.get("Content-Length")
    if raw_len is not None:
        try:
            declared = int(raw_len)
        except ValueError as exc:
            raise PublicHTTPError("invalid Content-Length") from exc
        if declared < 0 or declared > max_pdf_bytes:
            raise PublicHTTPError("response size limit exceeded")

    prefix = b"%PDF-"
    body = bytearray()
    encoded_total = 0
    decoded_total = 0

    def append_piece(piece: bytes) -> None:
        nonlocal decoded_total
        if not piece:
            return
        decoded_total += len(piece)
        if len(body) < len(prefix):
            header = bytes(body) + piece[:len(prefix) - len(body)]
            possible_pdf = prefix.startswith(header)
        else:
            possible_pdf = body.startswith(prefix)
        limit = max_pdf_bytes if possible_pdf else max_non_pdf_bytes
        if decoded_total > limit:
            raise PublicHTTPError("response size limit exceeded")
        body.extend(piece)

    if response.is_stream_consumed:
        append_piece(response.content)
        _budget(None)
        if not body.startswith(prefix) and len(body) > max_non_pdf_bytes:
            raise PublicHTTPError("response size limit exceeded")
        return bytes(body)

    decoder = (
        zlib.decompressobj(31 if encoding == "gzip" else 15)
        if encoding in ("gzip", "deflate")
        else None
    )
    try:
        for chunk in response.iter_raw():
            _budget(None)
            encoded_total += len(chunk)
            if encoded_total > max_pdf_bytes:
                raise PublicHTTPError("encoded response size limit exceeded")
            if decoder is None:
                append_piece(chunk)
                continue
            pending = chunk
            while True:
                if len(body) < len(prefix) and prefix.startswith(bytes(body)):
                    # Read only enough decoded bytes to determine %PDF- vs non-PDF before lifting the cap.
                    budget = len(prefix) - len(body)
                else:
                    limit = max_pdf_bytes if body.startswith(prefix) else max_non_pdf_bytes
                    if decoded_total > limit:
                        raise PublicHTTPError("response size limit exceeded")
                    budget = min(65536, limit - decoded_total + 1)
                decoded = decoder.decompress(pending, budget)
                if decoder.unused_data:
                    raise PublicHTTPError("trailing compressed response data")
                append_piece(decoded)
                pending = decoder.unconsumed_tail
                if decoder.eof or (not pending and len(decoded) < budget):
                    break
        _budget(None)
        if decoder and not decoder.eof:
            raise PublicHTTPError("incomplete compressed response")
    except zlib.error as exc:
        raise PublicHTTPError("invalid compressed response") from exc
    if not body.startswith(prefix) and len(body) > max_non_pdf_bytes:
        raise PublicHTTPError("response size limit exceeded")
    return bytes(body)


def _host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


class Source:
    name = "flaresolverr"
    temporary_candidates = True

    def __init__(self) -> None:
        self.last_request: float | None = None
        self.blocked_hosts: set[str] = set()

    def _throttle(self, deadline: float | None = None) -> bool:
        if self.last_request is not None:
            wait = _MIN_INTERVAL_SECONDS - (time.monotonic() - self.last_request)
            if wait > 0:
                if deadline is not None and time.monotonic() + wait >= deadline:
                    return False
                time.sleep(wait)
        if deadline is not None and time.monotonic() >= deadline:
            return False
        self.last_request = time.monotonic()
        return True

    @staticmethod
    def _solver_url() -> str | None:
        raw = os.environ.get("ZPP_FLARESOLVERR_URL", "").strip()
        try:
            return _validate_solver_url(raw)
        except ValueError:
            _LOG.warning("ZPP_FLARESOLVERR_URL must be a valid loopback HTTP/HTTPS URL and port")
            return None

    @staticmethod
    def _solver_timeout() -> int:
        raw = os.environ.get("ZPP_FLARESOLVERR_TIMEOUT", "60").strip()
        try:
            seconds = int(raw)
        except ValueError:
            seconds = 60
        return max(10, min(120, seconds))

    @staticmethod
    def _targets() -> tuple[str, ...]:
        configured = os.environ.get("ZPP_FLARESOLVERR_BASE_URLS", "").strip()
        single = os.environ.get("ZPP_FLARESOLVERR_BASE_URL", "").strip()
        if configured:
            raw_list = configured.split(",")
        elif single:
            raw_list = [single]
        else:
            return _DEFAULT_TARGETS
        targets: list[str] = []
        seen: set[str] = set()
        for item in raw_list:
            candidate = item.strip()
            if not candidate:
                continue
            has_placeholder = "{doi}" in candidate
            probe = candidate.replace("{doi}", "10.1000/placeholder") if has_placeholder else candidate.rstrip("/")
            try:
                parts = urlsplit(probe)
            except ValueError:
                continue
            if (
                parts.scheme != "https"
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or parts.fragment
            ):
                continue
            if not has_placeholder and parts.query:
                continue
            normalized = candidate if has_placeholder else candidate.rstrip("/")
            key = normalized.lower()
            if key not in seen:
                seen.add(key)
                targets.append(normalized)
            if len(targets) >= _MAX_TARGETS:
                break
        return tuple(targets)

    def _solve_page(self, solver_url: str, target_url: str) -> dict | None:
        timeout_sec = self._solver_timeout()
        payload: dict[str, object] = {
            "cmd": "request.get",
            "url": target_url,
            "maxTimeout": timeout_sec * 1000,
        }
        session = os.environ.get("ZPP_FLARESOLVERR_SESSION", "").strip()
        if session and _SESSION_RE.fullmatch(session):
            payload["session"] = session
        if os.environ.get("ZPP_FLARESOLVERR_DISABLE_MEDIA", "").strip().lower() in ("1", "true", "yes"):
            payload["disableMedia"] = True
        wait_raw = os.environ.get("ZPP_FLARESOLVERR_WAIT_SECONDS", "").strip()
        if wait_raw:
            try:
                wait_sec = max(0, min(15, int(wait_raw)))
                if wait_sec > 0:
                    payload["waitInSeconds"] = wait_sec
            except ValueError:
                pass

        self._throttle()
        try:
            with httpx.Client(timeout=timeout_sec + 15, trust_env=False, follow_redirects=False) as client:
                with client.stream(
                    "POST",
                    solver_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                ) as response:
                    if response.status_code != 200:
                        _LOG.warning("FlareSolverr service returned non-200 status")
                        return None
                    buf = bytearray()
                    for chunk in response.iter_bytes():
                        buf.extend(chunk)
                        if len(buf) > _MAX_SOLVER_RESPONSE_BYTES:
                            _LOG.warning("FlareSolverr response exceeded size limit")
                            return None
                    data = httpx.Response(200, content=bytes(buf)).json()
        except (httpx.HTTPError, ValueError, OSError):
            _LOG.warning("FlareSolverr service unreachable or returned invalid response")
            return None
        if not isinstance(data, dict) or data.get("status") != "ok":
            message = str(data.get("message", "")) if isinstance(data, dict) else ""
            if _has_any_challenge(message):
                self.blocked_hosts.add(_host_of(target_url))
            _LOG.warning("FlareSolverr did not return an ok solution")
            return None
        solution = data.get("solution")
        return solution if isinstance(solution, dict) else None

    def _download_pdf(
        self,
        url: str,
        *,
        landing_url: str,
        cookies: list[dict],
        user_agent: str,
        timeout: float = 45.0,
    ) -> bytes | None:
        current = url
        deadline = time.monotonic() + timeout
        for _ in range(MAX_REDIRECTS + 1):
            parsed, addresses = guard_url(current)
            host = (parsed.host or "").lower()
            if host in self.blocked_hosts:
                return None
            if not self._throttle(deadline):
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            headers: dict[str, str] = {
                "User-Agent": user_agent,
                "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
                "Accept-Encoding": "identity",
            }
            referer = clean_referer(landing_url, str(parsed))
            if referer:
                headers["Referer"] = referer
            cookie_str = _cookie_header(cookies, parsed)
            if cookie_str:
                headers["Cookie"] = cookie_str
            try:
                with _network_deadline(deadline), make_public_client(timeout=remaining, follow_redirects=False) as client:
                    with client.stream(
                        "GET",
                        parsed,
                        headers=headers,
                        extensions={"zpp_addresses": addresses},
                    ) as response:
                        status = response.status_code
                        if status in (301, 302, 303, 307, 308):
                            location = response.headers.get("Location", "")
                            if not location:
                                return None
                            current = urljoin(str(parsed), location)
                            continue
                        if status in (401, 403, 429, 503):
                            if status != 403 or _EPDF_HREF_RE.search(parsed.path) is None:
                                self.blocked_hosts.add(host)
                            _LOG.warning("PDF download blocked by remote host status")
                            return None
                        if status != 200:
                            return None
                        body = _read_bounded_body(
                            response,
                            max_pdf_bytes=_MAX_PDF_BYTES,
                            max_non_pdf_bytes=MAX_HTML_BYTES,
                        )
            except PublicHTTPError:
                return None
            except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError):
                return None
            if body.startswith(b"%PDF-"):
                return body
            if _has_any_challenge(body):
                self.blocked_hosts.add(host)
                _LOG.warning("PDF endpoint returned verification challenge; host paused for run")
            return None
        return None

    def fetch(self, item_meta, dest_dir):
        doi = (getattr(item_meta, "doi", "") or "").strip()
        if not _DOI_RE.fullmatch(doi):
            return None
        try:
            item_key = safe_key(getattr(item_meta, "item_key", ""))
        except ValueError:
            return None
        solver_url = self._solver_url()
        if not solver_url:
            return None
        targets = self._targets()
        if not targets:
            _LOG.warning("No valid FlareSolverr target URLs configured")
            return None

        for template in targets:
            target_url = _build_target_url(template, doi)
            try:
                guarded_target, _ = guard_url(target_url)
            except PublicHTTPError:
                _LOG.warning("Rejected non-public or invalid target URL")
                continue
            target_host = (guarded_target.host or "").lower()
            if target_host in self.blocked_hosts:
                continue

            solution = self._solve_page(solver_url, str(guarded_target))
            if not solution:
                continue

            landing_url = str(solution.get("url") or guarded_target)
            try:
                guarded_landing, _ = guard_url(landing_url)
            except PublicHTTPError:
                _LOG.warning("FlareSolverr landed on non-public or invalid URL")
                continue
            landing_host = (guarded_landing.host or "").lower()
            if landing_host in self.blocked_hosts:
                continue

            http_status = solution.get("status")
            if http_status in (401, 403, 429, 503):
                self.blocked_hosts.add(landing_host)
                _LOG.warning("Target host returned blocked HTTP status via FlareSolverr")
                continue
            if http_status not in (200, None):
                continue

            html_text = solution.get("response")
            if not isinstance(html_text, str):
                continue
            if _has_hard_challenge(html_text):
                self.blocked_hosts.add(landing_host)
                _LOG.warning("Verification or challenge page remains after FlareSolverr; host paused for run")
                continue

            parser = _PDFLinkExtractor()
            parser.feed(html_text)
            extracted = bool(parser.raw_links() or parser.has_pdf_viewer_embed)
            if not extracted and _has_any_challenge(html_text):
                self.blocked_hosts.add(landing_host)
                _LOG.warning("Verification or login wall detected without PDF links; host paused for run")
                continue
            candidates = _candidate_urls(str(guarded_landing), parser)
            if not candidates:
                continue

            cookies = solution.get("cookies")
            cookie_list = cookies if isinstance(cookies, list) else []
            user_agent = _sanitize_ua(solution.get("userAgent"))

            for candidate in candidates:
                try:
                    pdf_bytes = self._download_pdf(
                        candidate,
                        landing_url=str(guarded_landing),
                        cookies=cookie_list,
                        user_agent=user_agent,
                    )
                except PublicHTTPError:
                    continue
                if pdf_bytes and pdf_bytes.startswith(b"%PDF-"):
                    try:
                        return write_bytes(
                            dest_dir,
                            f"{item_key}-flaresolverr.pdf",
                            pdf_bytes,
                            max_bytes=_MAX_PDF_BYTES,
                        )
                    except (OSError, ValueError):
                        _LOG.warning("Failed to write candidate PDF to destination directory")
                        return None
        return None


source = Source()
