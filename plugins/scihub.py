"""Sci-Hub plugin illustration for personal research use.

Use may be unlawful in some jurisdictions; the user is responsible. This is an
illustration of the plugin protocol and is neither endorsed nor hosted here.
"""
from __future__ import annotations
import logging
import os
import re
import shutil
import site
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, urljoin, urlsplit
import httpx

_LOG = logging.getLogger(__name__)
_LOCK = threading.Lock()
_LAST = 0.0
_BLOCKED_UNTIL = 0.0

class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        tag = tag.lower()
        if tag == 'meta' and (data.get('name') or '').lower() == 'citation_pdf_url':
            self.links.append(data.get('content', ''))
        elif tag in ('embed', 'iframe'):
            self.links.append(data.get('src', ''))
        elif tag == 'object':
            self.links.append(data.get('data', ''))

        elif tag == 'a':
            href = data.get('href') or ''
            if 'download' in data or re.search(r'\.pdf(?:[?#]|$)', href, re.I):
                self.links.append(href)

def _candidate_urls(base, links):
    seen = set()
    for raw in links:
        if not raw or not raw.strip() or raw.strip().startswith('#'):
            continue
        try:
            link = urljoin(base, raw.strip())
            if not _valid_https(link):
                continue
            viewer_file = parse_qs(urlsplit(link).query).get('file')
            if viewer_file:
                link = urljoin(link, viewer_file[0])
            if _valid_https(link) and link not in seen:
                seen.add(link)
                yield link
        except ValueError:
            continue

def _valid_https(url):
    try:
        parts = urlsplit(url)
        return parts.scheme == 'https' and bool(parts.hostname) and not parts.username and not parts.password
    except ValueError:
        return False

def _throttle():
    global _LAST
    with _LOCK:
        now = time.monotonic()
        if now < _BLOCKED_UNTIL:
            return False
        if _LAST:
            time.sleep(max(0, 5 - (now - _LAST)))
        _LAST = time.monotonic()
        return True

def _request(client, url):
    global _BLOCKED_UNTIL
    for _ in range(4):
        if not _valid_https(url): return None
        if not _throttle(): return None
        response = client.get(url)
        if response.status_code in (301, 302, 303, 307, 308):
            url = urljoin(url, response.headers.get('location', ''))
            continue
        if response.status_code in (429, 503):
            retry = response.headers.get('retry-after', '')
            try:
                delay = max(0.0, float(retry))
            except ValueError:
                try:
                    date = parsedate_to_datetime(retry)
                    if date.tzinfo is None: date = date.replace(tzinfo=timezone.utc)
                    delay = max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    delay = 5.0
            with _LOCK:
                _BLOCKED_UNTIL = max(_BLOCKED_UNTIL, time.monotonic() + delay)
            return None
        return response if response.status_code == 200 else None
    return None

def _challenge(content):
    text = content[:100000].lower()
    return any(word in text for word in (b'altcha-widget', b'captcha', b'cf-challenge', b'cloudflare challenge', b'please verify you are human', b'type="password"', b"type='password'"))

def _browser_engine():
    # The system installation may be in the user's site-packages even when the
    # calling virtual environment does not expose it.
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        user_site = site.getusersitepackages()
        if user_site not in sys.path:
            site.addsitedir(user_site)
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None
    return sync_playwright

def _browser_executable(playwright):
    cache = Path.home() / '.cache/ms-playwright'
    for browser, names, pattern in (
        (playwright.chromium, ('chromium', 'chromium-browser', 'google-chrome'), 'chromium-*/chrome-linux64/chrome'),
        (playwright.firefox, ('firefox',), 'firefox-*/firefox/firefox'),
    ):
        candidates = [browser.executable_path]
        candidates += [shutil.which(name) for name in names]
        candidates += sorted(str(p) for p in cache.glob(pattern))
        executable = next((p for p in candidates if p and os.path.isfile(p) and os.access(p, os.X_OK)), None)
        if executable:
            return browser, executable
    return None

