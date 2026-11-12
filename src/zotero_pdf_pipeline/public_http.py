"""Guarded public HTTPS, optional environment proxies and bounded requests."""
from __future__ import annotations
import email.utils
import ipaddress
import random
import re
import socket
import ssl
import threading
import time
import zlib
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from urllib.request import getproxies_environment, proxy_bypass_environment
from urllib.parse import urljoin, urlsplit, urlunsplit
import httpx
import httpcore
from httpcore._backends.sync import SyncBackend, SyncStream, TLSinTLSStream

GENERIC_UA = 'zotero-pdf-pipeline/0.1'
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_RETRIES = 2
MAX_RETRY_AFTER = 60.0
MAX_BACKOFF = 8.0
MAX_INLINE_WAIT = 8.0
CONTACT_HOSTS = {'api.openalex.org', 'api.unpaywall.org', 'api.crossref.org'}
_LAST_REQUEST = {}
_COOLDOWN = {}
_RATE_LOCK = threading.Lock()
_RATE_SECONDS = {'export.arxiv.org': 3.0, 'arxiv.org': 3.0}
_USE_ENV_PROXY = ContextVar('zpp_use_env_proxy', default=True)
_DEADLINE = ContextVar('zpp_deadline', default=None)

@contextmanager
def _network_deadline(deadline):
    token = _DEADLINE.set(deadline)
    try: yield
    finally: _DEADLINE.reset(token)

def _budget(timeout):
    deadline = _DEADLINE.get()
    if deadline is None: return timeout
    remaining = deadline - time.monotonic()
    if remaining <= 0: raise PublicHTTPError('request deadline exceeded')
    return remaining if timeout is None else min(timeout, remaining)

def _sendall(sock, data, timeout):
    # SSLSocket.sendall has its own partial-write loop. Cap each actual send
    # here so it cannot restart an old timeout while making slow progress.
    pending = memoryview(data)
    while pending:
        sock.settimeout(_budget(timeout))
        sent = sock.send(pending)
        if not sent: raise OSError('socket closed during write')
        pending = pending[sent:]

class _DeadlineSocket:
    """Cap every socket operation inside httpcore's TLS-in-TLS BIO loop."""
    def __init__(self, sock): self.sock, self.timeout = sock, None
    def __getattr__(self, name): return getattr(self.sock, name)
    def settimeout(self, timeout): self.timeout = timeout
    def recv(self, size):
        self.sock.settimeout(_budget(self.timeout))
        return self.sock.recv(size)
    def sendall(self, data):
        _sendall(self.sock, data, self.timeout)

class _DeadlineStream:
    def __init__(self, stream): self.stream = stream
    def read(self, max_bytes, timeout=None):
        return self.stream.read(max_bytes, timeout=_budget(timeout))
    def write(self, buffer, timeout=None):
        timeout = _budget(timeout)
        if isinstance(self.stream, SyncStream):
            sock = self.stream.get_extra_info('socket')
            with httpcore._exceptions.map_exceptions({socket.timeout: httpcore.WriteTimeout, OSError: httpcore.WriteError}):
                _sendall(sock, buffer, timeout)
        else: self.stream.write(buffer, timeout=timeout)
    def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        try:
            timeout = _budget(timeout)
            if isinstance(self.stream, SyncStream) and isinstance(self.stream.get_extra_info('socket'), ssl.SSLSocket):
                # HTTPS proxy: cap each nested handshake/read, including BIO loops
                # which can receive many records before producing plaintext.
                with httpcore._exceptions.map_exceptions({socket.timeout: httpcore.ConnectTimeout, OSError: httpcore.ConnectError}):
                    stream = TLSinTLSStream(_DeadlineSocket(self.stream.get_extra_info('socket')),
                                            ssl_context, server_hostname, timeout)
            else:
                stream = self.stream.start_tls(ssl_context, server_hostname=server_hostname, timeout=timeout)
        except BaseException:
            self.close()
            raise
        return _DeadlineStream(stream)
    def close(self): self.stream.close()
    def get_extra_info(self, name): return self.stream.get_extra_info(name)

