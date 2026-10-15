"""ASK-70 regressions: real hosted code, synthetic transport, no device access."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import jibo_hosted_updates as h
import jibo_tui as tui
from test_hosted_catalog import HostedFixture
import os
import sys


class DataFolderTests(HostedFixture):
    def test_readonly_start_lists_signed_images_and_preserves_local_packages(self):
        start = self.cache.parent / 'start'
        start.mkdir()
        legacy = start / 'updates'
        legacy.mkdir()
        official = legacy / 'official.tar.bz2'
        official.write_bytes(b'local fixture only')
        owned = self.cache.parent / 'data'
        prior = Path.cwd()
        os.chdir(start)
        start.chmod(0o555)
        legacy.chmod(0o555)
        try:
            with patch.dict(os.environ, {'XDG_DATA_HOME': str(owned)}), \
                    patch.object(tui, '_terminal_available', return_value=False):
                updates, cache = tui.hosted_folders()
                self.assertEqual(updates, owned / 'jibo-dfu' / 'updates')
                catalog = h.fetch_catalog(timeout=2)
                self.assertEqual(catalog.latest_id, self.row['id'])
                seen = []
                def candidates(folder):
                    seen.append(Path(folder))
                    return [official] if Path(folder) == legacy else []
                with patch.object(tui, 'select_option', return_value='local:0'):
                    choice, _ = tui._choose_image(SimpleNamespace(_update_candidates=candidates), h, 'Choose', True)
                self.assertEqual(choice, ('local', official))
                self.assertIn(legacy, seen)
                path = Path(h.download_image(self.row['id']))
                self.assertEqual(path.parent, updates)
                self.assertEqual(h.local_hosted_images()[0].downloaded_path, str(path))
                self.assertEqual(list(legacy.iterdir()), [official])
        finally:
            legacy.chmod(0o755)
            start.chmod(0o755)
            os.chdir(prior)

    def test_completed_legacy_downloads_remain_discoverable(self):
        start = self.cache.parent / 'legacy-start'
        start.mkdir()
        old = start / 'updates'
        path = h.download_image(self.row['id'], dest_dir=old)
        prior = Path.cwd()
        os.chdir(start)
        try:
            with patch.dict(os.environ, {'XDG_DATA_HOME': str(self.cache.parent / 'new-data')}):
                self.assertIn(path, [image.downloaded_path for image in h.local_hosted_images()])
        finally:
            os.chdir(prior)

    def test_macos_uses_application_support(self):
        with patch.object(sys, 'platform', 'darwin'), patch.object(Path, 'home', return_value=Path('/example/home')):
            updates, _ = tui.hosted_folders()
            self.assertEqual(updates, Path('/example/home/Library/Application Support/jibo-dfu/updates'))



class HttpDetailTests(HostedFixture):
    def test_http_status_and_cloudflare_diagnostics_reach_chooser(self):
        for status, headers, body, cloudflare in (
                (403, {}, b'', False), (503, {}, b'', False),
                (403, {'cf-mitigated': 'challenge'}, b'', True),
                (503, {}, b'<html><title>Attention Required! | Cloudflare</title></html>', True),
                (403, {'Server': 'cloudflare'}, b'ordinary origin error', False)):
            with self.subTest(status=status, headers=headers, cloudflare=cloudflare):
                path = '/flash/index.json'
                self.server.statuses[path] = status
                self.server.extra_headers[path] = headers
                self.server.error_bodies[path] = body
                with patch.object(tui, 'hosted_folders', return_value=(self.cache.parent, self.cache)), \
                        patch.object(tui, '_terminal_available', return_value=False), \
                        patch.object(h, '_retry_wait'):
                    catalog, local, code = tui._load_chooser(h)
                    items, _ = tui.hosted_chooser_items(catalog, local, [Path('official.tar.bz2')], error_code=code)
                    _, message = tui._choose_image(SimpleNamespace(_update_candidates=lambda _: []), h, 'Choose', True)
                for text in (items[0].label, message):
                    self.assertIn(f'HTTP {status}', text)
                    self.assertEqual('Cloudflare blocked this request' in text, cloudflare)

    def test_cached_list_preserves_http_diagnostic(self):
        h.fetch_catalog(cache_dir=self.cache)
        self.server.statuses['/flash/index.json'] = 403
        catalog = h.fetch_catalog(cache_dir=self.cache)
        items, _ = tui.hosted_chooser_items(catalog)
        self.assertIn('HTTP 403', items[1].label)


class LocalErrorTests(unittest.TestCase):
    def test_unwritable_cache_never_blames_jibo_io_and_names_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            start = Path(directory)
            cache = start / 'updates' / '.hosted'
            start.chmod(0o555)
            try:
                with patch.object(tui, 'hosted_folders', return_value=(cache.parent, cache)), \
                        patch.object(tui, '_terminal_available', return_value=False), \
                        patch.object(h, '_fetch', side_effect=AssertionError('must fail before network')):
                    choice, message = tui._choose_image(SimpleNamespace(_update_candidates=lambda _: []),
                                                        h, 'Choose image', True)
                self.assertIsNone(choice)
                self.assertNotIn('jibo.io answered with an error', message)
                self.assertIn(str(cache), message)
                self.assertNotIn('not enough free space', message)
                for call in (lambda: h.fetch_catalog(cache_dir=cache),
                             lambda: h.cached_catalog(cache_dir=cache),
                             lambda: h.download_image('latest', dest_dir=cache)):
                    with self.assertRaises(h.HostedError) as caught:
                        call()
                    self.assertEqual(caught.exception.code, 'disk')
                    self.assertIn(str(cache), str(caught.exception))
            finally:
                start.chmod(0o755)

    def test_unknown_exception_is_local_not_an_http_response(self):
        error = RuntimeError('synthetic unexpected failure')
        code = tui._error_code(error)
        self.assertEqual(code, 'local')
        self.assertIn('on this computer', tui.hosted_error_text(code))
