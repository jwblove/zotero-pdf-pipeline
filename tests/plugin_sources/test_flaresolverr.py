import gzip
import importlib.util
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from zotero_pdf_pipeline.models import PaperNeed
from zotero_pdf_pipeline.plugins import load_source
from zotero_pdf_pipeline.public_http import PublicHTTPError

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "test_local_flaresolverr_adapter", ROOT / "plugins/flaresolverr.py"
)
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)

PAPER = PaperNeed(item_key="ABCDEFGH", doi="10.1234/test.paper", title="Test Paper")


@pytest.fixture
def source(monkeypatch):
    for key in (
        "ZPP_FLARESOLVERR_URL",
        "ZPP_FLARESOLVERR_BASE_URLS",
        "ZPP_FLARESOLVERR_BASE_URL",
        "ZPP_FLARESOLVERR_TIMEOUT",
        "ZPP_FLARESOLVERR_SESSION",
        "ZPP_FLARESOLVERR_DISABLE_MEDIA",
        "ZPP_FLARESOLVERR_WAIT_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(adapter.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        adapter,
        "guard_url",
        lambda url, **_kw: _fake_guard(url),
    )
    return adapter.Source()


def _fake_guard(url: str):
    parsed = httpx.URL(url)
    if parsed.scheme != "https" or not parsed.host or parsed.userinfo:
        raise PublicHTTPError("only public HTTPS URLs without userinfo are allowed")
    if parsed.host in ("127.0.0.1", "localhost", "10.0.0.1", "192.168.1.1"):
        raise PublicHTTPError("non-public address rejected")
    return parsed.copy_with(fragment=None), ("93.184.216.34",)


def _mock_clients(monkeypatch, solver_handler, pdf_handler):
    solver_calls = []
    pdf_calls = []

    def solver_transport(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        solver_calls.append((str(request.url), body))
        return solver_handler(request, body)

    def pdf_transport(request: httpx.Request) -> httpx.Response:
        pdf_calls.append((str(request.url), dict(request.headers)))
        return pdf_handler(request)

    real_client = httpx.Client

    def fake_solver_client(**kwargs):
        assert kwargs.get("trust_env") is False
        return real_client(transport=httpx.MockTransport(solver_transport), **{k: v for k, v in kwargs.items() if k != "transport"})

    def fake_public_client(**kwargs):
        return real_client(transport=httpx.MockTransport(pdf_transport), trust_env=False, **{k: v for k, v in kwargs.items() if k != "transport"})

    monkeypatch.setattr(adapter.httpx, "Client", fake_solver_client)
    monkeypatch.setattr(adapter, "make_public_client", fake_public_client)
    return solver_calls, pdf_calls


def test_real_loader():
    loaded = load_source("flaresolverr", ROOT / "plugins")
    assert loaded.name == "flaresolverr"
    assert callable(loaded.fetch)


def test_default_doi_citation_pdf_with_matching_ua_and_cookie(monkeypatch, source, tmp_path):
    ua = "Mozilla/5.0 Chrome/152.0.0.0"

    def solver(_req, body):
        assert body["cmd"] == "request.get"
        assert body["url"] == "https://doi.org/10.1234/test.paper"
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "solution": {
                    "url": "https://journal.example/article/123",
                    "status": 200,
                    "userAgent": ua,
                    "cookies": [
                        {"name": "cf_clearance", "value": "clear123", "domain": ".journal.example", "path": "/"},
                        {"name": "other_site", "value": "secret", "domain": "other.example", "path": "/"},
                        {"name": "expired", "value": "old", "domain": ".journal.example", "path": "/", "expiry": 1},
                    ],
                    "response": (
                        "<html><head>"
                        '<meta name="citation_pdf_url" content="/article/123.pdf">'
                        "</head><body>"
                        '<form><input type="password"></form>'
                        "</body></html>"
                    ),
                },
            },
        )

    def pdf(req):
        assert str(req.url) == "https://journal.example/article/123.pdf"
        assert req.headers["user-agent"] == ua
        assert req.headers["cookie"] == "cf_clearance=clear123"
        assert req.headers["referer"] == "https://journal.example/article/123"
        return httpx.Response(200, content=b"%PDF-1.7\nbody")

    solver_calls, pdf_calls = _mock_clients(monkeypatch, solver, pdf)
    out = source.fetch(PAPER, tmp_path)
    assert out is not None and out.parent == tmp_path
    assert out.read_bytes() == b"%PDF-1.7\nbody"
    assert len(solver_calls) == 1
    assert len(pdf_calls) == 1


