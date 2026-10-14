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


@pytest.mark.parametrize('case', [
    'classic', 'terminal', 'gui-success', 'gui-failed', 'never-ready',
    'watcher-failed', 'monitor-failed', 'show-console', 'setup-decline',
    'gui-start-failed', 'cleanup-failed', 'monitor-prompt', 'terminal-failed', 'hide-fallback',
    'watcher-exit-race', 'monitor-exit-race', 'gui-handle', 'watcher-handle', 'gui-code-unavailable',
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


@pytest.mark.parametrize('args', [
    ['-ShowConsole'], ['-ShowConsole', '-Distro', 'Ubuntu-22.04'],
    ['-Distro', 'Ubuntu-22.04', '-ShowConsole'],
])
def test_show_console_switch_is_forwarded_after_the_banner(tmp_path, monkeypatch, capsys, args):
    module = windows_launcher(tmp_path, monkeypatch, args)
    calls = []

    def call(argv):
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
    monkeypatch.setattr(module, 'run', lambda *args: calls.append(args))
    module.windows(tmp_path)
    assert str(tmp_path / 'scripts/release/console.ps1') + ';.' in calls[0]


def test_windows_ci_runs_the_console_lifecycle_regressions():
    workflow = (ROOT / '.github/workflows/release.yml').read_text()
    assert 'powershell -NoProfile -File scripts/release/test-console.ps1 -Case all' in workflow


@pytest.mark.parametrize('failure', ['checksum', 'arguments'])
def test_exe_failure_keeps_diagnostics_visible_until_acknowledged(tmp_path, monkeypatch, capsys, failure):
    module = windows_launcher(tmp_path, monkeypatch, ['--bogus'] if failure == 'arguments' else [])
    if failure == 'checksum':
        (tmp_path / 'payload/payload.sha256').write_text('0' * 64)
    prompts = []

    def acknowledge(prompt):
        text = capsys.readouterr().out
        assert text.splitlines()[0] == 'Jibo DFU Toolkit (build aaaaaaa)'
        assert 'Launcher:' in text
        prompts.append(prompt)
        return ''

    monkeypatch.setattr('builtins.input', acknowledge)
    assert hasattr(module, 'run'), 'EXE failures need a visible acknowledgement path'
    assert module.run() == 1
    assert prompts == ['Press Enter to close']


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
                 'wsl-provision.sh', 'LICENSE', 'NOTICE.md', 'THIRD-PARTY-NOTICES.md'):
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
    import shutil
    import time

    pwsh = os.environ.get('PWSH') or shutil.which('pwsh') or shutil.which('powershell.exe')
    if not pwsh:
        pytest.skip('requires PowerShell')
    helper = ROOT / 'scripts/release/console.ps1'
    assert helper.is_file(), 'missing console prompt synchronization'
    child = tmp_path / 'console-lock.ps1'
    child.write_text(r'''
param($Helper, $Trace, $Hold, $Release, $Mode)
$ErrorActionPreference = 'Stop'
$env:DFU_CONSOLE_TEST_TRACE = $Trace
Add-Type -TypeDefinition @'
using System;
using System.IO;
public class ToolkitConsoleNative {
    public static IntPtr GetConsoleWindow() { return new IntPtr(42); }
    public static int DwmGetWindowAttribute(IntPtr hwnd, int attr, out int value, int size) { value=0; return 0; }
    public static bool IsWindowVisible(IntPtr hwnd) { return false; }
    public static bool ShowWindow(IntPtr hwnd, int command) { File.AppendAllText(Environment.GetEnvironmentVariable("DFU_CONSOLE_TEST_TRACE"), command + "\n"); return false; }
}
'@
. $Helper
if ($Mode -eq 'prompt') {
    Invoke-ToolkitConsolePrompt {
        [IO.File]::WriteAllText($Hold, 'prompt open')
        while (-not (Test-Path $Release)) { Start-Sleep -Milliseconds 20 }
    }
} else {
    [IO.File]::WriteAllText(($Hold + '.attempt'), 'hide requested')
    Set-ToolkitConsole
    [IO.File]::WriteAllText(($Hold + '.done'), 'hide completed')
}
''')
    trace, hold, release = [tmp_path / name for name in ('trace', 'hold', 'release')]
    children = []

    def wait_for(path):
        deadline = time.monotonic() + 10
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert path.exists(), 'child synchronization did not run: ' + path.name

    try:
        for mode, marker in [('prompt', hold), ('hide', Path(str(hold) + '.attempt'))]:
            children.append(subprocess.Popen([pwsh, '-NoProfile', '-File', str(child), str(helper),
                                              str(trace), str(hold), str(release), mode],
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            wait_for(marker)
        time.sleep(0.1)
        assert not Path(str(hold) + '.done').exists(), 'readiness hid an active prompt'
        assert trace.read_text().splitlines() == ['9']
        release.write_text('approved')
        for process in children:
            out, err = process.communicate(timeout=10)
            assert process.returncode == 0, out + err
        assert trace.read_text().splitlines() == ['9', '0']
    finally:
        release.touch()
        for process in children:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=10)


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
