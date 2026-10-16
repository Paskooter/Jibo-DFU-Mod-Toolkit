"""Signed loopback downloads through the real flash path; USB is always fake."""
from contextlib import ExitStack, redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tarfile
from unittest.mock import patch

from test_hosted_catalog import HostedFixture
import jibo_hosted_updates as h
import jibo_flash_contract as c
import jibo_updates as u
import jibo_dfu as d


class FlashFlowTests(HostedFixture):
    def run_flash(self, *, readback=False):
        root = self.cache.parent
        operation = root / 'operation'
        operation.mkdir()
        writes, reads, order, opens = [], [], [], []
        real_open = tarfile.open
        def opened(*args, **kwargs):
            mode = kwargs.get('mode', args[1] if len(args) > 1 else 'r')
            if mode.startswith('r'):
                opens.append(mode)
            return real_open(*args, **kwargs)
        def transfer(argv, **kwargs):
            writes.append(argv[argv.index('-a') + 1])
            order.append('write')
        def backup(*args, **kwargs):
            order.append('backup')
            return {'image': 'synthetic-rollback.img'}
        def upload(util, port, name, size, target, *args):
            reads.append(name)
            saved = json.loads((operation / 'update-manifest.json').read_text())
            return next(w['candidate_sha256'] for w in saved['writes'] if w['partition'] == name)
        with ExitStack() as stack:
            for target, name, options in (
                (tarfile, 'open', {'side_effect': opened}),
                (d, 'devices', {'return_value': [{'port': '1-1', 'state': 'dfu'}]}),
                (d, '_dfu_context', {'return_value': ('1-1', list(d.UPDATE_ORDER), 'synthetic-device', '')}),
                (d, '_has_unique_device_tag', {'return_value': True}),
                (d, '_read_gpt_capacities', {'return_value': {**u.KNOWN_CAPACITIES, 'skills': 10991139328}}),
                (d, '_new_operation_dir', {'return_value': operation}),
                (d, 'backup_var', {'side_effect': backup}),
                (d, '_verify_var_backup_for_robot', {'return_value': None}),
                (d, '_upload_partition', {'side_effect': upload}),
                (d, 'run_with_progress', {'side_effect': transfer}),
                (d, 'run', {'return_value': ''}),
                (u, '_run_checked', {'return_value': ''}),
                (u, '_filesystem_geometry', {'return_value': (4096, 8)}),
                (u, '_read_ext4_file', {'return_value': b'#!/bin/sh\n# synthetic'}),
                (u, '_runs_first_boot_resize', {'return_value': True}),
            ):
                stack.enter_context(patch.object(target, name, **options))
            stack.enter_context(redirect_stdout(io.StringIO()))
            stack.enter_context(redirect_stderr(io.StringIO()))
            path = h.download_image(self.row['id'], dest_dir=root / 'downloads')
            if readback:
                result = d.flash_hosted_update(path, var_policy='without-calibration', dfu_util='fake',
                                               confirmation='FLASH UPDATE', unsafe_without_calibration=True,
                                               verify_readback=True)
            else:
                result = d.flash_hosted_update(path, var_policy='without-calibration', dfu_util='fake',
                                               confirmation='FLASH UPDATE', unsafe_without_calibration=True)
        print('FLASHFLOW_MEASURE', json.dumps({'tar_read_opens': len(opens), 'modes': opens,
                                               'post_write_reads': len(reads)}))
        self.assertEqual(writes, list(d.UPDATE_ORDER))
        self.assertLess(order.index('backup'), order.index('write'))
        self.assertFalse(list(operation.glob('prepared-*')))
        return result, opens, reads

    def test_phoenix_defaults_to_transfer_completion_without_readback(self):
        result, opens, reads = self.run_flash()
        self.assertEqual(reads, [])
        saved = json.loads(Path(result['manifest']).read_text())
        self.assertEqual(saved['readback_policy'], 'DFU transfer completion')
        self.assertEqual(result['status'], 'transferred; reset requested')

    def test_download_to_first_write_uses_at_most_two_tar_passes(self):
        result, opens, reads = self.run_flash()
        self.assertLessEqual(len(opens), 2, opens)

    def test_explicit_readback_still_reads_each_written_partition(self):
        result, opens, reads = self.run_flash(readback=True)
        self.assertEqual(reads, list(d.UPDATE_ORDER))
        self.assertEqual(result['status'], 'verified; reset requested')