def test_cross_origin_redirect_strips_unrelated_cookies_and_referer(monkeypatch, source, tmp_path):
    ua = "Mozilla/5.0 Chrome/152.0.0.0"

    def solver(_req, _body):
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "solution": {
                    "url": "https://journal.example/article/123",
                    "status": 200,
                    "userAgent": ua,
                    "cookies": [
                        {"name": "journal_only", "value": "secret1", "domain": "journal.example", "path": "/"},
                        {"name": "cdn_token", "value": "cdn1", "domain": ".cdn.example", "path": "/pdf"},
                    ],
                    "response": '<embed id="pdf" src="/download/123#page=1">',
                },
            },
        )

    def pdf(req):
        if str(req.url) == "https://journal.example/download/123":
            assert req.headers["cookie"] == "journal_only=secret1"
            assert "referer" in req.headers
            return httpx.Response(302, headers={"Location": "https://files.cdn.example/pdf/paper.pdf"})
        assert str(req.url) == "https://files.cdn.example/pdf/paper.pdf"
        assert req.headers.get("cookie") == "cdn_token=cdn1"
        assert "referer" not in req.headers
        return httpx.Response(200, content=gzip.compress(b"%PDF-1.6\ngzipped"), headers={"Content-Encoding": "gzip"})

    _mock_clients(monkeypatch, solver, pdf)
    out = source.fetch(PAPER, tmp_path)
    assert out.read_bytes() == b"%PDF-1.6\ngzipped"


def test_direct_pdf_landing_page_with_chrome_viewer_embed(monkeypatch, source, tmp_path):
    def solver(_req, _body):
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "solution": {
                    "url": "https://repo.example/papers/123.pdf",
                    "status": 200,
                    "userAgent": "Mozilla/5.0",
                    "cookies": [{"name": "cf_clearance", "value": "ok", "domain": ".repo.example", "path": "/"}],
                    "response": '<html><body><embed type="application/pdf" src="about:blank"></body></html>',
                },
            },
        )

    def pdf(req):
        assert str(req.url) == "https://repo.example/papers/123.pdf"
        return httpx.Response(200, content=b"%PDF-1.5\ndirect")

    _mock_clients(monkeypatch, solver, pdf)
    out = source.fetch(PAPER, tmp_path)
    assert out.read_bytes() == b"%PDF-1.5\ndirect"


def test_ignores_ad_iframes_and_unwraps_viewer_file_query(monkeypatch, source, tmp_path):
    def solver(_req, _body):
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "solution": {
                    "url": "https://journal.example/view",
                    "status": 200,
                    "userAgent": "Mozilla/5.0",
                    "cookies": [],
                    "response": (
                        '<iframe src="https://www.googletagmanager.com/ns.html"></iframe>'
                        '<iframe src="https://challenges.cloudflare.com/turnstile"></iframe>'
                        '<a download href="/viewer?file=%2Fstatic%2Fpaper.pdf">Download</a>'
                    ),
                },
            },
        )

    def pdf(req):
        assert str(req.url) == "https://journal.example/static/paper.pdf"
        return httpx.Response(200, content=b"%PDF-1.4\nunwrapped")

    _, pdf_calls = _mock_clients(monkeypatch, solver, pdf)
    out = source.fetch(PAPER, tmp_path)
    assert out.read_bytes() == b"%PDF-1.4\nunwrapped"
    assert [u for u, _ in pdf_calls] == ["https://journal.example/static/paper.pdf"]


