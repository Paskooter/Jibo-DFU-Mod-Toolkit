"""Portable structural controls only; never native Windows acceptance."""
import os
from pathlib import Path
import re
import subprocess
import shutil
import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / 'scripts/release/test-usbipd-native.ps1'
PWSH = os.environ.get('PWSH') or shutil.which('pwsh')
OVERLAY = os.environ.get('USBIPD_SOURCE_ROOT', str(ROOT))


def test_bounds_matrix_is_explicit_and_preserves_old_cases():
    text = HARNESS.read_text()
    for name in ('healthy', 'broken', 'hang', 'pipe', 'noisy-stderr', 'blocked-output',
                 'pipe-preflight', 'remove-cap', 'first-fast', 'blocked-discovery',
                 'blocked-filesystem', 'shared-remaining', 'shared-cutoff'):
        assert "'" + name + "'" in text, 'missing native bounds case: ' + name
    assert "'deadline-native'" in text
    assert "'after-deadline'" in text
    assert 'Assert-BoundsEvidence' in text
    assert 'NativeBoundsJob' in text


@pytest.mark.skipif(not PWSH, reason='already installed PowerShell required; no downloads')
def test_native_modes_refuse_linux(tmp_path):
    if os.name == 'nt':
        pytest.skip('Linux fail-closed control only')
    assert PWSH is not None
    for mode in ('Run', 'Validate'):
        result = subprocess.run([PWSH, '-NoProfile', '-File', str(HARNESS), '-Mode', mode,
                                 '-ToolkitRoot', OVERLAY, '-OutputRoot', str(tmp_path / mode)],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 1
        assert 'Native acceptance requires Windows PowerShell 5.1' in result.stderr
        assert not (tmp_path / mode).exists()


def test_enclosing_job_has_no_journal_signal_authority():
    source = (ROOT / 'tests/fixtures/usbipd_native_job.cs').read_text()
    assert 'AssignProcessToJobObject(job, process.Handle)' in source
    assert 'TerminateJobObject(job, 1)' in source
    assert 'status.Active == 0' in source
    assert 'Kill(' not in source
    assert 'IsBackground = true' in source
    assert 'GetProcessById' in source  # observation only; never signalling
    assert not re.search(r'GetProcessById.*(?:Kill|Terminate)', source)


@pytest.mark.skipif(not PWSH, reason='already installed PowerShell required; no downloads')
def test_read_only_liveness_preserves_json_datetime_kind(tmp_path):
    assert PWSH is not None
    driver = tmp_path / 'read-only.ps1'
    driver.write_text(r'''
param($Target,$RecordPath)
$ErrorActionPreference='Stop'
$ast=[Management.Automation.Language.Parser]::ParseFile($Target,[ref]$null,[ref]$null)
foreach($name in @('Read-Json','Save-Json','Test-RecordedProcessAlive')) {
 $f=@($ast.EndBlock.Statements | Where-Object {$_ -is [Management.Automation.Language.FunctionDefinitionAst] -and $_.Name -ceq $name})[0]
 . ([scriptblock]::Create($f.Extent.Text))
}
$p=[Diagnostics.Process]::GetCurrentProcess()
try {
 Save-Json $RecordPath @{pid=$PID; exe=$p.MainModule.FileName; utc=$p.StartTime.ToUniversalTime().ToString('o')}
 if(-not (Test-RecordedProcessAlive (Read-Json $RecordPath))) {throw 'JSON timestamp changed a live identity into absence'}
 $bad=Read-Json $RecordPath; $bad.utc='not-a-date'; $rejected=$false
 try {$null=Test-RecordedProcessAlive $bad} catch {$rejected=$true}
 if(-not $rejected) {throw 'Malformed timestamp became absence'}
 'PASS read-only liveness, no signalling'
} finally {$p.Dispose()}
''')
    result = subprocess.run([PWSH, '-NoProfile', '-File', str(driver), str(HARNESS),
                             str(tmp_path / 'observation.json')], capture_output=True,
                            text=True, timeout=10, env={**os.environ, 'TZ': 'America/New_York'})
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PASS read-only liveness, no signalling' in result.stdout