class _DeadlineBackend:
    """Bound each connect/read/TLS operation, not httpcore's cached timeout.

    Synchronous getaddrinfo (the URL guard and proxy resolver) cannot be
    interrupted here. Check the budget after resolution, before any connection.
    """
    def __init__(self): self.backend = SyncBackend()
    def connect_tcp(self, host, port, timeout=None, **kwargs):
        # Direct destinations are pinned literals. Resolve proxy hostnames once
        # so create_connection cannot spend the full timeout on every address.
        addresses = [host] if _is_ip(host) else list(dict.fromkeys(
            record[4][0] for record in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
        error = httpcore.ConnectError('DNS returned no address')
        for address in addresses:
            try:
                return _DeadlineStream(self.backend.connect_tcp(address, port, timeout=_budget(timeout), **kwargs))
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc: error = exc
        raise error
    def connect_unix_socket(self, path, timeout=None, **kwargs):
        return _DeadlineStream(self.backend.connect_unix_socket(path, timeout=_budget(timeout), **kwargs))
    def sleep(self, seconds): self.backend.sleep(_budget(seconds))

def _bound_transport(transport):
    # HTTPX does not expose network_backend in HTTPTransport's constructor.
    # Keep its proxy/TLS configuration and replace only the pool's I/O backend.
    transport._pool._network_backend = _DeadlineBackend()
    return transport

@contextmanager
def proxy_policy(enabled):
    """Scope the public-client policy without modifying environment variables."""
    token = _USE_ENV_PROXY.set(enabled)
    try: yield
    finally:
        _USE_ENV_PROXY.reset(token)

class PublicHTTPError(RuntimeError):
    """Messages are fixed diagnostics, never raw exception/server text."""

def _resolve(host, port):
    return [record[4][0] for record in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]

def guard_url(url: str, *, _allow_test_addresses=False) -> tuple[httpx.URL, tuple[str, ...]]:
    try:
        if len(str(url)) > 8192: raise PublicHTTPError('URL length limit exceeded')
        parsed = httpx.URL(url)
        if parsed.scheme != 'https' or not parsed.host or parsed.userinfo:
            raise PublicHTTPError('only public HTTPS URLs without userinfo are allowed')
        literal = _is_ip(parsed.host)
        if not literal:
            host = parsed.raw_host.decode('ascii').lower().removesuffix('.')
            labels = host.split('.')
            if (len(labels) < 2 or len(host) > 253
                    or not re.fullmatch(r'(?:[a-z]{2,63}|xn--[a-z0-9-]+)', labels[-1])
                    or host in ('localhost', 'local', 'internal')
                    or host.endswith(('.localhost', '.local', '.internal'))
                    or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in labels)):
                raise PublicHTTPError('local or invalid hostname refused')
        addresses = (str(ipaddress.ip_address(parsed.host)),) if literal else tuple(dict.fromkeys(_resolve(parsed.host, parsed.port or 443)))
        if not addresses:
            raise PublicHTTPError('DNS returned no address')
        for value in addresses:
            address = ipaddress.ip_address(value)
            blocked = (not address.is_global or address.is_multicast or address.is_reserved
                       or getattr(address, 'ipv4_mapped', None) is not None
                       or getattr(address, 'is_site_local', False))
            if isinstance(address, ipaddress.IPv6Address):
                blocked |= any(address in ipaddress.ip_network(net) for net in
                               ('::/96', '64:ff9b::/96', '64:ff9b:1::/48', '2002::/16', '2001::/32'))
            if not _allow_test_addresses and blocked:
                raise PublicHTTPError('non-public/fake-IP addresses are unsupported; correct DNS/network routing outside the application')
        return parsed.copy_with(fragment=None), addresses
    except PublicHTTPError:
        raise
    except (ValueError, OSError, httpx.InvalidURL) as exc:
        raise PublicHTTPError('invalid URL or DNS failure') from None

def _is_ip(host):
    try: ipaddress.ip_address(host); return True
    except ValueError: return False

class PinnedTransport(httpx.HTTPTransport):
    def handle_request(self, request):
        addresses = request.extensions.get('zpp_addresses')
        if not addresses:
            raise PublicHTTPError('missing validated address')
        host = request.url.raw_host.decode('ascii')
        headers = request.headers.copy()
        headers['Host'] = request.url.netloc.decode('ascii')
        extensions = dict(request.extensions, sni_hostname=host)
        pinned = httpx.Request(request.method, request.url.copy_with(host=addresses[0]),
                               headers=headers, stream=request.stream, extensions=extensions)
        return super().handle_request(pinned)

def _environment_proxy(url):
    proxies = getproxies_environment()
    # Standard library handles lower-case precedence, domain suffixes, ports,
    # wildcard bypass and CGI's special treatment of HTTP_PROXY.
    target = url.netloc.decode('ascii')
    if url.port is None: target += ':443' if url.scheme == 'https' else ':80'
    if proxy_bypass_environment(target, proxies): return None
    return proxies.get(url.scheme) or proxies.get('all')

class PublicTransport(httpx.BaseTransport):
    """Pin direct requests; let an explicitly configured proxy resolve targets.

    The request layer validates DNS before every hop in either mode. A proxy
    can resolve the hostname again, so proxied DNS rebinding remains a residual
    risk; every target still requires public DNS results before the request.
    """
    def __init__(self, use_env_proxy):
        self.use_env_proxy = use_env_proxy
        self.direct = _bound_transport(PinnedTransport())
        self.proxies = {}

    def handle_request(self, request):
        if not request.extensions.get('zpp_addresses'):
            raise PublicHTTPError('missing validated address')
        proxy = _environment_proxy(request.url) if self.use_env_proxy else None
        if not proxy: return self.direct.handle_request(request)
        if proxy not in self.proxies:
            self.proxies[proxy] = _bound_transport(httpx.HTTPTransport(proxy=proxy))
        return self.proxies[proxy].handle_request(request)

    def close(self):
        self.direct.close()
        for transport in self.proxies.values(): transport.close()

def make_public_client(**kwargs):
    # Proxy selection is explicit in our transport; HTTPX must not add another
    # environment-derived routing layer on top of the guarded/pinned requests.
    return httpx.Client(transport=PublicTransport(_USE_ENV_PROXY.get()), trust_env=False, **kwargs)

def _rate_limit(url):
    host = urlsplit(url).hostname or ''
    with _RATE_LOCK:
        now = time.monotonic()
        if _COOLDOWN.get(host, 0) > now:
            raise PublicHTTPError('host cooldown active; skipped')
        interval = _RATE_SECONDS.get(host, 1.0)
        delay = _LAST_REQUEST.get(host, now - interval) + interval - now
        if delay > 0: time.sleep(delay)
        _LAST_REQUEST[host] = time.monotonic()

def _retry_delay(response, attempt):
    retry = response.headers.get('Retry-After', '')
    try: seconds = float(retry)
    except ValueError:
        try: seconds = (email.utils.parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError): seconds = 0
    seconds = min(MAX_RETRY_AFTER, max(0.0, seconds))
    return max(seconds, min(MAX_BACKOFF, 2 ** attempt + random.uniform(0, .5)))

def _origin(url):
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or 443

def clean_referer(value, destination):
    if not value or _origin(value) != _origin(destination): return None
    parts = urlsplit(value)
    if parts.username or parts.password: return None
    return urlunsplit((parts.scheme, parts.netloc, parts.path, '', ''))

def _chunks(response, maximum):
    """Decode gzip/deflate with an output budget, including decompression bombs."""
    encoding = response.headers.get('Content-Encoding', '').lower().strip()
    if encoding not in ('', 'identity', 'gzip', 'deflate'):
        raise PublicHTTPError('unsupported content encoding')
    # MockTransport may supply a response which HTTPX has already decoded.
    if response.is_stream_consumed:
        yield response.content
        return
    decoder = zlib.decompressobj(31 if encoding == 'gzip' else 15) if encoding in ('gzip','deflate') else None
    total = encoded = 0
    try:
        for chunk in response.iter_raw():
            _budget(None)
            encoded += len(chunk)
            if encoded > maximum: raise PublicHTTPError('encoded response size limit exceeded')
            if decoder is None:
                total += len(chunk)
                if total > maximum: raise PublicHTTPError('decoded response size limit exceeded')
                yield chunk
                continue
            pending = chunk
            while True:
                budget = min(65536, maximum-total+1)
                decoded = decoder.decompress(pending, budget)
                if decoder.unused_data: raise PublicHTTPError('trailing compressed response data')
                total += len(decoded)
                if total > maximum: raise PublicHTTPError('decoded response size limit exceeded')
                yield decoded
                pending = decoder.unconsumed_tail
                if decoder.eof or (not pending and len(decoded)<budget): break
        if decoder and not decoder.eof:
            raise PublicHTTPError('incomplete compressed response')
    except zlib.error as exc:
        raise PublicHTTPError('invalid compressed response') from None

def request(url, timeout=45, *, max_bytes=MAX_JSON_BYTES, headers=None, params=None,
            client_factory=make_public_client, rate_limit=_rate_limit, _allow_test_addresses=False,
            _retry=True):
    """Budget network I/O across hops/retries with the default public client.

    Synchronous DNS, rate-limit waits/locks, client_factory callbacks and client
    initialization are not interruptible; their elapsed time is charged before
    subsequent I/O. Custom client transports must bound their own reads.
    """
    try:
        current = str(httpx.URL(url, params=params)) if params is not None else str(url)
        original_origin = _origin(current)
    except (ValueError, httpx.InvalidURL) as exc:
        raise PublicHTTPError('invalid URL') from None
    supplied = dict(headers or {})
    deadline = time.monotonic() + timeout
    hops = 0
    attempt = 0
    while True:
        parsed, addresses = guard_url(current, _allow_test_addresses=_allow_test_addresses)
        with _RATE_LOCK:
            if _COOLDOWN.get(parsed.host, 0) > time.monotonic():
                raise PublicHTTPError('host cooldown active; skipped')
        rate_limit(str(parsed))
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise PublicHTTPError('request deadline exceeded')
        outgoing = httpx.Headers(supplied)
        outgoing["Accept-Encoding"] = "identity"
        if _origin(str(parsed)) != original_origin:
            outgoing.pop('Authorization', None)
        if parsed.host not in CONTACT_HOSTS or _origin(str(parsed)) != original_origin:
            outgoing['User-Agent'] = GENERIC_UA
            # Do not forward a polite-pool query to a redirect's third-party host.
            parsed = parsed.copy_remove_param('mailto').copy_remove_param('email')
        referer = clean_referer(outgoing.get('Referer'), str(parsed))
        outgoing.pop('Referer', None)
        if referer: outgoing['Referer'] = referer
        try:
            with _network_deadline(deadline), client_factory(timeout=remaining, follow_redirects=False) as client:
                with client.stream('GET', parsed, headers=outgoing, extensions={'zpp_addresses': addresses}) as response:
                    _budget(None)
                    status = response.status_code
                    if status in (301, 302, 303, 307, 308):
                        if hops >= MAX_REDIRECTS or not response.headers.get('Location'):
                            raise PublicHTTPError('redirect limit or missing Location')
                        current = urljoin(str(parsed), response.headers['Location'])
                        hops += 1
                        attempt = 0
                        continue
                    if status in (429, 503):
                        delay = _retry_delay(response, attempt)
                        if not _retry or attempt >= MAX_RETRIES or delay > MAX_INLINE_WAIT or time.monotonic() + delay >= deadline:
                            with _RATE_LOCK:
                                _COOLDOWN[parsed.host] = time.monotonic() + max(delay, 5.0)
                            raise PublicHTTPError(f'HTTP {status}; host cooldown active')
                        time.sleep(delay)
                        attempt += 1
                        continue
                    if status != 200: raise PublicHTTPError(f'HTTP {status}')
                    try: length = int(response.headers.get('Content-Length', '0'))
                    except ValueError: raise PublicHTTPError('invalid Content-Length')
                    if length < 0 or length > max_bytes: raise PublicHTTPError('response size limit exceeded')
                    body = bytearray()
                    for chunk in _chunks(response, max_bytes):
                        if time.monotonic() >= deadline: raise PublicHTTPError('request deadline exceeded')
                        effective_max = max_bytes
                        if not body and not chunk.startswith(b'%PDF-'):
                            effective_max = min(effective_max, MAX_HTML_BYTES)
                        if body and not body.startswith(b'%PDF-'):
                            effective_max = min(effective_max, MAX_HTML_BYTES)
                        if len(body) + len(chunk) > effective_max: raise PublicHTTPError('decoded response size limit exceeded')
                        body.extend(chunk)
                    return httpx.Response(200, content=bytes(body), request=httpx.Request('GET', parsed),
                                          headers={k:v for k,v in response.headers.items() if k.lower() not in ('content-encoding', 'content-length')})
        except PublicHTTPError: raise
        except httpx.TimeoutException as exc: raise PublicHTTPError('请求超时') from None
        except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError, ImportError) as exc:
            raise PublicHTTPError(f'request failed ({type(exc).__name__})') from None
