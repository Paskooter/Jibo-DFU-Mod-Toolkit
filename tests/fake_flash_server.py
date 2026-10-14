"""Loopback-only HTTP fixture; never permits insecure production transport.

Synthetic key material is generated inside a private temporary test directory.
The public key can be injected by tests; the private key never leaves this fixture.
"""
import base64
from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import ssl
from pathlib import Path
import subprocess
import tempfile
from threading import Event, Thread


class FakeFlashServer:
    def __init__(self, *, tls=False, tls_hostname=None):
        self.files = {}
        self.requests = []
        self.tcp_nodelay = []
        self.redirects = {}
        self.statuses = {}
        self.responses = {}  # Per-path scripted (status, Retry-After) errors.
        self.truncate = {}
        self.delay = 0
        self.delays = {}
        self.header_delays = {}
        self.header_drips = {}
        self.chunk_sizes = {}
        self.chunked_bodies = {}  # Raw framing for bounded-read probes.
        self.stopping = Event()
        self.ignore_range = False
        self.bad_range = False
        self.tmp = tempfile.TemporaryDirectory(prefix='test-only-flash-key-')
        self.private_key = Path(self.tmp.name) / 'test-only-private.pem'
        self.public_key = Path(self.tmp.name) / 'test-only-public.pem'
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'ED25519', '-out',
                        str(self.private_key)], check=True, capture_output=True)
        self.private_key.chmod(0o600)
        subprocess.run(['openssl', 'pkey', '-in', str(self.private_key), '-pubout',
                        '-out', str(self.public_key)], check=True, capture_output=True)
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                # Deadline probes control delay explicitly, not through the
                # host's Nagle/delayed-ACK packet-coalescing policy.
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                fixture.tcp_nodelay.append(self.connection.getsockopt(
                    socket.IPPROTO_TCP, socket.TCP_NODELAY))

            def log_message(self, *args):
                pass

            def end_headers(self):
                delay = fixture.header_drips.get(self.path, 0)
                if not delay:
                    return super().end_headers()
                buffer = getattr(self, '_headers_buffer')
                buffer.append(b'\r\n')
                raw = b''.join(buffer)
                buffer.clear()
                try:
                    for byte in raw:
                        if fixture.stopping.wait(delay):
                            return
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_HEAD(self):
                self.do_GET(head=True)

            def do_GET(self, head=False):
                fixture.requests.append((self.path, dict(self.headers)))
                if fixture.stopping.wait(fixture.header_delays.get(self.path, 0)):
                    return
                if self.path in fixture.redirects:
                    self.send_response(302)
                    self.send_header('Location', fixture.redirects[self.path])
                    self.end_headers()
                    return
                scripted = fixture.responses.get(self.path)
                if scripted or self.path in fixture.statuses:
                    status, retry_after = scripted.pop(0) if scripted else (fixture.statuses[self.path], '1')
                    self.send_response(status)
                    if retry_after is not None:
                        self.send_header('Retry-After', retry_after)
                    self.end_headers()
                    return
                data = fixture.files.get(self.path)
                if data is None:
                    self.send_error(404)
                    return
                total = len(data)
                offset = 0
                ranged = self.headers.get('Range')
                if ranged and not fixture.ignore_range:
                    offset = int(ranged.removeprefix('bytes=').removesuffix('-'))
                    if offset >= total:
                        self.send_response(416)
                        self.send_header('Content-Range', 'bytes */' + str(total))
                        self.end_headers()
                        return
                    self.send_response(206)
                    start = offset + int(fixture.bad_range)
                    self.send_header('Content-Range', f'bytes {start}-{total-1}/{total}')
                else:
                    self.send_response(200)
                data = data[offset:]
                if self.path in fixture.chunked_bodies:
                    self.send_header('Transfer-Encoding', 'chunked')
                    data = fixture.chunked_bodies[self.path]
                else:
                    self.send_header('Content-Length', str(len(data)))
                self.send_header('ETag', '"synthetic-v1"')
                self.end_headers()
                if not head:
                    data = data[:fixture.truncate.get(self.path, len(data))]
                    try:
                        chunk_size = fixture.chunk_sizes.get(self.path, 4096)
                        for start in range(0, len(data), chunk_size):
                            if fixture.stopping.wait(fixture.delays.get(self.path, fixture.delay)):
                                return
                            self.wfile.write(data[start:start+chunk_size])
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass

        class OwnedServer(ThreadingHTTPServer):
            daemon_threads = False  # server_close joins every owned request.

        self.server = OwnedServer(('127.0.0.1', 0), Handler)
        self.client_context = None
        if tls:
            certificate = Path(self.tmp.name) / 'test-only-tls.pem'
            subprocess.run(['openssl', 'req', '-new', '-x509', '-key', str(self.private_key),
                            '-subj', '/CN=' + (tls_hostname or '127.0.0.1'), '-addext',
                            'subjectAltName=' + ('DNS:' + tls_hostname if tls_hostname else 'IP:127.0.0.1'),
                            '-days', '1', '-out', str(certificate)], check=True, capture_output=True)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate, self.private_key)
            self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
            self.client_context = ssl.create_default_context(cafile=str(certificate))
        self.origin = ('https' if tls else 'http') + '://127.0.0.1:' + str(self.server.server_port)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)

    def envelope(self, payload, *, bad_signature=False):
        raw = (json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
               if isinstance(payload, dict) else payload)
        message = Path(self.tmp.name) / 'payload'
        signature = Path(self.tmp.name) / 'signature'
        message.write_bytes(raw)
        subprocess.run(['openssl', 'pkeyutl', '-sign', '-rawin', '-inkey',
                        str(self.private_key), '-in', str(message), '-out',
                        str(signature)], check=True, capture_output=True)
        sig = signature.read_bytes()
        if bad_signature:
            sig = bytes([sig[0] ^ 1]) + sig[1:]
        return json.dumps({'schema': 1, 'key_id': 'phoenix-flash-2026-10',
                           'payload': base64.b64encode(raw).decode(),
                           'signature': base64.b64encode(sig).decode()}).encode()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stopping.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()

    def make_image(self, image_id='synthetic-13', *, sequence=1, expires=None):
        """Canonical A2 wire fixture; dummy bytes, never hardware firmware."""
        import io
        import os
        import tarfile
        import time
        now = int(time.time())
        identity = {'id': image_id, 'base_release': '13.0.0-test-only',
                    'components': {k: '0.0.0-test-only' for k in ('os', 'services', 'oobe-config', '@be/be')},
                    'profile': 'jibo-t124-stock-gpt-13-v1', 'strategy': 'compact-prefix-v1'}
        content = {role: os.urandom(32768) for role in ('rootfs', 'services', 'skills', 'var')}
        members = [{'name': f'phoenix-{image_id}/flash_jibo/output/images/{role}.ext4',
                    'role': role, 'size': len(raw), 'sha256': self.sha(raw),
                    'geometry': {'block_size': 4096, 'block_count': len(raw)//4096, 'features': []}}
                   for role, raw in content.items()]
        metadata = {**identity, 'schema': 1, 'kind': 'phoenix-flash-metadata-v1', 'members': members,
                    'var_policies': ['keep-calibration', 'without-calibration'],
                    'official_base': {'release': identity['base_release'], 'sha256': 'a'*64, 'provenance': 'SYNTHETIC'},
                    'sources': {k: 'b'*40 for k in ('repoint_commit', 'helper_commit', 'be_commit')},
                    'recipe': {'revision': 'synthetic', 'toolchain_sha256': 'c'*64},
                    'gate_receipts': {'synthetic': 'd'*64}}
        metadata_raw = self.envelope(metadata)
        archive_buffer = io.BytesIO()
        with tarfile.open(fileobj=archive_buffer, mode='w:bz2') as archive:
            for member in members:
                entry = tarfile.TarInfo(member['name'])
                entry.size = member['size']
                archive.addfile(entry, io.BytesIO(content[member['role']]))
            entry = tarfile.TarInfo(f'phoenix-{image_id}/phoenix-flash.json')
            entry.size = len(metadata_raw)
            archive.addfile(entry, io.BytesIO(metadata_raw))
        archive_raw = archive_buffer.getvalue()
        archive_path = f'/flash/images/{image_id}/{self.sha(archive_raw)}.tar.bz2'
        manifest_path = f'/flash/manifests/{image_id}.json'
        common = {**identity, 'version': '1.0.0-test-only', 'size': len(archive_raw), 'sha256': self.sha(archive_raw),
                  'url': self.origin + archive_path, 'minimum_toolkit_version': '1.0.0',
                  'required_capabilities': ['hosted-flash-v1'], 'released_at': now,
                  'notes': 'SYNTHETIC TEST ONLY', 'acceptance_scope': 'synthetic offline; not hardware acceptance'}
        artifact = {**metadata, **common, 'kind': 'phoenix-full-flash-v1',
                    'embedded_metadata_sha256': self.sha(metadata_raw),
                    'unpacked_size': sum(m['size'] for m in members), 'workspace_bytes': 4000000000}
        manifest_raw = self.envelope(artifact)
        row = {**common, 'manifest_url': self.origin + manifest_path, 'manifest_size': len(manifest_raw),
               'manifest_sha256': self.sha(manifest_raw), 'offered': True}
        index = {'schema': 1, 'kind': 'phoenix-flash-index-v1', 'sequence': sequence,
                 'generated_at': now, 'expires_at': now + 3600 if expires is None else expires,
                 'latest_image_id': image_id, 'images': [row]}
        self.files[archive_path] = archive_raw
        self.files[manifest_path] = manifest_raw
        self.files['/flash/index.json'] = self.envelope(index)
        return row, index, artifact

    @staticmethod
    def sha(data):
        return hashlib.sha256(data).hexdigest()
