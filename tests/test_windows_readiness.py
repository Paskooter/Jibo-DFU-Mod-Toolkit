"""Focused synthetic Windows readiness regressions; no WSL or system changes."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
CASES = [
    'native-streams', 'stderr-warning', 'stdout-warning', 'glibc-parse', 'glibc-old', 'glibc-command', 'native-start',
    'architecture', 'uid', 'uid-requested', 'wslg', 'ubuntu-id', 'ubuntu-version',
    'invalid-name', 'wsl1-private', 'wsl1-requested', 'missing-requested', 'duplicate-requested',
    'release-conflict-id', 'release-conflict-version', 'release-malformed', 'release-quote',
    'glibc-multiline', 'glibc-overflow', 'wslg-output',
    'ubuntu-missing-id', 'ubuntu-missing-version', 'ubuntu-malformed-version', 'ubuntu-overflow-version',
    'glibc-conflict', 'glibc-junk', 'glibc-stderr-data', 'architecture-conflict', 'uid-conflict',
    'release-duplicate', 'release-warning-junk', 'ready24', 'explicit-custom', 'automatic-allowlist',
    'native-timeout', 'native-probe-streams',
    'automatic-declined-check', 'mixed-requested',
    'wsl1-ubuntu', 'duplicate-ubuntu', 'mixed-ubuntu',
    'hypervisor', 'wsl-advice-install-suffix', 'wsl-advice-update-suffix',
    'wsl-advice-install-assignment', 'wsl-advice-update-choice',
    'post-import-readiness', 'native-data-warning',
]


@pytest.mark.parametrize('case', CASES)
def test_readiness_through_native_capture(tmp_path, case):
    pwsh = os.environ.get('PWSH') or shutil.which('pwsh') or shutil.which('powershell.exe')
    if not pwsh:
        pytest.skip('requires PowerShell (set PWSH to a local runtime)')
    assert pwsh is not None
    result = subprocess.run(
        [pwsh, '-NoProfile', '-NonInteractive', '-File',
         str(ROOT / 'scripts/release/test-readiness.ps1'), '-Case', case,
         '-BootstrapPath', os.environ.get('READINESS_BOOTSTRAP', str(ROOT / 'scripts/release/bootstrap.ps1'))],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
        env={**os.environ, 'TMPDIR': str(tmp_path)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PASS readiness: ' + case in result.stdout
