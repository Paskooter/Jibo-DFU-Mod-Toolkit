"""Harmless local supervisor/receipt controls, never native Windows acceptance."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = Path(os.environ.get('SUPPORT_NATIVE_RUNNER', ROOT / 'scripts/release/test-support-native.ps1'))
PWSH = os.environ.get('PWSH') or shutil.which('pwsh') or shutil.which('powershell.exe')
SCRATCH = 'support native ' + 'd' * 32


def invoke(tmp_path, body, runner=RUNNER):
    assert runner.is_file(), 'SUPPORT-RUNNER-MISSING: bounded owned-process runner absent'
    if not PWSH:
        pytest.skip('PowerShell required for real local controls')
    script = tmp_path / 'control.ps1'
    script.write_text("$ErrorActionPreference='Stop'\n. '" + str(runner).replace("'", "''") + "'\n" + body)
    return subprocess.run([PWSH, '-NoProfile', '-NonInteractive', '-File', str(script)],
                          text=True, capture_output=True, timeout=35, cwd=tmp_path)


def test_real_owned_hang_stops_and_following_child_succeeds(tmp_path):
    child = tmp_path / 'harmless child.ps1'
    child.write_text("""param($Hang)
Write-Output 'owned child entered'
[Console]::Error.WriteLine('owned stderr')
if ($Hang -eq 'yes') {
    $descendant=New-Object Diagnostics.Process
    $descendant.StartInfo=New-Object Diagnostics.ProcessStartInfo
    $descendant.StartInfo.FileName=(Get-Process -Id $PID).Path
    $descendant.StartInfo.Arguments='-NoProfile -NonInteractive -Command "Start-Sleep 60"'
    $descendant.StartInfo.UseShellExecute=$false; $descendant.StartInfo.CreateNoWindow=$true
    $null=$descendant.Start(); $null=$descendant.Handle
    Write-Output ('owned descendant ' + $descendant.Id)
    Start-Sleep 60
}
exit 0
""")
    result = invoke(tmp_path, """
$hostPath=(Get-Process -Id $PID).Path
$hang=Invoke-SupportChild $hostPath ('-NoProfile -NonInteractive -File "' + (Join-Path $PWD 'harmless child.ps1') + '" -Hang yes') (Join-Path $PWD 'hang') 'owned-test' 2
$after=Invoke-SupportChild $hostPath ('-NoProfile -NonInteractive -File "' + (Join-Path $PWD 'harmless child.ps1') + '" -Hang no') (Join-Path $PWD 'after') 'owned-test' 8
$rejection=''
try { Assert-SupportPhase $hang 'owned-test' $hang.started_utc $hang.ended_utc }
catch { $rejection=$_.Exception.Message }
if ($rejection -cne 'Child timed out') { throw ('Wrong hung-child gate rejection: ' + $rejection) }
Assert-SupportPhase $after 'owned-test' $after.started_utc $after.ended_utc
@{hang=$hang;after=$after;hang_rejection=$rejection;native_acceptance=$false} | ConvertTo-Json -Depth 10
""")
    assert result.returncode == 0, result.stdout + result.stderr
    receipts = json.loads(result.stdout)
    hang, after = receipts['hang'], receipts['after']
    assert receipts['hang_rejection'] == 'Child timed out'
    assert hang['launch']['use_shell_execute'] is False and hang['launch']['create_no_window'] is True
    assert hang['launch']['environment']['JIBO_DFU_SETUP_LOG'] == ''
    assert 'support temp owned-test' in hang['launch']['environment']['TEMP']
    if os.name != 'nt':
        import re
        descendant = re.search(r'owned descendant (\d+)', (tmp_path / 'hang/stdout.txt').read_text())
        assert descendant, 'OWNED-DESCENDANT-NOT-ENTERED'
        # Read-only journal observation is not authority to signal anything.
        status = Path('/proc', descendant[1], 'status')
        assert not status.exists() or 'State:\tZ' in status.read_text(), 'OWNED-DESCENDANT-SURVIVED'
    assert hang['timed_out'] is True and hang['stopped'] is True
    assert hang['state'] == 'complete' and hang['exit_code'] != 0
    assert after['timed_out'] is False and after['stopped'] is True
    assert after['state'] == 'complete' and after['exit_code'] == 0
    for phase in ('hang', 'after'):
        assert 'owned child entered' in (tmp_path / phase / 'stdout.txt').read_text()
        assert 'owned stderr' in (tmp_path / phase / 'stderr.txt').read_text()
        assert 'BEGIN' in (tmp_path / phase / 'progress.txt').read_text()
        assert 'END' in (tmp_path / phase / 'progress.txt').read_text()
        assert json.loads((tmp_path / phase / 'phase.json').read_text()) == receipts[phase]
        assert not Path('/proc', str(receipts[phase]['pid'])).exists()


def test_real_owned_nonzero_exit_is_rejected_by_phase_gate(tmp_path):
    child = tmp_path / 'nonzero.ps1'
    child.write_text("Write-Output 'owned nonzero entered'; exit 17\n")
    result = invoke(tmp_path, """
