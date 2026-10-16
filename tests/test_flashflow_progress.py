"""Progress, cancellation and process-only trust over signed local fixtures."""
import os
from pathlib import Path
import tarfile
import time
from unittest.mock import patch

from test_hosted_catalog import HostedFixture
import jibo_flash_contract as c
import jibo_hosted_updates as h
import jibo_updates as u


class FlashProgressTests(HostedFixture):
    def download(self, **kwargs):
        return Path(h.download_image(self.row['id'], dest_dir=self.cache.parent / 'downloads', **kwargs))

    def test_slow_post_download_check_never_goes_silent_for_two_seconds(self):
        events = []
        original = c.verify_archive
        def slow(*args, **kwargs):
            time.sleep(2.3)
            return original(*args, **kwargs)
        with patch.object(c, 'verify_archive', side_effect=slow):
            self.download(progress=lambda event: events.append((time.monotonic(), event)))
        last_download = max(i for i, (_, event) in enumerate(events) if event.phase == 'download')
        checking = events[last_download:]
        gaps = [b[0] - a[0] for a, b in zip(checking, checking[1:])]
        print('FLASHFLOW_PROGRESS', 'max_gap_seconds=', max(gaps), 'events=', len(checking))
        self.assertLess(max(gaps), 2)
        percents = [event.percent for _, event in checking if event.phase == 'verify' and event.percent is not None]
        self.assertEqual(percents, sorted(percents))
        self.assertFalse(any(event.phase == 'verify' and event.percent == 100 for _, event in checking))

    def test_same_file_memo_reuses_check_but_each_identity_change_rechecks(self):
        path = self.download()
        real_open = tarfile.open
        with patch.object(tarfile, 'open', wraps=real_open) as opened:
            h.verify_local_image(path)
            u.validate_package(path)
            self.assertEqual(opened.call_count, 0)
            for change in ('mtime', 'ctime', 'inode'):
                old = path.stat()
                if change == 'mtime':
                    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 1000000))
                elif change == 'ctime':
                    path.chmod(0o400)
                else:
                    replacement = path.with_name('replacement')
                    replacement.write_bytes(path.read_bytes())
                    os.utime(replacement, ns=(old.st_atime_ns, old.st_mtime_ns))
                    os.replace(replacement, path)
                before = opened.call_count
                h.verify_local_image(path)
                self.assertEqual(opened.call_count, before + 1, change)
        with path.open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaises(h.HostedError):
            h.verify_local_image(path)

    def test_cached_metadata_is_not_mutable_and_signed_hash_is_part_of_trust(self):
        path = self.download()
        checked = h.verify_local_image(path)
        checked['metadata']['members'][0]['sha256'] = '0' * 64
        self.assertNotEqual(h.verify_local_image(path)['metadata']['members'][0]['sha256'], '0' * 64)
        forged = {**self.manifest, 'sha256': '0' * 64}
        with self.assertRaises(c.ContractError):
            c.verify_archive(path, forged)

    def test_cancel_during_check_never_publishes_a_usable_download(self):
        cancel = h.CancelToken()
        def progress(event):
            if event.phase == 'verify' and event.total:
                cancel.cancel()
        with self.assertRaises(h.HostedError) as caught:
            self.download(progress=progress, cancel=cancel)
        self.assertEqual(caught.exception.code, 'canceled')
        self.assertFalse(list((self.cache.parent / 'downloads').glob('*.tar.bz2')))

    def test_cancel_during_extraction_cleans_all_temporary_images(self):
        path = self.download()
        package = u.validate_package(path)
        work = self.cache.parent / 'prepared'
        work.mkdir()
        token = h.CancelToken()
        def progress(event):
            if event.phase == 'prepare' and event.bytes:
                token.cancel()
        with self.assertRaises(c.ContractError):
            with c.operation(progress, token), u._package_images(package, workdir=work):
                self.fail('Canceled extraction returned images')
        self.assertEqual(list(work.iterdir()), [])


    def test_repacked_file_after_validation_must_match_original_compressed_hash(self):
        import io
        path = self.download()
        package = u.validate_package(path)
        raw = path.read_bytes()
        with tarfile.open(fileobj=io.BytesIO(raw)) as source, tarfile.open(path, 'w:bz2') as output:
            for member in source:
                member.mtime += 1
                output.addfile(member, source.extractfile(member))
        work = self.cache.parent / 'prepared'
        work.mkdir()
        with self.assertRaises(u.UpdateError):
            with u._package_images(package, workdir=work):
                self.fail('Repacked source bypassed its original transport hash')
        self.assertEqual(list(work.iterdir()), [])

    def test_slow_preparation_keeps_plain_progress_and_cancel_active(self):
        import jibo_tui as tui
        events = []
        def run(title, work, token, detail=''):
            return work(lambda event: events.append((time.monotonic(), event)), token), None
        def prepare():
            time.sleep(2.3)
        with patch.object(tui, 'run_with_progress', side_effect=run):
            tui._run_image_work(prepare, "Preparing Jibo's software")
        self.assertGreater(len(events), 8)
        self.assertLess(max(b[0] - a[0] for a, b in zip(events, events[1:])), 2)
        labels = [tui.format_progress(event) for _, event in events if event.phase == 'prepare']
        self.assertTrue(all(label.startswith("Preparing Jibo's software") for label in labels))
        self.assertGreater(len(set(labels)), 1)

    def test_signed_bad_downloads_never_reach_usb_and_quarantine_without_images(self):
        import copy
        import io
        import jibo_dfu as d
        original = self.server.files[self.row['url'].removeprefix(self.server.origin)]
        for change in ('member', 'signature', 'structure', 'size', 'hash'):
            with self.subTest(change=change):
                out = io.BytesIO()
                metadata_raw = None
                with tarfile.open(fileobj=io.BytesIO(original)) as source, tarfile.open(fileobj=out, mode='w:bz2') as archive:
                    for member in source:
                        raw = source.extractfile(member).read()
                        if change == 'member' and member.name.endswith('skills.ext4'):
                            raw = b'x' * len(raw)
                        if member.name.endswith('phoenix-flash.json'):
                            if change == 'signature':
                                metadata = c.verify_envelope(raw, 'metadata')
                                raw = self.server.envelope(metadata, bad_signature=True)
                            metadata_raw = raw
                        member.size = len(raw)
                        archive.addfile(member, io.BytesIO(raw))
                    if change == 'structure':
                        archive.addfile(tarfile.TarInfo('unexpected-hook'))
                raw = out.getvalue()
                row, manifest, index = copy.deepcopy(self.row), copy.deepcopy(self.manifest), copy.deepcopy(self.index)
                digest = self.server.sha(raw)
                url = self.server.origin + '/flash/images/' + row['id'] + '/' + digest + '.tar.bz2'
                for document in (row, manifest):
                    document.update(sha256=digest, size=len(raw), url=url)
                manifest['embedded_metadata_sha256'] = self.server.sha(metadata_raw)
                signed = self.server.envelope(manifest)
                row.update(manifest_size=len(signed), manifest_sha256=self.server.sha(signed))
                index['images'] = [row]
                self.server.files['/flash/index.json'] = self.server.envelope(index)
                self.server.files[row['manifest_url'].removeprefix(self.server.origin)] = signed
                if change == 'size':
                    raw += b'extra bytes'
                elif change == 'hash':
                    raw = b'x' * len(raw)
                self.server.files[url.removeprefix(self.server.origin)] = raw
                destination = self.cache.parent / change
                with patch.object(d, 'devices', side_effect=AssertionError('Unchecked image reached USB')) as usb:
                    with self.assertRaises(h.HostedError):
                        path = h.download_image(row['id'], dest_dir=destination)
                        d.flash_hosted_update(path, dfu_util='fake')
                usb.assert_not_called()
                self.assertFalse(list(destination.rglob('*.ext4')))
                self.assertFalse(list(destination.glob('*.tar.bz2')))
                if change != 'size':
                    self.assertEqual(len(list(destination.glob('*.bad-*'))), 1)

    def test_mutation_during_extraction_is_refused_and_cleans_images(self):
        path = self.download()
        package = u.validate_package(path)
        work = self.cache.parent / 'prepared'
        work.mkdir()
        original = u._extract_archive_images
        def mutate(*args, **kwargs):
            extracted = original(*args, **kwargs)
            with path.open('ab') as stream:
                stream.write(b'changed')
            return extracted
        with patch.object(u, '_extract_archive_images', side_effect=mutate):
            with self.assertRaises(u.UpdateError):
                with u._package_images(package, workdir=work):
                    self.fail('Changed source returned images')
        self.assertEqual(list(work.iterdir()), [])
