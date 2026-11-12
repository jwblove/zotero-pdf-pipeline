"""Strict public-DNS guard rejects synthetic addresses before any transport."""
import httpx
import pytest
from types import SimpleNamespace
from zotero_pdf_pipeline import public_http as net


@pytest.fixture
def routes(monkeypatch):
    seen=[]
    def init(self, **kw):
        self.proxy = kw.get('proxy')
        self._pool = SimpleNamespace()  # Production installs its bounded backend.
    monkeypatch.setattr(httpx.HTTPTransport,'__init__',init)
    def handle(self,request):
        seen.append((self.proxy,request))
        if request.url.path=='/redirect':
            return httpx.Response(302,headers={'Location':'https://next.invalid/pdf'})
        return httpx.Response(200,content=b'%PDF-test')
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',handle)
    monkeypatch.setattr(httpx.HTTPTransport,'close',lambda self:None)
    return seen


@pytest.mark.parametrize('proxy',[False,True])
@pytest.mark.parametrize('answers',[
    ['198.18.0.8'], ['198.19.255.254'], ['93.184.216.34','198.18.0.8'],
    ['198.18.0.8','10.0.0.1'], ['10.0.0.1'], ['169.254.1.1']])
def test_nonpublic_dns_is_refused_with_or_without_proxy(monkeypatch,routes,proxy,answers):
    if proxy:monkeypatch.setenv('HTTPS_PROXY','http://proxy.invalid:8080')
    monkeypatch.setattr(net,'_resolve',lambda *a:answers)
    with pytest.raises(net.PublicHTTPError,match='correct DNS/network routing outside the application'):
        net.request('https://public.invalid/pdf')
    assert not routes


def test_no_proxy_never_bypasses_dns_guard(monkeypatch,routes):
    monkeypatch.setenv('HTTPS_PROXY','http://proxy.invalid:8080')
    monkeypatch.setenv('NO_PROXY','*')
    monkeypatch.setattr(net,'_resolve',lambda *a:['198.18.0.8'])
    with pytest.raises(net.PublicHTTPError):net.request('https://public.invalid/pdf')
    assert not routes


@pytest.mark.parametrize('host',[
    '198.18.0.8','198.19.0.9','127.0.0.1','10.0.0.1','169.254.1.1','[::1]',
    '[::ffff:198.18.0.8]','localhost','a.localhost','a.local','a.internal','single',
    'LOCALHOST.','a.LOCAL.','127.1','0x7f.0x0.0x0.0x1','bad_host.invalid'])
def test_literals_and_local_names_are_not_public_targets(monkeypatch,routes,host):
    monkeypatch.setenv('HTTPS_PROXY','http://proxy.invalid:8080')
    monkeypatch.setattr(net,'_resolve',lambda *a:['198.18.0.8'])
    with pytest.raises(net.PublicHTTPError):net.request('https://'+host+'/pdf')
    assert not routes


def test_redirect_to_fake_ip_dns_is_refused(monkeypatch,routes):
    monkeypatch.setattr(net,'_resolve',lambda host,port:['93.184.216.34'] if host=='public.invalid' else ['198.18.0.8'])
    with pytest.raises(net.PublicHTTPError):net.request('https://public.invalid/redirect')
    assert len(routes)==1
