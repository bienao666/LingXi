"""System proxy policy and real HTTP CONNECT/SOCKS5 TLS transport tests."""
import datetime
import http.server
import ipaddress
import select
import socket
import socketserver
import ssl
import struct
import threading
from contextlib import contextmanager
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from sqlalchemy import select as sqlselect
from lingxi import network
from lingxi.api import app
from lingxi.db import SessionLocal
from lingxi.models import Audit
from lingxi.network import ProxyInput, PinnedTransport, SystemProxy, load_proxy, proxy_route


def policy(**values):
    return ProxyInput(enabled=True, http_proxy='http://172.20.192.1:7890',
                      https_proxy='http://172.20.192.1:7891', all_proxy='socks5://172.20.192.1:7892', **values)


def test_proxy_identity_csrf_versions_and_encrypted_persistence(client):
    with TestClient(app) as anonymous:
        assert anonymous.get('/api/admin/proxy').status_code == 401
        assert anonymous.put('/api/admin/proxy',json={}).status_code == 401
    initial=client.get('/api/admin/proxy').json()
    assert initial['enabled'] is False and initial['version']==0
    result=client.put('/api/admin/proxy',json=policy().model_dump())
    assert result.status_code==200 and result.json()['version']==1
    with SessionLocal() as db:
        row=db.get(SystemProxy,'system')
        assert '172.20.192.1' not in row.encrypted_config
        assert db.scalar(sqlselect(Audit).where(Audit.action=='system_proxy_update')).target_id=='system'
    assert load_proxy().https_proxy=='http://172.20.192.1:7891'
    assert client.put('/api/admin/proxy',json=policy().model_dump()).status_code==409
    body=result.json() | {'enabled':False}
    assert client.put('/api/admin/proxy',json=body).json()['version']==2
    assert load_proxy().enabled is False
    client.headers.pop('X-CSRF-Token')
    assert client.put('/api/admin/proxy',json=body).status_code==403


@pytest.mark.parametrize('changes', [
    {'http_proxy':'file:///etc/passwd'}, {'all_proxy':'socks4://host:1234'},
    {'http_proxy':'http://username:private-password@host:7890'}, {'http_proxy':'http://host'},
    {'http_proxy':'http://host:7890/path'}, {'enabled':True,'http_proxy':'http://host:7890'},
    {'no_proxy':['bad/url']},
])
def test_invalid_proxy_configuration_never_saves(client, changes):
    result=client.put('/api/admin/proxy',json=changes)
    assert result.status_code==422
    assert 'private-password' not in result.text
    assert load_proxy().version==0


def test_protocol_precedence_and_explicit_direct_rules():
    p=policy(no_proxy=['.internal.example','10.0.0.0/8'])
    assert proxy_route('https://api.example','8.8.8.8',p)==('http://172.20.192.1:7891','https_proxy')
    p.https_proxy=''
    assert proxy_route('https://api.example','8.8.8.8',p)==('socks5://172.20.192.1:7892','all_proxy')
    assert proxy_route('http://api.example','8.8.8.8',p)[1]=='http_proxy'
    for url,ip in [('http://hindsight','172.20.0.3'),('http://local','127.0.0.2'),('http://[::1]','::1')]:
        assert proxy_route(url,ip,p)==(None,'internal_direct')
    for url,ip in [('https://api.internal.example','8.8.8.8'),('https://internal.example','8.8.8.8'),('http://private','10.1.2.3')]:
        assert proxy_route(url,ip,p)==(None,'no_proxy')
    assert proxy_route('https://notinternal.example','8.8.8.8',p)[0] is not None
    p.enabled=False
    assert proxy_route('https://api.example','8.8.8.8',p)==(None,'direct')


def test_new_clients_use_saved_policy_and_shared_memory_gateway(configured, monkeypatch):
    seen=[]
    def transport(address, hostname, proxy=None):
        seen.append(proxy)
        return httpx.MockTransport(lambda req:httpx.Response(200,json={'data':[]}))
    monkeypatch.setattr(network,'validate_endpoint',lambda *a:'8.8.8.8')
    monkeypatch.setattr(network,'PinnedTransport',transport)
    body=policy().model_dump()
    configured.put('/api/admin/proxy',json=body)
    assert configured.get('/api/providers/provider/models').status_code==200
    from lingxi import api
    from lingxi.config import settings
    monkeypatch.setattr(settings(),'memory_gateway_token','test-internal')
    # Hindsight's external model calls pass through the same provider transport.
    assert configured.post('/internal/model/v1/chat/completions',json={},headers={'Authorization':'Bearer test-internal'}).status_code==200
    assert seen==[body['https_proxy'],body['https_proxy']]
    configured.put('/api/admin/proxy',json=body | {'version':1,'enabled':False})
    assert configured.get('/api/providers/provider/models').status_code==200
    assert seen[-1] is None


