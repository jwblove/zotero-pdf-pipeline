import socket
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('network disabled in tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)

    # Tests must not depend on the caller's proxy endpoints or credentials.
    for name in ('http_proxy','HTTP_PROXY','https_proxy','HTTPS_PROXY','all_proxy','ALL_PROXY','no_proxy','NO_PROXY'):
        monkeypatch.delenv(name, raising=False)
    # DNS is deterministic too; individual SSRF tests override this resolver.
    from zotero_pdf_pipeline import public_http, free_sources
    monkeypatch.setattr(public_http, '_resolve', lambda host, port: ['93.184.216.34'])
    public_http._LAST_REQUEST.clear()
    public_http._COOLDOWN.clear()
    monkeypatch.setattr(free_sources, '_MAX_PDF_BYTES', 100*1024*1024)
    import time
    now = [100.0]
    monkeypatch.setattr(time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))
