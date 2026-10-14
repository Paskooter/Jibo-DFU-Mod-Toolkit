"""Loopback fixture latency must come only from explicit test delays."""
import urllib.request
from fake_flash_server import FakeFlashServer


def test_flash_fixture_disables_server_packet_coalescing():
    with FakeFlashServer() as server:
        server.files['/probe'] = b'ok'
        with urllib.request.urlopen(server.origin + '/probe') as response:
            assert response.read() == b'ok'
        assert server.tcp_nodelay == [1]