$phase=Invoke-SupportChild (Get-Process -Id $PID).Path ('-NoProfile -NonInteractive -File "' + (Join-Path $PWD 'nonzero.ps1') + '"') (Join-Path $PWD 'nonzero') 'nonzero' 8
if ($phase.exit_code -ne 17 -or -not $phase.stopped -or $phase.state -cne 'complete') { throw 'NONZERO-PHASE-NOT-EXERCISED' }
try { Assert-SupportPhase $phase 'nonzero' $phase.started_utc $phase.ended_utc }
catch { Write-Output ('REJECT:' + $_.Exception.Message); exit 9 }
throw 'EXPECTED-REJECTION-ABSENT'
""")
    assert result.returncode == 9 and result.stdout.strip() == 'REJECT:Child exit was not zero', result.stdout + result.stderr


def test_failed_launch_retains_terminal_diagnostics(tmp_path):
    result = invoke(tmp_path, """
$phase=Invoke-SupportChild (Join-Path $PWD 'absent-owned-binary') '' (Join-Path $PWD 'failed-launch') 'failed-launch' 2
if ($phase.state -cne 'failed' -or $null -ne $phase.pid) { throw 'FAILED-LAUNCH-NOT-EXERCISED' }
try { Assert-SupportPhase $phase 'failed-launch' $phase.started_utc $phase.ended_utc }
catch { Write-Output ('REJECT:' + $_.Exception.Message); exit 9 }
throw 'EXPECTED-REJECTION-ABSENT'
""")
    assert result.returncode == 9 and result.stdout.strip() == 'REJECT:Phase failed or incomplete', result.stdout + result.stderr
    assert (tmp_path / 'failed-launch/supervisor-error.txt').stat().st_size > 0
    assert 'END support failed' in (tmp_path / 'failed-launch/progress.txt').read_text()


def test_partial_output_and_running_receipt_survive_real_hang(tmp_path):
    import time

    if not PWSH:
        pytest.skip('PowerShell required')
    child = tmp_path / 'hang.ps1'
    child.write_text("Write-Output 'incremental stdout'; [Console]::Error.WriteLine('incremental stderr'); Start-Sleep 60\n")
    wrapper = tmp_path / 'supervise.ps1'
    wrapper.write_text(". '" + str(RUNNER).replace("'", "''") + "'\n" + "Invoke-SupportChild (Get-Process -Id $PID).Path ('-NoProfile -NonInteractive -File \"' + (Join-Path $PWD 'hang.ps1') + '\"') (Join-Path $PWD 'capture') 'incremental' 4 | ConvertTo-Json\n")
    with subprocess.Popen([PWSH, '-NoProfile', '-NonInteractive', '-File', str(wrapper)],
                          cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                # stdout and stderr are separate pipes copied by separate tasks, so
                # one can land before the other. Wait for both in the same deadline.
                output = tmp_path / 'capture/stdout.txt'
                errors = tmp_path / 'capture/stderr.txt'
                if (output.exists() and 'incremental stdout' in output.read_text() and
                        errors.exists() and 'incremental stderr' in errors.read_text()):
                    break
                time.sleep(0.02)
            else:
                pytest.fail('INCREMENTAL-OUTPUT-MISSING: hung child output not persisted before deadline')
            phase = json.loads((tmp_path / 'capture/phase.json').read_text())
            assert phase['state'] == 'running' and phase['pid'] is not None
            assert 'BEGIN support' in (tmp_path / 'capture/progress.txt').read_text()
            assert 'incremental stderr' in (tmp_path / 'capture/stderr.txt').read_text()
        finally:
            stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stdout + stderr
        assert json.loads(stdout)['timed_out'] is True


def synthetic_bundle(tmp_path):
    """Complete shape-only evidence. No native process/result is represented."""
    import datetime
    import hashlib

    folder = tmp_path / 'SYNTHETIC-NOT-NATIVE'
    folder.mkdir()
    (folder / SCRATCH).mkdir()
    temp = folder / ('support temp ' + 'd' * 32)
    temp.mkdir()
    compilation = b'// SYNTHETIC compilation shape control; NOT compiled.\n'
    (folder / 'fixture-compilation.cs').write_bytes(compilation)
    now = datetime.datetime.now(datetime.timezone.utc)
    start = (now - datetime.timedelta(seconds=5)).isoformat()
    end = (now + datetime.timedelta(seconds=5)).isoformat()
    context: dict = dict(revision='a' * 40, ci_run_id='123', ci_attempt='1', ci_job='build',
                        source_root=str(ROOT), compilation_sha256=hashlib.sha256(compilation).hexdigest(),
                        runner_sha256=hashlib.sha256(RUNNER.read_bytes()).hexdigest(),
                        sources={'scripts/release/console.ps1': 'b' * 64},
                        runtime=dict(os='Windows_NT', windows='Microsoft Windows NT 10.0.20348.0',
                                     powershell='5.1.20348.1', edition='Desktop', clr='4.0.30319.42000',
                                     framework='C:/Windows/Microsoft.NET/Framework64/v4.0.30319/',
                                     host_name='powershell.exe', host_sha256='c' * 64,
                                     host_path='/SYNTHETIC-NOT-NATIVE/powershell.exe'))
    arguments = (f'-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{ROOT / "tests/fixtures/support_failure_scenarios.ps1"}" '
                 f'-Mode Native -Scratch "{folder / SCRATCH}" -ConsoleSource "{ROOT / "scripts/release/console.ps1"}"')
    launch = dict(api='ProcessStartInfo', binary=context['runtime']['host_path'], binary_sha256='c' * 64,
                  arguments=arguments, working_directory=str(ROOT), use_shell_execute=False, create_no_window=True, window_creation_flag='0x08000000',
                  environment={**{n: str(temp) for n in ('TEMP', 'TMP', 'TMPDIR', 'LOCALAPPDATA')}, 'JIBO_DFU_SETUP_LOG': ''})
    phase = dict(run_id='d' * 32, state='complete', started_utc=start, ended_utc=end,
                 timed_out=False, stopped=True, exit_code=0, pid=0, launch=launch, budget_seconds=90,
                 cleanup_allowance_seconds=10, admitted=True, tree_empty=True, owned_remaining=0,
                 containment_kind='windows-job')
    # Shape-only bootstrap receipt derived from the actual runner template. No
    # native process is executed or represented by this synthetic bundle.
    import base64
    template = RUNNER.read_text().split("$template=@'\n", 1)[1].split("\n'@", 1)[0]
    request = dict(binary=context['runtime']['host_path'], arguments=arguments, root=str(folder),
                   nonce='d' * 32, seconds=90, working_directory=str(ROOT))
    bootstrap = template.replace('REQUEST', base64.b64encode(json.dumps(request, separators=(',', ':'), ensure_ascii=False).encode()).decode())
    phase['supervision'] = dict(api='ProcessStartInfo', binary=context['runtime']['host_path'],
                               arguments='-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -EncodedCommand ' + base64.b64encode(bootstrap.encode('utf-16-le')).decode(),
                               bootstrap_sha256=hashlib.sha256(bootstrap.encode()).hexdigest(), gated_stdin=True)
    (folder / 'fixture-launch.json').write_text(json.dumps(launch))
    receipt = dict(mode='Native', windows=context['runtime']['windows'],
                   powershell=context['runtime']['powershell'], clr=context['runtime']['clr'],
                   clipboard='synthetic-memory-only', cases=5, events=4, forbidden_calls=0,
                   starts=6, stops=5, source_sha256='B' * 64, result='ok')
    manifest = dict(schema=1, kind='native-support', run_id='d' * 32, state='complete',
                    context=context, context_after=context, started_utc=start, ended_utc=end, inventory={})
    for path, value in [('run.json', manifest), ('phase.json', phase), (f'{SCRATCH}/receipt.json', receipt)]:
        (folder / path).write_text(json.dumps(value))
    (folder / 'stdout.txt').write_text('PASS WRAPPER-PERSISTENCE cases=5 events=4 forbidden=0\n')
    (folder / 'stderr.txt').write_text('synthetic driver diagnostics\n')
    (folder / 'progress.txt').write_text('BEGIN support\nEND support complete timeout=False\n')
    events = [dict(utc=start, event='setup-start', build='f' * 40, windows='Windows 11 Pro 25H2 (build 26200.9445)', wsl='not checked'),
              dict(utc=start, event='setup-end', result='failed', error='Synthetic failure')]
    for category, exception, hresult in [('external', 'System.Runtime.InteropServices.ExternalException', '0x800401D0'),
                                        ('thread-state', 'System.Threading.ThreadStateException', '0x80131520'),
                                        ('invalid-operation', 'System.InvalidOperationException', '0x80131509'),
                                        ('external', 'System.Runtime.InteropServices.ExternalException', '0x800401D0')]:
        events.append(dict(utc=start, event='clipboard-copy-failed', at_utc=start, exception_type=exception,
                           category=category, hresult=hresult, apartment='STA', managed_thread_id=3, summary_length=20))
    (folder / f'{SCRATCH}/setup.log').write_text(''.join(json.dumps(e) + '\n' for e in events))
    (tmp_path / 'expected.json').write_text(json.dumps(context))
    return folder

def seal_bundle(folder):
    import hashlib
    manifest = json.loads((folder / 'run.json').read_text())
    manifest['inventory'] = dict(
        files={p.relative_to(folder).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in sorted(folder.rglob('*')) if p.is_file() and p != folder / 'run.json'},
        directories=[p.relative_to(folder).as_posix() for p in sorted(folder.rglob('*')) if p.is_dir()])
    (folder / 'run.json').write_text(json.dumps(manifest))


def test_complete_synthetic_evidence_reaches_same_core_gate(tmp_path):
    folder = synthetic_bundle(tmp_path)
    seal_bundle(folder)
    result = invoke(tmp_path, """
