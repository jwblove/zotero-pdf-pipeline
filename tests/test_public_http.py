"""Public URL guard, address pinning, limits and retries: no live DNS/HTTP."""
import gzip
import httpx
import pytest
from zotero_pdf_pipeline import public_http as net, free_sources as free

@pytest.mark.parametrize('url', [
    'http://public.invalid/x', 'https://user:password@public.invalid/x',
    'https://127.0.0.1/x', 'https://10.2.3.4/x', 'https://172.16.0.1/x',
    'https://192.168.1.1/x', 'https://169.254.1.1/x', 'https://100.64.1.1/x',
    'https://224.0.0.1/x', 'https://0.0.0.0/x', 'https://240.0.0.1/x',
    'https://[::1]/x', 'https://[fc00::1]/x', 'https://[fe80::1]/x',
    'https://[ff02::1]/x', 'https://[::ffff:8.8.8.8]/x', 'https://[fec0::1]/x',
    'https://[64:ff9b::7f00:1]/x', 'file:///tmp/a.pdf'])
def test_forbidden_urls(url):
    with pytest.raises(net.PublicHTTPError): net.guard_url(url)

def factory(handler):
    return lambda **kwargs: httpx.Client(transport=httpx.MockTransport(handler), trust_env=False, **kwargs)

def test_mixed_public_private_dns_refused(monkeypatch):
    monkeypatch.setattr(net, '_resolve', lambda *a: ['93.184.216.34','127.0.0.1'])
    with pytest.raises(net.PublicHTTPError): net.guard_url('https://public.invalid')

def test_redirect_validated_each_hop_and_maximum(monkeypatch):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(302,headers={'Location':'https://127.0.0.1/private?token=hidden'})
    with pytest.raises(net.PublicHTTPError): net.request('https://public.invalid', client_factory=factory(handler))
    assert len(calls)==1
    calls.clear()
    def loop(request):
        calls.append(request)
        return httpx.Response(302,headers={'Location':'/again'})
    with pytest.raises(net.PublicHTTPError, match='redirect limit'):
        net.request('https://public.invalid',client_factory=factory(loop))
    assert len(calls)==net.MAX_REDIRECTS+1

def test_pinned_transport_uses_ip_host_and_sni(monkeypatch):
    seen=[]
    def handle(self, request):
        seen.append(request)
        return httpx.Response(200,content=b'OK')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', handle)
    with net.make_public_client() as client:
        client.get('https://public.invalid/a',extensions={'zpp_addresses':('93.184.216.34',)})
    assert seen[0].url.host=='93.184.216.34'
    assert seen[0].headers['host']=='public.invalid'
    assert seen[0].extensions['sni_hostname']=='public.invalid'

class Stream(httpx.SyncByteStream):
    def __init__(self, chunks): self.chunks=chunks
    def __iter__(self): yield from self.chunks

@pytest.mark.parametrize('headers,chunks', [
    ({'Content-Length':'1000'},[b'x']), ({},[b'x'*40,b'x'*40]),
    ({'Content-Encoding':'gzip'},[gzip.compress(b'x'*10000)])])
def test_limits_include_streamed_and_decoded_bytes(headers,chunks):
    def handler(req): return httpx.Response(200,headers=headers,stream=Stream(chunks))
    with pytest.raises(net.PublicHTTPError,match='size limit'):
        net.request('https://public.invalid',max_bytes=60,client_factory=factory(handler))

def test_valid_public_redirect_and_bounded_gzip():
    seen=[]
    def handler(req):
        seen.append(req)
        if req.url.path=='/start': return httpx.Response(302,headers={'Location':'/end'})
        return httpx.Response(200,headers={'Content-Encoding':'gzip'},stream=Stream([gzip.compress(b'%PDF-good')]))
    result=net.request('https://public.invalid/start',max_bytes=100,client_factory=factory(handler))
    assert result.content==b'%PDF-good' and len(seen)==2

