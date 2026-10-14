"""Loopback fixture latency must come only from explicit test delays."""
from threading import Event
import urllib.request
import pytest
from fake_flash_server import FakeFlashServer


def test_flash_fixture_disables_server_packet_coalescing():
    with FakeFlashServer() as server:
        server.files['/probe'] = b'ok'
        with urllib.request.urlopen(server.origin + '/probe') as response:
            assert response.read() == b'ok'
        assert server.tcp_nodelay == [1]


@pytest.mark.parametrize('tls', [False, True])
def test_flash_fixture_body_gate_holds_until_release(tls):
    with FakeFlashServer(tls=tls) as server:
        release, sent = Event(), Event()
        server.files['/probe'] = b'abc'
        server.body_gates['/probe'] = release
        server.body_sent['/probe'] = sent
        with urllib.request.urlopen(server.origin + '/probe', timeout=2,
                                    context=server.client_context) as response:
            assert not sent.is_set()
            release.set()
            assert sent.wait(2)
            assert response.read() == b'abc'


@pytest.mark.parametrize('tls', [False, True])
def test_flash_fixture_shutdown_releases_owned_gated_handler(tls):
    release, sent = Event(), Event()
    with FakeFlashServer(tls=tls) as server:
        server.files['/probe'] = b'abc'
        server.body_gates['/probe'] = release
        server.body_sent['/probe'] = sent
        response = urllib.request.urlopen(server.origin + '/probe', timeout=2,
                                          context=server.client_context)
        response.close()
        assert not release.is_set()
        assert not sent.is_set()
    assert release.is_set()
    assert not sent.is_set()
    assert not server.thread.is_alive()