$expected=Read-SupportJson (Join-Path $PWD 'expected.json')
Validate-SupportEvidence (Join-Path $PWD 'SYNTHETIC-NOT-NATIVE') $expected
Write-Output 'PASS complete synthetic baseline; NOT native acceptance'
""")
    assert result.returncode == 0, 'COMPLETE-BASELINE: ' + result.stdout + result.stderr
    assert 'PASS complete synthetic baseline' in result.stdout


REJECTIONS = [
    ('missing-run', 'Missing run receipt'),
    ('running', 'Run incomplete'),
    ('failed', 'Phase failed or incomplete'),
    ('missing-phase', 'Missing phase receipt'),
    ('missing-fixture', 'Missing fixture receipt'),
    ('stale', 'Stale phase receipt'),
    ('timed-out', 'Child timed out'),
    ('not-stopped', 'Child not stopped'),
    ('nonzero-exit', 'Child exit was not zero'),
    ('null-exit', 'Child exit was not zero'),
    ('string-exit', 'Child exit was not zero'),
    ('old-phase', 'Out-of-run phase timestamps'),
    ('old-fixture', 'Stale fixture receipt timestamp'),
    ('source', 'Source fingerprint mismatch: scripts/release/console.ps1'),
    ('fixture-source', 'Fixture source fingerprint mismatch'),
    ('runner', 'Runner fingerprint mismatch'),
    ('revision', 'Revision mismatch'),
    ('ci-run', 'CI run/attempt/job mismatch'),
    ('ci-attempt', 'CI run/attempt/job mismatch'),
    ('ci-job', 'CI run/attempt/job mismatch'),
    ('host', 'Runtime identity mismatch'),
    ('portable', 'Wrong fixture mode or clipboard boundary'),
    ('clipboard', 'Wrong fixture mode or clipboard boundary'),
    ('fixture-failed', 'Fixture result not ok'),
    ('extra-field', 'Fixture receipt shape mismatch'),
    ('cases', 'Fixture count mismatch: cases'),
    ('events', 'Fixture count mismatch: events'),
    ('forbidden_calls', 'Fixture count mismatch: forbidden_calls'),
    ('starts', 'Fixture count mismatch: starts'),
    ('stops', 'Fixture count mismatch: stops'),
    ('string-count', 'Fixture count mismatch: cases'),
    ('fixture-runtime', 'Fixture runtime mismatch'),
    ('missing-marker', 'Fixture success marker missing'),
    ('progress', 'Incomplete progress receipt'),
    ('missing-event', 'Persisted clipboard event inventory mismatch'),
    ('event-private-field', 'Persisted clipboard event shape mismatch'),
    ('event-provenance', 'Persisted clipboard provenance mismatch'),
    ('extra-root', 'Diagnostic file inventory mismatch'),
    ('extra-nested', 'Diagnostic file inventory mismatch'),
    ('extra-directory', 'Diagnostic directory inventory mismatch'),
    ('hash-tamper', 'Diagnostic hash mismatch: stderr.txt'),
    ('missing-after', 'Before/after context missing'),
    ('after-source', 'Source fingerprint mismatch: scripts/release/console.ps1'),
    ('budget', 'Wrong native child budget'),
    ('shell', 'Windowless process boundary mismatch'),
    ('window', 'Windowless process boundary mismatch'),
    ('apartment', 'Native child launch mismatch'),
    ('temp', 'Child temp ownership mismatch'),
    ('inherited-log', 'Inherited setup log forbidden'),
    ('compilation', 'Generated compilation source mismatch'),
    ('unallowlisted-event', 'Unallowlisted JSONL event'),
    ('setup-extra-field', 'Setup-start JSONL shape mismatch'),
    ('private-marker', 'Private fixture marker in JSONL'),
    ('hresult', 'Persisted clipboard provenance mismatch'),
    ('old-event', 'Clipboard event timestamp mismatch'),
]


@pytest.mark.parametrize('mutation,reason', REJECTIONS, ids=[r[0] for r in REJECTIONS])
def test_exact_rejections_with_otherwise_complete_evidence(tmp_path, mutation, reason):
    folder = synthetic_bundle(tmp_path)
    seal_bundle(folder)
    run = json.loads((folder / 'run.json').read_text())
    phase = json.loads((folder / 'phase.json').read_text())
    receipt = json.loads((folder / f'{SCRATCH}/receipt.json').read_text())
    if mutation == 'running':
        run['state'] = 'running'
    elif mutation == 'failed':
        phase['state'] = 'failed'
    elif mutation == 'stale':
        phase['run_id'] = 'e' * 32
    elif mutation == 'timed-out':
        phase['timed_out'] = True
    elif mutation == 'not-stopped':
        phase['stopped'] = False
    elif mutation in ('nonzero-exit', 'null-exit', 'string-exit'):
        phase['exit_code'] = {'nonzero-exit': 17, 'null-exit': None, 'string-exit': '0'}[mutation]
    elif mutation == 'old-phase':
        phase['started_utc'] = '2001-01-01T00:00:00Z'
    elif mutation == 'source':
        run['context']['sources']['scripts/release/console.ps1'] = 'e' * 64
    elif mutation == 'fixture-source':
        receipt['source_sha256'] = 'e' * 64
    elif mutation == 'runner':
        run['context']['runner_sha256'] = 'e' * 64
    elif mutation == 'revision':
        run['context']['revision'] = 'e' * 40
    elif mutation.startswith('ci-'):
        run['context'][{'ci-run': 'ci_run_id', 'ci-attempt': 'ci_attempt', 'ci-job': 'ci_job'}[mutation]] = 'wrong'
    elif mutation == 'host':
        run['context']['runtime']['host_sha256'] = 'e' * 64
    elif mutation == 'portable':
        receipt['mode'] = 'Portable'
    elif mutation == 'clipboard':
        receipt['clipboard'] = 'system'
    elif mutation == 'fixture-failed':
        receipt['result'] = 'failed'
    elif mutation == 'extra-field':
        receipt['private'] = 'not permitted'
    elif mutation in ('cases', 'events', 'forbidden_calls', 'starts', 'stops'):
        receipt[mutation] += 1
    elif mutation == 'string-count':
        receipt['cases'] = '5'
    elif mutation == 'fixture-runtime':
        receipt['clr'] = '9.0.10'
    elif mutation == 'missing-marker':
        (folder / 'stdout.txt').write_text('not a pass')
    elif mutation == 'progress':
        (folder / 'progress.txt').write_text('BEGIN support\n')
    elif mutation in ('missing-event', 'event-private-field', 'event-provenance'):
        events = [json.loads(line) for line in (folder / f'{SCRATCH}/setup.log').read_text().splitlines()]
        if mutation == 'missing-event':
            events.pop()
        elif mutation == 'event-private-field':
            events[2]['message'] = 'not permitted'
        else:
            events[2]['apartment'] = 'MTA'
        (folder / f'{SCRATCH}/setup.log').write_text(''.join(json.dumps(e) + '\n' for e in events))
    if mutation == 'missing-after':
        run['context_after'] = None
    elif mutation == 'after-source':
        run['context_after']['sources']['scripts/release/console.ps1'] = 'e' * 64
    elif mutation == 'budget':
        phase['budget_seconds'] = 120
    elif mutation in ('shell', 'window'):
        phase['launch'][{'shell': 'use_shell_execute', 'window': 'create_no_window'}[mutation]] = mutation == 'shell'
    elif mutation == 'apartment':
        phase['launch']['arguments'] += ' -MTA'
    elif mutation == 'temp':
        phase['launch']['environment']['TEMP'] = 'unowned'
    elif mutation == 'inherited-log':
        phase['launch']['environment']['JIBO_DFU_SETUP_LOG'] = 'unowned'
    elif mutation == 'compilation':
        (folder / 'fixture-compilation.cs').write_text('changed compilation source')
    elif mutation in ('unallowlisted-event', 'setup-extra-field', 'private-marker', 'hresult', 'old-event'):
        events = [json.loads(line) for line in (folder / f'{SCRATCH}/setup.log').read_text().splitlines()]
        if mutation == 'unallowlisted-event':
            events.append(dict(event='not-allowlisted', utc=phase['started_utc']))
        elif mutation == 'setup-extra-field':
            events[0]['message'] = 'unexpected'
        elif mutation == 'private-marker':
            events[2]['exception_type'] = 'fixture_private_custom_type'
        elif mutation == 'hresult':
            events[2]['hresult'] = '0x00000001'
        else:
            events[2]['at_utc'] = '2001-01-01T00:00:00Z'
        (folder / f'{SCRATCH}/setup.log').write_text(''.join(json.dumps(e) + '\n' for e in events))
    (folder / 'run.json').write_text(json.dumps(run))
    (folder / 'phase.json').write_text(json.dumps(phase))
    (folder / f'{SCRATCH}/receipt.json').write_text(json.dumps(receipt))
    # Re-seal semantic negatives so an unrelated hash failure cannot stand in.
    seal_bundle(folder)
    if mutation in ('missing-run', 'missing-phase', 'missing-fixture'):
        (folder / {'missing-run': 'run.json', 'missing-phase': 'phase.json',
                   'missing-fixture': f'{SCRATCH}/receipt.json'}[mutation]).unlink()
    elif mutation == 'old-fixture':
        os.utime(folder / f'{SCRATCH}/receipt.json', (1000000000, 1000000000))
    elif mutation == 'extra-root':
        (folder / 'unaccounted.txt').touch()
    elif mutation == 'extra-nested':
        (folder / f'{SCRATCH}/unaccounted.txt').touch()
    elif mutation == 'extra-directory':
        (folder / f'{SCRATCH}/unaccounted').mkdir()
    elif mutation == 'hash-tamper':
        (folder / 'stderr.txt').write_text('tampered')
    result = invoke(tmp_path, """
