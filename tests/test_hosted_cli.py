from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from test_hosted_catalog import HostedFixture
import jibo_dfu as d
import jibo_hosted_updates as h


class HostedCliTests(HostedFixture):
    def invoke(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(d, 'devices', side_effect=AssertionError('download/list must never discover USB')), \
                patch.object(d, 'tool', side_effect=AssertionError('download/list must never resolve USB tools')), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            result = d.main(argv)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_online_list_has_zero_usb_calls(self):
        dest = self.cache.parent / 'updates'
        result, stdout, stderr = self.invoke(['list-hosted-updates', '--directory', str(dest)])
        self.assertEqual(result, 0, stderr)
        data = json.loads(stdout)
        self.assertEqual(data['images'][0]['source'], 'hosted')
        self.assertTrue(data['images'][0]['recommended'])
        self.assertFalse(list(dest.glob('*.tar.bz2')))

    def test_explicit_offline_list_uses_authenticated_cache_without_network(self):
        dest = self.cache.parent / 'updates'
        h.fetch_catalog(cache_dir=dest / '.hosted')
        with patch.object(h, '_open', side_effect=AssertionError('offline made a network request')):
            result, stdout, stderr = self.invoke(['list-hosted-updates', '--offline', '--directory', str(dest)])
            self.assertEqual(result, 0, stderr)
            self.assertTrue(json.loads(stdout)['from_cache'])
            self.assertFalse(list(dest.glob('*.tar.bz2')))

    def test_removed_hosted_dry_run_is_rejected_before_download_or_usb(self):
        with patch.object(h, 'download_image', side_effect=AssertionError('dry-run auto-downloaded')):
            with self.assertRaises(SystemExit) as stopped:
                self.invoke(['flash-hosted-update', '--image', self.row['id'],
                             '--dry-run', '--directory', str(self.cache.parent / 'updates')])
        self.assertEqual(stopped.exception.code, 2)

    def test_unsafe_policy_needs_typed_confirmation_before_network_and_usb(self):
        with patch.object(h, '_open', side_effect=AssertionError('disabled policy fetched network')):
            result, stdout, stderr = self.invoke(['flash-hosted-update', '--image', 'latest',
                                                  '--without-calibration'])
        self.assertEqual(result, 1)
        self.assertIn('--confirm exactly FLASH UPDATE', stderr)

    def test_dependency_and_cancel_exit_codes_are_stable(self):
        for code, expected in (('dependency', 3), ('canceled', 130), ('signature', 1)):
            with patch.object(h, 'fetch_catalog', side_effect=h.HostedError(code, 'SYNTHETIC refusal')):
                result, stdout, stderr = self.invoke(['list-hosted-updates'])
            self.assertEqual(result, expected)
            self.assertIn(code, stderr)


if __name__ == '__main__':
    unittest.main()
