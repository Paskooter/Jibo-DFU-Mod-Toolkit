"""Receipt-gate fixtures only; these files are not render/screenshot evidence."""
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PWSH = os.environ.get('PWSH') or shutil.which('pwsh') or shutil.which('powershell.exe')


def fixture_receipts(tmp_path):
    for lane, count in [('standard', 144), ('high-contrast', 53)]:
        folder = tmp_path / lane
        folder.mkdir()
        names = [f'fixture-{i}' for i in range(count)]
        for name in names:
            (folder / (name + '.png')).touch()  # Existence gate only, never owner evidence.
        phases = ['setup-96', 'support-96', 'fallback-setup', 'launcher-text', 'fallback-launcher', 'clipboard', 'font-control']
        if lane == 'standard':
            phases += ['setup-144', 'setup-192', 'support-144', 'support-192', 'simulated-hc', 'scaled-forms', 'live']
        receipt: dict[str, Any] = dict(failed=False, problems=[], files=[n + ' 1x1' for n in names], phases={n: {'status': 'ok'} for n in phases},
                       clipboard='ok', fallback={'setup_window': 'ok', 'launcher': 'ok'}, font_lifetime_control='ok',
                       scaled_forms='ok', high_contrast_active=True, high_contrast_switched=True, high_contrast_restored=True, live={})
        for kind in ['stall', 'failure']:
            receipt['live'].update({kind + '_answer': 'stopped', kind + '_clipboard': 'ok', kind + '_layout': 'ok', kind + '_dialog': 'ok',
                                    kind + '_dpi': 96, kind + '_forced_dpi': 0})
        (folder / 'receipt.json').write_text(json.dumps(receipt))


def validate(tmp_path):
    if not PWSH:
        pytest.skip('requires PowerShell')
    return subprocess.run([PWSH, '-NoProfile', '-File', str(ROOT / 'scripts/release/test-preview-receipts.ps1'), '-OutDir', str(tmp_path)],
                          capture_output=True, text=True, timeout=30)


def test_complete_native100_receipts_are_accepted(tmp_path):
    fixture_receipts(tmp_path)
    result = validate(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('mutation', ['forced125', 'actual120', 'no-copy', 'still-open', 'disposed-font', 'missing-font', 'missing-scaled', 'missing-phase', 'timeout', 'no-high-contrast'])
def test_unhealthy_native_receipts_are_rejected(tmp_path, mutation):
    fixture_receipts(tmp_path)
    path = tmp_path / ('high-contrast' if mutation == 'no-high-contrast' else 'standard') / 'receipt.json'
    r = json.loads(path.read_text())
    if mutation == 'forced125':
        r['live']['stall_forced_dpi'] = 120
    elif mutation == 'actual120':
        r['live']['stall_dpi'] = 120
    elif mutation == 'no-copy':
        del r['live']['failure_clipboard']
    elif mutation == 'still-open':
        r['live']['stall_answer'] = 'still open'
    elif mutation == 'disposed-font':
        r['font_lifetime_control'] = 'retained=True; disposed font reproduced'
    elif mutation == 'missing-font':
        del r['font_lifetime_control']
    elif mutation == 'missing-scaled':
        del r['scaled_forms']
    elif mutation == 'missing-phase':
        del r['phases']['live']
    elif mutation == 'timeout':
        r['phases']['font-control']['status'] = 'timeout'
    elif mutation == 'no-high-contrast':
        r['high_contrast_active'] = False
    path.write_text(json.dumps(r))
    result = validate(tmp_path)
    assert result.returncode != 0, 'unhealthy receipt accepted: ' + mutation
