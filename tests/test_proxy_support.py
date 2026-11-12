"""Environment routing and target SSRF protection, using fake transports only."""
import httpx
import pytest
from types import SimpleNamespace
from zotero_pdf_pipeline import public_http as net, cli, plugins
from zotero_pdf_pipeline.config import load_config, FetchConfig, ConfigError
from zotero_pdf_pipeline.models import PaperNeed
from zotero_pdf_pipeline.state import Store


@pytest.fixture
def routes(monkeypatch):
    routes = []
    def init(self, *args, **kwargs):
        self.proxy = kwargs.get('proxy')
        self._pool = SimpleNamespace()  # Production installs its bounded backend.
    def handle(self, request):
        routes.append((self.proxy, request))
        if request.url.path == '/redirect':
            return httpx.Response(302, headers={'Location':'https://files.invalid/paper'})
        if request.url.path == '/private':
            return httpx.Response(302, headers={'Location':'https://127.0.0.1/admin'})
        return httpx.Response(200, content=b'%PDF-test')
    monkeypatch.setattr(httpx.HTTPTransport, '__init__', init)
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', handle)
    monkeypatch.setattr(httpx.HTTPTransport, 'close', lambda self:None)
    return routes


@pytest.mark.parametrize('name',['HTTPS_PROXY','https_proxy','ALL_PROXY','all_proxy'])
def test_standard_proxy_environment_routes_original_hostname(monkeypatch,routes,name):
    monkeypatch.setenv(name,'http://proxy.invalid:8080')
    assert net.request('https://public.invalid/paper').content == b'%PDF-test'
    proxy, request = routes[0]
    assert proxy == 'http://proxy.invalid:8080' and request.url.host == 'public.invalid'
    assert request.extensions['zpp_addresses'] == ('93.184.216.34',)


def test_lowercase_proxy_precedes_uppercase(monkeypatch,routes):
    monkeypatch.setenv('HTTPS_PROXY','http://upper.invalid:8080')
    monkeypatch.setenv('https_proxy','http://lower.invalid:8080')
    net.request('https://public.invalid/paper')
    assert routes[0][0] == 'http://lower.invalid:8080'


@pytest.mark.parametrize('bypass',['public.invalid','.public.invalid','public.invalid:443','*'])
def test_no_proxy_bypass_keeps_pinned_direct_transport(monkeypatch,routes,bypass):
    monkeypatch.setenv('HTTPS_PROXY','http://proxy.invalid:8080')
    monkeypatch.setenv('NO_PROXY',bypass)
    net.request('https://public.invalid/paper')
    proxy, request = routes[0]
    assert proxy is None and request.url.host == '93.184.216.34'
    assert request.headers['host'] == 'public.invalid' and request.extensions['sni_hostname'] == 'public.invalid'


def test_lowercase_no_proxy_domain_suffix(monkeypatch,routes):
    monkeypatch.setenv('https_proxy','http://proxy.invalid:8080')
    monkeypatch.setenv('NO_PROXY','other.invalid')
    monkeypatch.setenv('no_proxy','.public.invalid')
    net.request('https://child.public.invalid/paper')
    assert routes[0][0] is None


def test_disable_env_proxy_keeps_ip_pinning(monkeypatch,routes):
    monkeypatch.setenv('HTTPS_PROXY','http://proxy.invalid:8080')
    with net.proxy_policy(False): net.request('https://public.invalid/paper')
    assert routes[0][0] is None and routes[0][1].url.host == '93.184.216.34'
    assert net._USE_ENV_PROXY.get() is True


def test_redirect_reselects_proxy_and_validates_each_host(monkeypatch,routes):
    resolved=[]
    def resolve(host,port): resolved.append(host); return ['93.184.216.34']
    monkeypatch.setattr(net,'_resolve',resolve)
    monkeypatch.setenv('HTTPS_PROXY','http://proxy.invalid:8080')
    monkeypatch.setenv('NO_PROXY','files.invalid')
    net.request('https://public.invalid/redirect')
    assert [proxy for proxy,req in routes] == ['http://proxy.invalid:8080',None]
    assert resolved == ['public.invalid','files.invalid']


