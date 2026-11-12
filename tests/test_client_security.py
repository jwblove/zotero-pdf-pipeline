"""MCP/local API malformed and sensitive responses fail with fixed diagnostics."""
import io
import json
import httpx
import pytest
from zotero_pdf_pipeline.zotero_mcp import McpClient,McpError,_parse_response,MAX_RESPONSE_BYTES
from zotero_pdf_pipeline import zotero_local as local

@pytest.mark.parametrize('response', [None,[],{'error':{'message':'secret'}},
    {'jsonrpc':'2.0','error':{'code':-32603,'message':'secret?token=hidden'}},
    {'jsonrpc':'2.0','result':[]}])
def test_handshake_does_not_echo_raw_response(monkeypatch,response):
    client=McpClient('http://127.0.0.1:23120/mcp')
    monkeypatch.setattr(client,'_post',lambda *a,**k:(response,'raw-secret'))
    with pytest.raises(McpError) as exc: client.initialize()
    assert 'secret' not in str(exc.value) and 'hidden' not in str(exc.value)

@pytest.mark.parametrize('payload',[{'isError':True,'content':[{'text':'secret'}]},
    {'content':'secret'}, {'content':[None]}, {'content':[{'type':'text','text':'not json secret'}]},
    {'content':[{'type':'text','text':'{"success":false,"message":"secret"}'}]}])
def test_tool_shape_and_json_errors_redacted(monkeypatch,payload):
    client=McpClient('http://127.0.0.1:23120/mcp');client.session_id='session'
    monkeypatch.setattr(client,'_post',lambda *a,**k:({'jsonrpc':'2.0','result':payload},''))
    with pytest.raises(McpError) as exc: client.call_tool('write_item',{})
    assert 'secret' not in str(exc.value)

def test_sse_matches_request_id_not_first_event():
    raw='data: {"jsonrpc":"2.0","method":"notification"}\n\ndata: {"jsonrpc":"2.0","id":"other","result":{}}\n\ndata: {"jsonrpc":"2.0","id":"wanted","result":{"ok":true}}\n\n'
    assert _parse_response(raw,'wanted')['result']=={'ok':True}
    with pytest.raises(McpError): _parse_response(raw,'missing')

class Response(io.BytesIO):
    def __init__(self,body,headers=None): super().__init__(body);self.headers=headers or {}

@pytest.mark.parametrize('failure',[TimeoutError('secret'),ValueError('secret')])
def test_transport_errors_wrapped(monkeypatch,failure):
    def fail(*a,**k): raise failure
    monkeypatch.setattr('urllib.request.urlopen',fail)
    with pytest.raises(McpError) as exc: McpClient('http://127.0.0.1:23120/mcp')._post({'id':'a'})
    assert 'secret' not in str(exc.value)

def test_http_sse_stops_after_matching_reply(monkeypatch):
    def open_response(req,**kw):
        identifier=json.loads(req.data)['id']
        return Response(('data: '+json.dumps({'jsonrpc':'2.0','id':identifier,'result':{'ok':True}})+'\n\n').encode(),{'Content-Type':'text/event-stream'})
    monkeypatch.setattr('urllib.request.urlopen',open_response)
    result,_=McpClient('http://127.0.0.1:23120/mcp')._post({'id':'wanted'})
    assert result['result']['ok']

def test_sse_total_deadline(monkeypatch):
    now=[0.0]
    monkeypatch.setattr('zotero_pdf_pipeline.zotero_mcp.time.monotonic',lambda:now[0])
    class Endless(Response):
        def read1(self,maximum): now[0]+=1;return b': heartbeat\n\n'
    monkeypatch.setattr('urllib.request.urlopen',lambda *a,**k: Endless(b'',{'Content-Type':'text/event-stream'}))
    with pytest.raises(McpError,match='deadline'): McpClient('http://127.0.0.1:23120/mcp',timeout=3)._post({'id':'a'})
    assert now[0]==3

def test_response_size_limit(monkeypatch):
    monkeypatch.setattr('urllib.request.urlopen',lambda *a,**k: Response(b'x',{'Content-Length':str(MAX_RESPONSE_BYTES+1)}))
    with pytest.raises(McpError,match='size limit'): McpClient('http://127.0.0.1:23120/mcp')._post({'id':'a'})

@pytest.mark.parametrize('content',[b'not json secret',b'null',b'{"data":[]}',b'[{"data":{"creators":"bad"}}]'])
def test_local_invalid_response_wrapped(monkeypatch,content):
    monkeypatch.setattr(local,'make_client',lambda **kw: httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200,content=content))))
    client=local.ZoteroLocal('http://127.0.0.1:23119/api/users/0')
    try:
        with pytest.raises(local.ZoteroLocalError) as exc: client._get('/items')
        assert 'secret' not in str(exc.value)
    finally: client.close()

def test_local_timeout_wrapped(monkeypatch):
    def fail(req): raise TimeoutError('private secret URL')
    monkeypatch.setattr(local,'make_client',lambda **kw: httpx.Client(transport=httpx.MockTransport(fail)))
    client=local.ZoteroLocal('http://127.0.0.1:23119/api/users/0')
    try:
        with pytest.raises(local.ZoteroLocalError) as exc: client._get('/items')
        assert 'secret' not in str(exc.value)
    finally: client.close()
