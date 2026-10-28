"""Offline Mac transport controls: real TLS, signed catalogs, no USB."""
import ssl
import sys
from pathlib import Path
from unittest.mock import patch

import jibo_flash_contract as contract
import jibo_hosted_updates as hosted
from fake_flash_server import FakeFlashServer


def test_frozen_catalog_uses_bundled_roots_without_build_machine_trust(tmp_path):
    with FakeFlashServer(tls=True) as server:
        row, _, _ = server.make_image()
        ca = Path(server.tmp.name) / 'test-only-tls.pem'
        # Model a Mac without the CI Homebrew OpenSSL certificate directory.
        def empty_defaults():
            return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        with patch.object(sys, 'frozen', True, create=True), \
                patch.object(hosted, '_CA_BUNDLE', ca, create=True), \
                patch('ssl._create_default_https_context', side_effect=empty_defaults), \
                patch.object(hosted, 'ORIGIN', server.origin), \
                patch.object(hosted, 'INDEX_URL', server.origin + '/flash/index.json'), \
                patch.object(contract, 'trusted_keys', return_value={
                    'phoenix-flash-2026-10': server.public_key.read_bytes()}):
            catalog = hosted.fetch_catalog(cache_dir=tmp_path / 'cache', timeout=3)
        assert catalog.latest_id == row['id']
        assert not catalog.from_cache
        assert len(server.requests) == 1


def test_release_stages_ca_bundle_and_license_before_freezing(tmp_path, monkeypatch):
    import certifi
    from test_release_builds import load_script
    build = load_script('build')
    runtime = tmp_path / 'runtime'
    licenses = runtime / 'licenses'
    licenses.mkdir(parents=True)
    downloads = []
    monkeypatch.setattr(build, 'download', lambda *args: downloads.append(args))
    assert callable(getattr(build, 'bundle_ca_certificates', None)), 'release has no CA bundling step'
    build.bundle_ca_certificates(runtime, licenses)
    ca = runtime / 'assets/ca/cacert.pem'
    assert ca.read_bytes() == Path(certifi.where()).read_bytes()
    assert ssl.create_default_context(cafile=str(ca)).cert_store_stats()['x509_ca'] > 0
    assert (licenses / 'certifi-LICENSE').is_file()
    assert len(downloads) == 1 and len(downloads[0][2]) == 64


def test_native_selfcheck_includes_actual_transport_trust():
    from test_release_builds import load_script
    launcher = load_script('native_launcher')
    assert callable(getattr(launcher, 'hosted_transport_check', None)), 'self-check only tests signing keys'
    result = launcher.hosted_transport_check()
    assert result['certificate_verification'] and result['hostname_verification']
    assert result['ca_certificates'] > 0


import errno
import http.client
import socket
import urllib.error
import urllib.request
import pytest


@pytest.mark.parametrize('failure,code,detail', [
    (ssl.SSLCertVerificationError(1, 'certificate verify failed'), 'tls', 'certificate verification failed'),
    (ssl.SSLError(1, 'handshake failure'), 'tls', 'TLS handshake failed'),
    (socket.gaierror(socket.EAI_NONAME, 'name not known'), 'offline', 'DNS lookup failed'),
    (TimeoutError('timed out'), 'offline', 'Timed out'),
    (ConnectionResetError(errno.ECONNRESET, 'reset'), 'offline', 'Connection reset'),
    (ConnectionRefusedError(errno.ECONNREFUSED, 'refused'), 'offline', 'Connection refused'),
    (OSError(errno.ENETUNREACH, 'unreachable'), 'offline', 'Network route unavailable'),
    (PermissionError(errno.EACCES, 'sandbox denied'), 'offline', 'Network access was denied'),
    (http.client.RemoteDisconnected('closed'), 'offline', 'closed the connection'),
    (http.client.BadStatusLine('invalid'), 'offline', 'Invalid HTTP response'),
    (OSError('Tunnel connection failed: 407 Proxy Authentication Required'), 'proxy', 'HTTP 407'),
    (urllib.error.URLError('unknown url type: socks'), 'proxy', 'Unsupported proxy protocol'),
    (ValueError('proxy URL with no authority: http://user:PRIVATE@'), 'proxy', 'Invalid proxy or URL configuration'),
    (urllib.error.URLError('unknown proxy http://user:PRIVATE@proxy.invalid/?token=PRIVATE'),
        'offline', 'URLError'),
])
def test_transport_failure_reaches_visible_summary_and_operation_log(tmp_path, monkeypatch,
                                                                    failure, code, detail):
    import jibo_gui_core as core
    from gui_fakes import FakeHostedApi
    monkeypatch.setattr(hosted, '_deadline_opener', lambda _: type('FailedOpener', (), {
        'open': lambda *a, **kw: (_ for _ in ()).throw(urllib.error.URLError(failure))})())
    api = FakeHostedApi()
    api.hosted, api.packages = hosted, []
    controller = core.GuiController(api, prompter=lambda _: None, runner=core.sync_runner)
    try:
        monkeypatch.setattr(controller.flows, 'hosted_folders', lambda: (tmp_path, tmp_path / 'cache'))
        controller.request_refresh()
        with core.capture_output(controller):
            assert controller.start_action('flash-update')
        outcome = [event[1] for event in controller.drain() if event[0] == 'finished'][-1]
        summary = '\n'.join(outcome.lines)
        log = '\n'.join(controller.output_state().lines)
        assert detail in summary
        assert detail in log
        assert 'PRIVATE' not in summary + log
        assert not api.writes
    finally:
        controller.shutdown()
    with pytest.raises(hosted.HostedError) as caught:
        hosted.fetch_catalog(cache_dir=tmp_path / 'cache2', timeout=1)
    assert caught.value.code == code


