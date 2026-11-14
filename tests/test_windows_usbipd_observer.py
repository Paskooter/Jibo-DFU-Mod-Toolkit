"""Real C# PID publication/observer seam; Mono controls are NOT native acceptance.

The producer's file transport is held at two deterministic writer boundaries.
No timing repetition, USB/WSL calls, process signalling, or historical git objects.
"""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
MCS = shutil.which('mcs')
MONO = shutil.which('mono')
pytestmark = pytest.mark.skipif(not (MCS and MONO), reason='existing Mono compiler/runtime required; no downloads')


@pytest.fixture(scope='module')
def observer_control(tmp_path_factory):
    assert MCS is not None and MONO is not None
    folder = tmp_path_factory.mktemp('observer-compiler')
    producer = folder / 'producer.cs'
    # Keep the actual producer control flow. Pause only File's write transport;
    # the observer is compiled byte-for-byte, including its catch/fail behavior.
    producer.write_text('using File = PublicationFile;\n' +
                        (ROOT / 'tests/fixtures/usbipd_bounds_native.cs').read_text())
    exe = folder / 'observer-control.exe'
    sources = [producer, ROOT / 'tests/fixtures/usbipd_native_job.cs',
               ROOT / 'tests/fixtures/usbipd_observer_publication.cs']
    result = subprocess.run([MCS, '-langversion:5', '-warnaserror+',
                             '-main:ObserverPublicationControl', '-out:' + str(exe),
                             *(str(path) for path in sources)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    return exe


def run_control(observer_control, tmp_path, *args):
    assert MONO is not None
    result = subprocess.run([MONO, str(observer_control), args[0], str(tmp_path), *args[1:]],
                            capture_output=True, text=True, timeout=8)
    print(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PASS ' in result.stdout


@pytest.mark.parametrize('sharing', ['read-shared', 'sharing-conflict'])
@pytest.mark.parametrize('phase', ['bounds-pipe-preflight', 'bounds-hang'])
@pytest.mark.parametrize('role', ['parent', 'child'])
def test_pid_is_not_observable_until_writer_closed(observer_control, tmp_path, phase, role, sharing):
    run_control(observer_control, tmp_path, 'publish', phase, role, sharing)


@pytest.mark.parametrize('mode', ['locked', 'corrupt', 'identity'])
def test_observer_query_errors_still_fail_closed(observer_control, tmp_path, mode):
    run_control(observer_control, tmp_path, mode)


PWSH = os.environ.get('PWSH') or shutil.which('pwsh')


@pytest.mark.skipif(not PWSH, reason='existing PowerShell required; no downloads')
@pytest.mark.parametrize('phase', ['bounds-pipe-preflight', 'bounds-hang'])
def test_missing_observations_are_rejected_by_unchanged_phase_predicate(tmp_path, phase):
    assert PWSH is not None
    driver = tmp_path / 'predicate.ps1'
    driver.write_text(r'''
param($Target,$Phase)
$ErrorActionPreference='Stop'
$ast=[Management.Automation.Language.Parser]::ParseFile($Target,[ref]$null,[ref]$null)
foreach($name in @('Assert-True','Assert-Rejection','Assert-BoundsEvidence')) {
 $f=@($ast.EndBlock.Statements | Where-Object {$_ -is [Management.Automation.Language.FunctionDefinitionAst] -and $_.Name -ceq $name})[0]
 . ([scriptblock]::Create($f.Extent.Text))
}
$case=$Phase.Substring(7); $token='a'*32
$v=@{run_id=$token; id=$case; before_outer_cleanup=$true; helper_sha256=('a'*64); fixture_sha256=('b'*64); elapsed_ms=1000; limit_ms=4000; accepted=$false; entries=1; raw_output_leaked=$false; readers_released=$true; containment_stopped=$true; parent_observed=$true; child_observed=$true; parent_exited=$true; child_exited=$true; path_unchanged=$true; poison_entered=$false}
if($case -eq 'pipe-preflight') {$v.child_exited=$false; $v.containment_stopped=$false; $v.stdout_pending=$true; $v.stderr_pending=$true}
Assert-BoundsEvidence ([pscustomobject]$v) $case $token
foreach($role in @('parent','child')) {
 $bad=$v.Clone(); $bad[$role+'_observed']=$false
 $expected=if($case -eq 'pipe-preflight') {'Bounds pipe inheritance preflight not proven'} elseif($role -eq 'parent') {'Bounds exact parent survived API return'} else {'Bounds exact descendant survived API return'}
 Assert-Rejection {Assert-BoundsEvidence ([pscustomobject]$bad) $case $token} $expected
 Write-Output ('PASS '+$Phase+' rejects missing '+$role+': '+$expected)
}
''')
    result = subprocess.run([PWSH, '-NoProfile', '-File', str(driver),
                             str(ROOT / 'scripts/release/test-usbipd-native.ps1'), phase],
                            capture_output=True, text=True, timeout=10)
    print(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count('PASS ' + phase + ' rejects missing ') == 2