def test_custom_targets_template_and_fallback(monkeypatch, source, tmp_path):
    monkeypatch.setenv(
        "ZPP_FLARESOLVERR_BASE_URLS",
        "https://first.example,https://second.example/lookup?doi={doi}",
    )
    monkeypatch.setenv("ZPP_FLARESOLVERR_DISABLE_MEDIA", "1")
    monkeypatch.setenv("ZPP_FLARESOLVERR_SESSION", "sess_01")
    monkeypatch.setenv("ZPP_FLARESOLVERR_WAIT_SECONDS", "2")

    def solver(_req, body):
        assert body["disableMedia"] is True
        assert body["session"] == "sess_01"
        assert body["waitInSeconds"] == 2
        if body["url"].startswith("https://first.example/"):
            return httpx.Response(200, json={"status": "ok", "solution": {"url": body["url"], "status": 404, "response": ""}})
        assert body["url"] == "https://second.example/lookup?doi=10.1234%2Ftest.paper"
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "solution": {
                    "url": body["url"],
                    "status": 200,
                    "userAgent": "Mozilla/5.0",
                    "cookies": [],
                    "response": '<embed id="pdf" src="/file.pdf">',
                },
            },
        )

    def pdf(req):
        return httpx.Response(200, content=b"%PDF-1.7\nsecond")

    solver_calls, _ = _mock_clients(monkeypatch, solver, pdf)
    out = source.fetch(PAPER, tmp_path)
    assert out.read_bytes() == b"%PDF-1.7\nsecond"
    assert len(solver_calls) == 2


def test_invalid_explicit_targets_do_not_fall_back_to_default(monkeypatch, source, tmp_path):
    monkeypatch.setenv("ZPP_FLARESOLVERR_BASE_URLS", "http://plain.example,https://bad.example/?q=1")
    solver_calls, _ = _mock_clients(monkeypatch, lambda *_: pytest.fail("should not call solver"), lambda *_: pytest.fail("should not download"))
    assert source.fetch(PAPER, tmp_path) is None
    assert not solver_calls


def test_non_loopback_solver_url_rejected(monkeypatch, source, tmp_path, caplog):
    monkeypatch.setenv("ZPP_FLARESOLVERR_URL", "http://192.168.1.50:8191/v1")
    _mock_clients(monkeypatch, lambda *_: pytest.fail("should not call remote solver"), lambda *_: pytest.fail("should not download"))
    with caplog.at_level(logging.WARNING):
        assert source.fetch(PAPER, tmp_path) is None
    assert "loopback" in caplog.text


def test_hard_challenge_pauses_only_challenged_host(monkeypatch, source, tmp_path):
    monkeypatch.setenv("ZPP_FLARESOLVERR_BASE_URLS", "https://challenged.example,https://healthy.example")

    def solver(_req, body):
        if "challenged.example" in body["url"]:
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "solution": {
                        "url": body["url"],
                        "status": 200,
                        "response": "<title>Just a moment...</title><div class='cf-challenge'></div>",
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "solution": {
                    "url": body["url"],
                    "status": 200,
                    "response": '<meta name="citation_pdf_url" content="/ok.pdf">',
                },
            },
        )

    def pdf(_req):
        return httpx.Response(200, content=b"%PDF-1.5\nhealthy")

    solver_calls, _ = _mock_clients(monkeypatch, solver, pdf)
    assert source.fetch(PAPER, tmp_path).read_bytes() == b"%PDF-1.5\nhealthy"
    assert "challenged.example" in source.blocked_hosts
    # Second call should skip challenged.example immediately without hitting FlareSolverr for it.
    paper2 = PaperNeed(item_key="IJKLMNOP", doi="10.1234/second", title="Second")
    assert source.fetch(paper2, tmp_path).read_bytes() == b"%PDF-1.5\nhealthy"
    assert [b["url"] for _, b in solver_calls] == [
        "https://challenged.example/10.1234/test.paper",
        "https://healthy.example/10.1234/test.paper",
        "https://healthy.example/10.1234/second",
    ]


