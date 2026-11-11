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


def test_readiness_fixture_never_repoints_process_systemroot():
    """Windows PowerShell 5.1 resolves GAC assemblies, csc.exe's signing
    provider and child processes through SystemRoot. Repointing it at the
    fixture made Add-Type, ConvertFrom-Json and taskkill fail on native
    windows-2022 while Linux pwsh/mono stayed green; only the wsl.exe path at
    the native capture boundary may be redirected."""
    import re
    text = (ROOT / 'scripts/release/test-readiness.ps1').read_text(encoding='utf-8')
    code = '\n'.join(line.split('#', 1)[0] for line in text.splitlines())
    assert not re.search(r'\$env:SystemRoot\s*=(?!=)', code, re.IGNORECASE)
    assert not re.search(r"(?:Set-Item|New-Item|Remove-Item)\b[^\n]*Env:\\?SystemRoot", code, re.IGNORECASE)
    assert "if ($File -ceq $script:ReadinessHostWsl)" in code
