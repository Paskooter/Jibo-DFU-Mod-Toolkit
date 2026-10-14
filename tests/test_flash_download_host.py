"""Pinned public URL checks over a loopback-only, throwaway-key TLS server."""
import copy
from contextlib import contextmanager
import hashlib
from pathlib import Path
import socket
from urllib.parse import urlsplit
from unittest.mock import patch

import pytest
from fake_flash_server import FakeFlashServer
import jibo_flash_contract as c
import jibo_hosted_updates as h

ORIGINS = ('https://download.jibo.io', 'https://jibo.io')


@contextmanager
def pinned_server(image_origin):
    with FakeFlashServer(tls=True, tls_hostname='jibo.io') as server:
        row, index, manifest = server.make_image()
        path = urlsplit(row['url']).path
        manifest_path = urlsplit(row['manifest_url']).path
        row['url'] = manifest['url'] = image_origin + path
        row['manifest_url'] = 'https://jibo.io' + manifest_path
        raw = server.envelope(manifest)
        row['manifest_size'] = len(raw)
        row['manifest_sha256'] = hashlib.sha256(raw).hexdigest()
        server.files[manifest_path] = raw
        server.files['/flash/index.json'] = server.envelope(index)
        real_connect = socket.create_connection

        def loopback_only(address, *args, **kwargs):
            assert address[0] in ('jibo.io', 'download.jibo.io'), address
            return real_connect(('127.0.0.1', server.server.server_port), *args, **kwargs)

        # Only fixture TLS hostname validation is relaxed; production remains intact.
        server.client_context.check_hostname = False
        with patch('socket.create_connection', side_effect=loopback_only), \
                patch('urllib.request.getproxies', return_value={}), \
                patch('ssl._create_default_https_context', return_value=server.client_context), \
                patch.object(c, 'trusted_keys', return_value={c.CONTRACT['key_id']: server.public_key.read_bytes()}), \
                patch.object(c, 'TOOLKIT_VERSION', '1.0.0'):
            yield server, row, index


@pytest.mark.parametrize('origin', ORIGINS)
def test_pinned_image_origin_downloads_and_revalidates(tmp_path, origin):
    with pinned_server(origin) as (server, row, index):
        catalog = h.fetch_catalog(cache_dir=tmp_path / 'cache')
        assert catalog.latest_id == row['id']
        completed = Path(h.download_image(row['id'], dest_dir=tmp_path / 'images'))
        assert hashlib.sha256(completed.read_bytes()).hexdigest() == row['sha256']
        assert h.verify_local_image(completed)['archive_sha256'] == row['sha256']
        hosts = {path: headers['Host'] for path, headers in server.requests}
        assert hosts['/flash/index.json'] == 'jibo.io'
        assert hosts[urlsplit(row['manifest_url']).path] == 'jibo.io'
        assert hosts[urlsplit(row['url']).path] == urlsplit(origin).netloc


BAD_ORIGINS = (
    'http://download.jibo.io', 'https://api.jibo.io', 'https://other.jibo.io',
    'https://download.jibo.io:443', 'https://download.jibo.io:8443',
    'https://user@download.jibo.io', 'https://user:pass@download.jibo.io',
    'https://download.jibo.io.', 'https://DOWNLOAD.jibo.io', 'HTTPS://download.jibo.io',
    'https://download.jibo.io.evil', 'https://downloаd.jibo.io',
    'https://xn--downlod-8fg.jibo.io', 'https://download%2ejibo.io',
    ' https://download.jibo.io', '\nhttps://download.jibo.io',
)


@pytest.mark.parametrize('origin', BAD_ORIGINS)
def test_third_or_noncanonical_image_origin_refused(tmp_path, origin):
    with pinned_server('https://jibo.io') as (server, row, index):
        row['url'] = origin + urlsplit(row['url']).path
        with pytest.raises((h.HostedError, c.ContractError)):
            h._index(server.envelope(index), None)
        assert server.requests == []
        with pytest.raises(h.HostedError):
            h._url(row['url'])


@pytest.mark.parametrize('suffix', ('?x=1', '#part', '?', '#', '/extra', '%2f', '/../x', '//x'))
def test_noncanonical_image_path_refused(suffix):
    url = ORIGINS[0] + '/flash/images/test/' + 'a' * 64 + '.tar.bz2' + suffix
    with pytest.raises(ValueError):
        __import__('jibo_flash_schema').check_schema(
            url, {'type': 'string', 'format': 'image-url'})


@pytest.mark.parametrize('field', ('manifest_url', 'url'))
def test_metadata_pins_and_exact_image_identity_preserved(field):
    with pinned_server(ORIGINS[0]) as (server, row, index):
        if field == 'manifest_url':
            row[field] = row[field].replace('https://jibo.io', ORIGINS[0])
        else:
            row[field] = row[field].replace(row['sha256'], '0' * 64)
        with pytest.raises((h.HostedError, c.ContractError)):
            h._index(server.envelope(index), None)
        assert server.requests == []
    with pytest.raises(h.HostedError):
        h._url(ORIGINS[0] + '/flash/index.json')


@pytest.mark.parametrize('origin', ORIGINS)
def test_cross_origin_redirect_refused_before_target_is_opened(origin):
    with pinned_server(origin) as (server, row, index):
        path = urlsplit(row['url']).path
        other = next(o for o in ORIGINS if o != origin)
        server.redirects[path] = other + path
        with pytest.raises(h.HostedError):
            h._open(row['url'], 3)
        assert [p for p, _ in server.requests] == [path]


@pytest.mark.parametrize('origin', ORIGINS)
def test_image_redirect_stays_in_same_origins_image_path(origin):
    with pinned_server(origin) as (server, row, index):
        path = urlsplit(row['url']).path
        target = '/flash/images/alternate/' + row['sha256'] + '.tar.bz2'
        server.files[target] = server.files[path]
        server.redirects[path] = origin + target
        with h._open(row['url'], 3) as response:
            assert response.read() == server.files[path]
        assert [p for p, _ in server.requests] == [path, target]
        server.requests.clear()
        server.redirects[path] = origin + '/flash/index.json'
        with pytest.raises(h.HostedError):
            h._open(row['url'], 3)
        assert [p for p, _ in server.requests] == [path]


def test_contract_has_exactly_two_image_origins():
    assert set(c.CONTRACT['image_origins']) == set(ORIGINS)
    assert c.CONTRACT['origin'] == 'https://jibo.io'
    assert h.INDEX_URL == 'https://jibo.io/flash/index.json'