def _browser_pdf(page_url):
    engine = _browser_engine()
    if engine is None:
        _LOG.warning('Browser mode unavailable: Playwright is not installed')
        return None
    try:
        with engine() as playwright:
            executable = _browser_executable(playwright)
            if not executable:
                _LOG.warning('Browser mode unavailable: no Chromium or Firefox browser executable found')
                return None
            browser, executable = executable
            profile = Path.home() / '.local/state/zotero-pipeline/scihub-profile'
            profile.mkdir(parents=True, exist_ok=True, mode=0o700)
            profile.chmod(0o700)
            if profile.stat().st_mode & 0o077:
                raise OSError('insecure browser profile')
            context = browser.launch_persistent_context(str(profile), executable_path=executable, headless=True)
            try:
                # Deny insecure network requests, including redirects and JS requests.
                context.route('**/*', lambda route: route.continue_() if _valid_https(route.request.url) else route.abort())
                page = context.pages[0] if context.pages else context.new_page()
                if not _throttle(): return None
                page.goto(page_url, wait_until='domcontentloaded', timeout=30000)
                deadline = time.monotonic() + 30
                links = []
                while time.monotonic() < deadline:
                    html = page.content()
                    if not _valid_https(page.url): return None
                    parser = _Links()
                    parser.feed(html)
                    links = list(_candidate_urls(page.url, parser.links))
                    if links and not _challenge(html.encode()):
                        break
                    links = []
                    page.wait_for_timeout(500)
                if not links:
                    _LOG.warning('Browser page remains challenged or requires human verification; no PDF found')
                    return None
                for link in links:
                    for _ in range(4):
                        if not _valid_https(link) or not _throttle(): break
                        response = context.request.get(link, max_redirects=0, timeout=30000)
                        if response.status in (301, 302, 303, 307, 308):
                            link = urljoin(link, response.headers.get('location', ''))
                            continue
                        if response.status == 200:
                            body = response.body()
                            if body.startswith(b'%PDF-'): return body
                        break
                return None
            finally:
                context.close()
    except OSError:
        _LOG.warning('Browser mode failed: browser profile or file access unavailable')
        return None
    except Exception:
        # Playwright exceptions can include URLs, paths, or page content.
        _LOG.warning('Browser mode failed or was blocked; browser or profile unavailable')
        return None

def _save(content, item_key, dest_dir):
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe_key = re.sub(r'[^A-Za-z0-9_-]', '_', item_key)
    path = dest_dir / f'{safe_key}-candidate.pdf'
    if path.is_symlink():
        raise OSError('candidate path is a symbolic link')
    fd, temporary = tempfile.mkstemp(dir=dest_dir, suffix='.part')
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path

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
            page_url = base.rstrip('/') + '/' + quote(item_meta.doi, safe='/')
            if os.environ.get('ZPP_SCIHUB_BROWSER') == '1':
                content = _browser_pdf(page_url)
                if not content or not content.startswith(b'%PDF-'): return None
                return _save(content, item_meta.item_key, dest_dir)
            with httpx.Client(timeout=45, follow_redirects=False) as client:
                page = _request(client, page_url)
                if page is None: return None
                if page.content.startswith(b'%PDF-'):
                    content = page.content
                else:
                    if _challenge(page.content): return None
                    parser = _Links()
                    parser.feed(page.text)
                    content = None
                    for link in _candidate_urls(str(page.url), parser.links):
                        try:
                            pdf = _request(client, link)
                        except (httpx.HTTPError, httpx.InvalidURL):
                            continue
                        if pdf is not None and pdf.content.startswith(b'%PDF-'):
                            content = pdf.content
                            break
                    if content is None: return None
                return _save(content, item_meta.item_key, dest_dir)
        except (httpx.HTTPError, OSError, ValueError):
            _LOG.warning('Plugin request or file operation failed')
            return None

source = Source()
