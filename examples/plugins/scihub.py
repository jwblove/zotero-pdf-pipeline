"""Sci-Hub plugin illustration for personal research use.

Use may be unlawful in some jurisdictions; the user is responsible. This is an
illustration of the plugin protocol and is neither endorsed nor hosted here.
"""
from __future__ import annotations
import logging
import os
import re
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlsplit
import httpx
from zotero_pdf_pipeline.public_http import request, PublicHTTPError, make_public_client, MAX_HTML_BYTES
from zotero_pdf_pipeline.safe_files import safe_key, write_bytes, DEFAULT_MAX_PDF_BYTES

_LOG = logging.getLogger(__name__)
_LOCK = threading.Lock()
_LAST = 0.0

class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        tag = tag.lower()
        if tag == 'meta' and data.get('name', '').lower() == 'citation_pdf_url':
            self.links.append(data.get('content', ''))
        elif tag in ('embed', 'iframe'):
            self.links.append(data.get('src', ''))
        elif tag == 'object':
            self.links.append(data.get('data', ''))

def _valid_https(url):
    try:
        parts = urlsplit(url)
        return parts.scheme == 'https' and bool(parts.hostname) and not parts.username and not parts.password
    except ValueError:
        return False

def _throttle(url):
    global _LAST
    with _LOCK:
        now = time.monotonic()
        if _LAST:
            time.sleep(max(0, 5 - (now - _LAST)))
        _LAST = time.monotonic()

def _request(url):
    # The shared transport guards every redirect and pins validated public DNS.
    return request(url, 45, max_bytes=DEFAULT_MAX_PDF_BYTES,
                   client_factory=make_public_client, rate_limit=_throttle, _retry=False)

def _challenge(content):
    text = content[:100000 if content.startswith(b'%PDF-') else MAX_HTML_BYTES].lower()
    return any(word in text for word in (b'captcha', b'cf-challenge', b'cloudflare challenge', b'please verify you are human', b'type="password"', b"type='password'"))

class Source:
    name = 'scihub'
    def fetch(self, item_meta, dest_dir: Path) -> Path | None:
        base = os.environ.get('ZPP_SCIHUB_BASE_URL', '').strip()
        if not base:
            _LOG.warning('Plugin base URL is not configured')
            return None
        if not _valid_https(base) or urlsplit(base).query or urlsplit(base).fragment:
            _LOG.warning('Plugin base URL must use HTTPS without credentials')
            return None
        if not item_meta.doi:
            return None
        try:
            safe_key(item_meta.item_key)
            page_url = base.rstrip('/') + '/' + quote(item_meta.doi, safe='/')
            page = _request(page_url)
            if _challenge(page.content): return None
            if page.content.startswith(b'%PDF-'):
                content = page.content
            else:
                if len(page.content) > MAX_HTML_BYTES: return None
                parser = _Links()
                parser.feed(page.text)
                content = None
                for link in parser.links[:20]:
                    link = urljoin(str(page.url), link)
                    viewer_file = parse_qs(urlsplit(link).query).get('file', [])
                    if viewer_file: link = urljoin(link, viewer_file[0])
                    if not _valid_https(link): continue
                    pdf = _request(link)
                    # A challenge on any hop ends this attempt immediately.
                    if _challenge(pdf.content): return None
                    if pdf.content.startswith(b'%PDF-'):
                        content = pdf.content
                        break
                if content is None: return None
            return write_bytes(dest_dir, f'{item_meta.item_key}-candidate.pdf', content)
        except (PublicHTTPError, httpx.HTTPError, OSError, ValueError):
            _LOG.warning('Plugin request or file operation failed (public HTTPS, size or cooldown guard)')
            return None

source = Source()
