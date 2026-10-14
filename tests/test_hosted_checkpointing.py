"""Bound checkpoint work without relaxing durable, byte-exact resume."""
import hashlib
import json
import math
import os
from pathlib import Path
import time
from unittest.mock import patch

from test_hosted_catalog import HostedFixture
import jibo_hosted_updates as h


class CheckpointTests(HostedFixture):
    def setUp(self):
        super().setUp()
        self.dest = self.cache.parent / 'updates'

    def assert_partial_receipt(self):
        partial = next(self.dest.glob('*.part'))
        receipt = json.loads(partial.with_name(partial.name + '.json').read_bytes())
        raw = partial.read_bytes()
        self.assertEqual(receipt['bytes'], len(raw))
        self.assertEqual(receipt['partial_sha256'], hashlib.sha256(raw).hexdigest())
        return receipt

    def test_cancel_between_checkpoints_flushes_exact_resumable_partial(self):
        token = h.CancelToken()
        def progress(event):
            if event.phase == 'download' and event.bytes >= 65536:
                token.cancel()
        with self.assertRaises(h.HostedError) as caught:
            h.download_image('latest', dest_dir=self.dest, cancel=token, progress=progress)
        self.assertEqual(caught.exception.code, 'canceled')
        receipt = self.assert_partial_receipt()
        self.assertGreater(receipt['bytes'], 0)
        self.assertLess(receipt['bytes'], self.row['size'])
        path = Path(h.download_image(self.row['id'], dest_dir=self.dest))
        self.assertEqual(h.c.hash_file(path), self.row['sha256'])
        headers = [headers for path, headers in self.server.requests
                   if path == self.row['url'].removeprefix(self.server.origin)][-1]
        self.assertEqual(headers['Range'], f'bytes={receipt["bytes"]}-')

    def test_transport_exception_between_checkpoints_preserves_resume(self):
        real_open = h._open
        def interrupted_open(url, *args, **kwargs):
            response = real_open(url, *args, **kwargs)
            if url == self.row['url']:
                read = response.read
                calls = [0]
                def interrupted_read(amount):
                    calls[0] += 1
                    if calls[0] == 3:
                        raise OSError('Synthetic connection interruption')
                    return read(amount)
                response.read = interrupted_read
            return response
        with patch.object(h, '_open', side_effect=interrupted_open):
            with self.assertRaises(h.HostedError) as caught:
                h.download_image('latest', dest_dir=self.dest)
        self.assertEqual(caught.exception.code, 'offline')
        receipt = self.assert_partial_receipt()
        self.assertEqual(receipt['bytes'], 2 * 65536)
        path = Path(h.download_image(self.row['id'], dest_dir=self.dest))
        self.assertEqual(h.c.hash_file(path), self.row['sha256'])

    def test_elapsed_interval_checkpoints_before_byte_threshold(self):
        real_atomic = h._atomic
        checkpoints = []
        clock = [time.monotonic()]
        def atomic(path, raw):
            if path.name.endswith('.part.json'):
                checkpoints.append(json.loads(raw)['bytes'])
            return real_atomic(path, raw)
        def progress(event):
            if event.phase == 'download':
                clock[0] += 2.1
        with patch.object(h, 'time', wraps=time) as clock_module, \
                patch.object(h, '_atomic', side_effect=atomic):
            clock_module.monotonic.side_effect = lambda: clock[0]
            h.download_image('latest', dest_dir=self.dest, progress=progress)
        self.assertTrue(any(0 < size < self.row['size'] for size in checkpoints),
                        'Elapsed time must trigger an intermediate checkpoint below 8 MiB')


class TlsCheckpointTests(CheckpointTests):
    tls = True


class LargeCheckpointTests(HostedFixture):
    tls = False
    assert_partial_receipt = CheckpointTests.assert_partial_receipt

    def setUp(self):
        super().setUp()
        self.dest = self.cache.parent / 'updates'
        self.row, self.index, self.manifest = self.server.make_image(member_size=16 * 1024 * 1024)
        self.archive_path = self.row['url'].removeprefix(self.server.origin)

    def test_large_download_bounds_slow_fsync_and_receipt_never_leads_data(self):
        self.assertGreaterEqual(self.row['size'], 64 * 1024 * 1024)
        calls = []
        durable = [0]
        real_atomic = h._atomic
        def slow_fsync(fd):
            time.sleep(0.02)  # A deterministic slow-disk model, not a timing assertion.
            calls.append(fd)
            partials = list(self.dest.glob('*.part'))
            if partials:
                info = os.fstat(fd)
                partial_info = partials[0].stat()
                if (info.st_dev, info.st_ino) == (partial_info.st_dev, partial_info.st_ino):
                    durable[0] = os.lseek(fd, 0, os.SEEK_CUR)
        def atomic(path, raw):
            if path.name.endswith('.part.json'):
                self.assertEqual(json.loads(raw)['bytes'], durable[0],
                                 'Receipt must describe the preceding data fsync, never future bytes')
            return real_atomic(path, raw)
        started = time.monotonic()
        with patch.object(h.os, 'fsync', side_effect=slow_fsync), \
                patch.object(h, '_atomic', side_effect=atomic):
            path = Path(h.download_image('latest', dest_dir=self.dest))
        # A checkpoint syncs both the data and its atomic receipt. Include
        # initial/final checkpoints, catalog/provenance writes and timer headroom.
        bound = 2 * math.ceil(self.row['size'] / (8 * 1024 * 1024)) + 9
        print(f'checkpoint probe: tls={self.tls if hasattr(self, "tls") else False} '
              f'bytes={self.row["size"]} fsyncs={len(calls)} bound={bound} '
              f'seconds={time.monotonic() - started:.3f}', flush=True)
        self.assertLessEqual(len(calls), bound)
        self.assertEqual(h.c.hash_file(path), self.row['sha256'])

    def test_large_interruption_resumes_exact_durable_receipt(self):
        self.server.truncate[self.archive_path] = 9 * 1024 * 1024 + 12345
        with self.assertRaises(h.HostedError):
            h.download_image('latest', dest_dir=self.dest)
        receipt = self.assert_partial_receipt()
        self.assertEqual(receipt['bytes'], self.server.truncate[self.archive_path])
        self.server.truncate.clear()
        path = Path(h.download_image(self.row['id'], dest_dir=self.dest))
        self.assertEqual(h.c.hash_file(path), self.row['sha256'])
        headers = [headers for path, headers in self.server.requests if path == self.archive_path][-1]
        self.assertEqual(headers['Range'], f'bytes={receipt["bytes"]}-')
        self.assertEqual(headers['If-Range'], '"synthetic-v1"')


class TlsLargeCheckpointTests(LargeCheckpointTests):
    tls = True
