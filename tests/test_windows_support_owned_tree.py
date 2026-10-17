"""Linux falsification of the support supervisor; never native acceptance.

The outer process is an independent subreaper. Only actual adopted direct
children and their retained pidfds authorize emergency test cleanup. The
supervisor under test is not the safety authority for these controls.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = Path(os.environ.get('SUPPORT_NATIVE_RUNNER', ROOT / 'scripts/release/test-support-native.ps1'))
PWSH = os.environ.get('PWSH') or shutil.which('pwsh')


def outer_probe(folder, case, runner, pwsh):
    import ctypes
    import select
    import signal

    folder = Path(folder).resolve()
    assert ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) == 0
    def children():
        return [int(p) for p in Path('/proc/self/task', str(os.getpid()), 'children').read_text().split()]
    def state(pid):
        try:
            text = Path('/proc', str(pid), 'stat').read_text()
            fields = text[text.rindex(')') + 2:].split()
            return dict(pid=pid, state=fields[0], ppid=int(fields[1]), start_ticks=int(fields[19]))
        except FileNotFoundError:
            return dict(pid=pid, state='absent')
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    leaf = folder / 'owned_leaf.py'
    ready = folder / 'leaf-ready.txt'
    leaf.write_text("import os,pathlib,sys,time\npathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\nprint('owned leaf entered',flush=True)\ntime.sleep(40)\n")
    parent = folder / 'owned_parent.py'
    parent.write_text("import pathlib,subprocess,sys,time\nleaf,ready,mode=sys.argv[1:]\n" +
        ("print('baseline entered',flush=True)\nsys.exit(0)\n" if case == 'baseline' else
         "p=subprocess.Popen([sys.executable,leaf,ready],stdout=None if mode=='inherited' else subprocess.DEVNULL,stderr=None if mode=='inherited' else subprocess.DEVNULL)\ndeadline=time.monotonic()+5\nwhile not pathlib.Path(ready).exists():\n if time.monotonic()>deadline: raise RuntimeError('leaf readiness timeout')\n time.sleep(.01)\nprint('owned parent leaf ready',flush=True)\n" +
         ("time.sleep(40)\n" if case in ('cleanup-helper-failure', 'termination-failure') else "sys.exit(0)\n")))
    setup = ''
    if case == 'cleanup-helper-failure':
        system = folder / 'FAKE_SYSTEM_ROOT'
        (system / 'System32').mkdir(parents=True)
        helper = system / 'System32/taskkill.exe'
        helper.write_text('#!/bin/sh\nprintf "owned helper deliberately refused\\n"\nexit 17\n')
        helper.chmod(0o700)
        setup = "$env:OS='Windows_NT'; $env:SystemRoot=" + quote(system) + "\n" + "if (Get-Command New-SupportContainment -ErrorAction SilentlyContinue) { function Stop-SupportChild { throw 'Owned tree cleanup failed (injected)' } }\n"
    elif case == 'admission-unconfirmed':
        setup = 'function Admit-SupportChild { }\n'
    elif case in ('initialization-failure', 'admission-failure', 'query-failure', 'termination-failure'):
        function, message = {
            'initialization-failure': ('New-SupportContainment', 'Owned containment initialization failed (injected)'),
            'admission-failure': ('Admit-SupportChild', 'Owned containment admission failed (injected)'),
            'query-failure': ('Get-SupportOwnedCount', 'Owned containment query failed (injected)'),
            'termination-failure': ('Stop-SupportChild', 'Owned containment termination failed (injected)'),
        }[case]
        setup = 'function ' + function + " { throw '" + message + "' }\n"
    arguments = '"' + str(parent) + '" "' + str(leaf) + '" "' + str(ready) + '" ' + ('inherited' if case == 'exited-parent-inherited-streams' else 'detached')
    wrapper = folder / 'control.ps1'
    wrapper.write_text("$ErrorActionPreference='Stop'\n. " + quote(runner) + ' -SourceRoot ' + quote(ROOT) + '\n' + setup +
        '$phase=Invoke-SupportChild ' + quote(sys.executable) + ' ' + quote(arguments) + ' ' + quote(folder / 'phase') + " 'ownership' 4\n" +
        "$rejection=''; try { Assert-SupportPhase $phase 'ownership' $phase.started_utc $phase.ended_utc } catch { $rejection=$_.Exception.Message }\n" +
        '@{phase=$phase;phase_gate_rejection=$rejection;native_acceptance=$false} | ConvertTo-Json -Depth 16\n')
    retained = {}
    observations = []
    process = None
    record: dict = dict(case=case, native_execution=False, safety_authority='independent outer subreaper, retained pidfds of actual adopted direct children; never journal PID signalling')
    def adopt():
        for pid in children():
            if process is not None and pid == process.pid or pid in retained:
                continue
            before = state(pid)
            fd = os.pidfd_open(pid)
            after = state(pid)
            assert before.get('ppid') == os.getpid() and after.get('ppid') == os.getpid()
            assert before.get('start_ticks') == after.get('start_ticks')
            retained[pid] = fd
    started = time.monotonic()
    try:
        with (folder / 'wrapper.stdout.txt').open('wb') as out, (folder / 'wrapper.stderr.txt').open('wb') as err:
            process = subprocess.Popen([pwsh, '-NoProfile', '-NonInteractive', '-File', str(wrapper)], cwd=folder, stdout=out, stderr=err)
            while process.poll() is None:
                adopt()
                # The leaf creates the file, then writes its PID: read '' until then.
                text = ready.read_text() if ready.exists() else ''
                if text.isdigit():
                    observed = state(int(text))  # read-only, never signal authority
                    if not observations or observations[-1]['state'] != observed['state']:
                        observations.append(observed)
                if time.monotonic() - started > 22:
                    raise TimeoutError('OUTER-PROBE-DEADLINE')
                time.sleep(.01)
            process.wait(timeout=1)
        adopt()
        assert process.returncode == 0, (folder / 'wrapper.stderr.txt').read_text()
        record.update(result=json.loads((folder / 'wrapper.stdout.txt').read_text()), observations=observations,
                      leaf_entered=ready.exists(), seconds_before_outer_cleanup=time.monotonic() - started)
        record['pre_cleanup_owned'] = [dict(**state(pid), pidfd_exit_readable=bool(select.select([fd], [], [], 0)[0])) for pid, fd in retained.items()]
        record['live_before_outer_cleanup'] = [s for s in record['pre_cleanup_owned'] if s['state'] not in ('absent', 'Z', 'X') and not s['pidfd_exit_readable']]
    finally:
        if process is not None and process.poll() is None:
            process.kill()  # retained original wrapper Popen, not any journal PID
            process.wait(timeout=5)
        cleanup = []
        deadline = time.monotonic() + 5
        while True:
            adopt()
            for pid, fd in list(retained.items()):
                alive = not bool(select.select([fd], [], [], 0)[0])
                if alive:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                assert select.select([fd], [], [], max(0, deadline - time.monotonic()))[0]
                waited, status = os.waitpid(pid, 0)
                assert waited == pid
                os.close(fd)
                del retained[pid]
                cleanup.append(dict(pid=pid, signalled=alive, wait_status=status, after=state(pid)))
            if not children():
                break
            assert time.monotonic() < deadline
        record.update(outer_cleanup=cleanup, all_outer_children_reaped=not children())
        (folder / 'RECEIPT.json').write_text(json.dumps(record, indent=2) + '\n')
    return record


@pytest.mark.skipif(sys.platform != 'linux', reason='independent Linux subreaper controls, not native proof')
@pytest.mark.parametrize('case', ['baseline', 'exited-parent-inherited-streams', 'exited-parent-detached-streams', 'cleanup-helper-failure',
                                 'initialization-failure', 'admission-failure', 'admission-unconfirmed', 'query-failure', 'termination-failure'])
def test_owned_tree_original_edges_and_failure_recovery(tmp_path, case):
    assert PWSH, 'PowerShell required for ownership controls'
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), str(tmp_path), case, str(RUNNER), PWSH],
                            capture_output=True, text=True, timeout=32)
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads((tmp_path / 'RECEIPT.json').read_text())
    assert record['all_outer_children_reaped']
    assert not record['live_before_outer_cleanup'], 'OWNED-TREE-SURVIVED: ' + case
    phase = record['result']['phase']
    rejection = record['result']['phase_gate_rejection']
    if case in ('initialization-failure', 'admission-failure', 'admission-unconfirmed'):
        assert not record['leaf_entered'], 'FIXTURE-EXECUTED-BEFORE-ADMISSION: ' + case
    elif case != 'baseline':
        assert record['leaf_entered'] and record['observations'], 'ORIGINAL-EDGE-NOT-EXERCISED: ' + case
    if 'failure' in case or case == 'admission-unconfirmed':
        assert phase['state'] == 'failed' and rejection == 'Phase failed or incomplete', 'OWNERSHIP-FAILURE-NOT-CLOSED: ' + case
        assert phase['stopped'] is False, 'ERROR-MISREPORTED-STOPPED: ' + case
    else:
        assert phase['state'] == 'complete' and rejection == '', 'OWNERSHIP-POSITIVE-BASELINE: ' + case
        assert phase['tree_empty'] is True and phase['owned_remaining'] == 0, 'EMPTY-TREE-PROOF-MISSING: ' + case
        assert phase['admitted'] is True, 'ADMISSION-PROOF-MISSING: ' + case


@pytest.mark.parametrize('field,value,reason', [
    ('admitted', False, 'Owned containment not admitted'),
    ('admitted', 'true', 'Owned containment not admitted'),
    ('tree_empty', False, 'Owned tree emptiness not proved'),
    ('tree_empty', 'true', 'Owned tree emptiness not proved'),
    ('owned_remaining', 1, 'Owned tree emptiness not proved'),
    ('owned_remaining', '0', 'Owned tree emptiness not proved'),
    ('owned_remaining', None, 'Owned tree emptiness not proved'),
    ('containment_kind', 'linux-test-subreaper', 'Native owned containment mismatch'),
    ('cleanup_allowance_seconds', 20, 'Native owned containment mismatch'),
    ('supervision.gated_stdin', False, 'Native gated bootstrap mismatch'),
    ('supervision.arguments', 'not-the-bootstrap', 'Native gated bootstrap mismatch'),
    ('supervision.bootstrap_sha256', '0' * 64, 'Native gated bootstrap mismatch'),
    ('actual.arguments', 'not-the-fixture', 'Actual fixture launch mismatch'),
    ('actual.use_shell_execute', True, 'Actual fixture launch mismatch'),
    ('actual.environment.TEMP', 'unowned', 'Actual fixture environment mismatch'),
])
def test_exact_ownership_rejection_with_complete_baseline(tmp_path, field, value, reason):
    from test_windows_support_native_ci import synthetic_bundle, seal_bundle, invoke
    folder = synthetic_bundle(tmp_path)
    target = folder / ('fixture-launch.json' if field.startswith('actual.') else 'phase.json')
    data = json.loads(target.read_text())
    keys = field.split('.')[1:] if field.startswith('actual.') else field.split('.')
    cursor = data
    for key in keys[:-1]:
        cursor = cursor[key]
    cursor[keys[-1]] = value
    target.write_text(json.dumps(data))
    seal_bundle(folder)
    result = invoke(tmp_path, """
