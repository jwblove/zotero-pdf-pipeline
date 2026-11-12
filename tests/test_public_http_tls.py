"""Real TLS over isolated loopback; production URL guards remain enabled."""
import os
import shutil
import socket
import ssl
import subprocess
import threading
import pytest
from zotero_pdf_pipeline import public_http as net

# Capture before function-scoped no-network monkeypatches. Only the exact local
# fixture socket is allowed below; all other connection attempts still fail.
_CONNECT = socket.socket.connect
_CREATE_CONNECTION = socket.create_connection


@pytest.mark.parametrize('address,hostname,valid',[
    ('93.184.216.34','public.fixture.invalid',True),
    ('93.184.216.34','wrong.fixture.invalid',False)])
def test_guarded_transport_preserves_host_sni_and_certificate_validation(
        tmp_path,monkeypatch,address,hostname,valid):
    openssl=shutil.which('openssl')
    if not openssl:pytest.skip('openssl unavailable for ephemeral loopback TLS certificate')
    cert,key=tmp_path/'cert.pem',tmp_path/'key.pem'
    created=subprocess.run([openssl,'req','-x509','-newkey','rsa:2048','-nodes',
        '-keyout',str(key),'-out',str(cert),'-days','1','-subj','/CN=public.fixture.invalid',
        '-addext','subjectAltName=DNS:public.fixture.invalid'],
        env={'PATH':'/usr/bin:/bin'},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
    assert created.returncode==0
    server=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert,key)  # Fresh fixture keys only; no user credentials.
    observed={'sni':None,'http':b''}
    server.set_servername_callback(lambda sock,name,ctx:observed.update(sni=name))
    listener=None
    try:
        listener=socket.socket()
        listener.bind(('127.0.0.1',0));listener.listen(1)
    except OSError:
        if listener is not None:listener.close()
        pytest.skip('sandbox does not allow an isolated loopback TLS listener')
    port=listener.getsockname()[1]
    listener.settimeout(5)
    errors=[]
    def serve():
        try:
            with listener.accept()[0] as raw:
                raw.settimeout(5)
                with server.wrap_socket(raw,server_side=True) as stream:
                    while b'\r\n\r\n' not in observed['http']:
                        part=stream.recv(4096)
                        if not part:break
                        observed['http']+=part
                    body=b'%PDF-local-fixture'
                    stream.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(body)).encode()+
                                   b'\r\nConnection: close\r\n\r\n'+body)
        except ssl.SSLError:
            if valid:errors.append('unexpected TLS error')
        except Exception as error:errors.append(type(error).__name__)
    thread=threading.Thread(target=serve,daemon=True);thread.start()
    def only_fixture(sock,endpoint):
        assert endpoint==('127.0.0.1',port),'non-fixture socket refused'
        return _CONNECT(sock,endpoint)
    def simulated_route(endpoint,*args,**kwargs):
        # Stand in for a public route after the unmodified guard validated the
        # public address. No security opt-out is passed to request.
        assert endpoint==(address,port),'unexpected transport destination'
        return _CREATE_CONNECTION(('127.0.0.1',port),*args,**kwargs)
    monkeypatch.setattr(socket.socket,'connect',only_fixture)
    monkeypatch.setattr(socket,'create_connection',simulated_route)
    monkeypatch.setattr(net,'_resolve',lambda *a:[address])
    monkeypatch.setenv('SSL_CERT_FILE',str(cert))
    try:
        with net.proxy_policy(False):
            if valid:
                response=net.request(f'https://{hostname}:{port}/pdf',timeout=3,max_bytes=100)
                assert response.content==b'%PDF-local-fixture'
            else:
                with pytest.raises(net.PublicHTTPError):net.request(f'https://{hostname}:{port}/pdf',timeout=3)
        thread.join(6)
        assert not thread.is_alive() and not errors
        assert observed['sni']==hostname
        if valid:assert f'Host: {hostname}:{port}\r\n'.encode() in observed['http']
        else:assert not observed['http']  # Certificate rejection before HTTP.
    finally:
        listener.close()
        thread.join(6)
