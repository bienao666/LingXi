"""Actual TLS WebSocket upgrade over the production pinned HTTPX transport."""
import base64
import hashlib
import http.server
import json
import socket
from contextlib import nullcontext

import httpx
import pytest
from httpx_ws import connect_ws

from lingxi.network import PinnedTransport
from test_network import tls_origin, serve, tunnel, read_exact


@pytest.mark.parametrize('protocol', ['direct', 'http', 'socks5', 'https'])
def test_native_websocket_keeps_proxy_and_tls_contract(tls_origin, protocol, monkeypatch):
    _, trust, names, tls = tls_origin
    seen, requests = [], []
    class Origin(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def do_GET(self):
            requests.append((self.path, self.headers.get('Host'), self.headers.get('Authorization')))
            assert self.headers['Upgrade'].lower() == 'websocket'
            key = self.headers['Sec-WebSocket-Key']
            accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'Upgrade')
            self.send_header('Sec-WebSocket-Accept', accept)
            self.end_headers()
            # Receive a small masked text frame from the real client.
            head = self.rfile.read(2)
            assert head[0] == 0x81 and head[1] & 128
            size = head[1] & 127
            assert size < 126
            mask, data = self.rfile.read(4), self.rfile.read(size)
            payload = bytes(v ^ mask[i % 4] for i, v in enumerate(data))
            assert json.loads(payload) == {'type': 'session.update'}
            reply = json.dumps({'type': 'session.updated', 'session': {'type': 'transcription'}}).encode()
            self.wfile.write(bytes([0x81, len(reply)]) + reply)
            self.wfile.flush()
            # Keep the socket open until the client sends close.
            self.rfile.read(2)
            self.close_connection = True
        def log_message(self, *args): pass
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Origin)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    original = socket.getaddrinfo
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host,*a,**kw: original('127.0.0.1' if host == 'proxy.test' else host,*a,**kw))
    context = nullcontext(None) if protocol == 'direct' else tunnel(protocol, seen, tls)
    with serve(server) as port, context as proxy:
        selected = httpx.Proxy(proxy, ssl_context=trust) if protocol == 'https' else proxy
        transport = PinnedTransport('127.0.0.1', 'origin.test', proxy=selected, verify=trust)
        with httpx.Client(transport=transport, timeout=3, trust_env=False, headers={'Authorization':'Bearer transport-fixture'}) as client:
            with connect_ws(f'https://origin.test:{port}/v1/realtime?intent=transcription', client,
                    keepalive_ping_interval_seconds=None) as websocket:
                websocket.send_json({'type':'session.update'})
                assert websocket.receive_json(timeout=3)['session']['type'] == 'transcription'
    assert requests == [('/v1/realtime?intent=transcription', f'origin.test:{port}', 'Bearer transport-fixture')]
    assert seen == ([] if protocol == 'direct' else [('127.0.0.1', port)])
    assert names == (['proxy.test'] if protocol == 'https' else []) + ['origin.test']
