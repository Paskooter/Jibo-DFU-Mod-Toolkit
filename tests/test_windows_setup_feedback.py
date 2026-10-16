"""Executable PowerShell setup supervision checks; no WSL/USB or public network."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
PWSH = os.environ.get('PWSH') or shutil.which('pwsh') or shutil.which('powershell.exe')


def run_ps(code, tmp_path, timeout=12):
    if not PWSH:
        pytest.skip('requires PowerShell (set PWSH for portable installs)')
    script = tmp_path / 'probe.ps1'
    script.write_text("$ErrorActionPreference='Stop'\n. '" + str(ROOT / 'scripts/release/console.ps1') + "'\n" + code)
    return subprocess.run([PWSH, '-NoProfile', '-File', str(script)], capture_output=True,
                          text=True, timeout=timeout, env={**os.environ, 'TMPDIR': str(tmp_path)})


def test_never_finishing_native_command_is_killed_within_step_limit(tmp_path):
    # The old no-argument WaitForExit is cut off by this independent outer bound.
    code = """
$timer = [Diagnostics.Stopwatch]::StartNew()
$result = Invoke-ToolkitNative -File (Get-Process -Id $PID).Path -Arguments @('-NoProfile','-Command','Start-Sleep 60') -TimeoutSeconds 0.3
if (-not $result.TimedOut -or $result.ExitCode -ne 124 -or $timer.Elapsed.TotalSeconds -gt 5) { throw 'Hung child was not diagnosed and killed within the step budget' }
Write-Output 'PASS bounded hung command'
"""
    try:
        result = run_ps(code, tmp_path, timeout=8)
    except subprocess.TimeoutExpired:
        pytest.fail('old native wait exceeded the independent 8-second safety bound')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PASS bounded hung command' in result.stdout