def test_http_403_and_non_pdf_rejected_without_disk_writes(monkeypatch, source, tmp_path):
    def solver(_req, _body):
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "solution": {
                    "url": "https://blocked.example/paper",
                    "status": 200,
                    "response": '<meta name="citation_pdf_url" content="/paper.pdf">',
                },
            },
        )

    def pdf(_req):
        return httpx.Response(200, content=b"<html><body>Please verify you are human</body></html>")

    _mock_clients(monkeypatch, solver, pdf)
    assert source.fetch(PAPER, tmp_path) is None
    assert "blocked.example" in source.blocked_hosts
    assert not list(tmp_path.iterdir())


def test_private_target_rejected_before_calling_solver(monkeypatch, source, tmp_path):
    monkeypatch.setenv("ZPP_FLARESOLVERR_BASE_URL", "https://127.0.0.1")
    _mock_clients(monkeypatch, lambda *_: pytest.fail("must not forward private target"), lambda *_: pytest.fail("must not download"))
    assert source.fetch(PAPER, tmp_path) is None


def test_unreachable_solver_returns_none_with_redacted_log(monkeypatch, source, tmp_path, caplog):
    def solver(_req, _body):
        raise httpx.ConnectError("connection refused to secret-token")

    _mock_clients(monkeypatch, solver, lambda *_: pytest.fail("must not download"))
    with caplog.at_level(logging.WARNING):
        assert source.fetch(PAPER, tmp_path) is None
    assert "secret-token" not in caplog.text
    assert "unreachable" in caplog.text


def test_destination_symlink_escape_refused(monkeypatch, source, tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"original")
    (dest / "ABCDEFGH-flaresolverr.pdf").symlink_to(outside)

    def solver(_req, _body):
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "solution": {
                    "url": "https://journal.example/p",
                    "status": 200,
                    "response": '<meta name="citation_pdf_url" content="/p.pdf">',
                },
            },
        )

    _mock_clients(monkeypatch, solver, lambda _r: httpx.Response(200, content=b"%PDF-1.5\nnew"))
    assert source.fetch(PAPER, dest) is None
    assert outside.read_bytes() == b"original"


def test_unsafe_item_key_and_missing_doi(source, tmp_path):
    bad_key = SimpleNamespace(item_key="../escape", doi="10.1234/test")
    no_doi = PaperNeed(item_key="ABCDEFGH", doi="", title="No DOI")
    assert source.fetch(bad_key, tmp_path) is None
    assert source.fetch(no_doi, tmp_path) is None


def test_pdf_with_captcha_in_title_not_false_positive(monkeypatch, source, tmp_path):
    def solver(_req, _body):
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "solution": {
                    "url": "https://journal.example/article/captcha-study",
                    "status": 200,
                    "userAgent": "Mozilla/5.0",
                    "cookies": [],
                    "response": (
                        "<html><head><title>A Survey of CAPTCHA Security</title>"
                        '<meta name="citation_pdf_url" content="/article/captcha-study.pdf">'
                        "</head><body><h1>CAPTCHA</h1></body></html>"
                    ),
                },
            },
        )

    _mock_clients(monkeypatch, solver, lambda _r: httpx.Response(200, content=b"%PDF-1.7\ncaptcha study"))
    out = source.fetch(PAPER, tmp_path)
    assert out is not None and out.read_bytes() == b"%PDF-1.7\ncaptcha study"


@pytest.mark.parametrize(
    "html",
    [
        '<html><head><title translate="en:title">Sci-Hub: are you are robot?</title><script src="/scripts/altcha.min.js"></script></head></html>',
        '<html><head><title>DDOS-GUARD</title></head><body><div class="ddg-captcha"></div></body></html>',
    ],
)
def test_altcha_and_ddos_guard_pages_pause_host(monkeypatch, source, tmp_path, html):
    monkeypatch.setenv("ZPP_FLARESOLVERR_BASE_URL", "https://mirror.example")

    def solver(_req, body):
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "solution": {
                    "url": body["url"],
                    "status": 200,
                    "userAgent": "Mozilla/5.0",
                    "cookies": [],
                    "response": html,
                },
            },
        )

    _mock_clients(monkeypatch, solver, lambda *_: pytest.fail("must not download on challenge page"))
    assert source.fetch(PAPER, tmp_path) is None
    assert "mirror.example" in source.blocked_hosts


