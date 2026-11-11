"""Workspace bounds include one full replay copy, not only compact prefixes."""
from pathlib import Path
from types import SimpleNamespace

import pytest

import jibo_dfu as dfu
import jibo_flash_contract as contract


@pytest.fixture
def metadata():
    image_id = 'synthetic-journal-budget'
    sizes = {'rootfs': 8 * 1024**2, 'services': 16 * 1024**2,
             'skills': 32 * 1024**2, 'var': 2 * 1024**2}
    value = {
        'schema': 1, 'kind': 'phoenix-flash-metadata-v1', 'id': image_id,
        'base_release': '13.0.0-test-only',
        'components': dict.fromkeys(('os', 'services', 'oobe-config', '@be/be'), '0.0.0-test-only'),
        'profile': 'jibo-t124-stock-gpt-13-v1', 'strategy': 'compact-prefix-v1',
        'members': [{'name': f'phoenix-{image_id}/flash_jibo/output/images/{role}.ext4',
                     'role': role, 'size': size, 'sha256': 'a' * 64,
                     'geometry': {'block_size': 4096, 'block_count': size // 4096, 'features': []}}
                    for role, size in sizes.items()],
        'var_policies': ['keep-calibration', 'without-calibration'],
        'official_base': {'release': '13.0.0-test-only', 'sha256': 'b' * 64, 'provenance': 'SYNTHETIC'},
        'sources': dict.fromkeys(('repoint_commit', 'helper_commit', 'be_commit'), 'c' * 40),
        'recipe': {'revision': 'synthetic', 'toolchain_sha256': 'd' * 64},
        'gate_receipts': {'synthetic': 'e' * 64},
    }
    return contract.validate_metadata(value)


def _pre_replay_budget(metadata, var_capacity):
    sizes = {m['role']: m['size'] for m in metadata['members']}
    return (2 * sum(sizes.values()) + 3 * var_capacity + max(max(sizes.values()), var_capacity) +
            2 * sizes['rootfs'] + sizes['skills'] + 1024**3)


def test_workspace_reserves_a_var_replay_copy_beyond_existing_candidates(metadata):
    var_capacity = 524288000
    assert contract.workspace_bytes(metadata, var_capacity) >= _pre_replay_budget(metadata, var_capacity) + var_capacity


def test_declared_workspace_floor_cannot_reduce_replay_budget(metadata):
    var_capacity = 524288000
    metadata['workspace_bytes'] = 1
    required = _pre_replay_budget(metadata, var_capacity) + var_capacity
    assert contract.workspace_bytes(metadata, var_capacity) >= required
    metadata['workspace_bytes'] = required * 2
    assert contract.workspace_bytes(metadata, var_capacity) == required * 2


def test_hosted_flash_preflight_charges_full_largest_gpt_replay_copy_before_preparation(
        tmp_path, monkeypatch, metadata):
    capacities = {'rootfsA': 1024**3, 'rootfsB': 1024**3, 'services': 1024**3,
                  'skills': 4 * 1024**3, 'var': 524288000}
    # The compact source members are smaller than the robot's partition backups.
    required = _pre_replay_budget(metadata, capacities['var']) + capacities['skills']
    monkeypatch.setattr(dfu, 'BACKUP_ROOT', tmp_path / 'backups')
    monkeypatch.setattr(dfu.shutil, 'disk_usage', lambda _: SimpleNamespace(free=required - 1))
    monkeypatch.setattr(dfu.updates, 'validate_package', lambda _: SimpleNamespace(
        source=tmp_path / 'package', version='synthetic', phoenix={**metadata, '_metadata_sha256': 'a' * 64}))
    monkeypatch.setattr(dfu, 'devices', lambda: [{'port': 'synthetic-1', 'state': 'dfu'}])
    monkeypatch.setattr(dfu, '_dfu_context', lambda *a, **kw: (
        'synthetic-1', list(capacities) + [dfu.MARKER], 'serial-sha256:' + 'a' * 64, ''))
    monkeypatch.setattr(dfu, '_read_gpt_capacities', lambda *a: capacities)

    def forbidden(*_args, **_kwargs):
        raise AssertionError('No preparation, backup or transfer is allowed after a failed space preflight')

    monkeypatch.setattr(dfu.updates, 'prepare_compact_images', forbidden)
    monkeypatch.setattr(dfu, 'backup_var', forbidden)
    monkeypatch.setattr(dfu, 'run_with_progress', forbidden)
    monkeypatch.setattr(dfu, 'run', forbidden)
    with pytest.raises(dfu.DfuError, match='Not enough free space'):
        dfu.flash_update('synthetic-package', dfu_util='offline', out=tmp_path / 'operation', dry_run=True)
    assert not (tmp_path / 'operation').exists()