try { Validate-SupportEvidence (Join-Path $PWD 'SYNTHETIC-NOT-NATIVE') (Read-SupportJson (Join-Path $PWD 'expected.json')) }
catch { Write-Output ('REJECT:' + $_.Exception.Message); exit 9 }
throw 'EXPECTED-OWNERSHIP-REJECTION-ABSENT'
""")
    assert result.returncode == 9 and result.stdout.strip() == 'REJECT:' + reason, 'EXACT-OWNERSHIP-REJECTION-ABSENT: ' + field + '\n' + result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != 'linux', reason='Linux bootstrap execution control, not native acceptance')
def test_gated_bootstrap_defers_exact_unshimmed_invocation(tmp_path):
    """Exercise unchanged bootstrap bytes on Linux, NOT Windows/Native fixture."""
    import base64
    import hashlib
    from test_windows_support_native_ci import invoke
    payload = tmp_path / 'inert argument observer.py'
    marker = tmp_path / 'fixture-entered.json'
    payload.write_text("import json,os,pathlib,sys\npathlib.Path(sys.argv[1]).write_text(json.dumps(dict(argv=sys.argv[2:],executable=sys.executable,environment={n:os.environ.get(n,'') for n in ['TEMP','TMP','TMPDIR','LOCALAPPDATA','JIBO_DFU_SETUP_LOG']})))\nprint('inert fixture stdout',flush=True)\nprint('inert fixture stderr',file=sys.stderr,flush=True)\n")
    arguments = f'"{payload}" "{marker}" -Mode Native -Scratch "{tmp_path / "scratch with spaces"}" -ConsoleSource "{tmp_path / "not-executed.ps1"}"'
    (tmp_path / 'request.json').write_text(json.dumps(dict(binary=sys.executable, arguments=arguments, root=str(tmp_path), nonce='bootstrap-proof', seconds=4)))
    result = invoke(tmp_path, """