def test_fetch_plugin_integration_with_config(monkeypatch, tmp_path):
    from zotero_pdf_pipeline.config import load_config
    from zotero_pdf_pipeline.plugins import fetch_plugin

    config = tmp_path / "config.toml"
    config.write_text('[paths]\narchive_dir="archive"\n[plugins]\nenabled=["flaresolverr"]\n')
    cfg = load_config(config)
    assert "flaresolverr" in cfg.plugins.enabled

    loaded = load_source("flaresolverr", ROOT / "plugins")
    module = __import__(loaded.__class__.__module__)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    monkeypatch.setattr(module, "guard_url", lambda url, **_kw: _fake_guard(url))

    real_client = httpx.Client

    def solver_transport(_req):
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "solution": {
                    "url": "https://journal.example/p",
                    "status": 200,
                    "userAgent": "Mozilla/5.0",
                    "cookies": [],
                    "response": '<meta name="citation_pdf_url" content="/p.pdf">',
                },
            },
        )

    monkeypatch.setattr(
        module.httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(solver_transport), **{k: v for k, v in kw.items() if k != "transport"}),
    )
    monkeypatch.setattr(
        module,
        "make_public_client",
        lambda **kw: real_client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"%PDF-1.5\nintegrated")),
            trust_env=False,
            **{k: v for k, v in kw.items() if k != "transport"},
        ),
    )
    staging = cfg.paths.staging_dir
    resolved = fetch_plugin("flaresolverr", ROOT / "plugins", PAPER, staging)
    assert resolved is not None
    assert resolved.source == "flaresolverr"
    assert resolved.file_path.parent == staging
    assert resolved.file_path.read_bytes() == b"%PDF-1.5\nintegrated"

    assert list(staging.iterdir()) == [resolved.file_path]


@pytest.mark.parametrize("port", ["abc", "65536"])
def test_invalid_solver_port(monkeypatch, source, tmp_path, port):
    monkeypatch.setenv("ZPP_FLARESOLVERR_URL", f"http://127.0.0.1:{port}/v1")
    _mock_clients(monkeypatch, lambda *_: pytest.fail("should not call solver"), lambda *_: pytest.fail("should not download"))
    assert source._solver_url() is None
    assert source.fetch(PAPER, tmp_path) is None


@pytest.mark.parametrize(
    "raw_url, expected",
    [
        ("http://127.0.0.1:8191/v1", "http://127.0.0.1:8191/v1"),
        ("http://127.0.0.1", "http://127.0.0.1/v1"),
        ("http://[::1]:8191/v1", "http://[::1]:8191/v1"),
        ("http://[::1]/v1", "http://[::1]/v1"),
    ],
)
def test_valid_loopback_solver_urls_including_ipv6(monkeypatch, source, raw_url, expected):
    monkeypatch.setenv("ZPP_FLARESOLVERR_URL", raw_url)
    assert source._solver_url() == expected


@pytest.mark.parametrize(
    "encoding, compressor",
    [
        ("gzip", gzip.compress),
        ("deflate", lambda data: __import__("zlib").compress(data)),
    ],
)
@pytest.mark.parametrize("size", [128, 129, 800_000])
def test_compressed_non_pdf_limit_gzip_and_deflate(encoding, compressor, size):
    payload = compressor(b"x" * size)

    class CanaryStream(httpx.SyncByteStream):
        def __init__(self, raw: bytes) -> None:
            self.chunks = [raw[i:i + 32] for i in range(0, len(raw), 32)]
            self.pulled = 0

        def __iter__(self):
            for chunk in self.chunks:
                self.pulled += 1
                yield chunk

    stream = CanaryStream(payload)
    response = httpx.Response(200, headers={"Content-Encoding": encoding}, stream=stream)
    if size == 128:
        assert adapter._read_bounded_body(response, max_pdf_bytes=1_000_000, max_non_pdf_bytes=128) == b"x" * size
        assert stream.pulled == len(stream.chunks)
    else:
        with pytest.raises(PublicHTTPError, match="size limit"):
            adapter._read_bounded_body(response, max_pdf_bytes=1_000_000, max_non_pdf_bytes=128)
        if size == 800_000 and len(stream.chunks) > 2:
            assert stream.pulled < len(stream.chunks)


