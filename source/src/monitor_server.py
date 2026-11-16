"""Read-only, authenticated, bounded LAN preview HTTP service; no disk routes."""
import hmac
import ipaddress
import json
import secrets
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

import monitor_web

NETWORKS = tuple(ipaddress.ip_network(n) for n in
                 ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '169.254.0.0/16', '127.0.0.0/8'))


def local_address(address):
    try:
        ip = ipaddress.ip_address(address)
        return any(ip in net for net in NETWORKS)
    except ValueError:
        return False


class _HTTP(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False
    request_queue_size = 8

    def server_bind(self):
        # Avoid HTTPServer's reverse DNS lookup on the UI thread.
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def verify_request(self, request, client_address):
        return local_address(client_address[0])

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        pass  # Disconnections do not produce per-frame console/disk logs.


class _Handler(BaseHTTPRequestHandler):
    server_version = 'FLIR-Monitor'
    sys_version = ''

    def setup(self):
        self.request.settimeout(2.0)
        super().setup()

    def log_message(self, *args):
        pass  # Never log credentials or URLs.

    def respond(self, status, body=b'', content_type='text/plain; charset=utf-8'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; img-src data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        if body:
            self.wfile.write(body)

    def do_GET(self):
        service = self.server.service
        if service.closed:
            return self.respond(503)
        authority = '{}:{}'.format(*self.server.server_address[:2])
        if self.headers.get('Host') != authority:
            return self.respond(403)
        origin = self.headers.get('Origin')
        if origin and origin != 'http://' + authority:
            return self.respond(403)
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            return self.respond(403)
        path = urlsplit(self.path)
        if path.scheme or path.netloc:
            return self.respond(400)
        if path.path in monitor_web.ASSETS:
            mime, content = monitor_web.ASSETS[path.path]
            return self.respond(200, content, mime)
        if path.path != '/api/state':
            return self.respond(404)
        credential = self.headers.get('Authorization', '')
        if len(credential) > 128 or not hmac.compare_digest(credential.encode('utf-8'), ('Bearer ' + service.token).encode('ascii')):
            return self.respond(401)
        try:
            query = parse_qs(path.query, max_num_fields=4)
            since = int(query.get('since', ['-1'])[0])
        except (ValueError, TypeError):
            return self.respond(400)
        client = query.get('client', [''])[0]
        if not 8 <= len(client) <= 64 or not client.isalnum():
            return self.respond(400)
        payload = service.snapshot(since, client)
        self.respond(200, json.dumps(payload, ensure_ascii=False, separators=(',', ':'),
                                    allow_nan=False).encode('utf-8'), 'application/json; charset=utf-8')

    def do_POST(self):
        self.respond(405)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_POST


class Service:
    def __init__(self, host, port=8765):
        if not local_address(host):
            raise ValueError('请选择本机局域网IPv4地址。')
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.closed = False
        self.stopped = threading.Event()
        self.state, self.preview, self.clients = {}, None, {}
        self.updated = time.monotonic()
        self.version = 0
        self.http = _HTTP((host, port), _Handler)
        self.http.service = self
        self.http.slots = threading.BoundedSemaphore(4)
        self.url = 'http://{}:{}/'.format(*self.http.server_address[:2])
        self.link = self.url + '#key=' + self.token
        self.thread = threading.Thread(target=self.http.serve_forever, kwargs={'poll_interval': .1},
                                       name='FLIR-LAN-monitor', daemon=True)
        try:
            self.thread.start()
        except BaseException:
            self.http.server_close()
            raise

    def publish(self, state):
        with self.lock:
            self.state, self.updated = state, time.monotonic()

    def set_preview(self, image):
        with self.lock:
            if self.closed:
                return
            self.version += 1
            self.preview = {**image, 'version': self.version} if image else None

    def active_clients(self):
        now = time.monotonic()
        with self.lock:
            self.clients = {key: at for key, at in self.clients.items() if now - at < 8}
            return len(self.clients)

    def snapshot(self, since, client):
        now = time.monotonic()
        with self.lock:
            self.clients = {key: at for key, at in self.clients.items() if now - at < 8}
            if client not in self.clients and len(self.clients) >= 16:
                self.clients.pop(min(self.clients, key=self.clients.get))
            self.clients[client] = now
            preview = None
            if self.preview is not None:
                preview = {key: value for key, value in self.preview.items() if key != 'monotonic_s'}
                # Original acquisition timestamps use perf_counter. Python3.10
                # on Windows does not promise the same epoch as monotonic().
                preview['age_s'] = max(0, time.perf_counter() - self.preview['monotonic_s'])
                if since == self.preview['version']:
                    preview.pop('data_url', None)
            return {**self.state, 'snapshot_age_s': max(0, now - self.updated),
                    'frame': preview, 'preview_version': self.version}

    def stop(self):
        if self.closed:
            return
        self.closed = True
        with self.lock:
            self.state, self.preview, self.clients = {}, None, {}
        def finish():
            try:
                self.http.shutdown()
                self.http.server_close()
            finally:
                self.stopped.set()
        threading.Thread(target=finish, name='FLIR-LAN-stop', daemon=True).start()
