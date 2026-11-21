"""Local adapter for the #pdf[src] convention used by SciDownl (Tishacy/SciDownl, 3e66abd).

Does not import or run upstream code, discover mirrors, or use browser sessions.
"""
from html.parser import HTMLParser
import logging
import os
import re
import time
from urllib.parse import quote, urldefrag, urljoin, urlsplit

from zotero_pdf_pipeline.public_http import PublicHTTPError, request
from zotero_pdf_pipeline.safe_files import write_bytes

_MAX = 50 * 1024 * 1024
_LOG = logging.getLogger(__name__)
_DEFAULT_MIRRORS = ("https://sci-hub.ru", "https://sci-hub.su")
_MAX_MIRRORS = 3
_CHALLENGE = re.compile(
    rb"cf-challenge|cdn-cgi/challenge-platform|captcha|altcha-widget|verify you are human|type\s*=\s*['\"]password",
    re.I,
)


class _Viewer(HTMLParser):
    def __init__(self):
        super().__init__()
        self.src = None

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if self.src is None and fields.get("id") == "pdf":
            self.src = fields.get("src")


def _origin(url):
    u = urlsplit(url)
    if u.scheme != "https" or not u.hostname or u.username is not None or u.password is not None:
        raise ValueError("expected HTTPS without credentials")
    return u.hostname, u.port or 443


def _get(url, *, headers=None):
    return request(url, timeout=25, max_bytes=_MAX, headers=headers, _retry=False)


class Source:
    name = "scidownl"

    def __init__(self):
        self.last_request = None
        self.stopped = False

    def _request(self, url, *, headers=None):
        if self.last_request is not None:
            time.sleep(max(0, 5 - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        return _get(url, headers=headers)

    @staticmethod
    def _mirrors():
        configured = os.environ.get("ZPP_SCIHUB_BASE_URLS", "").strip()
        single = os.environ.get("ZPP_SCIHUB_BASE_URL", "").strip()
        candidates = configured.split(",") if configured else ([single] if single else _DEFAULT_MIRRORS)
        mirrors = {}
        for candidate in candidates:
            base = candidate.strip().rstrip("/")
            if not base:
                continue
            try:
                u = urlsplit(base)
                if u.query or u.fragment:
                    continue
                mirrors.setdefault(_origin(base), base)
            except ValueError:
                continue
            if len(mirrors) >= _MAX_MIRRORS:
                break
        return tuple(mirrors.values())

    def fetch(self, item_meta, dest_dir):
        if self.stopped or not re.fullmatch(r"10\.\d{4,9}/[^\s?#]+", item_meta.doi or ""):
            return None
        mirrors = self._mirrors()
        if not mirrors:
            _LOG.warning("No valid SciDownl mirror is configured")
        for base in mirrors:
            result = self._fetch_from(base, item_meta, dest_dir)
            if result is not None or self.stopped:
                return result
        return None

    def _fetch_from(self, base, item_meta, dest_dir):
        try:
            origin = _origin(base)
            page = self._request(base.rstrip("/") + "/" + quote(item_meta.doi, safe="/"))
            landing = str(page.url)
            if _origin(landing) != origin:
                return None
            body = page.content
            if not body.startswith(b"%PDF-"):
                if _CHALLENGE.search(body):
                    self.stopped = True
                    _LOG.warning("Verification or login required; SciDownl stopped for this run")
                    return None
                viewer = _Viewer()
                viewer.feed(page.text)
                if not viewer.src:
                    return None
                url = urldefrag(urljoin(landing, viewer.src))[0]
                if _origin(url) != origin:
                    return None
                result = self._request(url, headers={"Referer": landing})
                if _origin(str(result.url)) != origin:
                    return None
                body = result.content
                if not body.startswith(b"%PDF-"):
                    if _CHALLENGE.search(body):
                        self.stopped = True
                    return None
            return write_bytes(dest_dir, item_meta.item_key + "-scidownl.pdf", body, max_bytes=_MAX)
        except PublicHTTPError as exc:
            if str(exc).startswith(("HTTP 401", "HTTP 403", "HTTP 429", "HTTP 503")):
                self.stopped = True
            _LOG.warning("SciDownl request rejected or unavailable")
            return None
        except (ValueError, OSError):
            _LOG.warning("SciDownl URL or file rejected")
            return None


source = Source()
