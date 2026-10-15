"""ASK-70 regressions: real hosted code, synthetic transport, no device access."""
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import jibo_hosted_updates as h
import jibo_tui as tui


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