@pytest.mark.parametrize('fault', ['missing', 'corrupt', 'unreadable'])
def test_frozen_ca_file_failure_names_local_cause_and_path(tmp_path, monkeypatch, fault):
    ca = tmp_path / 'cacert.pem'
    if fault != 'missing':
        ca.write_text('not a PEM certificate')
    if fault == 'unreadable':
        ca.chmod(0)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(hosted, '_CA_BUNDLE', ca, raising=False)
    # A local trust refusal must happen before DNS/connect, not turn into HTTP.
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **kw: pytest.fail('network reached'))
    try:
        with pytest.raises(hosted.HostedError) as caught:
            hosted.fetch_catalog(cache_dir=tmp_path / 'cache')
        assert caught.value.code == 'dependency'
        assert 'CA certificate file' in str(caught.value)
        assert str(ca) in str(caught.value)
    finally:
        if ca.exists():
            ca.chmod(0o600)


def test_dns_failure_comes_through_real_urllib_without_http(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise socket.gaierror(socket.EAI_AGAIN, 'temporary DNS failure')
    monkeypatch.setattr(socket, 'getaddrinfo', unavailable)
    with pytest.raises(hosted.HostedError, match='DNS lookup failed') as caught:
        hosted.fetch_catalog(cache_dir=tmp_path / 'cache', timeout=1)
    assert caught.value.code == 'offline'


def test_ipv6_route_failure_still_tries_ipv4(tmp_path, monkeypatch):
    with FakeFlashServer() as server:
        server.files['/flash/probe'] = b'IPv4 fallback works'
        resolver = socket.getaddrinfo
        calls = []
        def addresses(host, port, *args, **kwargs):
            assert host == '127.0.0.1'
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('::1', port, 0, 0)),
                    *resolver(host, port, *args, **kwargs)]
        connect = socket.socket.connect
        def connecting(sock, address):
            calls.append(sock.family)
            if sock.family == socket.AF_INET6:
                raise OSError(errno.ENETUNREACH, 'fixture IPv6 has no route')
            return connect(sock, address)
        monkeypatch.setattr(socket, 'getaddrinfo', addresses)
        monkeypatch.setattr(socket.socket, 'connect', connecting)
        monkeypatch.setattr(hosted, 'ORIGIN', server.origin)
        assert hosted._fetch(server.origin + '/flash/probe', 128, 2, None) == b'IPv4 fallback works'
        assert calls == [socket.AF_INET6, socket.AF_INET]