def test_proxy_test_does_not_save_and_reports_route(client, monkeypatch):
    from lingxi import api
    monkeypatch.setattr(api,'validate_endpoint',lambda *a:'8.8.8.8')
    monkeypatch.setattr(api,'outbound_client',lambda *a,**kw:httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(200))))
    result=client.post('/api/admin/proxy/test',json=policy().model_dump() | {'test_url':'https://example.com'})
    assert result.status_code==200 and result.json()['route']=='https_proxy'
    assert load_proxy().version==0


class ThreadServer(socketserver.ThreadingTCPServer):
    allow_reuse_address=True
    daemon_threads=True


def read_exact(sock, size):
    data=b''
    while len(data)<size:
        chunk=sock.recv(size-len(data))
        if not chunk:
            raise ConnectionError('Test socket closed')
        data+=chunk
    return data


@contextmanager
def serve(server):
    thread=threading.Thread(target=lambda:server.serve_forever(poll_interval=0.01),daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)


@pytest.fixture
def tls_origin(tmp_path):
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'origin.test')])
    now=datetime.datetime.now(datetime.timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(days=1))
          .not_valid_after(now+datetime.timedelta(days=1))
          .add_extension(x509.SubjectAlternativeName([x509.DNSName('origin.test'),x509.DNSName('proxy.test')]),critical=False)
          .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(key,hashes.SHA256()))
    cert_path,key_path=tmp_path/'cert.pem',tmp_path/'key.pem'
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert_path,key_path)
    seen=[]
    context.set_servername_callback(lambda sock,name,ctx:seen.append(name))
    class Origin(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers['Host']);self.send_response(200)
            self.send_header('Content-Length','8');self.end_headers();self.wfile.write(b'proxy-ok')
        def log_message(self,*args): pass
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Origin)
    server.socket=context.wrap_socket(server.socket,server_side=True)
    with serve(server) as port:
        yield port,ssl.create_default_context(cafile=str(cert_path)),seen,context


@contextmanager
def tunnel(protocol, seen, server_context=None):
    class Proxy(socketserver.BaseRequestHandler):
        def handle(self):
            sock=self.request;sock.settimeout(3)
            if protocol in ('http','https'):
                head=b''
                while not head.endswith(b'\r\n\r\n'): head+=read_exact(sock,1)
                method,target,_=head.split(b'\r\n',1)[0].split(b' ')
                assert method==b'CONNECT'
                host,port=target.decode().split(':')
                seen.append((host,int(port)))
                remote=socket.create_connection((host,int(port)),timeout=3)
                sock.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
            else:
                version,count=read_exact(sock,2);assert version==5
                read_exact(sock,count);sock.sendall(b'\x05\x00')
                version,command,reserved,kind=read_exact(sock,4)
                assert version==5 and command==1 and kind==1
                host=socket.inet_ntoa(read_exact(sock,4));port=struct.unpack('!H',read_exact(sock,2))[0]
                seen.append((host,port));remote=socket.create_connection((host,port),timeout=3)
                sock.sendall(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00')
            with remote:
                while True:
                    ready,_,_=select.select([sock,remote],[],[],3)
                    if not ready: break
                    for source in ready:
                        data=source.recv(65536)
                        if not data:return
                        (remote if source is sock else sock).sendall(data)
    server=ThreadServer(('127.0.0.1',0),Proxy)
    if protocol=='https':server.socket=server_context.wrap_socket(server.socket,server_side=True)
    with serve(server) as port:
        yield f'{protocol}://'+('proxy.test' if protocol=='https' else '127.0.0.1')+f':{port}'


@pytest.mark.parametrize('protocol',['http','socks5','https'])
def test_real_proxy_tunnel_keeps_pinned_ip_and_validates_original_tls_hostname(tls_origin,protocol,monkeypatch):
    origin_port,context,names,server_context=tls_origin;seen=[]
    original=socket.getaddrinfo
    monkeypatch.setattr(socket,'getaddrinfo',lambda host,*a,**kw:original('127.0.0.1' if host=='proxy.test' else host,*a,**kw))
    with tunnel(protocol,seen,server_context) as proxy:
        proxy_config=httpx.Proxy(proxy,ssl_context=context) if protocol=='https' else proxy
        transport=PinnedTransport('127.0.0.1','origin.test',proxy=proxy_config,verify=context)
        with httpx.Client(transport=transport,trust_env=False,timeout=3) as client:
            result=client.get(f'https://origin.test:{origin_port}/test')
    assert result.text=='proxy-ok'
    assert seen==[('127.0.0.1',origin_port)]
    assert names==(['proxy.test'] if protocol=='https' else [])+['origin.test',f'origin.test:{origin_port}']


def test_failed_proxy_never_falls_back_to_origin(tls_origin):
    port,context,names,_=tls_origin
    with socket.socket() as unavailable:
        unavailable.bind(('127.0.0.1',0))
        proxy=f'http://127.0.0.1:{unavailable.getsockname()[1]}'
        with httpx.Client(transport=PinnedTransport('127.0.0.1','origin.test',proxy=proxy,verify=context),timeout=0.5) as client:
            with pytest.raises((httpx.ConnectError,httpx.ConnectTimeout)):client.get(f'https://origin.test:{port}/')
    assert names==[]