$expected=Read-SupportJson (Join-Path $PWD 'expected.json')
try { Validate-SupportEvidence (Join-Path $PWD 'SYNTHETIC-NOT-NATIVE') $expected }
catch { Write-Output ('REJECT:' + $_.Exception.Message); exit 9 }
throw 'EXPECTED-REJECTION-ABSENT'
""")
    assert result.returncode == 9, 'EXACT-REJECTION-ABSENT: ' + mutation + '\n' + result.stdout + result.stderr
    assert result.stdout.strip() == 'REJECT:' + reason, 'WRONG-REJECTION: ' + mutation + '\n' + result.stdout + result.stderr


@pytest.mark.parametrize('field,value', [('os', 'Linux'), ('powershell', '7.5.4'), ('powershell', '5.0.10586.117'), ('edition', 'Core'),
                                        ('clr', '9.0.10'), ('host_name', 'pwsh'), ('framework', '/mono/4.5/')])
def test_native_identity_guard_rejects_non_native_shapes(tmp_path, field, value):
    synthetic_bundle(tmp_path)
    result = invoke(tmp_path, """
$runtime=(Read-SupportJson (Join-Path $PWD 'expected.json')).runtime
Assert-SupportNative $runtime
$runtime.FIELD='VALUE'
try { Assert-SupportNative $runtime }
catch { Write-Output ('REJECT:' + $_.Exception.Message); exit 9 }
throw 'EXPECTED-REJECTION-ABSENT'
""".replace('FIELD', field).replace('VALUE', value))
    assert result.returncode == 9, result.stdout + result.stderr
    assert result.stdout.strip() == 'REJECT:Native Windows PowerShell 5.1 and .NET Framework required'


@pytest.mark.skipif(os.name == 'nt', reason='Linux refusal control, not native execution')
@pytest.mark.parametrize('mode', ['Run', 'Validate'])
def test_native_entry_refuses_linux_without_creating_output(tmp_path, mode):
    if not PWSH:
        pytest.skip('PowerShell required')
    output = tmp_path / 'must-not-exist'
    result = subprocess.run([PWSH, '-NoProfile', '-NonInteractive', '-File', str(RUNNER),
                             '-Mode', mode, '-OutputRoot', str(output)], text=True,
                            capture_output=True, timeout=10, cwd=tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'Native Windows PowerShell 5.1 and .NET Framework required' in result.stderr
    assert not output.exists()


def test_receipt_pid_cannot_signal_a_fresh_owned_sentinel(tmp_path):
    if not PWSH:
        pytest.skip('PowerShell required')
    folder = synthetic_bundle(tmp_path)
    # We retain the sentinel's original Popen object and alone authorize its cleanup.
    with subprocess.Popen([PWSH, '-NoProfile', '-NonInteractive', '-Command', 'Start-Sleep 60']) as sentinel:
        try:
            phase = json.loads((folder / 'phase.json').read_text())
            phase.update(pid=sentinel.pid, exit_code=17)
            (folder / 'phase.json').write_text(json.dumps(phase))
            seal_bundle(folder)
            result = invoke(tmp_path, """
