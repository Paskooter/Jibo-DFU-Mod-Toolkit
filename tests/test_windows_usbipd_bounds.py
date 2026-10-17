"""Offline resolver bounds controls; real POSIX children, no device/install actions.

PWSH selects the already-installed runtime. USBIPD_HELPER selects an exact helper
copy for old/removal controls. Native companion instructions are in the fixture.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys


import pytest

HELPER = Path(os.environ.get('USBIPD_HELPER', Path(__file__).resolve().parents[1] / 'usbipd.ps1'))
PWSH = os.environ.get('PWSH') or shutil.which('pwsh')
pytestmark = pytest.mark.skipif(os.name == 'nt' or not PWSH, reason='POSIX process controls; native companion required')


def q(value):
    return "'" + str(value).replace("'", "''") + "'"


def execute(tmp_path, body, env=None, timeout=23):
    assert PWSH is not None
    script = tmp_path / 'case.ps1'
    script.write_text("$ErrorActionPreference='Stop'\n. " + q(HELPER) + '\n' + body)
    # The harness owns this session from creation; cleanup never consumes PID files.
    supervisor = Path(__file__).parent / 'fixtures' / 'usbipd_bounds_supervise.py'
    process = subprocess.Popen([sys.executable, str(supervisor), str(timeout), PWSH, '-NoProfile', '-File', str(script)],
                               env={**os.environ, 'TMPDIR': str(tmp_path), **(env or {})},
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    try:
        out, err = process.communicate(timeout=timeout+4)
        print(err.strip())
        assert process.returncode == 0, out + err
        result = json.loads(out.strip())
        print(json.dumps({'helper': str(HELPER), **result}, sort_keys=True))
        return result
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=3)


def binary(tmp_path, mode='healthy'):
    path = tmp_path / ('usbipd ' + mode + '.exe')
    path.write_text('''#!/bin/sh
printf 'probe\\n' >> "$BOUNDS_TRACE"
printf '%s' "$$" > "$BOUNDS_PARENT"
case "$BOUNDS_MODE" in
 hang|pipe)
  /bin/sleep 60 &
  printf '%s' "$!" > "$BOUNDS_CHILD"
  if [ "$BOUNDS_MODE" = hang ]; then wait; else printf '5.3.0\\n'; exit 0; fi;;
 noisy) /usr/bin/yes 'token=fixture-secret';;
 broken) exit 17;;
esac
printf '5.3.0\\n'
''')
    path.chmod(0o755)
    return path


def inventory(path, delay=0):
    return '''
function Get-Command { param($Name,$CommandType,$ErrorAction)
 if ($CommandType -ne 'Application') { throw 'not Application-only' }
 [IO.File]::AppendAllText($env:BOUNDS_INVENTORY,('lookup:'+ $Name + "`n"))
 if ($Name -eq 'usbipd.exe') { return [pscustomobject]@{Source=PATH} }
}
function Get-ItemProperty { param($LiteralPath,$Name,$ErrorAction)
 [IO.File]::AppendAllText($env:BOUNDS_INVENTORY,('registry:'+ $LiteralPath + "`n"))
 if ($LiteralPath -eq 'HKLM:\\SOFTWARE\\usbipd-win') { Start-Sleep -Milliseconds DELAY }
 return $null
}
'''.replace('PATH', q(path) if path else '$null').replace('DELAY', str(delay))


def env_for(tmp_path, mode='healthy'):
    return {'BOUNDS_MODE': mode, 'BOUNDS_TRACE': str(tmp_path/'probe.log'),
            'BOUNDS_PARENT': str(tmp_path/'parent.pid'),
            'BOUNDS_CHILD': str(tmp_path/'child.pid'), 'BOUNDS_INVENTORY': str(tmp_path/'inventory.log'),
            'ProgramW6432': str(tmp_path/'absent'), 'ProgramFiles': str(tmp_path/'absent')}


def test_healthy_first_not_delayed_by_lower_inventory(tmp_path):
    path = binary(tmp_path)
    result = execute(tmp_path, inventory(path, 16200) + '''
$t=[Diagnostics.Stopwatch]::StartNew()
$r=Resolve-UsbipdExecutable
[pscustomobject]@{resolved=$r; seconds=$t.Elapsed.TotalSeconds} | ConvertTo-Json -Compress
''', env_for(tmp_path))
    assert result['resolved'] == str(path), 'healthy first candidate suppressed by later discovery'
    assert result['seconds'] < 6, 'lower-priority inventory delayed an already healthy candidate'
    assert (tmp_path/'probe.log').read_text().splitlines() == ['probe']
    assert 'registry:' not in (tmp_path/'inventory.log').read_text()


@pytest.mark.parametrize('mode', ['hang', 'pipe'])
def test_owned_descendants_and_pipe_readers_released(tmp_path, mode):
    path = binary(tmp_path, mode)
    # Observation occurs BEFORE execute's owned-session cleanup. PID is read only,
    # never signalling authority; separate entry and live-state observations.
    result = execute(tmp_path, '''
$t=[Diagnostics.Stopwatch]::StartNew()
$r=Test-UsbipdExecutable -Path PATH -TimeoutSeconds 2
$child=[int][IO.File]::ReadAllText($env:BOUNDS_CHILD)
$parent=[int][IO.File]::ReadAllText($env:BOUNDS_PARENT)
$alive=$false
try { $s=[IO.File]::ReadAllText('/proc/'+$child+'/stat'); $alive=($s -notmatch '\\) Z ') } catch {}
$parentAlive=$false
try { $s=[IO.File]::ReadAllText('/proc/'+$parent+'/stat'); $parentAlive=($s -notmatch '\\) Z ') } catch {}
[pscustomobject]@{accepted=$r; seconds=$t.Elapsed.TotalSeconds; parent=$parent; parentAlive=$parentAlive; child=$child; childAlive=$alive; diagnostics=$script:UsbipdLastSupervision} | ConvertTo-Json -Compress -Depth 5
'''.replace('PATH', q(path)), env_for(tmp_path, mode), timeout=10)
    assert (tmp_path/'probe.log').read_text().splitlines() == ['probe']
    assert not result['accepted'], 'descendant/pipe holder accepted'
    assert not result['childAlive'], 'exact created sleep child survives resolver return'
    assert not result['parentAlive'], 'exact created CLI parent survives resolver return'
    assert result['seconds'] < 3, 'cleanup is outside the probe deadline'
    assert result['diagnostics']['ReadersReleased'], 'supervisor pipe readers not released'


@pytest.mark.parametrize('boundary', ['lookup', 'filesystem'])
def test_blocked_boundary_is_inside_shared_deadline(tmp_path, boundary):
    path = binary(tmp_path)
    if boundary == 'lookup':
        seam = '''
function Get-Command { param($Name,$CommandType,$ErrorAction)
 [IO.File]::WriteAllText($env:BOUNDS_INVENTORY,'blocked-lookup-entered')
 Start-Sleep -Milliseconds 16200
 return $null
}
'''
    else:
        seam = inventory(path) + '''
function Test-Path { param($LiteralPath,$PathType)
 [IO.File]::WriteAllText($env:BOUNDS_INVENTORY,'blocked-filesystem-entered')
 Start-Sleep -Milliseconds 16200
 return $true
}
'''
    result = execute(tmp_path, seam + '''
$budget=if ($script:UsbipdBudgetSeconds) {$script:UsbipdBudgetSeconds} else {15}
$t=[Diagnostics.Stopwatch]::StartNew()
$r=Resolve-UsbipdExecutable
[pscustomobject]@{resolved=$r; seconds=$t.Elapsed.TotalSeconds; budget=$budget; diagnostics=$script:UsbipdLastSupervision} | ConvertTo-Json -Compress -Depth 5
''', env_for(tmp_path))
    assert (tmp_path/'inventory.log').read_text() == 'blocked-' + boundary + '-entered'
    assert not result['resolved']
    assert result['seconds'] <= result['budget'] + .3, 'blocking provider/filesystem escaped the total deadline'
    assert not (tmp_path/'probe.log').exists(), 'probe incorrectly ran after blocked discovery/filesystem'
    assert result['diagnostics']['ContainmentStopped']
    assert result['diagnostics']['ReadersReleased']


@pytest.mark.parametrize('mode', ['healthy', 'noisy-stderr', 'blocked-output'])
def test_output_cap_and_genuine_success(tmp_path, mode):
    path = binary(tmp_path, mode)
    if mode == 'noisy-stderr':
        # A valid stdout plus oversized STDERR passes if the cap is removed.
        path.write_text('#!/bin/sh\nprintf "probe\\n" >> "$BOUNDS_TRACE"\nprintf "5.3.0\\n"\n/usr/bin/head -c 5000 /dev/zero >&2\n')
    elif mode == 'blocked-output':
        path.write_text('#!/bin/sh\nprintf "probe\\n" >> "$BOUNDS_TRACE"\nexec /usr/bin/yes token=fixture-secret\n')
    result = execute(tmp_path, '''
$t=[Diagnostics.Stopwatch]::StartNew()
$r=Test-UsbipdExecutable -Path PATH -TimeoutSeconds 2
[pscustomobject]@{accepted=$r; seconds=$t.Elapsed.TotalSeconds; diagnostics=$script:UsbipdLastSupervision} | ConvertTo-Json -Compress -Depth 5
'''.replace('PATH', q(path)), env_for(tmp_path, mode), timeout=10)
    assert (tmp_path/'probe.log').read_text().splitlines() == ['probe']
    assert result['accepted'] is (mode == 'healthy'), 'output-cap/readiness decision incorrect'
    assert result['seconds'] < 2, 'output rejection waited for timeout instead of enforcing cap'
    assert 'fixture-secret' not in json.dumps(result)
    assert result['diagnostics']['ReadersReleased']
    assert result['diagnostics']['ContainmentStopped']


def test_explicit_path_refused_without_fallback(tmp_path):
    path = binary(tmp_path)
    result = execute(tmp_path, inventory(path) + '''
$r=Resolve-UsbipdExecutable -PreferredPath PATH
[pscustomobject]@{resolved=$r; diagnostics=$script:UsbipdLastSupervision} | ConvertTo-Json -Compress -Depth 5
'''.replace('PATH', q(str(path)+'.missing')), env_for(tmp_path))
    assert not result['resolved']
    assert not (tmp_path/'inventory.log').exists(), 'bad preferred path fell back to inventory'
    assert not (tmp_path/'probe.log').exists()
    assert result['diagnostics']['ReadersReleased']


def test_later_healthy_candidate_uses_remaining_shared_budget(tmp_path):
    path = binary(tmp_path)
    broken = tmp_path/'broken.exe'
    broken.write_text('#!/bin/sh\nprintf "broken\\n" >> "$BOUNDS_TRACE"\n/bin/sleep 1\nexit 17\n')
    broken.chmod(0o755)
    seam = inventory(path).replace("if ($Name -eq 'usbipd.exe') { return [pscustomobject]@{Source="+q(path)+"} }",
        "if ($Name -eq 'usbipd.exe') { return [pscustomobject]@{Source="+q(broken)+"} }; if ($Name -eq 'usbipd') { return [pscustomobject]@{Source="+q(path)+"} }")
    result = execute(tmp_path, seam + '''
$script:UsbipdBudgetSeconds=3
$t=[Diagnostics.Stopwatch]::StartNew()
$r=Resolve-UsbipdExecutable
[pscustomobject]@{resolved=$r; seconds=$t.Elapsed.TotalSeconds; diagnostics=$script:UsbipdLastSupervision} | ConvertTo-Json -Compress -Depth 5
''', env_for(tmp_path), timeout=8)
    assert result['resolved'] == str(path)
    assert (tmp_path/'probe.log').read_text().splitlines() == ['broken', 'probe']
    assert result['seconds'] < 3
    assert result['diagnostics']['ReadersReleased']


def test_later_probe_cannot_reset_total_budget(tmp_path):
    first = tmp_path/'first.exe'
    second = tmp_path/'second.exe'
    first.write_text('#!/bin/sh\nprintf "first\\n" >> "$BOUNDS_TRACE"\n/bin/sleep 1\nexit 17\n')
    second.write_text('#!/bin/sh\nprintf "second\\n" >> "$BOUNDS_TRACE"\n/bin/sleep 2\nprintf "5.3.0\\n"\n')
    first.chmod(0o755); second.chmod(0o755)
    seam = inventory(first).replace("if ($Name -eq 'usbipd.exe') { return [pscustomobject]@{Source="+q(first)+"} }",
        "if ($Name -eq 'usbipd.exe') { return [pscustomobject]@{Source="+q(first)+"} }; if ($Name -eq 'usbipd') { return [pscustomobject]@{Source="+q(second)+"} }")
    result = execute(tmp_path, seam + '''
$script:UsbipdBudgetSeconds=3
$t=[Diagnostics.Stopwatch]::StartNew()
$r=Resolve-UsbipdExecutable
[pscustomobject]@{resolved=$r; seconds=$t.Elapsed.TotalSeconds; diagnostics=$script:UsbipdLastSupervision} | ConvertTo-Json -Compress -Depth 5
''', env_for(tmp_path), timeout=8)
    assert not result['resolved'], 'later probe reset the invocation budget'
    assert (tmp_path/'probe.log').read_text().splitlines() == ['first', 'second']
    assert result['seconds'] < 3.3
    assert result['diagnostics']['ReadersReleased']
