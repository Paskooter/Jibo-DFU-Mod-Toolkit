"""Monotonic, real-process fixtures for the probe's admission/execution split.

Only Test-Path (an existing transferred read seam) is delayed, inside product
containment and its running total clock. No prewarming or runtime dependencies.
"""
import json
import sys


def assert_owned_cleanup(stderr):
    cleanup = json.loads(stderr.splitlines()[0])
    assert not cleanup['outer_timeout'], 'outer rescue substituted for product return'
    assert not cleanup['remaining_direct_children']
    assert all(item['state_before_test_cleanup'] == 'Z' for item in cleanup['adopted_states']), 'outer rescue found a live product descendant'


def admission(seconds):
    return '''
$env:PROBE_CONTRACT_ORIGIN=[string][Diagnostics.Stopwatch]::GetTimestamp()
function Test-Path { param($LiteralPath,$PathType)
 [IO.File]::WriteAllText((Join-Path $env:TMPDIR 'admission.entered'),'entered')
 $until=[double]$env:PROBE_CONTRACT_ORIGIN+DELAY*[Diagnostics.Stopwatch]::Frequency
 while ([Diagnostics.Stopwatch]::GetTimestamp() -lt $until) { [Threading.Thread]::Sleep(2) }
 return (Microsoft.PowerShell.Management\\Test-Path -LiteralPath $LiteralPath -PathType $PathType)
}
'''.replace('DELAY', str(seconds))


def probe_binary(tmp_path, mode):
    path = tmp_path / ('usbipd ' + mode + '.exe')
    path.write_text('#!' + sys.executable + '''
import json
import os
from pathlib import Path
import subprocess
import sys
import time

root = Path(os.environ['TMPDIR'])
# CPython CLOCK_MONOTONIC and .NET Stopwatch use the same Linux monotonic clock;
# each observation asserts that this timestamp is inside the caller interval.
(root/'cli.entry').write_text(str(time.monotonic_ns()))
(root/'native.log').write_text(str(Path(sys.argv[0]))+'\\t'+' '.join(sys.argv[1:])+'\\n')
(root/'probe.log').write_text('probe\\n')
(root/'parent.pid').write_text(str(os.getpid()))
assert sys.argv[1:] == ['--version']
mode = MODE
if mode == 'late':
    time.sleep(.4)
else:
    child = subprocess.Popen(['/bin/sleep','60'])
    (root/'child.pid').write_text(str(child.pid))
    if mode == 'hang':
        print('token=fixture-secret', file=sys.stderr, flush=True)
        child.wait()
print('5.3.0', flush=True)
'''.replace('MODE', repr(mode)))
    path.chmod(0o755)
    return path


def observation(call):
    """Observe return and exact journal identities BEFORE outer harness cleanup."""
    return '''
$frequency=[Diagnostics.Stopwatch]::Frequency
$start=[Diagnostics.Stopwatch]::GetTimestamp()
$r=CALL
$finish=[Diagnostics.Stopwatch]::GetTimestamp()
$entry=[long][IO.File]::ReadAllText((Join-Path $env:TMPDIR 'cli.entry'))
$parent=[int][IO.File]::ReadAllText((Join-Path $env:TMPDIR 'parent.pid'))
$child=$null; $childAlive=$false
$childFile=Join-Path $env:TMPDIR 'child.pid'
if ([IO.File]::Exists($childFile)) {
 $child=[int][IO.File]::ReadAllText($childFile)
 try { $s=[IO.File]::ReadAllText('/proc/'+$child+'/stat'); $childAlive=($s -notmatch '\\) Z ') } catch {}
}
$parentAlive=$false
try { $s=[IO.File]::ReadAllText('/proc/'+$parent+'/stat'); $parentAlive=($s -notmatch '\\) Z ') } catch {}
[pscustomobject]@{accepted=[bool]$r; seconds=($finish-$start)/$frequency; enteredSeconds=($finish/$frequency-$entry/1e9); clockOrdered=($start/$frequency -le $entry/1e9 -and $entry/1e9 -le $finish/$frequency); parent=$parent; parentAlive=$parentAlive; child=$child; childAlive=$childAlive; diagnostics=$script:UsbipdLastSupervision} | ConvertTo-Json -Compress -Depth 5
'''.replace('CALL', call)