try { Validate-SupportEvidence (Join-Path $PWD 'SYNTHETIC-NOT-NATIVE') (Read-SupportJson (Join-Path $PWD 'expected.json')) }
catch { Write-Output ('REJECT:' + $_.Exception.Message); exit 9 }
throw 'EXPECTED-REJECTION-ABSENT'
""")
            assert result.returncode == 9 and result.stdout.strip() == 'REJECT:Child exit was not zero', result.stdout + result.stderr
            assert sentinel.poll() is None, 'RECEIPT-PID-SIGNALLED: sentinel did not survive validator'
        finally:
            sentinel.terminate()
            sentinel.wait(timeout=10)


def test_compilation_provenance_reads_literals_without_executing_product(tmp_path):
    import hashlib

    candidate = tmp_path / 'candidate with spaces'
    (candidate / 'scripts/release').mkdir(parents=True)
    (candidate / 'tests/fixtures').mkdir(parents=True)
    join = 'dialogThread.Join(); // An explicit user decision, never a command timeout.'
    ui = 'UI\n' + join
    (candidate / 'scripts/release/console.ps1').write_text(
        "throw 'PRODUCT-MUST-NOT-EXECUTE'\n$script:ToolkitSetupUiSource = @'\n" + ui + "\n'@\n$script:ToolkitQuestionModelSource = @'\nMODEL\n'@\n")
    (candidate / 'tests/fixtures/support_clipboard_faults.cs').write_text('FAULTS\n')
    (candidate / 'tests/fixtures/support_failure_controller.cs').write_text('CONTROLLER\n')
    expected = ui.replace(join, join + '\n        if (SupportFailureController.ThrowAfterDialog) throw new InvalidOperationException("synthetic wrapper error");') + 'MODELFAULTS\nCONTROLLER\n'
    result = invoke(tmp_path, """
