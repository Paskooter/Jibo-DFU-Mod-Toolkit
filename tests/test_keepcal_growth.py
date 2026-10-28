"""Real ext4 controls for first-boot growth; no USB, mounts or robots."""
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import jibo_dfu as dfu
import jibo_images as images
from calibration_support import FILES, IDENTITY, PATHS, make_image
from test_updates import STOCK_13_RESIZE_SCRIPT, compact_set

MARKER = '/etc/first_boot_resize.done'
SCRIPT = '/etc/first_boot_resize'
pytestmark = pytest.mark.skipif(
    not all(shutil.which(n) for n in ('mke2fs', 'debugfs', 'e2fsck', 'resize2fs', 'dumpe2fs')),
    reason='e2fsprogs required')


def inputs(root, package_marker):
    calibration = {PATHS[k]: FILES[k] for k in ('left', 'right', 'inter_camera', 'imu', 'audio')}
    backup = make_image(root, 'backup.img', {**calibration, '/jibo/identity.json': IDENTITY,
        MARKER: b'old completed boot\n', '/jibo/credentials.json': b'{"region":"api"}'})
    stock = make_image(root, 'stock.img', {**{PATHS[k]: FILES[k] for k in ('left', 'right', 'inter_camera')},
        SCRIPT: STOCK_13_RESIZE_SCRIPT, **({MARKER: b''} if package_marker else {})})
    return stock, backup, calibration


def build(root, stock, backup, source):
    candidate = root / 'candidate.img'
    if source == 'local':
        result = images.build_factory_fresh_var(stock, backup, candidate)
    else:
        from jibo_calibration_restore import build_fresh_var
        import jibo_calibration as core
        result = build_fresh_var(stock, backup, candidate,
            {k: FILES[k] for k in ('left', 'right', 'inter_camera', 'imu', 'audio')}, core)
    assert images.sha256_file(candidate) == result['sha256']
    return candidate


def blocks(image):
    text = subprocess.run(['dumpe2fs', '-h', str(image)], check=True, capture_output=True, text=True).stdout
    return int(next(line.split(':', 1)[1] for line in text.splitlines() if line.startswith('Block count:')))


