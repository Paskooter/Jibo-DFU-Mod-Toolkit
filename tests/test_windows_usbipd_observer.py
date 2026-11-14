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


@pytest.mark.parametrize('sharing', ['read-shared', 'sharing-conflict'])
@pytest.mark.parametrize('phase', ['bounds-pipe-preflight', 'bounds-hang'])
@pytest.mark.parametrize('role', ['parent', 'child'])
def test_republication_keeps_previous_complete_identity_until_writer_closed(observer_control, tmp_path, phase, role, sharing):
    run_control(observer_control, tmp_path, 'publish-repeat', phase, role, sharing)


@pytest.fixture(scope='module')
def native_bounds_fixture(tmp_path_factory):
    assert MCS is not None
    exe = tmp_path_factory.mktemp('real-bounds-fixture') / 'usbipd.exe'
    result = subprocess.run([MCS, '-langversion:5', '-warnaserror+', '-out:' + str(exe),
                             str(ROOT / 'tests/fixtures/usbipd_bounds_native.cs')],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    return exe


@pytest.mark.parametrize('second_mode,second_exit,second_stdout',
                         [('healthy', 0, '5.3.0\n'), ('broken', 17, '')])
def test_real_fixture_repeated_probe_preserves_second_identity_and_readiness(
        native_bounds_fixture, tmp_path, second_mode, second_exit, second_stdout):
    assert MONO is not None
    # Both probes share the phase directory, just like the resolver's fallback.
    # Do not delete/clear the first journal or its entry before the second call.
    for index, (mode, expected_exit, expected_stdout) in enumerate(
            [('broken', 17, ''), (second_mode, second_exit, second_stdout)], 1):
        with subprocess.Popen([MONO, str(native_bounds_fixture), '--version'],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              env={**os.environ, 'USBIPD_BOUNDS_DIRECTORY': str(tmp_path),
                                   'USBIPD_BOUNDS_MODE': mode}) as process:
            stdout, stderr = process.communicate(timeout=8)
            print(f'probe={index} mode={mode} pid={process.pid} exit={process.returncode} '
                  f'stdout={stdout!r} stderr={stderr!r}')
            assert process.returncode == expected_exit, stdout + stderr
            assert stdout == expected_stdout
            assert stderr == ''
            assert (tmp_path / 'parent.pid').read_text() == str(process.pid)
        assert (tmp_path / 'probe.entries').read_text() == 'version\n' * index
        assert not list(tmp_path.glob('*.writing'))


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


@pytest.mark.skipif(not PWSH, reason='existing PowerShell required; no downloads')
@pytest.mark.parametrize('case', ['shared-remaining', 'shared-cutoff'])
def test_shared_probe_predicates_keep_readiness_entries_deadline_and_cleanup(tmp_path, case):
    assert PWSH is not None
    driver = tmp_path / 'shared-predicate.ps1'
    driver.write_text(r'''
param($Target,$Case)
$ErrorActionPreference='Stop'
$ast=[Management.Automation.Language.Parser]::ParseFile($Target,[ref]$null,[ref]$null)
foreach($name in @('Assert-True','Assert-Rejection','Assert-BoundsEvidence')) {
 $f=@($ast.EndBlock.Statements | Where-Object {$_ -is [Management.Automation.Language.FunctionDefinitionAst] -and $_.Name -ceq $name})[0]
 . ([scriptblock]::Create($f.Extent.Text))
}
$token='a'*32
$v=@{run_id=$token; id=$Case; before_outer_cleanup=$true; helper_sha256=('a'*64); fixture_sha256=('b'*64); elapsed_ms=3000; limit_ms=3300; accepted=($Case -eq 'shared-remaining'); resolved_exact=($Case -eq 'shared-remaining'); entries=2; raw_output_leaked=$false; readers_released=$true; containment_stopped=$true; path_unchanged=$true; poison_entered=$false}
Assert-BoundsEvidence ([pscustomobject]$v) $Case $token
$bad=$v.Clone(); $bad.accepted=-not $v.accepted
Assert-Rejection {Assert-BoundsEvidence ([pscustomobject]$bad) $Case $token} 'Bounds readiness decision incorrect'
foreach($count in @(1,3)) {
 $bad=$v.Clone(); $bad.entries=$count
 Assert-Rejection {Assert-BoundsEvidence ([pscustomobject]$bad) $Case $token} 'Bounds actual probe entry inventory incorrect'
}
$bad=$v.Clone(); $bad.elapsed_ms=3301
Assert-Rejection {Assert-BoundsEvidence ([pscustomobject]$bad) $Case $token} 'Bounds total deadline exceeded'
foreach($flag in @('readers_released','containment_stopped')) {
 $bad=$v.Clone(); $bad[$flag]=$false
 Assert-Rejection {Assert-BoundsEvidence ([pscustomobject]$bad) $Case $token} 'Bounds product cleanup incomplete'
}
if($Case -eq 'shared-remaining') {
 $bad=$v.Clone(); $bad.resolved_exact=$false
 Assert-Rejection {Assert-BoundsEvidence ([pscustomobject]$bad) $Case $token} 'Bounds exact path/precedence lost'
}
Write-Output ('PASS '+$Case+': unchanged readiness, two entries, deadline and cleanup refusals')
''')
    result = subprocess.run([PWSH, '-NoProfile', '-File', str(driver),
                             str(ROOT / 'scripts/release/test-usbipd-native.ps1'), case],
                            capture_output=True, text=True, timeout=10)
    print(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PASS ' + case + ': unchanged readiness' in result.stdout