$SourceRoot=Join-Path $PWD 'candidate with spaces'
$source=Get-SupportCompilation
@{source=$source;sha256=(Hash-SupportText $source)} | ConvertTo-Json
""")
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt['source'] == expected
    assert receipt['sha256'] == hashlib.sha256(expected.encode()).hexdigest()


def test_windowless_api_contract_matches_product_python_boundary():
    import ast

    tree = ast.parse((ROOT / 'scripts/release/windows_launcher.py').read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == 'call'
             and any(k.arg == 'creationflags' for k in node.keywords)]
    assert len(calls) == 1
    flags = next(k.value for k in calls[0].keywords if k.arg == 'creationflags')
    assert isinstance(flags, ast.IfExp) and ast.literal_eval(flags.orelse) == 0x08000000
    runner = RUNNER.read_text(encoding='ascii')
    assert '$child.StartInfo.UseShellExecute=$false; $child.StartInfo.CreateNoWindow=$true' in runner
    assert 'Start-Process' not in runner and '-NoNewWindow' not in runner
    assert '-STA' not in runner and '-MTA' not in runner


def test_runner_syntax_and_native_fixture_argv(tmp_path):
    text = RUNNER.read_text(encoding='ascii')
    assert '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File' in text
    assert '-Mode Native -Scratch' in text and '-ConsoleSource' in text
    assert '-STA' not in text and '-Mode Portable' not in text
    assert 'Start-ToolkitSetup' not in text and 'Stop-ToolkitSetup' not in text
    assert '$null=$child.Handle' in text
    result = invoke(tmp_path, """
