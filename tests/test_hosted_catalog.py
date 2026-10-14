import dataclasses
import importlib
import unittest
from unittest.mock import patch


class FrozenApiTests(unittest.TestCase):
    def test_frozen_api_import_has_no_network_or_usb(self):
        with patch('urllib.request.urlopen', side_effect=AssertionError('network at import')):
            module = importlib.import_module('jibo_hosted_updates')
        self.assertEqual(module.INDEX_URL, 'https://jibo.io/flash/index.json')
        self.assertTrue(module.HostedImage.__dataclass_params__.frozen)
        self.assertTrue(module.Catalog.__dataclass_params__.frozen)
        self.assertEqual([field.name for field in dataclasses.fields(module.Progress)],
                         ['phase', 'bytes', 'total', 'percent', 'rate', 'eta', 'message'])
        cancel = module.CancelToken()
        self.assertFalse(cancel.cancelled)
        cancel.cancel()
        self.assertTrue(cancel.cancelled)


from contextlib import ExitStack
from pathlib import Path
import tempfile
import time
from threading import Timer
from fake_flash_server import FakeFlashServer
import jibo_hosted_updates as h


class HostedFixture(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.server = self.stack.enter_context(FakeFlashServer(tls=getattr(self, 'tls', False)))
        if self.server.client_context is not None:
            self.stack.enter_context(patch('ssl._create_default_https_context',
                                           return_value=self.server.client_context))
        self.row, self.index, self.manifest = self.server.make_image()
        self.cache = Path(self.stack.enter_context(tempfile.TemporaryDirectory())) / 'cache'
        self.stack.enter_context(patch.object(h, 'INDEX_URL', self.server.origin + '/flash/index.json'))
        self.stack.enter_context(patch.object(h, 'ORIGIN', self.server.origin, create=True))
        import jibo_flash_contract as c
        self.stack.enter_context(patch.object(c, 'trusted_keys', return_value={
            'phoenix-flash-2026-10': self.server.public_key.read_bytes()}))
        self.stack.enter_context(patch.object(c, 'TOOLKIT_VERSION', '1.0.0'))

class RetryTests(HostedFixture):
    def test_429_then_503_retry_after_recover_same_catalog(self):
        self.server.responses['/flash/index.json'] = [(429, '0'), (503, '0')]
        cat = h.fetch_catalog(cache_dir=self.cache, timeout=2)
        self.assertFalse(cat.from_cache)
        self.assertEqual(cat.latest_id, self.row['id'])
        self.assertEqual([path for path, _ in self.server.requests], ['/flash/index.json'] * 3)

    def test_retryable_statuses_stop_after_three_attempts(self):
        for status, code in ((429, 'rate_limited'), (503, 'http')):
            with self.subTest(status=status):
                self.server.requests.clear()
                self.server.responses['/flash/index.json'] = [(status, '0')] * 4
                with self.assertRaises(h.HostedError) as caught:
                    h.fetch_catalog(cache_dir=self.cache, timeout=2)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(len(self.server.requests), 3)

    def test_nonretryable_http_status_is_not_retried(self):
        self.server.statuses['/flash/index.json'] = 404
        with self.assertRaises(h.HostedError) as caught:
            h.fetch_catalog(cache_dir=self.cache, timeout=2)
        self.assertEqual(caught.exception.code, 'http')
        self.assertEqual(len(self.server.requests), 1)

    def _assert_retry_after_seconds_is_not_shortened(self):
        self.server.responses['/flash/index.json'] = [(429, '1')]
        real_wait = h._retry_wait
        wait_durations = []

        def measured_wait(delay, cancel):
            started = time.monotonic()
            real_wait(delay, cancel)
            wait_durations.append(time.monotonic() - started)

        # Measure only the real retry boundary, not signature/cache/fsync work.
        # Keep the loopback request and the actual sleep implementation intact.
        with patch.object(h, '_retry_wait', side_effect=measured_wait) as wait:
            cat = h.fetch_catalog(cache_dir=self.cache, timeout=3)
        wait.assert_called_once()
        self.assertGreaterEqual(wait_durations[0], 0.95,
                                'Retry-After wait itself must not be shortened')
        self.assertEqual(wait.call_args.args[0], 1)
        self.assertFalse(cat.from_cache)
        self.assertEqual(len(self.server.requests), 2)

    def test_retry_after_seconds_is_not_shortened(self):
        self._assert_retry_after_seconds_is_not_shortened()

    def test_retry_after_seconds_excludes_slow_cache_work(self):
        real_atomic = h._atomic
        slow_cache_calls = []

        def slow_atomic(path, raw):
            if path.name == 'index.json':
                # This alone satisfied the old full-fetch elapsed-time oracle.
                time.sleep(1.05)
                slow_cache_calls.append(path.name)
            return real_atomic(path, raw)

        with patch.object(h, '_atomic', side_effect=slow_atomic):
            try:
                self._assert_retry_after_seconds_is_not_shortened()
            finally:
                self.assertEqual(slow_cache_calls, ['index.json'])

    def test_missing_or_invalid_retry_after_uses_exponential_backoff(self):
        self.server.responses['/flash/index.json'] = [(429, None), (503, 'invalid')]
        with patch.object(h, '_retry_wait', wraps=h._retry_wait) as wait:
            cat = h.fetch_catalog(cache_dir=self.cache, timeout=3)
        self.assertFalse(cat.from_cache)
        self.assertEqual([call.args[0] for call in wait.call_args_list], [0.25, 0.5])
        self.assertEqual(len(self.server.requests), 3)

    def test_retry_after_date_parsing_and_invalid_fallback(self):
        from email.utils import formatdate
        with patch.object(h.time, 'time', return_value=1000):
            self.assertEqual(h._retry_delay(formatdate(1002, usegmt=True), 0), 2)
            self.assertEqual(h._retry_delay(formatdate(998, usegmt=True), 0), 0)
        for value in ('-1', 'nan', '0.5', 'bad date'):
            with self.subTest(value=value):
                self.assertEqual(h._retry_delay(value, 1), 0.5)

    def test_excessive_retry_after_refuses_without_early_retry(self):
        for value, timeout in (('1', 0.2), ('999999999', 3), ('9' * 129, 3)):
            with self.subTest(value=value):
                self.server.requests.clear()
                self.server.responses['/flash/index.json'] = [(429, value)]
                started = time.monotonic()
                with self.assertRaises(h.HostedError) as caught:
                    h.fetch_catalog(cache_dir=self.cache, timeout=timeout)
                self.assertEqual(caught.exception.code, 'rate_limited')
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(len(self.server.requests), 1)

    def test_cancel_interrupts_retry_after_without_second_request(self):
        token = h.CancelToken()
        self.server.responses['/flash/index.json'] = [(429, '1')]
        timer = Timer(0.05, token.cancel)
        timer.start()
        self.addCleanup(timer.join)
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        with self.assertRaises(h.HostedError) as caught:
            h.fetch_catalog(cache_dir=self.cache, timeout=3, cancel=token)
        self.assertEqual(caught.exception.code, 'canceled')
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(len(self.server.requests), 1)


class DeadlineTests(HostedFixture):
    def test_retry_wait_consumes_metadata_body_budget(self):
        path = '/flash/deadline.json'
        self.server.files[path] = b'abc'
        self.server.responses[path] = [(503, '1')]
        self.server.chunk_sizes[path] = 1
        self.server.delays[path] = 0.16
        started = time.monotonic()
        with self.assertRaises(h.HostedError) as caught:
            h._fetch(self.server.origin + path, 10, 1.25, None)
        self.assertEqual(caught.exception.code, 'offline')
        self.assertLess(time.monotonic() - started, 1.4)
        self.assertEqual(len(self.server.requests), 2)

    def test_in_origin_redirects_do_not_reset_receive_deadline(self):
        first, second, third = ('/flash/first.json', '/flash/second.json', '/flash/third.json')
        self.server.redirects[first] = self.server.origin + second
        self.server.redirects[second] = self.server.origin + third
        self.server.header_delays[first] = 0.16
        self.server.header_delays[second] = 0.16
        self.server.files[third] = b'abc'
        started = time.monotonic()
        with self.assertRaises(h.HostedError) as caught:
            h._fetch(self.server.origin + first, 10, 0.25, None)
        self.assertEqual(caught.exception.code, 'offline')
        self.assertLess(time.monotonic() - started, 0.4)
        self.assertEqual([path for path, _ in self.server.requests], [first, second])

    def test_archive_stream_body_is_not_capped_by_opening_deadline(self):
        path = '/flash/deadline.json'
        self.server.files[path] = b'abc'
        self.server.chunk_sizes[path] = 1
        self.server.delays[path] = 0.16
        started = time.monotonic()
        with h._open(self.server.origin + path, 0.25) as response:
            self.assertEqual(response.read(3), b'abc')
        self.assertGreater(time.monotonic() - started, 0.4)

    def test_slow_headers_obey_overall_receive_deadline(self):
        path = '/flash/deadline.json'
        self.server.files[path] = b'abc'
        self.server.header_drips[path] = 0.02
        started = time.monotonic()
        with self.assertRaises(h.HostedError) as caught:
            h._fetch(self.server.origin + path, 10, 0.25, None)
        self.assertEqual(caught.exception.code, 'offline')
        self.assertLess(time.monotonic() - started, 0.4)
        self.assertEqual(len(self.server.requests), 1)

    def test_slow_chunk_framing_obeys_remaining_receive_deadline(self):
        path = '/flash/deadline.json'
        self.server.files[path] = b'abc'
        self.server.chunked_bodies[path] = b'0003\r\nabc\r\n0\r\n\r\n'
        self.server.chunk_sizes[path] = 1
        self.server.delays[path] = 0.16
        started = time.monotonic()
        with self.assertRaises(h.HostedError) as caught:
            h._fetch(self.server.origin + path, 10, 0.25, None)
        self.assertEqual(caught.exception.code, 'offline')
        self.assertLess(time.monotonic() - started, 0.4)
        self.assertEqual(len(self.server.requests), 1)

    def test_chunked_metadata_still_succeeds_within_budget(self):
        path = '/flash/deadline.json'
        self.server.files[path] = b'abc'
        self.server.chunked_bodies[path] = b'1\r\na\r\n2\r\nbc\r\n0\r\n\r\n'
        self.assertEqual(h._fetch(self.server.origin + path, 10, 1, None), b'abc')

    def test_slow_drip_metadata_obeys_overall_read_deadline(self):
        path = '/flash/deadline.json'
        self.server.files[path] = b'abc'
        self.server.chunk_sizes[path] = 1
        self.server.delays[path] = 0.16
        started = time.monotonic()
        with self.assertRaises(h.HostedError) as caught:
            h._fetch(self.server.origin + path, 10, 0.25, None)
        self.assertEqual(caught.exception.code, 'offline')
        self.assertLess(time.monotonic() - started, 0.4)
        self.assertEqual(len(self.server.requests), 1)

    def test_headers_consume_budget_before_stalled_body_read(self):
        path = '/flash/deadline.json'
        self.server.files[path] = b'abc'
        self.server.header_delays[path] = 0.16
        self.server.delays[path] = 0.3
        started = time.monotonic()
        with self.assertRaises(h.HostedError) as caught:
            h._fetch(self.server.origin + path, 10, 0.25, None)
        self.assertEqual(caught.exception.code, 'offline')
        self.assertLess(time.monotonic() - started, 0.38)
        self.assertEqual(len(self.server.requests), 1)


class TlsDeadlineTests(DeadlineTests):
    tls = True

    def test_untrusted_tls_certificate_is_refused(self):
        import ssl
        with patch('ssl._create_default_https_context', ssl.create_default_context):
            with self.assertRaises(h.HostedError) as caught:
                h._fetch(self.server.origin + '/flash/index.json', 4096, 1, None)
        self.assertEqual(caught.exception.code, 'offline')
        self.assertEqual(self.server.requests, [])


class CatalogTests(HostedFixture):
    def test_signed_catalog_and_offline_cache_no_robot_data_headers(self):
        cat = h.fetch_catalog(cache_dir=self.cache)
        self.assertFalse(cat.from_cache)
        self.assertEqual(cat.latest_id, self.row['id'])
        self.assertTrue(cat.images[0].compatible)
        headers = self.server.requests[0][1]
        self.assertEqual(headers['Accept-Encoding'], 'identity')
        self.assertFalse(any(k.lower() in ('cookie', 'authorization') for k in headers))
        self.server.statuses['/flash/index.json'] = 503
        cat = h.fetch_catalog(cache_dir=self.cache)
        self.assertTrue(cat.from_cache)
        self.assertIn('http', cat.error)

    def test_expiry_duplicate_ids_float_size_and_unsafe_urls_are_refused(self):
        for change in ('expiry', 'duplicate', 'float', 'url'):
            with self.subTest(change=change):
                import copy
                index = copy.deepcopy(self.index)
                if change == 'expiry':
                    index['expires_at'] = '2000-01-01T00:00:00Z'
                elif change == 'duplicate':
                    index['images'].append(index['images'][0])
                elif change == 'float':
                    index['images'][0]['size'] = 1.0
                else:
                    index['images'][0]['url'] = 'https://evil.invalid/flash/image.tar.bz2'
                self.server.files['/flash/index.json'] = self.server.envelope(index)
                with self.assertRaises(h.HostedError):
                    h.fetch_catalog(cache_dir=self.cache)

    def test_monotonic_sequence_refuses_replay_without_cache_fallback(self):
        self.index['sequence'] = 9
        self.server.files['/flash/index.json'] = self.server.envelope(self.index)
        h.fetch_catalog(cache_dir=self.cache)
        self.index['sequence'] = 8
        self.server.files['/flash/index.json'] = self.server.envelope(self.index)
        with self.assertRaises(h.HostedError):
            h.fetch_catalog(cache_dir=self.cache)

    def test_unknown_capability_disables_row_without_promoting_older(self):
        self.index['images'][0]['required_capabilities'] = ['unknown-new-capability']
        self.server.files['/flash/index.json'] = self.server.envelope(self.index)
        # Canonical A2 v1 is closed-world: unrecognized capability is malformed.
        with self.assertRaises(h.HostedError):
            h.fetch_catalog(cache_dir=self.cache)

    def test_off_origin_redirect_is_rejected_before_target_connection(self):
        self.server.redirects['/flash/index.json'] = 'http://127.0.0.1:1/flash/index.json'
        with self.assertRaises(h.HostedError):
            h.fetch_catalog(cache_dir=self.cache)
        self.assertEqual(len(self.server.requests), 1)

    def test_canceled_before_fetch_makes_no_request(self):
        token = h.CancelToken()
        token.cancel()
        with self.assertRaises(h.HostedError) as caught:
            h.fetch_catalog(cache_dir=self.cache, cancel=token)
        self.assertEqual(caught.exception.code, 'canceled')
        self.assertEqual(self.server.requests, [])


if __name__ == '__main__':
    unittest.main()