def test_proxy_connect_refusal_keeps_status_not_credentials(tmp_path, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    seen = []
    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass
        def do_CONNECT(self):
            seen.append(self.path)
            self.send_response(407, 'Proxy Authentication Required')
            self.end_headers()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        proxy = f'http://test:PRIVATE@127.0.0.1:{server.server_port}'
        monkeypatch.setattr(urllib.request, 'getproxies', lambda: {'https': proxy})
        monkeypatch.setattr(urllib.request, 'proxy_bypass', lambda _: False)
        with pytest.raises(hosted.HostedError, match='HTTP 407') as caught:
            hosted.fetch_catalog(cache_dir=tmp_path / 'cache', timeout=2)
        assert caught.value.code == 'proxy'
        assert 'PRIVATE' not in str(caught.value)
        assert seen == ['jibo.io:443']
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('mismatch', ['untrusted', 'hostname'])
def test_bad_tls_stays_refused_even_with_bundled_roots(tmp_path, monkeypatch, mismatch):
    import certifi
    with FakeFlashServer(tls=True, tls_hostname='wrong.invalid') as server:
        ca = Path(certifi.where()) if mismatch == 'untrusted' else Path(server.tmp.name) / 'test-only-tls.pem'
        monkeypatch.setattr(sys, 'frozen', True, raising=False)
        monkeypatch.setattr(hosted, '_CA_BUNDLE', ca)
        monkeypatch.setattr(hosted, 'ORIGIN', server.origin)
        monkeypatch.setattr(hosted, 'INDEX_URL', server.origin + '/flash/index.json')
        with pytest.raises(hosted.HostedError, match='certificate verification failed') as caught:
            hosted.fetch_catalog(cache_dir=tmp_path / 'cache', timeout=2)
        assert caught.value.code == 'tls'
        assert not server.requests
        if mismatch == 'hostname':
            assert 'mismatch' in str(caught.value)


@pytest.mark.parametrize('source', ['certifi', 'system'])
def test_source_ca_loading_errors_are_dependency_failures(tmp_path, monkeypatch, source):
    import certifi
    if source == 'certifi':
        monkeypatch.setattr(certifi, 'where', lambda: str(tmp_path / 'missing-certifi.pem'))
    else:
        monkeypatch.setattr(ssl, '_create_default_https_context',
                            lambda: (_ for _ in ()).throw(ssl.SSLError('invalid SSL_CERT_FILE')))
    with pytest.raises(hosted.HostedError) as caught:
        hosted.fetch_catalog(cache_dir=tmp_path / 'cache', timeout=1)
    assert caught.value.code == 'dependency'
    assert ('missing-certifi.pem' if source == 'certifi' else 'SSL_CERT_FILE') in str(caught.value)


def test_selfcheck_rejects_missing_frozen_ca_without_network(tmp_path, monkeypatch):
    from test_release_builds import load_script
    launcher = load_script('native_launcher')
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(hosted, '_CA_BUNDLE', tmp_path / 'missing.pem')
    monkeypatch.setattr(socket, 'create_connection', lambda *a, **kw: pytest.fail('self-check used network'))
    with pytest.raises(hosted.HostedError, match='CA certificate file is missing'):
        launcher.hosted_transport_check()


def test_https_catalog_needs_no_curl_or_subprocess(tmp_path, monkeypatch):
    import subprocess
    with FakeFlashServer(tls=True) as server:
        row, _, _ = server.make_image()
        monkeypatch.setattr(hosted, 'ORIGIN', server.origin)
        monkeypatch.setattr(hosted, 'INDEX_URL', server.origin + '/flash/index.json')
        monkeypatch.setattr(hosted, '_CA_BUNDLE', Path(server.tmp.name) / 'test-only-tls.pem')
        monkeypatch.setattr(contract, 'trusted_keys', lambda: {'phoenix-flash-2026-10': server.public_key.read_bytes()})
        monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('hosted transport spawned a process'))
        monkeypatch.setenv('PATH', '')
        assert hosted.fetch_catalog(cache_dir=tmp_path / 'cache').latest_id == row['id']


def test_mac_ci_selects_homebrew_openssl_for_fixture_keys():
    root = Path(__file__).resolve().parents[1]
    for name, marker in [('macos.yml', 'Test hosted TLS roots and network diagnostics offline'),
                         ('release.yml', 'Test native Mac hosted transport and Copy details offline')]:
        text = (root / '.github/workflows' / name).read_text()
        step = text.split('- name: ' + marker, 1)[1].split('- name:', 1)[0]
        assert 'brew --prefix openssl@3' in step, 'Mac fixture can select incompatible system openssl'
        assert 'export PATH=' in step
        assert 'tests/test_hosted_macos_transport.py' in step


def test_tls_failure_does_not_fall_back_to_saved_catalog(tmp_path, monkeypatch):
    with FakeFlashServer() as server:
        row, _, _ = server.make_image()
        monkeypatch.setattr(hosted, 'ORIGIN', server.origin)
        monkeypatch.setattr(hosted, 'INDEX_URL', server.origin + '/flash/index.json')
        monkeypatch.setattr(contract, 'trusted_keys', lambda: {'phoenix-flash-2026-10': server.public_key.read_bytes()})
        cache = tmp_path / 'cache'
        assert hosted.fetch_catalog(cache_dir=cache).latest_id == row['id']
        def bad(*args, **kwargs):
            raise urllib.error.URLError(ssl.SSLCertVerificationError(1, 'untrusted'))
        monkeypatch.setattr(hosted, '_deadline_opener', lambda _: type('Opener', (), {'open': bad})())
        with pytest.raises(hosted.HostedError) as caught:
            hosted.fetch_catalog(cache_dir=cache)
        assert caught.value.code == 'tls'
