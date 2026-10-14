"""Offline transport phase timings for short deadline regression probes."""
import http.client
import json
import platform
import ssl
import sys
import time
from contextlib import ExitStack
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fake_flash_server import FakeFlashServer
import jibo_hosted_updates as h


def probe(tls, kind):
    events = []
    start = time.monotonic()

    def note(name, **fields):
        events.append({'at': round(time.monotonic() - start, 6),
                       'phase': name, **fields})

    def measured(name, real):
        def call(*args, **kwargs):
            before = time.monotonic()
            note(name + '.start')
            try:
                value = real(*args, **kwargs)
                note(name + '.end', elapsed=round(time.monotonic() - before, 6))
                return value
            except Exception as exc:
                note(name + '.error', elapsed=round(time.monotonic() - before, 6),
                     error=type(exc).__name__)
                raise
        return call

    with FakeFlashServer(tls=tls) as server, ExitStack() as stack:
        stack.enter_context(patch.object(h, 'ORIGIN', server.origin))
        if tls:
            stack.enter_context(patch('ssl._create_default_https_context',
                                      return_value=server.client_context))
        handler = server.server.RequestHandlerClass
        real_setup = handler.setup

        def setup(instance):
            note('server.setup')
            real_setup(instance)
            write = instance.wfile.write
            def timed_write(data):
                note('server.write', size=len(data))
                return write(data)
            instance.wfile.write = timed_write

        real_get = handler.do_GET
        def get(instance, *args, **kwargs):
            note('server.request', path=instance.path)
            return real_get(instance, *args, **kwargs)

        stack.enter_context(patch.object(handler, 'setup', setup))
        stack.enter_context(patch.object(handler, 'do_GET', get))
        for obj, attr, name in ((h, '_deadline_opener', 'opener'),
                                (http.client.HTTPConnection, 'connect', 'connect'),
                                (http.client.HTTPSConnection, 'connect', 'tls_connect'),
                                (http.client.HTTPResponse, 'begin', 'headers'),
                                (h._Redirect, 'redirect_request', 'redirect'),
                                (h._DeadlineSocket, 'recv_into', 'deadline_recv'),
                                (ssl.SSLSocket, 'recv_into', 'tls_recv')):
            stack.enter_context(patch.object(obj, attr, measured(name, getattr(obj, attr))))
        start = time.monotonic()
        error = None
        try:
            if kind == 'redirect':
                first, second, third = ('/flash/first.json', '/flash/second.json', '/flash/third.json')
                server.redirects[first] = server.origin + second
                server.redirects[second] = server.origin + third
                server.header_delays[first] = 0.16
                server.header_delays[second] = 0.16
                server.files[third] = b'abc'
                h._fetch(server.origin + first, 10, 0.25, None)
            else:
                path = '/flash/deadline.json'
                server.files[path] = b'abc'
                server.chunk_sizes[path] = 1
                server.delays[path] = 0.16
                with h._open(server.origin + path, 0.25) as response:
                    sock = response.fp.raw._sock
                    note('stream.open', wrapper=type(sock).__name__, timeout=sock.gettimeout())
                    raw = response.read(3)
                    note('stream.read', result=raw.decode())
        except Exception as exc:
            error = type(exc).__name__ + ': ' + str(exc)
        note('client.finished', error=error, requests=[p for p, _ in server.requests])
    print(json.dumps({'tls': tls, 'kind': kind, 'events': events}), flush=True)


if __name__ == '__main__':
    print(json.dumps({'python': sys.version, 'platform': platform.platform(),
                      'openssl': ssl.OPENSSL_VERSION}), flush=True)
    for _ in range(5):
        for tls in (False, True):
            for kind in ('redirect', 'stream'):
                probe(tls, kind)
