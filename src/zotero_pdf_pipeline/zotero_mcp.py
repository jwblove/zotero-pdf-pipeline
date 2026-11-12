"""Bounded MCP HTTP/SSE client. Server text is never included in errors or logs."""
from __future__ import annotations
import json
import logging
import time
import urllib.error
import urllib.request
import uuid

LOG = logging.getLogger('pipeline.mcp')
PROTOCOL_VERSION = '2024-11-05'
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_ALLOWED_CODES = {-32700, -32600, -32601, -32602, -32603}

class McpError(RuntimeError):
    pass

def _envelope(value, request_id=None):
    if not isinstance(value, dict) or value.get('jsonrpc') != '2.0':
        raise McpError('Invalid MCP response shape')
    if request_id is not None and value.get('id') != request_id:
        raise McpError('MCP response id mismatch')
    if 'error' in value:
        error = value['error']
        code = error.get('code') if isinstance(error, dict) else None
        suffix = f' ({code})' if type(code) is int and code in _ALLOWED_CODES else ''
        raise McpError('MCP protocol error' + suffix)
    if not isinstance(value.get('result'), dict):
        raise McpError('Invalid MCP result shape')
    return value

def _parse_response(raw: str, request_id=None) -> dict:
    if len(raw.encode('utf-8')) > MAX_RESPONSE_BYTES:
        raise McpError('MCP response size limit exceeded')
    text = raw.strip()
    if text.startswith(('event:', 'data:', ':')) or '\ndata:' in text:
        # SSE events can include notifications and replies for other requests.
        for event in text.replace('\r\n','\n').split('\n\n'):
            data = '\n'.join(line[5:].lstrip() for line in event.splitlines() if line.startswith('data:'))
            if not data: continue
            try: value = json.loads(data)
            except (ValueError, RecursionError): continue
            if not isinstance(value, dict) or (request_id is not None and value.get('id') != request_id): continue
            if 'result' not in value and 'error' not in value: continue
            return _envelope(value, request_id)
        raise McpError('No matching MCP SSE reply')
    try: value = json.loads(text)
    except (ValueError, RecursionError) as exc: raise McpError('Invalid MCP JSON') from None
    return _envelope(value, request_id)

class McpClient:
    def __init__(self, url: str, timeout: int = 120):
        self.url, self.timeout, self.session_id = url, timeout, None

    def _post(self, payload: dict, expect_json: bool = True):
        headers = {'Content-Type':'application/json', 'Accept':'application/json, text/event-stream'}
        if self.session_id: headers['Mcp-Session-Id'] = self.session_id
        request = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=headers, method='POST')
        deadline = time.monotonic() + self.timeout
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                sid = response.headers.get('Mcp-Session-Id')
                if sid: self.session_id = sid
                if not expect_json: return None, ''
                length = int(response.headers.get('Content-Length', '0'))
                if not 0 <= length <= MAX_RESPONSE_BYTES: raise McpError('MCP response size limit exceeded')
                content = bytearray()
                sse = 'text/event-stream' in response.headers.get('Content-Type', '')
                while True:
                    if time.monotonic() >= deadline: raise McpError('MCP response deadline exceeded')
                    # HTTP headers may take the configured import timeout. Once
                    # streaming, cap idle reads to remaining total time (<= 5 s).
                    sock = getattr(getattr(getattr(response, 'fp', None), 'raw', None), '_sock', None)
                    if sock is not None:
                        sock.settimeout(min(5, max(.001, deadline-time.monotonic())))
                    # read1 returns available SSE bytes without waiting for a full buffer.
                    chunk = getattr(response, 'read1', response.read)(min(65536, MAX_RESPONSE_BYTES+1-len(content)))
                    if time.monotonic() >= deadline: raise McpError('MCP response deadline exceeded')
                    if not chunk: break
                    content.extend(chunk)
                    if len(content)>MAX_RESPONSE_BYTES: raise McpError('MCP response size limit exceeded')
                    if sse and (b'\n\n' in content or b'\r\n\r\n' in content):
                        # Only parse complete events; do not accept a partial last event.
                        text = content.decode('utf-8', 'replace').replace('\r\n','\n')
                        complete = text.rsplit('\n\n',1)[0] + '\n\n'
                        try: parsed = _parse_response(complete, payload.get('id'))
                        except McpError as exc:
                            if str(exc) != 'No matching MCP SSE reply': raise
                        else: return parsed, ''
                return _parse_response(content.decode('utf-8'), payload.get('id')), ''
        except McpError: raise
        except urllib.error.HTTPError as exc:
            code = exc.code if type(exc.code) is int and 100 <= exc.code <= 599 else 0
            raise McpError(f'MCP HTTP {code}') from None
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise McpError('MCP connection failed or timed out') from None
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise McpError('Invalid MCP response') from None

    def initialize(self) -> dict:
        response, _ = self._post({'jsonrpc':'2.0', 'id':str(uuid.uuid4()), 'method':'initialize',
            'params':{'protocolVersion':PROTOCOL_VERSION, 'capabilities':{},
                      'clientInfo':{'name':'zotero-pdf-pipeline', 'version':'0.1'}}})
        response = _envelope(response)
        self._post({'jsonrpc':'2.0','method':'notifications/initialized','params':{}}, expect_json=False)
        return response['result']

    def call_tool(self, name: str, arguments: dict) -> object:
        if not self.session_id: self.initialize()
        response, _ = self._post({'jsonrpc':'2.0', 'id':str(uuid.uuid4()), 'method':'tools/call',
                                 'params':{'name':name, 'arguments':arguments}})
        payload = _envelope(response)['result']
        if payload.get('isError'): raise McpError('MCP tool returned an error')
        content = payload.get('content')
        if not isinstance(content, list) or any(not isinstance(c,dict) for c in content):
            raise McpError('Invalid MCP tool content')
        texts = [c.get('text') for c in content if c.get('type')=='text']
        if any(not isinstance(text,str) for text in texts): raise McpError('Invalid MCP tool text')
        try: data = json.loads('\n'.join(texts))
        except (ValueError, RecursionError) as exc: raise McpError('Invalid MCP tool JSON') from None
        if not isinstance(data,(dict,list)): raise McpError('Invalid MCP tool data')
        if isinstance(data,dict) and (data.get('success') is False or data.get('error')):
            raise McpError('MCP tool reported failure')
        return data