@pytest.mark.parametrize(
    "encoding, compressor",
    [
        ("gzip", gzip.compress),
        ("deflate", lambda data: __import__("zlib").compress(data)),
    ],
)
def test_compressed_split_pdf_prefix_and_exact_pdf_limit(monkeypatch, encoding, compressor):
    decompressed_calls: list[int] = []
    real_decompressobj = adapter.zlib.decompressobj

    class RecordingDecoder:
        def __init__(self, wbits):
            self._inner = real_decompressobj(wbits)

        def decompress(self, data, max_length=0):
            decompressed_calls.append(max_length)
            return self._inner.decompress(data, max_length)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(adapter.zlib, "decompressobj", RecordingDecoder)

    # Exact max_pdf_bytes boundary with %PDF- split across chunks
    exact = b"%PDF-1.7\n" + b"a" * 55  # total 64 bytes
    compressed_exact = compressor(exact)
    split_chunks = [compressed_exact[:6], compressed_exact[6:12], compressed_exact[12:]]

    class ChunkStream(httpx.SyncByteStream):
        def __init__(self, parts):
            self.parts = parts
            self.pulled = 0

        def __iter__(self):
            for part in self.parts:
                self.pulled += 1
                yield part

    resp_ok = httpx.Response(200, headers={"Content-Encoding": encoding}, stream=ChunkStream(split_chunks))
    assert adapter._read_bounded_body(resp_ok, max_pdf_bytes=64, max_non_pdf_bytes=16) == exact

    # One byte over max_pdf_bytes must fail immediately on first overshoot without exhausting large tail
    oversized = b"%PDF-1.7\n" + b"a" * 56  # total 65 bytes
    resp_over = httpx.Response(200, headers={"Content-Encoding": encoding}, stream=ChunkStream([compressor(oversized)]))
    with pytest.raises(PublicHTTPError, match="size limit"):
        adapter._read_bounded_body(resp_over, max_pdf_bytes=64, max_non_pdf_bytes=16)

    # Non-PDF bomb in a single chunk must cap decompress max_length near non-PDF budget, not max_pdf_bytes
    decompressed_calls.clear()
    bomb = compressor(b"<html>" + b"z" * 200_000)
    resp_bomb = httpx.Response(
        200,
        headers={"Content-Encoding": encoding},
        stream=ChunkStream([bomb, b"never-pulled-1", b"never-pulled-2"]),
    )
    with pytest.raises(PublicHTTPError, match="size limit"):
        adapter._read_bounded_body(resp_bomb, max_pdf_bytes=1_000_000, max_non_pdf_bytes=32)
    assert all(limit <= 33 for limit in decompressed_calls)


def test_download_pdf_slow_drip_hits_total_deadline(monkeypatch, source):
    now = [0.0]
    monkeypatch.setattr(adapter.time, "monotonic", lambda: now[0])
    import zotero_pdf_pipeline.public_http as net
    monkeypatch.setattr(net.time, "monotonic", lambda: now[0])

    class DripStream(httpx.SyncByteStream):
        def __init__(self):
            self.yielded = 0

        def __iter__(self):
            for byte in b"%PDF-1.7\n" + b"x" * 100:
                now[0] += 1.0
                self.yielded += 1
                yield bytes([byte])

    stream = DripStream()
    real_client = httpx.Client
    monkeypatch.setattr(
        adapter,
        "make_public_client",
        lambda **kw: real_client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, stream=stream)),
            trust_env=False,
            **{k: v for k, v in kw.items() if k != "transport"},
        ),
    )

    result = source._download_pdf(
        "https://journal.example/slow.pdf",
        landing_url="https://journal.example/paper",
        cookies=[],
        user_agent="Mozilla/5.0",
        timeout=3.0,
    )
    assert result is None
    assert now[0] <= 3.5
    assert stream.yielded <= 3


