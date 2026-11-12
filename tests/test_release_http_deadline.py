"""Release deadline regressions: fake clocks/backends, no network."""
import gzip
import socket
import zlib

import httpcore
import httpx
import pytest
from httpcore._backends.sync import SyncBackend
from zotero_pdf_pipeline import public_http as net


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(net.time, 'monotonic', lambda: now[0])
    return now


class Bytes(httpx.SyncByteStream):
    def __init__(self, payload, clock=None):
        self.payload, self.clock, self.count = payload, clock, 0
    def __iter__(self):
        for value in self.payload:
            self.count += 1
            if self.clock is not None: self.clock[0] += 1
            yield bytes([value])


def fetch(stream, encoding='', maximum=2048, timeout=3):
    def factory(**kwargs):
        return httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(
            200, headers={'Content-Encoding': encoding}, stream=stream)), **kwargs)
    return net.request('https://public.invalid/pdf', timeout=timeout, max_bytes=maximum,
                       client_factory=factory, rate_limit=lambda url: None)


@pytest.mark.parametrize('encoding', ['', 'gzip', 'deflate'])
def test_one_byte_drip_stops_at_three_seconds(clock, encoding):
    payload = bytes(range(250)) * 4
    if encoding == 'gzip': payload = gzip.compress(payload)
    if encoding == 'deflate': payload = zlib.compress(payload)
    stream = Bytes(payload, clock)
    with pytest.raises(net.PublicHTTPError, match='deadline'):
        fetch(stream, encoding)
    assert 2 <= stream.count <= 3
    assert clock[0] <= 3


@pytest.mark.parametrize('encoding,compress', [('gzip', gzip.compress), ('deflate', zlib.compress)])
def test_truncated_compressed_body_fails(encoding, compress):
    with pytest.raises(net.PublicHTTPError, match='incomplete'):
        fetch(Bytes(compress(b'%PDF-body')[:-2]), encoding)


@pytest.mark.parametrize('encoding,compress', [('', lambda x:x), ('gzip', gzip.compress), ('deflate', zlib.compress)])
def test_decoded_size_budget_survives_small_chunks(encoding, compress):
    with pytest.raises(net.PublicHTTPError, match='size limit'):
        fetch(Bytes(compress(b'x' * 10000)), encoding, maximum=100)


@pytest.mark.parametrize('phase', ['body', 'headers', 'tls', 'connect'])
@pytest.mark.parametrize('proxy', [False, True])
def test_real_httpcore_uses_remaining_budget_before_blocking(monkeypatch, clock, phase, proxy):
    """Run actual HTTPX/httpcore state machines over a timeout-recording backend.

    The stalled operation advances by its supplied timeout then raises; a stale
    three-second timeout would overshoot the absolute deadline and fail here.
    """
    calls = []
    class Stream:
        reads = 0
        def read(self, max_bytes, timeout=None):
            calls.append(('read', clock[0], timeout))
            self.reads += 1
            if phase == 'headers' or self.reads > 1:
                clock[0] += timeout
                raise httpcore.ReadTimeout()
            clock[0] = 2.75
            return b'HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\nx'
        def write(self, buffer, timeout=None): calls.append(('write', clock[0], timeout))
        def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            calls.append(('tls', clock[0], timeout))
            if phase == 'tls':
                clock[0] += timeout
                raise httpcore.ConnectTimeout()
            return self
        def close(self): pass
        def get_extra_info(self, name): return None
    def connect(self, host, port, timeout=None, **kwargs):
        calls.append(('connect', clock[0], timeout))
        if phase == 'connect':
            clock[0] += timeout
            raise httpcore.ConnectTimeout()
        clock[0] = 2.5
        return Stream()
    monkeypatch.setattr(SyncBackend, 'connect_tcp', connect)
    if proxy:
        monkeypatch.setenv('HTTPS_PROXY', 'http://93.184.216.35:8080')
        # Fake proxy tunnel succeeds, then the same real HTTP/1.1 response path.
        original = Stream.read
        def read(self, max_bytes, timeout=None):
            if not hasattr(self, 'tunnel'):
                self.tunnel = True
                calls.append(('proxy_headers', clock[0], timeout))
                return b'HTTP/1.1 200 Connection established\r\n\r\n'
            return original(self, max_bytes, timeout)
        Stream.read = read
    with pytest.raises(net.PublicHTTPError):
        net.request('https://public.invalid/pdf', timeout=3, rate_limit=lambda u:None)
    assert clock[0] <= 3
    assert calls[0] == ('connect', 0, 3)
    for _, started, budget in calls:
        assert 0 < budget <= 3 - started
    if phase == 'body': assert calls[-1][2] == pytest.approx(.25)
    if phase in ('headers', 'tls'): assert calls[-1][2] == pytest.approx(.5)