$tokens=$null; $errors=$null
$null=[Management.Automation.Language.Parser]::ParseFile($SupportRunner,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
Write-Output 'PASS parser; NOT PS5.1 execution'
""")
    assert result.returncode == 0, result.stdout + result.stderr


def test_workflow_run_upload_final_gate_is_narrow_and_ordered():
    workflow = (ROOT / '.github/workflows/release.yml').read_text()
    start = workflow.find('      - name: Native support wrapper fixture (Windows PowerShell 5.1)\n')
    assert start >= 0, 'SUPPORT-CI-WIRING-MISSING: Run/upload/final Validate absent'
    end = workflow.index('      - name: Record artifact hashes and byte sizes\n')
    block = workflow[start:end]
    assert block.count("if: always() && matrix.platform == 'Windows'") == 3
    assert block.index('-Mode Run') < block.index('actions/upload-artifact@v4') < block.index('-Mode Validate')
    assert block.count('if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }') == 2
    assert 'continue-on-error' not in block
    assert 'path: support-native-receipts/' in block
    assert 'if-no-files-found: error' in block
    # Do not require a historical Git object: Actions checkouts are shallow.
    # Full byte-preservation against the delegated base is a private patch check.
    header = workflow.split('jobs:', 1)[0]
    assert '  workflow_dispatch:\n' in header
    assert "tags: ['v*']" in header
    assert 'branches: [feature/release-builds, release/toolkit-2026-10-flashflow]' in header
    assert 'permissions:\n  contents: read\n' in header
    assert 'timeout-minutes: 3' in block and 'timeout-minutes: 1' in block
    release = workflow.split('\n  release:\n', 1)[1]
    assert "if: startsWith(github.ref, 'refs/tags/v')" in release
    assert 'Refusing to modify a published release' in release
    assert 'gh release create "$TAG" --draft --verify-tag' in release
    assert 'sha256sum Jibo-DFU-* LICENSE THIRD-PARTY-NOTICES.md build-info-*.json > SHA256SUMS' in release


def test_workflow_contract_does_not_need_git_history(tmp_path, monkeypatch):
    workflow = (ROOT / '.github/workflows/release.yml').read_bytes()
    (tmp_path / '.github/workflows').mkdir(parents=True)
    (tmp_path / '.github/workflows/release.yml').write_bytes(workflow)
    monkeypatch.setitem(globals(), 'ROOT', tmp_path)
    test_workflow_run_upload_final_gate_is_narrow_and_ordered()