def test_download_pdf_stalled_read_caps_remaining_budget_and_closes_socket(monkeypatch, source):
    import httpcore
    from httpcore._backends.sync import SyncBackend
    import zotero_pdf_pipeline.public_http as net

    now = [0.0]
    monkeypatch.setattr(adapter.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(net.time, "monotonic", lambda: now[0])

    observed_timeouts = []
    closed = []

    class StallingStream:
        reads = 0

        def read(self, max_bytes, timeout=None):
            observed_timeouts.append(("read", now[0], timeout))
            self.reads += 1
            if self.reads == 1:
                now[0] = 2.75
                return b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n%PDF-"
            now[0] += timeout
            raise httpcore.ReadTimeout()

        def write(self, buffer, timeout=None):
            observed_timeouts.append(("write", now[0], timeout))

        def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            observed_timeouts.append(("tls", now[0], timeout))
            return self

        def close(self):
            closed.append(True)

        def get_extra_info(self, name):
            return None

    def fake_connect(self, host, port, timeout=None, **kwargs):
        observed_timeouts.append(("connect", now[0], timeout))
        now[0] = 2.25
        return StallingStream()

    monkeypatch.setattr(SyncBackend, "connect_tcp", fake_connect)

    result = source._download_pdf(
        "https://journal.example/stalled.pdf",
        landing_url="https://journal.example/paper",
        cookies=[],
        user_agent="Mozilla/5.0",
        timeout=3.0,
    )
    assert result is None
    assert closed == [True]
    assert now[0] <= 3.0
    assert observed_timeouts[-1] == ("read", 2.75, pytest.approx(0.25))


def test_extracts_direct_pdf_before_epdf_and_ignores_download_images(monkeypatch, source, tmp_path):
    def solver(_req, _body):
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "solution": {
                    "url": "https://journal.example/doi/10.1234/test.paper",
                    "status": 200,
                    "userAgent": "Mozilla/5.0",
                    "cookies": [{"name": "cf_clearance", "value": "ok", "domain": ".journal.example", "path": "/"}],
                    "response": (
                        '<a href="/doi/epdf/10.1234/test.paper">ePDF</a>'
                        '<a download href="/cms/asset/a1/fig1.jpg">Fig1</a>'
                        '<a download href="/cms/asset/a2/fig2.gif">Fig2</a>'
                        '<a download href="/cms/asset/a3/fig3.png">Fig3</a>'
                        '<a download href="/cms/asset/a4/fig4.svg">Fig4</a>'
                        '<a download href="/cms/asset/a5/fig5.webp">Fig5</a>'
                        '<a href="/doi/pdf/10.1234/test.paper?download=true">PDF</a>'
                    ),
                },
            },
        )

    def pdf(req):
        assert str(req.url) == "https://journal.example/doi/pdf/10.1234/test.paper?download=true"
        return httpx.Response(200, content=b"%PDF-1.6\nccs")

    _, pdf_calls = _mock_clients(monkeypatch, solver, pdf)
    out = source.fetch(PAPER, tmp_path)
    assert out is not None and out.read_bytes() == b"%PDF-1.6\nccs"
    assert [u for u, _ in pdf_calls] == ["https://journal.example/doi/pdf/10.1234/test.paper?download=true"]


