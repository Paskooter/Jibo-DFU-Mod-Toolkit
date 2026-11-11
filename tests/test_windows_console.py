"""Release console lifecycle checks; no Windows setup or device access."""
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def windows_launcher(tmp_path, monkeypatch, args):
    root = tmp_path / 'payload'
    root.mkdir()
    (root / 'payload.tar.gz').write_bytes(b'fixture')
    (root / 'payload.sha256').write_text(hashlib.sha256(b'fixture').hexdigest())
    (root / 'revision.txt').write_text('a' * 40)
    monkeypatch.setattr(sys, '_MEIPASS', str(root), raising=False)
    monkeypatch.setattr(sys, 'argv', ['toolkit'] + args)
    spec = importlib.util.spec_from_file_location('console_launcher', ROOT / 'scripts/release/windows_launcher.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bootstrap_harness_stubs_native_dialogs_in_both_child_processes():
    source = (ROOT / 'scripts/release/test-bootstrap.ps1').read_text()
    assert source.count('public class ToolkitConsoleNative') == 2
    assert source.count('public static int MessageBox') == 2


@pytest.mark.parametrize('reminder', ['question', 'wait', 'question-behind'])
def test_question_reminder_fixture_observes_delayed_event(tmp_path, reminder):
    """Owned scheduler control, not a native Windows UI or CI40 diagnosis."""
    import shutil
    pwsh = os.environ.get('PWSH') or shutil.which('pwsh') or shutil.which('powershell.exe')
    if not pwsh:
        pytest.skip('requires PowerShell')
    fixture = ROOT / 'scripts/release/test-console.ps1'
    assert pwsh is not None
    source = fixture.read_text()
    # Execute the real reminder assertion from the Windows fixture against an
    # event source whose first tick is delayed past the old 300/500 ms sleeps.
    if reminder == 'question':
        assertion = source.split('# Advance the real question', 1)[1].split(
            "[ToolkitSetupStatus]::AnswerQuestion('closed')", 1)[0]
    elif reminder == 'wait':
        assertion = source.split('# Fire the 60-second wait reminder', 1)[1].split('if ($window.TopMost', 1)[0]
    else:
        assertion = source.split('# reminder must not take the front again.', 1)[1].split(
            "$model = [ToolkitSetupStatus]", 1)[1].split('if ($window.TopMost', 1)[0]
        assertion = '\n$model = [ToolkitSetupStatus]' + assertion
    assertion = assertion.split('\n', 1)[1]
    kind = 'user-wait-reminder' if reminder == 'wait' else 'question-reminder'
    script = tmp_path / 'reminder-delay.ps1'
    script.write_text(r"""
$ErrorActionPreference = 'Stop'
. '""" + str(ROOT / 'scripts/release/console.ps1').replace("'", "''") + r"""'
Add-Type -TypeDefinition ($script:ToolkitQuestionModelSource + @'
public static class ToolkitSetupStatus {
    static readonly ToolkitQuestionState question = new ToolkitQuestionState();
    static readonly System.Diagnostics.Stopwatch clock = System.Diagnostics.Stopwatch.StartNew();
    static int elapsed;
    static double lastExternalAttention = 0;
    public static int Reads;
    public static string Kind;
    public static void Advance(int ms) { elapsed += ms; }
    public static ToolkitInteractionEvent[] DrainEvents() {
        Reads++;
        if (elapsed >= 600 && lastExternalAttention <= clock.Elapsed.TotalSeconds &&
            (Kind == "user-wait-reminder" || question.Remind(clock.Elapsed.TotalSeconds)))
            return new[] { new ToolkitInteractionEvent(Kind, "Synthetic delayed tick") };
        return new ToolkitInteractionEvent[0];
    }
}
'@)
function Start-Sleep { param([int]$Milliseconds); [ToolkitSetupStatus]::Advance($Milliseconds) }
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile('""" + str(fixture).replace("'", "''") + r"""', [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($f in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Wait-ToolkitFixtureInteraction'}, $true)) { Invoke-Expression $f.Extent.Text }
$flags = [Reflection.BindingFlags]'Static,NonPublic'
$clock = [ToolkitSetupStatus].GetField('clock',$flags).GetValue($null)
[ToolkitSetupStatus]::Kind = '""" + kind + r"""'
""" + assertion + r"""
if ([ToolkitSetupStatus]::Reads -lt 2) { throw 'Did not wait for an observed timer event' }
$refused = $false
try { Wait-ToolkitFixtureInteraction -Kind question-reminder -TimeoutSeconds 0.03 -ReadEvents { @() } }
catch { if ($_.Exception.Message -notmatch 'question-reminder.*deadline') { throw }; $refused = $true }
if (-not $refused) { throw 'A permanently missing reminder was accepted' }
'PASS deterministic reminder: delayed 600 ms event observed; absent event refused'
""")
    result = subprocess.run([pwsh, '-NoProfile', '-NonInteractive', '-File', str(script)],
                            capture_output=True, text=True,
                            env={**os.environ, 'TMPDIR': str(tmp_path)}, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PASS deterministic reminder' in result.stdout


@pytest.mark.parametrize('case', [
    'setup-questions', 'setup-wait-behind', 'support-not-topmost', 'setup-feedback', 'setup-stalls', 'wsl-prerequisite', 'installer-wait', 'setup-download', 'native-status', 'native-command', 'classic', 'terminal', 'gui-success', 'gui-failed', 'never-ready',
    'watcher-failed', 'monitor-failed', 'show-console', 'setup-decline',
    'gui-start-failed', 'cleanup-failed', 'monitor-prompt', 'terminal-failed', 'hide-fallback',
    'watcher-exit-race', 'monitor-exit-race', 'gui-handle', 'watcher-handle', 'gui-code-unavailable', 'window-copy', 'window-missing', 'display-copy-preflight', 'display-copy-repaired',
])
def test_console_lifecycle_with_mocked_win32(tmp_path, case):
    import shutil
    pwsh = os.environ.get('PWSH') or shutil.which('pwsh') or shutil.which('powershell.exe')
    if not pwsh:
        pytest.skip('requires PowerShell')
    result = subprocess.run([pwsh, '-NoProfile', '-File', str(ROOT / 'scripts/release/test-console.ps1'),
                             '-Case', case], capture_output=True, text=True,
                            env={**os.environ, 'TMPDIR': str(tmp_path)}, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PASS console: ' + case in result.stdout


def test_download_fixture_accepts_delayed_coalesced_presentation(tmp_path):
    """UI notifications may coalesce; they must never gate the HTTP producer."""
    import shutil
    pwsh = os.environ.get('PWSH') or shutil.which('pwsh') or shutil.which('powershell.exe')
    if not pwsh:
        pytest.skip('requires PowerShell')
    assert pwsh is not None
    source = (ROOT / 'scripts/release/test-console.ps1').read_text()
    observe = 'if ($server) { $server.Observe([int]$Percent) }'
    assert source.count(observe) == 1
    # Keep the real WebClient, bytes and consumer loop. Delay the first positive
    # presentation and coalesce subsequent notifications until at least half the
    # body has arrived. The old per-64-KiB presentation barrier deadlocks here.
    source = source.replace(observe, r"""
        if ($Percent -gt 0) {
            $script:positivePresentations++
            if ($script:positivePresentations -eq 1 -or $Percent -lt 50) {
                $script:withheldPresentations++
            } else {
                $server.Observe([int]$Percent)
                $script:deliveredPresentations++
            }
        }
""")
    reset = '$script:intermediate = 0'
    assert source.count(reset) == 1
    source = source.replace(reset, reset + r"""
            $script:positivePresentations = 0
            $script:withheldPresentations = 0
            $script:deliveredPresentations = 0
""")
    check = "if ($stalled -and (Test-Path $file)) { throw 'Stalled download was not removed' }"
    assert source.count(check) == 1
    source = source.replace(check, check + r"""
                if ($mode -eq 'active' -and ($script:withheldPresentations -lt 1 -or $script:deliveredPresentations -lt 1)) {
                    throw 'Controlled delayed/coalesced presentation was not exercised'
                }
""")
    script = tmp_path / 'coalesced-download.ps1'
    script.write_text(source)
    result = subprocess.run([pwsh, '-NoProfile', '-NonInteractive', '-File', str(script),
                             '-BootstrapPath', str(ROOT / 'scripts/release/bootstrap.ps1'),
                             '-Case', 'setup-download'], capture_output=True, text=True,
                            env={**os.environ, 'TMPDIR': str(tmp_path)}, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PASS console: setup-download' in result.stdout


def test_download_fixture_refusals_survive_framework_timespan_rounding(tmp_path):
    """Native Windows PowerShell 5.1 regression (release run 43029164361).

    .NET Framework computes TimeSpan.TotalSeconds as ticks * (1.0 / 1e7), so an
    injected virtual gap of exactly 400 ms reads as 0.39999999999999997 s and
    never reaches the 0.4 s fixture threshold. .NET Core divides exactly, which
    hid this on Linux. Model the Framework arithmetic in the process-local copy
    of the real consumer and run the unchanged setup-download refusals.
    """
    import shutil
    pwsh = os.environ.get('PWSH') or shutil.which('pwsh') or shutil.which('powershell.exe')
    if not pwsh:
        pytest.skip('requires PowerShell')
    assert pwsh is not None
    source = (ROOT / 'scripts/release/test-console.ps1').read_text()
    inject = "Invoke-Expression $downloadSource.Replace($clockLine, '$idle = [ToolkitFixtureIdleClock]::StartNew()')"
    assert source.count(inject) == 1
    consumer = (ROOT / 'scripts/release/console.ps1').read_text()
    reading = '$idle.Elapsed.TotalSeconds'
    assert consumer.count(reading) == 1
    framework = "($idle.Elapsed.Ticks * (1.0 / 10000000))"
    source = source.replace(inject, inject.replace(
        'Invoke-Expression $downloadSource',
        "Invoke-Expression $downloadSource.Replace('" + reading + "', '" + framework + "')"))
    script = tmp_path / 'framework-timespan-download.ps1'
    script.write_text(source)
    result = subprocess.run([pwsh, '-NoProfile', '-NonInteractive', '-File', str(script),
                             '-BootstrapPath', str(ROOT / 'scripts/release/bootstrap.ps1'),
                             '-Case', 'setup-download'], capture_output=True, text=True,
                            env={**os.environ, 'TMPDIR': str(tmp_path)}, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    for mode in ('active', 'idle', 'headers'):
        assert 'PASS download observation: mode=' + mode in result.stdout
    assert 'PASS console: setup-download' in result.stdout


@pytest.mark.parametrize('args', [
    ['-ShowConsole'], ['-ShowConsole', '-Distro', 'Ubuntu-22.04'],
    ['-Distro', 'Ubuntu-22.04', '-ShowConsole'],
])
def test_show_console_switch_is_forwarded_after_the_banner(tmp_path, monkeypatch, capsys, args):
    module = windows_launcher(tmp_path, monkeypatch, args)
    calls = []

    def call(argv, **kwargs):
        assert capsys.readouterr().out.splitlines()[0] == 'Jibo DFU Toolkit (build aaaaaaa)'
        calls.append(argv)
        return 41

    monkeypatch.setattr(subprocess, 'call', call)
    assert module.main() == 41
    assert calls[0][-len(args):] == args


def test_console_controller_is_embedded_in_windows_release(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts/release'))
    spec = importlib.util.spec_from_file_location('console_build', ROOT / 'scripts/release/build.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (tmp_path / 'payload.tar.gz').write_bytes(b'fixture')
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    monkeypatch.setattr(module, 'download', lambda *args: None)
    calls = []
    from release_fakes import windows_build_run
    monkeypatch.setattr(module, 'run', windows_build_run(calls, tmp_path))
    module.windows(tmp_path)
    assert str(tmp_path / 'scripts/release/console.ps1') + ';.' in calls[0]


def test_windows_ci_runs_the_console_lifecycle_regressions():
    workflow = (ROOT / '.github/workflows/release.yml').read_text()
    assert 'powershell -NoProfile -File scripts/release/test-console.ps1 -Case all' in workflow


def test_wsl_probe_closes_stdin_without_framework_autoflush_input(tmp_path):
    import shutil
    pwsh = os.environ.get('PWSH') or shutil.which('pwsh') or shutil.which('powershell.exe')
    if not pwsh:
        pytest.skip('requires PowerShell')
    assert pwsh is not None
    # .NET Framework Process.Start constructs StandardInput with Console.InputEncoding
    # and enables AutoFlush. Encoding.UTF8 emits EF BB BF even without a Write call.
    # Model that exact transport on Core, then run the unchanged strict-stdin fixture.
    script = tmp_path / 'framework-input.ps1'
    script.write_text(r"""
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition @'
using System;
using System.Diagnostics;
using System.IO;
public class ToolkitFrameworkInputProcess : Process {
    public static int Starts;
    public static int PreambleBytes;
    public new bool Start() {
        var encoding = Console.InputEncoding;
        bool started = base.Start();
        Starts++;
        using (var input = new StreamWriter(StandardInput.BaseStream, encoding, 4096, true)) {
            input.AutoFlush = true;
        }
        PreambleBytes += encoding.GetPreamble().Length;
        return started;
    }
}
'@
function New-Object {
    param([string]$TypeName, [object[]]$ArgumentList)
    if ($TypeName -eq 'Diagnostics.Process') {
        return Microsoft.PowerShell.Utility\New-Object ToolkitFrameworkInputProcess
    }
    Microsoft.PowerShell.Utility\New-Object @PSBoundParameters
}
$previous = [Console]::InputEncoding
try {
    [Console]::InputEncoding = [Text.Encoding]::UTF8
    & '""" + str(ROOT / 'scripts/release/test-console.ps1').replace("'", "''") + r"""' -Case wsl-prerequisite
    $code = $LASTEXITCODE
    if ($code -ne 0) { exit $code }
    if ([ToolkitFrameworkInputProcess]::Starts -ne 10) { throw 'Framework transport was not exercised for every probe' }
    if ([ToolkitFrameworkInputProcess]::PreambleBytes -ne 0) { throw 'Probe sent input before closing stdin' }
    if ([BitConverter]::ToString([Console]::InputEncoding.GetPreamble()) -cne 'EF-BB-BF') { throw 'Probe did not restore the caller input encoding' }
    . '""" + str(ROOT / 'scripts/release/console.ps1').replace("'", "''") + r"""'
    $failed = Invoke-ToolkitNative -File (Join-Path $PSScriptRoot 'missing/wsl.exe') -Arguments @('--version') -WatchWslSetup
    if ($failed.ExitCode -ne 1 -or -not $failed.StartError -or $failed.WslSetupRequired -or $failed.TimedOut) { throw 'Failed probe start was misclassified' }
    if ([BitConverter]::ToString([Console]::InputEncoding.GetPreamble()) -cne 'EF-BB-BF') { throw 'Failed start did not restore the caller input encoding' }
    'PASS framework stdin: 10 probes, zero input bytes, encoding restored'
} finally { [Console]::InputEncoding = $previous }
exit $code
""")
    result = subprocess.run([pwsh, '-NoProfile', '-NonInteractive', '-File', str(script)],
                            capture_output=True, text=True,
                            env={**os.environ, 'TMPDIR': str(tmp_path)}, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PASS framework stdin: 10 probes, zero input bytes, encoding restored' in result.stdout


@pytest.mark.parametrize('failure', ['checksum', 'arguments'])
def test_exe_failure_keeps_diagnostics_visible_until_acknowledged(tmp_path, monkeypatch, capsys, failure):
    module = windows_launcher(tmp_path, monkeypatch, ['--bogus'] if failure == 'arguments' else [])
    if failure == 'checksum':
        (tmp_path / 'payload/payload.sha256').write_text('0' * 64)
    dialogs = []

    def acknowledge(message):
        assert capsys.readouterr().out.splitlines()[0] == 'Jibo DFU Toolkit (build aaaaaaa)'
        dialogs.append(message)

    monkeypatch.setattr(module, 'show_error', acknowledge)
    monkeypatch.setattr('builtins.input', lambda _: pytest.fail('invisible console wait'))
    assert module.run() == 1
    assert len(dialogs) == 1
    assert ('checksum' if failure == 'checksum' else 'Usage:') in dialogs[0]


def test_self_check_requires_the_console_controller(tmp_path, monkeypatch):
    import io
    import tarfile

    module = windows_launcher(tmp_path, monkeypatch, ['--self-check'])
    root = tmp_path / 'payload'
    with tarfile.open(root / 'payload.tar.gz', 'w:gz') as archive:
        for name in ('jibo-dfu', 'AppRun', '_internal/runtime/licenses/LICENSE',
                     '_internal/runtime/licenses/THIRD-PARTY-NOTICES.md'):
            entry = tarfile.TarInfo(name)
            entry.size = 1
            archive.addfile(entry, io.BytesIO(b'x'))
    (root / 'payload.sha256').write_text(hashlib.sha256((root / 'payload.tar.gz').read_bytes()).hexdigest())
    for name in ('bootstrap.ps1', 'run.ps1', 'wsl-usb-permissions.sh', 'wsl-user-launch.sh',
                 'wsl-provision.sh', 'LICENSE', 'NOTICE.md', 'THIRD-PARTY-NOTICES.md', 'usbipd.ps1'):
        (root / name).write_text('fixture')
    with pytest.raises(RuntimeError, match='Missing embedded Windows helper: console.ps1'):
        module.main()
    (root / 'console.ps1').write_text('fixture')
    assert module.main() == 0


@pytest.mark.parametrize('args', [
    ['-ShowConsole', '-ShowConsole'], ['-Distro', 'a', '-Distro', 'b'],
    ['-ShowConsole', '--version'], ['-Distro', 'a b'], ['-Distro', 'a\"b'],
    ['-Distro'], ['-ShowConsole', 'extra'], ['-ShowConsole:true'], ['--bogus'],
])
def test_show_console_does_not_weaken_argument_validation(tmp_path, monkeypatch, args):
    module = windows_launcher(tmp_path, monkeypatch, args)
    monkeypatch.setattr(subprocess, 'call', lambda _: pytest.fail('invalid args reached PowerShell'))
    with pytest.raises(RuntimeError, match='Usage:.*ShowConsole'):
        module.main()



def test_ready_signal_cannot_hide_console_during_a_prompt(tmp_path):
    helper = (ROOT / 'scripts/release/console.ps1').read_text()
    bootstrap = (ROOT / 'scripts/release/bootstrap.ps1').read_text()
    assert 'GetConsoleWindow' not in helper
    assert 'ShowWindow(owner, 5)' in helper  # Owned error fallback is shown, never a console hidden.
    assert 'Set-ToolkitConsole' not in bootstrap
    assert 'Stop-ToolkitSetup' in bootstrap
    assert 'Invoke-ToolkitConsolePrompt' not in (ROOT / 'run.ps1').read_text()


@pytest.mark.parametrize('code', [0, 41])
def test_windows_wrapper_forwards_readiness_and_cleans_both_markers(tmp_path, code):
    if not hasattr(os, 'geteuid') or os.geteuid() == 0:
        pytest.skip('requires non-root Linux user')
    binary = tmp_path / 'gui'
    binary.write_text('#!/bin/bash\n'
                      '[[ ! -e $JIBO_GUI_READY_FILE ]] || exit 9\n'
                      '[[ -s $SESSION ]] || exit 10\n'
                      'printf "ready\\n" > "$JIBO_GUI_READY_FILE"\n'
                      f'exit {code}\n')
    binary.chmod(0o755)
    session = tmp_path / 'session'
    ready = tmp_path / 'ready'
    ready.write_text('stale')
    result = subprocess.run(['bash', str(ROOT / 'scripts/release/wsl-user-launch.sh'),
                             str(binary), str(session), str(ready)],
                            env={**os.environ, 'SESSION': str(session)},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == code, result.stderr
    assert not session.exists() and not ready.exists()


@pytest.mark.skipif(os.environ.get('JIBO_GUI_TEST_DISPLAY') != '1', reason='requires a display')
@pytest.mark.parametrize('mapped', [False, True, 'initial'])
def test_gui_readiness_requires_a_mapped_main_window(tmp_path, monkeypatch, mapped):
    import jibo_gui
    from gui_fakes import FakeApi

    ready = tmp_path / 'ready'
    monkeypatch.setenv('JIBO_GUI_READY_FILE', str(ready))
    root = jibo_gui.create_root()
    if mapped == 'initial':
        root.update()
    else:
        root.withdraw()
    monkeypatch.setattr(jibo_gui, 'create_root', lambda: root)
    windows = []
    original_window = jibo_gui.ToolkitWindow

    def window(*args):
        result = original_window(*args)
        windows.append(result)
        return result

    monkeypatch.setattr(jibo_gui, 'ToolkitWindow', window)

    def mainloop():
        if mapped != 'initial':
            root.update()
            assert not ready.exists(), 'construction is not window readiness'
        if mapped:
            root.deiconify()
            root.after(200, root.quit)
            original_mainloop()
            assert root.winfo_viewable()
            assert ready.read_text() == 'ready\n'

    original_mainloop = root.mainloop
    monkeypatch.setattr(root, 'mainloop', mainloop)
    try:
        assert jibo_gui.main(FakeApi('none')) == 0
    finally:
        for window in windows:
            window.close()
