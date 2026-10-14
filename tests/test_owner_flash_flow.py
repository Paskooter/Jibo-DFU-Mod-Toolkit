"""One reviewed flow with real signed loopback downloads and ephemeral files."""
import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import jibo_dfu as d
import jibo_hosted_updates as h
import jibo_tui as tui
from gui_fakes import FakeHostedApi, sample_catalog
from test_hosted_catalog import HostedFixture


class OwnerFlashChoiceTests(unittest.TestCase):
    def test_menu_has_no_download_only_action(self):
        self.assertNotIn('download-phoenix', [i.key for i in tui.TerminalMenu(FakeHostedApi()).items])

    def test_featured_first_choice_names_source_not_raw_base_or_retained_download(self):
        items, _ = tui.hosted_chooser_items(sample_catalog(), (), [Path('official.tar.bz2')])
        self.assertEqual(items[0].tone, 'recommended')
        self.assertIn('downloads from jibo.io when you flash', items[0].label)
        self.assertNotIn('lastdance', items[0].label)
        self.assertNotIn('downloaded', items[0].label)
        self.assertEqual(items[1].label, 'official.tar.bz2')

    def test_cli_has_no_download_or_offline_flash_path(self):
        for argv in (['download-update', '--image', 'latest'],
                     ['flash-hosted-update', '--image', 'latest', '--offline'],
                     ['flash-hosted-update', '--image', 'latest', '--dry-run']):
            with self.subTest(argv=argv), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                    patch.object(h, '_open', side_effect=h.HostedError('offline', 'Network disabled by test')):
                with self.assertRaises(SystemExit) as stopped:
                    d.main(argv)
                self.assertEqual(stopped.exception.code, 2)


