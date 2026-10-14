"""Resume interrupted hosted transfers inside one reviewed flash flow."""
from pathlib import Path
from threading import Timer
import time
from unittest.mock import patch

from test_hosted_catalog import HostedFixture
from gui_fakes import FakeHostedApi
import jibo_hosted_updates as h
import jibo_tui as tui


class HostedFlowResumeTests(HostedFixture):
    def setUp(self):
        super().setUp()
        self.dest = self.cache.parent / 'packages'
        self.image = h.fetch_catalog(cache_dir=self.cache).images[0]
        self.archive = self.row['url'].removeprefix(self.server.origin)
        self.api = FakeHostedApi()
        self.api.hosted = h
        self.calls, self.waits, self.progress = [], [], []
        self.original_download = h.download_image

    def flow(self, behavior, wait=None):
        def download(image_id, **kwargs):
            self.calls.append(image_id)
            behavior(len(self.calls), kwargs)
            return self.original_download(image_id, **kwargs)
        original_run = tui.run_with_progress
        def run(title, work, cancel, detail=''):
            def wrapped(report, token):
                def progress(event):
                    self.progress.append(event)
                    report(event)
                return work(progress, token)
            return original_run(title, wrapped, cancel, detail)
        def delayed(seconds, cancel, hosted):
            self.waits.append(seconds)
            if wait:
                return wait(seconds, cancel, hosted)
        with patch.object(tui, 'hosted_folders', return_value=(self.dest, self.cache)), \
                patch.object(tui, '_choose_image', return_value=(('hosted', self.image), '')), \
                patch.object(tui, '_choose_flash_policy', return_value='keep-calibration'), \
                patch.object(tui, '_terminal_available', return_value=False), \
                patch.object(tui, 'confirm_action', return_value=True), \
                patch.object(tui, 'run_with_progress', side_effect=run), \
                patch.object(tui, '_wait_for_download_retry', side_effect=delayed, create=True), \
                patch.object(h, 'download_image', side_effect=download):
            return tui._run_hosted_update(self.api, h, '1-1')

    def test_ui_fake_does_not_allocate_real_download_files(self):
        from gui_fakes import FakeHosted
        fake = FakeHosted()
        with patch.object(h, 'flash_download_session', side_effect=AssertionError('UI fake performed real file I/O')):
            with fake.flash_download_session('phx-2', dest_dir=self.dest) as session:
                self.assertFalse(session.complete)
        self.assertFalse(self.dest.exists())

    def test_truncate_twice_then_resume_and_flash_exactly_once(self):
        def behavior(attempt, kwargs):
            if attempt <= 2:
                self.server.truncate[self.archive] = 9000
            else:
                self.server.truncate.pop(self.archive, None)
        result = self.flow(behavior)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(self.calls, [self.row['id']] * 3)
        self.assertEqual(self.api.writes, ['flash_hosted_update'])
        headers = [headers for path, headers in self.server.requests if path == self.archive]
        self.assertEqual(len(headers), 3)
        self.assertEqual(headers[1]['Range'], 'bytes=9000-')
        self.assertEqual(headers[2]['Range'], 'bytes=18000-')
        self.assertEqual(self.waits, [2, 5])
        self.assertFalse(list(self.dest.rglob('*.tar.bz2')))

    def test_always_truncated_stops_after_bound_and_keeps_partial_without_flash(self):
        with self.assertRaisesRegex(h.HostedError, 'Nothing was written to the robot.*continue'):
            self.flow(lambda attempt, kwargs: self.server.truncate.__setitem__(self.archive, 9000))
        limit = tui.HOSTED_DOWNLOAD_ATTEMPTS
        self.assertEqual(len(self.calls), limit)
        self.assertEqual(self.calls, [self.row['id']] * limit)
        self.assertEqual(self.api.writes, [])
        self.assertFalse(any(call[0] == 'flash_hosted_update' for call in self.api.calls))
        partial = next(self.dest.rglob('*.part'))
        self.assertEqual(partial.stat().st_size, 9000 * limit)
        self.assertTrue(partial.with_name(partial.name + '.json').is_file())
        self.assertEqual(self.waits, list(tui.HOSTED_DOWNLOAD_BACKOFF[:limit - 1]))

    def test_signature_failure_is_not_retried(self):
        self.server.files['/flash/index.json'] = self.server.envelope(self.index, bad_signature=True)
        result = self.flow(lambda attempt, kwargs: None)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.waits, [])
        self.assertEqual(self.api.writes, [])
        self.assertFalse(any(path == self.archive for path, headers in self.server.requests))

    def test_signed_size_conflict_is_not_retried(self):
        self.server.files[self.archive] += b'oversize'
        result = self.flow(lambda attempt, kwargs: None)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.waits, [])
        self.assertEqual(self.api.writes, [])

    def test_retry_after_is_honored_for_503_and_429(self):
        for status, retry_after in ((503, '6'), (429, '7')):
            with self.subTest(status=status):
                self.calls.clear()
                self.waits.clear()
                self.api.writes.clear()
                self.server.responses[self.archive] = [(status, retry_after)]
                result = self.flow(lambda attempt, kwargs: None)
                self.assertEqual(result['status'], 'verified')
                self.assertEqual(len(self.calls), 2)
                self.assertEqual(self.waits, [int(retry_after)])
                self.assertEqual(self.api.writes, ['flash_hosted_update'])

    def test_hash_failure_is_not_retried(self):
        self.server.files[self.archive] = b'x' * len(self.server.files[self.archive])
        result = self.flow(lambda attempt, kwargs: None)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.waits, [])
        self.assertEqual(self.api.writes, [])
        self.assertFalse(list(self.dest.rglob('*.part*')))

    def test_expired_signed_index_is_not_retried(self):
        index = dict(self.index, generated_at=int(time.time()) - 7200,
                     expires_at=int(time.time()) - 3600)
        self.server.files['/flash/index.json'] = self.server.envelope(index)
        result = self.flow(lambda attempt, kwargs: None)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.waits, [])
        self.assertEqual(self.api.writes, [])

    def test_cli_latest_is_pinned_once_across_resumed_attempts(self):
        import io
        from contextlib import redirect_stdout
        import jibo_dfu as d
        calls, flashed = [], []
        def download(image_id, **kwargs):
            calls.append(image_id)
            if len(calls) == 1:
                self.server.truncate[self.archive] = 9000
                try:
                    return self.original_download(image_id, **kwargs)
                finally:
                    row, index, _ = self.server.make_image('newer-image', sequence=2)
                    index['images'].append(self.row)
                    self.server.files['/flash/index.json'] = self.server.envelope(index)
                    self.server.truncate.clear()
            return self.original_download(image_id, **kwargs)
        def flash(path, **kwargs):
            self.assertEqual(h.verify_local_image(path)['image'].id, self.row['id'])
            flashed.append(path)
            return {'status': 'verified'}
        with patch.object(h, 'download_image', side_effect=download), \
                patch.object(tui, '_wait_for_download_retry'), \
                patch.object(d, 'flash_hosted_update', side_effect=flash), redirect_stdout(io.StringIO()):
            result = d.main(['flash-hosted-update', '--image', 'latest', '--directory', str(self.dest)])
        self.assertEqual(result, 0)
        self.assertEqual(calls, [self.row['id']] * 2)
        self.assertEqual(len(flashed), 1)
        self.assertFalse(list(self.dest.rglob('*.tar.bz2')))

    def test_cancel_during_backoff_stops_promptly_and_keeps_partial(self):
        real_wait = getattr(tui, '_wait_for_download_retry', None)
        durations = []
        tokens = []
        def wait(seconds, cancel, hosted):
            tokens.append(cancel)
            timer = Timer(0.03, cancel.cancel)
            timer.start()
            started = time.monotonic()
            try:
                real_wait(seconds, cancel, hosted)
            finally:
                durations.append(time.monotonic() - started)
                timer.cancel()
                timer.join()
        result = self.flow(lambda attempt, kwargs: self.server.truncate.__setitem__(self.archive, 9000), wait)
        self.assertEqual(result['status'], 'cancelled')
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(tokens), 1)
        self.assertTrue(tokens[0].cancelled)
        self.assertLess(durations[0], 0.5)
        self.assertEqual(self.api.writes, [])
        self.assertTrue(list(self.dest.rglob('*.part')))

    def test_resume_message_has_next_attempt_number_and_bound(self):
        self.server.truncate[self.archive] = 9000
        def wait(seconds, cancel, hosted):
            self.server.truncate.clear()
        result = self.flow(lambda attempt, kwargs: None, wait)
        self.assertEqual(result['status'], 'verified')
        lines = [tui.format_progress(event) for event in self.progress if event.phase == 'resume']
        self.assertEqual(lines, ['Connection dropped, resuming… (attempt 2 of 6)'])

    def test_gui_backoff_shows_resume_message_and_remains_cancelable(self):
        from threading import Event
        import jibo_gui_core as core
        answers = iter(['hosted:' + self.row['id'], 'keep-calibration'])
        controller = core.GuiController(self.api, prompter=lambda prompt: next(answers),
                                        runner=core.sync_runner)
        controller.request_refresh()
        controller._runner = core.thread_runner
        flows = controller.flows
        waiting = Event()
        real_wait = flows._wait_for_download_retry
        calls = []
        def download(image_id, **kwargs):
            calls.append(image_id)
            self.server.truncate[self.archive] = 9000
            return self.original_download(image_id, **kwargs)
        def wait(seconds, cancel, hosted):
            waiting.set()
            return real_wait(seconds, cancel, hosted)
        try:
            with patch.object(flows, 'hosted_folders', return_value=(self.dest, self.cache)), \
                    patch.object(flows, '_wait_for_download_retry', side_effect=wait), \
                    patch.object(h, 'download_image', side_effect=download):
                self.assertTrue(controller.start_action('flash-update'))
                self.assertTrue(waiting.wait(5))
                self.assertTrue(controller.download_active)
                self.assertIn('Connection dropped, resuming', controller.output_state().status)
                self.assertTrue(controller.cancel_download())
                deadline = time.monotonic() + 5
                while controller.busy and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(controller.busy)
                self.assertEqual(calls, [self.row['id']])
                outcomes = [event[1] for event in controller.drain() if event[0] == 'finished']
                self.assertEqual(outcomes[-1].status, 'cancelled')
                self.assertEqual(self.api.writes, [])
                self.assertTrue(list(self.dest.rglob('*.part')))
        finally:
            controller.shutdown()