def test_proxy_address_fallback_decreases_connect_timeout(monkeypatch, clock):
    calls = []
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.invalid:8080')
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a,**k:[
        (socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.35',8080)),
        (socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.36',8080))])
    def connect(self, host, port, timeout=None, **kwargs):
        calls.append((host, timeout))
        if len(calls) == 1:
            clock[0] += 2.75
            raise httpcore.ConnectError()
        clock[0] += timeout
        raise httpcore.ConnectTimeout()
    monkeypatch.setattr(SyncBackend, 'connect_tcp', connect)
    with pytest.raises(net.PublicHTTPError):
        net.request('https://public.invalid/pdf', timeout=3, rate_limit=lambda u:None)
    assert calls == [('93.184.216.35',3), ('93.184.216.36',.25)]
    assert clock[0] == 3


@pytest.mark.parametrize('phase', ['headers','body'])
def test_real_httpcore_drip_multiple_reads_use_smaller_timeouts(monkeypatch, clock, phase):
    calls = []
    class Stream:
        initial = True
        def read(self, max_bytes, timeout=None):
            calls.append(timeout)
            clock[0] += min(1, timeout)
            if phase == 'headers': return b'H'
            if self.initial:
                self.initial = False
                return b'HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\nx'
            return b'x'
        def write(self, *a,**k): pass
        def start_tls(self, *a,**k): return self
        def close(self): pass
        def get_extra_info(self, name): return None
    monkeypatch.setattr(SyncBackend,'connect_tcp',lambda *a,**k:Stream())
    with pytest.raises(net.PublicHTTPError,match='deadline'):
        net.request('https://public.invalid/pdf', timeout=3, rate_limit=lambda u:None)
    assert calls == [3,2,1] and clock[0] == 3


@pytest.mark.parametrize('phase', ['handshake', 'read', 'write'])
def test_tls_in_tls_caps_each_bio_socket_operation(monkeypatch, clock, phase):
    """Actual httpcore TLS-in-TLS loop with a fake SSL engine and SSL socket."""
    import ssl
    from httpcore._backends.sync import SyncStream
    calls = []
    class Socket:
        timeout = None
        closed = False
        def settimeout(self, timeout): self.timeout = timeout
        def sendall(self, data): pass
        def send(self, data): return len(data)
        def recv(self, size):
            calls.append(self.timeout)
            clock[0] += min(1,self.timeout)
            return b'tls control record'
        def close(self): self.closed = True
    class SSL:
        def do_handshake(self):
            if phase == 'handshake': self.read(1)
        def read(self, size):
            raise ssl.SSLWantReadError(ssl.SSL_ERROR_WANT_READ, 'fixture wants read')
        def write(self, data): return self.read(len(data))
    class Context:
        def wrap_bio(self, **kwargs): return SSL()
    monkeypatch.setattr(net.ssl,'SSLSocket', Socket)
    sock = Socket()
    with net._network_deadline(3), pytest.raises(net.PublicHTTPError,match='deadline'):
        stream = net._DeadlineStream(SyncStream(sock)).start_tls(Context(), 'public.invalid', timeout=3)
        try:
            if phase == 'read': stream.read(1000, timeout=3)
            if phase == 'write': stream.write(b'request', timeout=3)
        finally: stream.close()
    assert calls == [3,2,1] and clock[0] == 3
    assert sock.closed  # Failed handshakes must release their socket.


@pytest.mark.parametrize('phase', ['body','headers','tls'])
def test_actual_sync_backend_and_socket_get_remaining_timeout(monkeypatch, clock, phase):
    """Keep real SyncBackend, SyncStream and HTTP/1.1; only sockets/TLS are fakes."""
    calls = []
    class Socket:
        timeout = None
        reads = 0
        closed = False
        def settimeout(self, timeout): self.timeout = timeout
        def setsockopt(self, *args): pass
        def sendall(self, data): pass
        def send(self, data): return len(data)
        def close(self): self.closed = True
        def recv(self, maximum):
            calls.append(('recv', clock[0], self.timeout))
            self.reads += 1
            if phase == 'headers' or self.reads > 1:
                clock[0] += self.timeout
                raise socket.timeout()
            clock[0] = 2.75
            return b'HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\nx'
    sock = Socket()
    def connect(address, timeout, **kwargs):
        assert address == ('93.184.216.34',443)
        calls.append(('connect',clock[0],timeout))
        clock[0] = 2.5
        return sock
    def tls(context, sock, **kwargs):
        calls.append(('tls',clock[0],sock.timeout))
        assert kwargs['server_hostname'] == 'public.invalid'
        if phase == 'tls':
            clock[0] += sock.timeout
            raise socket.timeout()
        return sock
    monkeypatch.setattr(socket,'create_connection',connect)
    monkeypatch.setattr(net.ssl.SSLContext,'wrap_socket',tls)
    with pytest.raises(net.PublicHTTPError):
        net.request('https://public.invalid/pdf',timeout=3,rate_limit=lambda u:None)
    assert clock[0] == 3 and sock.closed
    assert calls[0] == ('connect',0,3)
    assert calls[1] == ('tls',2.5,.5)
    assert all(0 < budget <= 3-started for _,started,budget in calls)
    if phase == 'body': assert calls[-1] == ('recv',2.75,.25)


def test_uninterruptible_guard_dns_is_charged_before_connect(monkeypatch, clock):
    def resolve(*args):
        clock[0] = 4  # The synchronous helper itself is not hard-bound.
        return ['93.184.216.34']
    monkeypatch.setattr(net,'_resolve',resolve)
    with pytest.raises(net.PublicHTTPError,match='deadline'):
        net.request('https://public.invalid/pdf',timeout=3,rate_limit=lambda u:None)
    assert clock[0] == 4  # No connection permitted after it returns too late.


@pytest.mark.parametrize('layer', ['stream', 'nested_tls_socket'])
def test_partial_socket_writes_do_not_restart_deadline(clock, layer):
    from httpcore._backends.sync import SyncStream
    calls = []
    class Socket:
        timeout = None
        def settimeout(self, timeout): self.timeout = timeout
        def send(self, data):
            calls.append(self.timeout)
            clock[0] += 1
            return 1
        def sendall(self, data):
            # Same partial-write structure as Python's SSLSocket.sendall.
            for _ in data: self.send(b'x')
        def close(self): pass
    sock = Socket()
    with net._network_deadline(3), pytest.raises(net.PublicHTTPError,match='deadline'):
        if layer == 'stream':
            net._DeadlineStream(SyncStream(sock)).write(b'x'*1000, timeout=3)
        else:
            wrapped = net._DeadlineSocket(sock)
            wrapped.settimeout(3)
            wrapped.sendall(b'x'*1000)
    assert calls == [3,2,1] and clock[0] == 3
