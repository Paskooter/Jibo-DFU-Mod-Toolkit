"""Verified synthetic backup selection; no devices, network or mounts."""
import json
import os
from pathlib import Path
import shutil

import pytest

import jibo_calibration as calibration
import jibo_dfu as dfu
import jibo_images as images
from calibration_support import (
    FACTORY_RECORD, FILES, GENERIC_CAMERA, IDENTITY, PATHS, POINTERCAL,
    journal_pending, make_image,
)

TAG = 'serial-sha256:' + 'a' * 64
TOOLS = ('debugfs', 'dumpe2fs', 'e2fsck', 'mke2fs')
pytestmark = pytest.mark.skipif(not all(shutil.which(n) for n in TOOLS), reason='e2fsprogs required')


def _receipt(directory, image, *, partition=None, modified=10):
    record = {'kind': 'jibo-partition-backup' if partition else 'jibo-var-backup',
              'device_tag': TAG, 'sha256': images.sha256_file(image),
              'size_bytes': image.stat().st_size, 'image': image.name}
    if partition:
        record['partition'] = partition
    manifest = directory / 'backup-manifest.json'
    manifest.write_text(json.dumps(record))
    os.utime(manifest, (modified, modified))
    return {'image': str(image), 'manifest': str(manifest), 'sha256': record['sha256']}


@pytest.mark.parametrize('partition,key,raw', [
    ('rootfsA', 'touchscreen', POINTERCAL), ('skills', 'factory_record', FACTORY_RECORD),
])
def test_partition_selection_finds_older_journal_only_item_after_newer_empty_backup(
        tmp_path, monkeypatch, partition, key, raw):
    saved = tmp_path / 'saved'
    newer = saved / 'partition-backup-002-newer-empty'
    older = saved / 'partition-backup-001-older-journal'
    newer.mkdir(parents=True)
    older.mkdir()
    before = make_image(tmp_path, 'before.img', {})
    after = tmp_path / 'after.img'
    shutil.copyfile(before, after)
    spec = next(s for s in calibration.ITEMS if s.key == key)
    metadata = {'mode': '0100640', 'uid': '123', 'gid': '10'}
    images._put_calibration_item(after, spec, raw, metadata, tmp_path)
    pending = journal_pending(older, before, after, name='partition.img')
    newer_image = make_image(newer, 'partition.img', {})
    _receipt(newer, newer_image, partition=partition, modified=20)
    expected = _receipt(older, pending, partition=partition, modified=10)
    assert images._extract(pending, PATHS[key], tmp_path / 'raw', optional=True) is None
    stock = make_image(tmp_path, 'stock.img', {})
    capacities = dict.fromkeys(dfu.UPDATE_ORDER, pending.stat().st_size)
    prepared = dict.fromkeys(('rootfsA', 'rootfsB', 'skills'), stock)
    workspace = tmp_path / 'accounted'
    workspace.mkdir()
    original = {p: images.sha256_file(p) for p in (pending, newer_image, stock)}
    monkeypatch.setattr(dfu, 'BACKUP_ROOT', saved)
    enumerated = list(dfu._saved_calibration_partitions(TAG, (partition,), capacities))
    assert [Path(b['image']) for b in enumerated] == [newer_image, pending]
    report = dfu._prepare_partition_calibration(prepared, capacities, TAG, workspace)
    group = 'skills' if partition == 'skills' else 'rootfs'
    assert report['status'][key] == 'restored'
    assert report['sources'][group] == expected
    for name in (('skills',) if partition == 'skills' else ('rootfsA', 'rootfsB')):
        assert images._extract(prepared[name], PATHS[key], tmp_path / 'read') == raw
        assert images._file_metadata(prepared[name], PATHS[key]) == metadata
    assert {p: images.sha256_file(p) for p in original} == original
    assert 'needs_recovery' in images._run_debugfs(pending, 'features').split()
    assert not list(workspace.glob('calibration-check-*'))
    assert not list(workspace.glob('jibo-journal-replay-*'))


def test_factory_selection_skips_verified_generic_backup_for_same_robot(tmp_path, monkeypatch):
    saved = tmp_path / 'saved'
    genuine_dir = saved / 'genuine'
    generic_dir = saved / 'generic'
    genuine_dir.mkdir(parents=True)
    generic_dir.mkdir()
    cameras = ('left', 'right', 'inter_camera')
    genuine = make_image(genuine_dir, 'var.img', {
        **{PATHS[k]: FILES[k] for k in cameras}, images.FACTORY_IDENTITY_PATH: IDENTITY})
    generic = make_image(generic_dir, 'var.img', {
        **{PATHS[k]: GENERIC_CAMERA[k] for k in cameras}, images.FACTORY_IDENTITY_PATH: IDENTITY})
    stock = make_image(tmp_path, 'stock.img', {PATHS[k]: GENERIC_CAMERA[k] for k in cameras})
    expected = _receipt(genuine_dir, genuine, modified=10)
    preferred = _receipt(generic_dir, generic, modified=20)
    original = {p: images.sha256_file(p) for p in (genuine, generic, stock)}
    monkeypatch.setattr(dfu, 'BACKUP_ROOT', saved)
    monkeypatch.setattr(dfu, 'EXPECTED_VAR_SIZE', genuine.stat().st_size)
    selected = dfu._select_factory_calibration_backup(TAG, preferred, stock)
    assert Path(selected['manifest']) == Path(expected['manifest'])
    assert {p: images.sha256_file(p) for p in original} == original