def run_stock_growth(root, candidate):
    """Execute the image's stock shell against disposable ext4 *files*."""
    boot = root / 'boot'
    boot.mkdir()
    script = images._extract(candidate, SCRIPT, root / 'script').decode('ascii')
    marker = boot / 'first_boot_resize.done'
    saved_marker = images._extract(candidate, MARKER, root / 'marker', optional=True)
    if saved_marker is not None:
        marker.write_bytes(saved_marker)
    script = script.replace('/var/etc/first_boot_resize.done', str(marker))
    partitions, before = {}, {}
    for index in (1, 2, 5, 4, 6):
        target = make_image(root, 'p%d.img' % index, {})
        before[index] = blocks(target)
        with target.open('r+b') as stream:
            stream.truncate(target.stat().st_size * 2)
        partitions[index] = target
        script = script.replace('/dev/mmcblk0p%d' % index, str(target))
    script = script.replace('/usr/sbin/resize2fs', shutil.which('resize2fs'))
    script = script.replace('/usr/sbin/e2fsck', shutil.which('e2fsck'))
    for command in ('/bin/umount /var', '/bin/umount /usr/local', '/bin/umount /opt', '/bin/mount -a', '/bin/sync'):
        script = script.replace(command, ':')
    result = subprocess.run(['sh', '-c', script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    actual = {index: blocks(path) for index, path in partitions.items()}
    assert actual == {index: count * 2 for index, count in before.items()}, result.stdout + result.stderr
    assert marker.is_file()


@pytest.mark.parametrize('source', ['local', 'remote'])
def test_keepcal_package_marker_rearmed_and_stock_grows_all_five_partitions(tmp_path, source):
    stock, backup, calibration = inputs(tmp_path, package_marker=True)
    original = {path: images.sha256_file(path) for path in (stock, backup)}
    candidate = build(tmp_path, stock, backup, source)
    for path, raw in {**calibration, '/jibo/identity.json': IDENTITY}.items():
        assert images._extract(candidate, path, tmp_path / 'item') == raw
    assert images._extract(candidate, '/jibo/credentials.json', tmp_path / 'absent', optional=True) is None
    run_stock_growth(tmp_path, candidate)
    assert images._extract(candidate, MARKER, tmp_path / 'absent', optional=True) is None
    assert {path: images.sha256_file(path) for path in original} == original


def test_backup_only_marker_is_not_imported_even_without_the_fix(tmp_path):
    stock, backup, _ = inputs(tmp_path, package_marker=False)
    candidate = build(tmp_path, stock, backup, 'local')
    assert images._extract(candidate, MARKER, tmp_path / 'absent', optional=True) is None
    run_stock_growth(tmp_path, candidate)


@pytest.mark.parametrize('policy', ['keep-calibration', 'without-calibration'])
def test_flash_marker_policy_and_failure_before_any_usb_write(tmp_path, policy, failure=None):
    stock, backup, _ = inputs(tmp_path, package_marker=True)
    tag = 'serial-sha256:' + 'a' * 64
    manifest = tmp_path / 'backup-manifest.json'
    digest = images.sha256_file(backup)
    manifest.write_text(json.dumps({'kind': 'jibo-var-backup', 'device_tag': tag, 'sha256': digest}))
    saved = {'image': str(backup), 'manifest': str(manifest), 'sha256': digest}
    capacities = {name: stock.stat().st_size for name in dfu.UPDATE_ORDER}
    package = SimpleNamespace(source=tmp_path / 'package', version='13.0.0', phoenix=None)
    written = {}
    def transfer(argv, **kwargs):
        written[argv[argv.index('-a') + 1]] = Path(argv[argv.index('-D') + 1]).read_bytes()
    from contextlib import ExitStack
    with ExitStack() as stack:
        for owner, name, kwargs in [
            (dfu, 'EXPECTED_VAR_SIZE', {'new': stock.stat().st_size}),
            (dfu, 'BACKUP_ROOT', {'new': tmp_path / 'saved'}),
            (dfu.updates, 'validate_package', {'return_value': package}),
            (dfu.updates, 'prepare_compact_images', {'return_value': compact_set(dict.fromkeys(dfu.UPDATE_ORDER, stock))}),
            (dfu, 'devices', {'return_value': [{'port': '1-1', 'state': 'dfu'}]}),
            (dfu, '_dfu_context', {'return_value': ('1-1', list(capacities) + [dfu.MARKER], tag, '')}),
            (dfu, '_read_gpt_capacities', {'return_value': capacities}),
            (dfu, '_package_content_sha256', {'return_value': 'b' * 64}),
            (dfu, 'backup_var', {'return_value': saved}),
            (dfu, 'run_with_progress', {'side_effect': transfer}),
            (dfu, 'run', {'return_value': ''}),
        ]:
            stack.enter_context(patch.object(owner, name, **kwargs))
        if failure:
            original_debugfs = images._run_debugfs
            def fail_marker(image, command, **kwargs):
                if command == 'rm ' + MARKER:
                    if failure == 'noop':
                        return ''
                    raise images.ImageError('Synthetic debugfs removal failure')
                return original_debugfs(image, command, **kwargs)
            stack.enter_context(patch.object(images, '_run_debugfs', side_effect=fail_marker))
            with pytest.raises(dfu.DfuError, match='Could not arm first-boot partition growth') as raised:
                dfu.flash_update('package', policy, port='1-1', dfu_util='offline', out=tmp_path / 'operation',
                    confirmation='FLASH UPDATE')
            assert 'No partition write was attempted' in str(raised.value)
            assert 'copy these details' in str(raised.value)
            assert 'Update record:' in str(raised.value)
            assert not written
            record = json.loads((tmp_path / 'operation/update-manifest.json').read_text())
            assert record['status'] == 'failed' and record['writes'] == []
            assert images.sha256_file(backup) == digest
            return
        result = dfu.flash_update('package', policy, port='1-1', dfu_util='offline', out=tmp_path / 'operation',
            confirmation='FLASH UPDATE', unsafe_without_calibration=policy == 'without-calibration')
    assert result['transferred_partitions'] == list(dfu.UPDATE_ORDER)
    if policy == 'without-calibration':
        assert written['var'] == stock.read_bytes()
    else:
        captured = tmp_path / 'written.img'
        captured.write_bytes(written['var'])
        assert images._extract(captured, MARKER, tmp_path / 'absent', optional=True) is None
    assert images.sha256_file(backup) == digest


@pytest.mark.parametrize('failure', ['error', 'noop'])
def test_failed_marker_removal_stops_flash_with_copyable_details(tmp_path, failure):
    test_flash_marker_policy_and_failure_before_any_usb_write(tmp_path, 'keep-calibration', failure)


@pytest.mark.parametrize('source', ['local', 'remote'])
@pytest.mark.parametrize('kind', ['directory', 'symlink'])
def test_unsafe_marker_inode_refused_without_changing_inputs(tmp_path, source, kind):
    stock, backup, _ = inputs(tmp_path, package_marker=False)
    if kind == 'directory':
        images._run_debugfs(stock, 'mkdir ' + MARKER, writable=True)
    else:
        images._run_debugfs(stock, 'symlink ' + MARKER + ' /jibo/identity.json', writable=True)
    original = {p: images.sha256_file(p) for p in (stock, backup)}
    with pytest.raises(images.ImageError, match='completed-boot marker is not a regular file'):
        build(tmp_path, stock, backup, source)
    assert not (tmp_path / 'candidate.img').exists()
    assert {p: images.sha256_file(p) for p in original} == original
