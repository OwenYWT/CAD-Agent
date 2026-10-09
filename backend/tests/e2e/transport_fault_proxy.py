"""Forward real HTTP/WS traffic and cut selected connections, never forge replies.

This test-only proxy distinguishes a request that never reached the server from
a committed write whose response was lost. Readback still uses the real API.
"""
import http.client
import select
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class TransportFaultProxy:
    def __init__(self, upstream: str):
        target = urlsplit(upstream)
        assert target.scheme == 'http' and target.hostname in {'127.0.0.1', 'localhost'}
        self.target = (target.hostname, target.port or 80)
        self.drop_response_once = None
        self.drop_request_once = None
        self.block_reads = None
        self.counts = {}
        self.dropped_after_response = []
        self.lock = threading.Lock()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def handle_request(self):
                try:
                    self.forward_request()
                except (BrokenPipeError, ConnectionResetError):
                    # Browser navigation/offline deliberately closes streams.
                    self.close_connection = True

            def forward_request(self):
                path = urlsplit(self.path).path
                identity = (self.command, path)
                with proxy.lock:
                    proxy.counts[identity] = proxy.counts.get(identity, 0) + 1
                    blocked = self.command == 'GET' and proxy.block_reads and path.startswith(proxy.block_reads)
                    if proxy.drop_request_once == identity:
                        proxy.drop_request_once = None
                        blocked = True
                    drop_response = proxy.drop_response_once == identity
                    if drop_response:
                        proxy.drop_response_once = None
                self.close_connection = True
                if blocked:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                if self.headers.get('Upgrade', '').lower() == 'websocket':
                    with socket.create_connection(proxy.target, timeout=10) as remote:
                        remote.settimeout(None)
                        head = f'{self.command} {self.path} HTTP/1.1\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in self.headers.items()) + '\r\n'
                        remote.sendall(head.encode('latin-1'))
                        while True:
                            ready, _, _ = select.select([self.connection, remote], [], [], 30)
                            for source in ready:
                                data = source.recv(65536)
                                if not data:
                                    return
                                (remote if source is self.connection else self.connection).sendall(data)
                    return
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                headers = dict(self.headers)
                headers['Connection'] = 'close'
                remote = http.client.HTTPConnection(*proxy.target, timeout=120)
                try:
                    remote.request(self.command, self.path, body=body, headers=headers)
                    response = remote.getresponse()
                    content = response.read()
                    if drop_response:
                        with proxy.lock:
                            proxy.dropped_after_response.append((identity, response.status))
                        self.connection.shutdown(socket.SHUT_RDWR)
                        return
                    self.send_response_only(response.status, response.reason)
                    for key, value in response.getheaders():
                        if key.lower() not in {'connection', 'transfer-encoding', 'content-length'}:
                            self.send_header(key, value)
                    self.send_header('Content-Length', str(len(content)))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(content)
                finally:
                    remote.close()

            do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = handle_request

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_address[1]}'

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