@pytest.mark.parametrize('status', [429,503])
def test_retry_after_backoff_and_cooldown(status):
    requests=[]
    def handler(req): requests.append(req); return httpx.Response(status,headers={'Retry-After':'2'})
    client=factory(handler)
    with pytest.raises(net.PublicHTTPError,match='cooldown'):
        net.request('https://api.semanticscholar.org/one', client_factory=client)
    assert len(requests)==3
    with pytest.raises(net.PublicHTTPError,match='cooldown'):
        net.request('https://api.semanticscholar.org/two', client_factory=client)
    assert len(requests)==3
    assert net._COOLDOWN['api.semanticscholar.org']>net.time.monotonic()

def test_long_retry_after_is_bounded_and_skips_later_items():
    calls=[]
    def handler(req): calls.append(req); return httpx.Response(429,headers={'Retry-After':'999999'})
    with pytest.raises(net.PublicHTTPError): net.request('https://public.invalid',client_factory=factory(handler))
    assert len(calls)==1
    assert net._COOLDOWN['public.invalid']-net.time.monotonic()==60

def test_retry_recovers_with_fake_clock():
    calls=[]
    def handler(req):
        calls.append(req)
        return httpx.Response(503,headers={'Retry-After':'3'}) if len(calls)==1 else httpx.Response(200,content=b'OK')
    start=net.time.monotonic()
    assert net.request('https://public.invalid',client_factory=factory(handler)).content==b'OK'
    assert net.time.monotonic()-start>=3 and len(calls)==2

def test_contact_and_authorization_not_sent_to_downloads():
    calls=[]
    def handler(req):
        calls.append(req)
        if len(calls)==1: return httpx.Response(302,headers={'Location':'https://pdf.invalid/file'})
        return httpx.Response(200,content=b'%PDF-good')
    net.request('https://api.openalex.org/works',params={'mailto':'contact@institute.invalid'},
        headers={'User-Agent':'pipeline contact@institute.invalid','Authorization':'Bearer secret',
                 'Referer':'https://api.openalex.org/page?token=secret#private'},client_factory=factory(handler))
    assert 'contact@' in calls[0].headers['User-Agent']
    assert calls[0].headers['Referer']=='https://api.openalex.org/page'
    assert 'Referer' not in calls[1].headers and 'Authorization' not in calls[1].headers
    assert 'contact@' not in calls[1].headers['User-Agent'] and 'mailto' not in calls[1].url.params

def test_free_source_uses_guard_for_landing_and_pdf(monkeypatch,tmp_path):
    monkeypatch.setattr(free, 'make_client', factory(lambda req: httpx.Response(200,text='<meta name="citation_pdf_url" content="https://127.0.0.1/private">')))
    pdf=free._landing_pdf('https://public.invalid',5)
    with pytest.raises(free.FreeSourceError): free._download(pdf,tmp_path/'x.pdf')
    assert not list(tmp_path.iterdir())

def test_public_ipv6_and_internal_test_only_override():
    assert net.guard_url('https://[2001:4860:4860::8888]/')[1]==('2001:4860:4860::8888',)
    assert net.guard_url('https://127.0.0.1/',_allow_test_addresses=True)[1]==('127.0.0.1',)

def test_pdf_cap_propagates_through_source_pipeline(monkeypatch,tmp_path):
    from zotero_pdf_pipeline.models import PaperNeed
    def handler(req):
        if req.url.host=='api.semanticscholar.org':return httpx.Response(200,json={'openAccessPdf':{'url':'https://pdf.invalid/x'}})
        return httpx.Response(200,content=b'%PDF-'+b'x'*100)
    monkeypatch.setattr(free,'make_client',factory(handler))
    diagnostics=[]
    assert free.try_all(PaperNeed('K','Title','10.1234/test'),tmp_path,['semantic_scholar'],False,
                        max_pdf_bytes=20,diagnostics=diagnostics) is None
    assert 'size limit' in diagnostics[0] and not list(tmp_path.iterdir())
