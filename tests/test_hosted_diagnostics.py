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


def _per_user_updates(root):
    """Per-user toolkit updates folder for ``root``, as this platform resolves it.

    Linux/WSL follows an absolute XDG_DATA_HOME; macOS deliberately ignores XDG and
    resolves ~/Library/Application Support through Path.home() (covered separately by
    test_macos_uses_application_support). Tests sandbox BOTH mechanisms at ``root`` so
    the redirect really lands on every platform instead of silently testing the
    runner's real user folder.
    """
    if sys.platform == 'darwin':
        return root / 'Library' / 'Application Support' / 'jibo-dfu' / 'updates'
    return root / 'jibo-dfu' / 'updates'


class DataFolderTests(HostedFixture):
    def test_output_write_error_is_disk_not_transport(self):
        real_fdopen = os.fdopen
        class Unwritable:
            def __init__(self, stream):
                self.stream = stream
            def __getattr__(self, name):
                return getattr(self.stream, name)
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def write(self, data):
                raise PermissionError('synthetic destination became unwritable')
        real_open = os.open
        partial_fds = set()
        def open_file(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            if str(path).endswith('.part'):
                partial_fds.add(fd)
            return fd
        def fdopen(fd, mode, *args, **kwargs):
            stream = real_fdopen(fd, mode, *args, **kwargs)
            if fd in partial_fds:
                partial_fds.remove(fd)
                return Unwritable(stream)
            return stream
        with patch.object(h.os, 'fdopen', side_effect=fdopen), patch.object(h.os, 'open', side_effect=open_file):
            with self.assertRaises(h.HostedError) as caught:
                h.download_image(self.row['id'], dest_dir=self.cache.parent / 'download')
        self.assertEqual(caught.exception.code, 'disk')
        self.assertIn(str(self.cache.parent / 'download'), str(caught.exception))

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
                    patch.object(Path, 'home', return_value=owned), \
                    patch.object(tui, '_terminal_available', return_value=False):
                updates, cache = tui.hosted_folders()
                self.assertEqual(updates, _per_user_updates(owned))
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
            with patch.dict(os.environ, {'XDG_DATA_HOME': str(self.cache.parent / 'new-data')}), \
                    patch.object(Path, 'home', return_value=self.cache.parent / 'new-home'):
                self.assertIn(path, [image.downloaded_path for image in h.local_hosted_images()])
        finally:
            os.chdir(prior)

    def test_macos_uses_application_support(self):
        with patch.object(sys, 'platform', 'darwin'), patch.object(Path, 'home', return_value=Path('/example/home')):
            updates, _ = tui.hosted_folders()
            self.assertEqual(updates, Path('/example/home/Library/Application Support/jibo-dfu/updates'))



class HttpDetailTests(HostedFixture):
    def setUp(self):
        super().setUp()
        from diagnostic_flash_server import install_error_response_pages
        install_error_response_pages(self.server)

    def test_http_status_and_cloudflare_diagnostics_reach_chooser(self):
        for status, headers, body, cloudflare in (
                (403, {}, b'', False), (503, {}, b'', False),
                (403, {'cf-mitigated': 'challenge'}, b'', True),
                (200, {'cf-mitigated': 'challenge'}, b'', True),
                (200, {}, b'<html><title>Attention Required! | Cloudflare</title></html>', True),
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


class GuiErrorTests(HostedFixture):
    def test_gui_worker_retains_http_status_and_local_folder_details(self):
        import jibo_gui_core as core
        from gui_fakes import FakeHostedApi
        api = FakeHostedApi()
        api.hosted = h
        api.packages = []
        for code, detail in [('http', 'HTTP 403. Cloudflare blocked this request.'),
                             ('disk', 'Folder: /example/readonly/updates. Permission denied')]:
            with self.subTest(code=code):
                controller = core.GuiController(api, prompter=lambda _: None, runner=core.sync_runner)
                try:
                    controller.request_refresh()
                    with patch.object(h, 'fetch_catalog', side_effect=h.HostedError(code, detail)):
                        self.assertTrue(controller.start_action('flash-update'))
                    outcomes = [event[1] for event in controller.drain() if event[0] == 'finished']
                    self.assertIn(detail, '\n'.join(outcomes[-1].lines))
                    self.assertFalse(api.writes)
                finally:
                    controller.shutdown()


class LocalErrorTests(unittest.TestCase):
    def test_flash_session_lock_permission_error_names_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / '.hosted-flash'
            root.mkdir()
            root.chmod(0o555)
            try:
                with self.assertRaises(h.HostedError) as caught:
                    with h.flash_download_session('synthetic', dest_dir=directory):
                        self.fail('unwritable session was entered')
                self.assertEqual(caught.exception.code, 'disk')
                self.assertIn(str(root), str(caught.exception))
            finally:
                root.chmod(0o755)

    def test_flash_session_marker_write_error_is_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(h.os, 'fsync', side_effect=PermissionError('synthetic marker write refused')):
                with self.assertRaises(h.HostedError) as caught:
                    with h.flash_download_session('synthetic', dest_dir=directory):
                        self.fail('unsaved session marker was accepted')
            self.assertEqual(caught.exception.code, 'disk')
            self.assertIn(str(Path(directory)), str(caught.exception))

    def test_exhausted_http_retries_keep_status_and_cloudflare_note(self):
        import urllib.error
        def failed(*args, **kwargs):
            raise h.HostedError('http', 'HTTP 503. Cloudflare blocked this request.') from urllib.error.HTTPError(
                h.INDEX_URL, 503, 'unavailable', {}, None)
        fake = SimpleNamespace(HostedError=h.HostedError, download_image=failed)
        with patch.object(tui, '_wait_for_download_retry'):
            with self.assertRaises(h.HostedError) as caught:
                tui.download_for_flash(fake, 'synthetic', dest_dir=Path('/unused'))
        self.assertEqual(caught.exception.code, 'download-stopped')
        self.assertIn('HTTP 503', str(caught.exception))
        self.assertIn('Cloudflare blocked this request', str(caught.exception))

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

    def test_hosted_error_can_never_carry_a_blank_message(self):
        """TOOLKIT-FIX2 bug 2: a HostedError('http', '') must not be constructible.

        The owner saw EXACTLY "No Phoenix images or local packages are
        available. jibo.io answered with an error." with no trailing detail.
        root-check-403c4b1-messages.py proved the bare text only appears for
        HostedError('http', ''). Guarantee by construction that str(error) is
        never empty for any code, so the chooser's enrichment
        (jibo_tui._load_chooser: `code += ": " + str(error)`) always has a
        non-empty detail to append.
        """
        for code in ('http', 'rate_limited', 'disk', 'offline', 'signature'):
            with self.subTest(code=code):
                err = h.HostedError(code, '')
                self.assertTrue(str(err), 'HostedError(%r, "") produced an empty message' % code)

    def test_blank_http_error_never_shows_the_bare_owner_message(self):
        """End-to-end: inject the exact blank-message HostedError the owner's
        screenshot implied, through the real chooser path, and confirm the
        bare 'jibo.io answered with an error.' text (no trailing detail) never
        reaches the user."""
        class Hosted:
            HostedError = h.HostedError
            CancelToken = h.CancelToken

            @staticmethod
            def fetch_catalog(**kw):
                raise h.HostedError('http', '')

            @staticmethod
            def cached_catalog(**kw):
                raise h.HostedError('offline', 'none')

            @staticmethod
            def local_hosted_images(d):
                return []

        with patch.object(tui, '_terminal_available', return_value=False):
            choice, message = tui._choose_image(SimpleNamespace(_update_candidates=lambda _: []),
                                                Hosted, 'Choose an image to install', True)
        self.assertIsNone(choice)
        bare = 'No Phoenix images or local packages are available. jibo.io answered with an error.'
        self.assertNotEqual(message.strip(), bare,
                            'The bare owner-reported message reached the user with no detail')
        self.assertIn('jibo.io answered with an error.', message)
        # Something specific must follow the generic sentence.
        self.assertTrue(message.strip() != 'jibo.io answered with an error.'
                        and len(message) > len(bare) - 10)

    def test_load_chooser_writes_full_diagnostic_to_operation_log(self):
        """The GUI "Show operation log" must contain the full diagnostic
        (exception class, message, code) even when the user-facing sentence
        is generic, so an owner who clicks it gets more than the bare text."""
        import io
        from contextlib import redirect_stdout

        class Hosted:
            HostedError = h.HostedError
            CancelToken = h.CancelToken

            @staticmethod
            def fetch_catalog(**kw):
                raise ConnectionResetError('synthetic reset by peer')

            @staticmethod
            def cached_catalog(**kw):
                raise h.HostedError('offline', 'none')

            @staticmethod
            def local_hosted_images(d):
                return []

        with patch.object(tui, '_terminal_available', return_value=False):
            captured = io.StringIO()
            with redirect_stdout(captured):
                tui._load_chooser(Hosted)
        log = captured.getvalue()
        self.assertIn('ConnectionResetError', log)
        self.assertIn('synthetic reset by peer', log)