class OwnerFlashFlowTests(HostedFixture):
    def flow(self, outcome):
        events = []
        dest = self.cache.parent / 'packages'
        dest.mkdir(exist_ok=True)
        own = dest / 'official.tar.bz2'
        own.write_bytes(b'user package')
        backups = self.cache.parent / 'Jibo-Backups'
        backups.mkdir(exist_ok=True)
        backup = backups / 'var.img'
        backup.write_bytes(b'robot backup')
        record = backups / 'update-manifest.json'
        record.write_text('operation record')
        image = h.fetch_catalog(cache_dir=self.cache).images[0]
        api = FakeHostedApi()
        api.hosted = h
        downloaded = []
        original_download = h.download_image
        def download(*args, **kwargs):
            events.append('download')
            path = original_download(*args, **kwargs)
            downloaded.append(Path(path))
            self.assertTrue(Path(path).is_file())
            self.assertTrue(h._provenance(Path(path)).is_file())
            events.append('verify')
            return path
        def choose_policy():
            events.append('calibration')
            return 'keep-calibration'
        def flash(path, **kwargs):
            self.assertTrue(Path(path).is_file())
            h.verify_local_image(path)
            events.append('review')
            accepted = kwargs['confirmation']({'phoenix': {'id': image.id}})
            if not accepted:
                return {'status': 'cancelled'}
            events.append('write')
            if outcome == 'failure':
                raise d.DfuError('write stopped')
            events.append('readback')
            return {'status': 'verified'}
        api.flash_hosted_update = flash
        with patch.object(tui, 'hosted_folders', return_value=(dest, self.cache)), \
                patch.object(tui, '_choose_image', return_value=(('hosted', image), '')), \
                patch.object(tui, '_choose_flash_policy', side_effect=choose_policy), \
                patch.object(tui, '_terminal_available', return_value=False), \
                patch.object(tui, 'confirm_action', return_value=outcome != 'cancel'), \
                patch.object(h, 'download_image', side_effect=download):
            if outcome == 'failure':
                with self.assertRaisesRegex(d.DfuError, 'write stopped'):
                    tui._run_hosted_update(api, h, '1-1')
            else:
                result = tui._run_hosted_update(api, h, '1-1')
                self.assertEqual(result['status'], 'cancelled' if outcome == 'cancel' else 'verified')
        self.assertEqual(events[:3], ['calibration', 'download', 'verify'])
        self.assertEqual(own.read_bytes(), b'user package')
        self.assertEqual(backup.read_bytes(), b'robot backup')
        self.assertEqual(record.read_text(), 'operation record')
        for path in downloaded:
            self.assertFalse(path.exists(), 'completed flash download was retained')
            self.assertFalse(h._provenance(path).exists())
        self.assertFalse(list(dest.rglob('*.ext4')))
        self.assertFalse(list(dest.rglob('*.provenance.json')))
        return events

    def test_completed_download_deleted_after_success(self):
        self.assertEqual(self.flow('success'), ['calibration', 'download', 'verify', 'review', 'write', 'readback'])

    def test_completed_download_deleted_after_write_failure(self):
        self.flow('failure')

    def test_completed_download_deleted_after_review_cancel(self):
        self.assertEqual(self.flow('cancel'), ['calibration', 'download', 'verify', 'review'])


    def test_interrupted_download_resumes_then_removes_all_working_files(self):
        dest = self.cache.parent / 'packages'
        image = h.fetch_catalog(cache_dir=self.cache).images[0]
        api = FakeHostedApi()
        api.hosted = h
        api.flash_hosted_update = lambda path, **kwargs: {'status': 'cancelled'}
        real_download = h.download_image
        def interrupt(image_id, **kwargs):
            report = kwargs['progress']
            token = kwargs['cancel']
            def progress(event):
                report(event)
                if event.phase == 'download' and event.bytes:
                    token.cancel()
            return real_download(image_id, **dict(kwargs, progress=progress))
        with patch.object(tui, 'hosted_folders', return_value=(dest, self.cache)), \
                patch.object(tui, '_choose_image', return_value=(('hosted', image), '')), \
                patch.object(tui, '_choose_flash_policy', return_value='keep-calibration'), \
                patch.object(tui, '_terminal_available', return_value=False):
            with patch.object(h, 'download_image', side_effect=interrupt):
                result = tui._run_hosted_update(api, h, '1-1')
            self.assertEqual(result['status'], 'cancelled')
            partial = next(dest.rglob('*.part'))
            saved = partial.stat().st_size
            self.assertTrue(partial.with_name(partial.name + '.json').is_file())
            result = tui._run_hosted_update(api, h, '1-1')
            self.assertEqual(result['status'], 'cancelled')
        archive = self.row['url'].removeprefix(self.server.origin)
        headers = [headers for path, headers in self.server.requests if path == archive]
        if saved < self.row['size']:
            self.assertEqual(headers[-1]['Range'], 'bytes=' + str(saved) + '-')
        self.assertFalse(list(dest.rglob('*.part')))
        self.assertFalse(list(dest.rglob('*.tar.bz2')))
        self.assertFalse(list(dest.rglob('*.provenance.json')))
        self.assertFalse(list(dest.rglob('index.json')))

    def test_cli_flash_download_files_exist_until_review_and_are_removed(self):
        dest = self.cache.parent / 'packages'
        paths = []
        def flash(path, **kwargs):
            path = Path(path)
            paths.append(path)
            self.assertTrue(path.is_file())
            self.assertTrue(h._provenance(path).is_file())
            self.assertEqual(h.verify_local_image(path)['image'].id, self.row['id'])
            for directory in ('extracted', 'prepared'):
                image = path.parent / directory / 'rootfs.ext4'
                image.parent.mkdir()
                image.write_bytes(b'working image')
            return {'status': 'cancelled'}
        with patch.object(d, 'flash_hosted_update', side_effect=flash), redirect_stdout(io.StringIO()):
            self.assertEqual(d.main(['flash-hosted-update', '--image', self.row['id'],
                                    '--directory', str(dest)]), 0)
        self.assertEqual(len(paths), 1)
        self.assertFalse(paths[0].parent.exists())

    def test_unowned_working_folder_is_never_deleted(self):
        dest = self.cache.parent / 'packages'
        directory = dest / '.hosted-flash' / self.row['id']
        directory.mkdir(parents=True)
        own = directory / 'my-package.tar.bz2'
        own.write_bytes(b'own')
        with self.assertRaisesRegex(h.HostedError, 'not owned'):
            with h.flash_download_session(self.row['id'], dest_dir=dest):
                self.fail('unowned folder was accepted')
        self.assertEqual(own.read_bytes(), b'own')

    def test_cleanup_does_not_follow_symlink_to_backup(self):
        dest = self.cache.parent / 'packages'
        backup = self.cache.parent / 'var.img'
        backup.write_bytes(b'backup')
        with h.flash_download_session(self.row['id'], dest_dir=dest) as session:
            (session.directory / 'extracted.ext4').symlink_to(backup)
            session.complete = True
        self.assertEqual(backup.read_bytes(), b'backup')


    def test_review_receives_exact_authenticated_archive_hash(self):
        path = h.download_image(self.row['id'], dest_dir=self.cache.parent / 'packages')
        seen = []
        def flash(package, policy, **kwargs):
            self.assertEqual(package, Path(path))
            kwargs['confirmation']({'phoenix': {'id': self.row['id']}})
            return {'status': 'cancelled'}
        with patch.object(d, 'flash_update', side_effect=flash):
            d.flash_hosted_update(path, confirmation=lambda plan: seen.append(plan) or False)
        self.assertEqual(seen[0]['phoenix']['archive_sha256'], self.row['sha256'])


if __name__ == '__main__':
    unittest.main()