@pytest.mark.parametrize('url',['https://127.0.0.1/admin','https://[::1]/admin','https://10.1.2.3/admin'])
def test_proxy_never_allows_private_target(monkeypatch,routes,url):
    monkeypatch.setenv('HTTPS_PROXY','http://proxy.invalid:8080')
    with pytest.raises(net.PublicHTTPError): net.request(url)
    assert not routes


def test_proxy_redirect_to_private_target_is_blocked(monkeypatch,routes):
    monkeypatch.setenv('HTTPS_PROXY','http://proxy.invalid:8080')
    with pytest.raises(net.PublicHTTPError): net.request('https://public.invalid/private')
    assert len(routes) == 1


def test_proxy_dns_result_still_must_be_public(monkeypatch,routes):
    monkeypatch.setenv('HTTPS_PROXY','http://proxy.invalid:8080')
    monkeypatch.setattr(net,'_resolve',lambda *a:['93.184.216.34','192.168.1.1'])
    with pytest.raises(net.PublicHTTPError): net.request('https://public.invalid/paper')
    assert not routes


def test_proxy_credentials_never_appear_in_errors(monkeypatch,routes,caplog):
    monkeypatch.setenv('HTTPS_PROXY','http://user:proxy-secret@proxy.invalid:8080')
    def fail(self,request): raise httpx.ProxyError('http://user:proxy-secret@proxy.invalid:8080')
    monkeypatch.setattr(httpx.HTTPTransport,'handle_request',fail)
    with pytest.raises(net.PublicHTTPError) as error: net.request('https://public.invalid/paper')
    assert 'proxy-secret' not in str(error.value) + caplog.text
    assert error.value.__suppress_context__


def test_proxy_setting_default_disable_and_type_validation(tmp_path):
    assert FetchConfig().use_env_proxy is True
    path=tmp_path/'settings.toml'
    path.write_text('[paths]\narchive_dir="archive"\n[fetch]\nuse_env_proxy=false\n')
    assert load_config(path).fetch.use_env_proxy is False
    path.write_text('[paths]\narchive_dir="archive"\n[fetch]\nuse_env_proxy="false"\n')
    with pytest.raises(ConfigError,match='use_env_proxy'):load_config(path)


def test_cli_proxy_setting_covers_crossref_builtins_and_plugin_helpers(monkeypatch,tmp_path):
    path=tmp_path/'settings.toml'
    path.write_text('[zotero]\ncollection_key="C"\n[paths]\narchive_dir="archive"\n[fetch]\nuse_env_proxy=false\nenable_crossref=true\nsource_order=[]\n[plugins]\nenabled=["fake"]\n')
    cfg=load_config(path)
    class Local:
        def __init__(self,*a):pass
        def items_without_attachment(self,key):return [PaperNeed('K','A complete research title')]
        def creators_of(self,key):return ['Lee']
        def close(self):pass
    checks=[]
    def lookup(*a):checks.append(('crossref',net._USE_ENV_PROXY.get()));return '10.1234/test'
    def builtin(*a,**k):checks.append(('builtin',net._USE_ENV_PROXY.get()));return None
    def plugin(*a,**k):checks.append(('plugin',net._USE_ENV_PROXY.get()));return None
    monkeypatch.setattr(cli,'ZoteroLocal',Local)
    monkeypatch.setattr(cli,'lookup_crossref',lookup)
    monkeypatch.setattr(cli,'try_free_sources',builtin)
    monkeypatch.setattr(plugins,'fetch_plugin',plugin)
    store=Store(cfg.paths.state_db)
    try:
        assert cli.cmd_fetch(cfg,store,['K'],plugin_names=['fake']) == 1
        assert checks == [('crossref',False),('builtin',False),('plugin',False)]
        assert net._USE_ENV_PROXY.get() is True
    finally:store.close()
