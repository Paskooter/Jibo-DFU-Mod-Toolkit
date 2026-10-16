from pathlib import Path
from threading import Timer
import time
import unittest
from unittest.mock import patch

from test_hosted_catalog import HostedFixture
import jibo_hosted_updates as h


class DownloadTests(HostedFixture):
    def setUp(self):
        super().setUp()
        self.dest = self.cache.parent / 'updates'
        self.archive_path = self.row['url'].removeprefix(self.server.origin)

    def test_download_hash_members_atomic_completion_and_offline_verification(self):
        events = []
        path = Path(h.download_image('latest', dest_dir=self.dest, progress=events.append))
        self.assertEqual(path.read_bytes(), self.server.files[self.archive_path])
        self.assertEqual(events[-1].phase, 'done')
        self.assertIsNone(events[-1].percent)  # Review handoff is not a full, frozen download bar.
        self.assertEqual(len(h.local_hosted_images(self.dest)), 1)
        with patch.object(h, '_open', side_effect=AssertionError('offline must not fetch')):
            self.assertEqual(h.local_hosted_images(self.dest)[0].downloaded_path, str(path))
        path.write_bytes(b'corrupt')
        self.assertEqual(h.local_hosted_images(self.dest), [])

    def test_truncated_download_resumes_exact_range_and_if_range(self):
        self.server.truncate[self.archive_path] = 12345
        with self.assertRaises(h.HostedError):
            h.download_image(self.row['id'], dest_dir=self.dest)
        partial = next(self.dest.glob('*.part'))
        self.assertEqual(partial.stat().st_size, 12345)
        self.server.truncate.clear()
        h.download_image(self.row['id'], dest_dir=self.dest)
        requests = [headers for path, headers in self.server.requests if path == self.archive_path]
        self.assertEqual(requests[-1]['Range'], 'bytes=12345-')
        self.assertEqual(requests[-1]['If-Range'], '"synthetic-v1"')

    def test_ignored_range_restarts_not_appends(self):
        self.server.truncate[self.archive_path] = 9000
        with self.assertRaises(h.HostedError):
            h.download_image('latest', dest_dir=self.dest)
        self.server.truncate.clear()
        self.server.ignore_range = True
        path = h.download_image('latest', dest_dir=self.dest)
        self.assertEqual(Path(path).stat().st_size, self.row['size'])

    def test_cancel_keeps_partial_and_never_returns_verified_path(self):
        cancel = h.CancelToken()
        def progress(event):
            if event.phase == 'download' and event.bytes > 0:
                cancel.cancel()
        with self.assertRaises(h.HostedError) as caught:
            h.download_image('latest', dest_dir=self.dest, progress=progress, cancel=cancel)
        self.assertEqual(caught.exception.code, 'canceled')
        self.assertEqual(h.local_hosted_images(self.dest), [])
        self.assertTrue(list(self.dest.glob('*.part')))

    def test_wrong_content_range_and_corrupt_archive_refused(self):
        self.server.truncate[self.archive_path] = 9000
        with self.assertRaises(h.HostedError):
            h.download_image('latest', dest_dir=self.dest)
        self.server.truncate.clear()
        self.server.bad_range = True
        with self.assertRaises(h.HostedError):
            h.download_image('latest', dest_dir=self.dest)
        self.server.bad_range = False
        self.server.files[self.archive_path] = b'x' * self.row['size']
        with self.assertRaises(h.HostedError) as caught:
            h.download_image('latest', dest_dir=self.dest)
        self.assertEqual(caught.exception.code, 'hash')
        self.assertEqual(h.local_hosted_images(self.dest), [])

    def test_low_space_symlink_and_exclusive_lock_refuse(self):
        with patch('shutil.disk_usage', return_value=type('Usage', (), {'free': 0})()):
            with self.assertRaises(h.HostedError) as caught:
                h.download_image('latest', dest_dir=self.dest)
        self.assertEqual(caught.exception.code, 'disk')
        self.dest.mkdir(exist_ok=True)
        target = self.dest / f'phoenix-{self.row["id"]}-{self.row["sha256"]}.tar.bz2'
        target.symlink_to(self.cache.parent / 'not-owned')
        with self.assertRaises(h.HostedError):
            h.download_image('latest', dest_dir=self.dest)
        target.unlink()
        lock = target.with_name(target.name + '.lock')
        lock.write_text('synthetic other process')
        with self.assertRaises(h.HostedError):
            h.download_image('latest', dest_dir=self.dest)

    def test_manifest_and_archive_retry_same_immutable_selection(self):
        manifest_path = self.row['manifest_url'].removeprefix(self.server.origin)
        for path in (manifest_path, self.archive_path):
            self.server.responses[path] = [(429, '0'), (503, '0')]
        path = Path(h.download_image(self.row['id'], dest_dir=self.dest))
        self.assertEqual(path.read_bytes(), self.server.files[self.archive_path])
        self.assertEqual([p for p, _ in self.server.requests],
                         ['/flash/index.json'] + [manifest_path] * 3 + [self.archive_path] * 3)

    def test_archive_retry_preserves_exact_range_and_if_range(self):
        self.server.truncate[self.archive_path] = 12345
        with self.assertRaises(h.HostedError):
            h.download_image(self.row['id'], dest_dir=self.dest)
        self.server.truncate.clear()
        self.server.requests.clear()
        self.server.responses[self.archive_path] = [(429, '0'), (503, '0')]
        path = h.download_image(self.row['id'], dest_dir=self.dest)
        requests = [headers for p, headers in self.server.requests if p == self.archive_path]
        self.assertEqual(len(requests), 3)
        for headers in requests:
            self.assertEqual(headers['Range'], 'bytes=12345-')
            self.assertEqual(headers['If-Range'], '"synthetic-v1"')
        self.assertEqual(Path(path).read_bytes(), self.server.files[self.archive_path])

    def test_cancel_during_archive_retry_retains_prior_partial(self):
        self.server.truncate[self.archive_path] = 12345
        with self.assertRaises(h.HostedError):
            h.download_image(self.row['id'], dest_dir=self.dest)
        partial = next(self.dest.glob('*.part'))
        before = partial.read_bytes()
        self.server.truncate.clear()
        self.server.requests.clear()
        self.server.responses[self.archive_path] = [(429, '1')]
        token = h.CancelToken()
        events = []
        started = []
        original_wait = h._retry_wait
        def wait(delay, cancel):
            started.append(time.monotonic())
            timer = Timer(0.05, token.cancel)
            timer.start()
            try:
                return original_wait(delay, cancel)
            finally:
                timer.cancel()
                timer.join()
        with patch.object(h, '_retry_wait', side_effect=wait):
            with self.assertRaises(h.HostedError) as caught:
                h.download_image(self.row['id'], dest_dir=self.dest,
                                 cancel=token, progress=events.append)
        self.assertEqual(caught.exception.code, 'canceled')
        self.assertEqual(len(started), 1)
        self.assertLess(time.monotonic() - started[0], 0.5)
        self.assertEqual([p for p, _ in self.server.requests if p == self.archive_path],
                         [self.archive_path])
        self.assertEqual(partial.read_bytes(), before)
        self.assertEqual(events[-1].phase, 'canceled')
        self.assertFalse(any(event.phase == 'done' for event in events))
        self.assertEqual(h.local_hosted_images(self.dest), [])

    def test_manifest_hash_or_signature_failure_never_downloads_archive(self):
        manifest_path = self.row['manifest_url'].removeprefix(self.server.origin)
        self.server.files[manifest_path] = b'x' * self.row['manifest_size']
        with self.assertRaises(h.HostedError):
            h.download_image('latest', dest_dir=self.dest)
        self.assertFalse(any(path == self.archive_path for path, _ in self.server.requests))


if __name__ == '__main__':
    unittest.main()