def test_temporary_candidates_quarantine_cleanup_and_external_file_preserved(monkeypatch, tmp_path):
    from zotero_pdf_pipeline import cli
    from zotero_pdf_pipeline.config import load_config
    from zotero_pdf_pipeline.plugins import fetch_plugin
    from zotero_pdf_pipeline.state import Store

    # 1. Validation failure with flaresolverr (temporary_candidates=True):
    # candidate goes to staging-rejected and staging itself stays clean.
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(
        '[zotero]\ncollection_key="C"\n[paths]\narchive_dir="archive"\n'
        '[fetch]\nsource_order=[]\n[plugins]\nenabled=["flaresolverr", "ext"]\n'
    )
    cfg = load_config(cfg_path)
    cfg.plugins.directory = ROOT / "plugins"

    class FakeLocal:
        def __init__(self, *a):
            pass

        def items_without_attachment(self, key):
            return [PaperNeed("K1", "Expected Very Different Title", "10.1234/test", authors=["Smith"])]

        def creators_of(self, key):
            return ["Smith"]

        def close(self):
            pass

    monkeypatch.setattr(cli, "ZoteroLocal", FakeLocal)
    loaded = load_source("flaresolverr", ROOT / "plugins")
    module = __import__(loaded.__class__.__module__)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    monkeypatch.setattr(module, "guard_url", lambda url, **_kw: _fake_guard(url))

    real_client = httpx.Client
    monkeypatch.setattr(
        module.httpx,
        "Client",
        lambda **kw: real_client(
            transport=httpx.MockTransport(
                lambda _r: httpx.Response(
                    200,
                    json={
                        "status": "ok",
                        "solution": {
                            "url": "https://journal.example/p",
                            "status": 200,
                            "userAgent": "Mozilla/5.0",
                            "cookies": [],
                            "response": '<meta name="citation_pdf_url" content="/p.pdf">',
                        },
                    },
                )
            ),
            **{k: v for k, v in kw.items() if k != "transport"},
        ),
    )
    monkeypatch.setattr(
        module,
        "make_public_client",
        lambda **kw: real_client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"%PDF-1.5\nbad content " + b"x" * 30000)),
            trust_env=False,
            **{k: v for k, v in kw.items() if k != "transport"},
        ),
    )

    store = Store(cfg.paths.state_db)
    try:
        assert cli.cmd_fetch(cfg, store, ["K1"], plugin_names=["flaresolverr"]) == 1
        row = store.get("K1")
        assert row["stage"] == "failed"
        assert Path(row["file_path"]).parent.name == "staging-rejected"
        assert list(cfg.paths.staging_dir.iterdir()) == []
    finally:
        store.close()

    # 2. Copy failure cleans up TemporaryDirectory created by fetch_plugin
    created_dirs = []
    import zotero_pdf_pipeline.plugins as plugins_mod
    real_tempdir = plugins_mod.TemporaryDirectory

    class RecordingTempDir(real_tempdir):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created_dirs.append(Path(self.name))

    monkeypatch.setattr(plugins_mod, "TemporaryDirectory", RecordingTempDir)
    monkeypatch.setattr(plugins_mod, "copy_file", lambda *a, **k: (_ for _ in ()).throw(OSError("copy failed")))
    with pytest.raises(OSError, match="copy failed"):
        fetch_plugin("flaresolverr", ROOT / "plugins", PAPER, cfg.paths.staging_dir)
    assert len(created_dirs) == 1 and not created_dirs[0].exists()

    # 3. Plugin returning an external existing file (without temporary_candidates) preserves original
    ext_dir = tmp_path / "ext_plugins"
    ext_dir.mkdir()
    external_pdf = tmp_path / "external_source.pdf"
    external_pdf.write_bytes(b"%PDF-1.5\nkeep me")
    (ext_dir / "ext.py").write_text(
        f'from pathlib import Path\nclass Ext:\n    name = "ext"\n    def fetch(self, item_meta, dest_dir):\n        return Path({str(external_pdf)!r})\nsource = Ext()\n'
    )
    monkeypatch.undo()
    try:
        resolved_ext = fetch_plugin("ext", ext_dir, PAPER, cfg.paths.staging_dir)
        assert resolved_ext is not None
        assert external_pdf.is_file() and external_pdf.read_bytes() == b"%PDF-1.5\nkeep me"
    finally:
        plugins_mod._FILE_MODULES.pop((ext_dir / "ext.py").resolve(), None)
        import sys
        sys.modules.pop("zpp_plugin_ext", None)