$r=Read-SupportJson (Join-Path $PWD 'request.json')
$bootstrap=Get-SupportBootstrap $r.binary $r.arguments $r.root $r.nonce $r.seconds
@{bootstrap=$bootstrap;sha256=(Hash-SupportText $bootstrap)} | ConvertTo-Json -Compress
""")
    assert result.returncode == 0, result.stdout + result.stderr
    generated = json.loads(result.stdout)
    bootstrap = generated['bootstrap']
    (tmp_path / 'bootstrap.ps1').write_text(bootstrap)
    assert hashlib.sha256(bootstrap.encode()).hexdigest() == generated['sha256']
    env = {**os.environ, **{n: str(tmp_path) for n in ('TEMP', 'TMP', 'TMPDIR', 'LOCALAPPDATA')}, 'JIBO_DFU_SETUP_LOG': ''}
    # No WindowStyle flag here: Linux PS rejects it. The generated bootstrap,
    # including the inner CreateNoWindow/UseShellExecute contract, is unmodified.
    with subprocess.Popen([PWSH, '-NoProfile', '-NonInteractive', '-EncodedCommand', base64.b64encode(bootstrap.encode('utf-16-le')).decode()],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env) as process:
        try:
            time.sleep(.8)
            assert process.poll() is None and not marker.exists(), 'BOOTSTRAP-EXECUTED-BEFORE-RELEASE'
            stdout, stderr = process.communicate('bootstrap-proof\n', timeout=8)
            assert process.returncode == 0, stdout + stderr
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
    observed = json.loads(marker.read_text())
    assert observed['argv'] == ['-Mode', 'Native', '-Scratch', str(tmp_path / 'scratch with spaces'), '-ConsoleSource', str(tmp_path / 'not-executed.ps1')]
    launch = json.loads((tmp_path / 'fixture-launch.json').read_text())
    assert launch['arguments'] == arguments and launch['binary'] == sys.executable
    assert launch['use_shell_execute'] is False and launch['create_no_window'] is True
    assert launch['environment'] == observed['environment']
    assert stdout.strip() == 'inert fixture stdout' and stderr.strip() == 'inert fixture stderr'


@pytest.mark.skipif(sys.platform != 'linux', reason='PS7 compile-only control; PS5.1 remains a native gate')
def test_independent_job_csharp5_syntax_only(tmp_path):
    from test_windows_support_native_ci import invoke
    result = invoke(tmp_path, """
$ast=[Management.Automation.Language.Parser]::ParseFile($SupportRunner,[ref]$null,[ref]$null)
$literal=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.StringConstantExpressionAst] -and $n.Value.Contains('public sealed class SupportOwnedJob')},$true))
if ($literal.Count -ne 1) { throw 'JOB-SOURCE-LITERAL-MISSING' }
Add-Type -TypeDefinition $literal[0].Value -CompilerOptions '/langversion:5','/warnaserror'
Write-Output 'PASS independent Job C#5 syntax only; Windows APIs NOT executed'
""")
    assert result.returncode == 0 and 'PASS independent Job C#5 syntax only' in result.stdout, result.stdout + result.stderr


if __name__ == '__main__':
    outer_probe(*sys.argv[1:])
