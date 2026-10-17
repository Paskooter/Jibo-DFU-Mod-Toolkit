"""Synthetic support seams only; never run installers, WSL, USB or a user clipboard."""
import json
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import subprocess
import select
import signal

import pytest

ROOT = Path(__file__).resolve().parents[1]
PWSH = os.environ.get('PWSH') or shutil.which('pwsh') or shutil.which('powershell.exe')
CONSOLE = Path(os.environ.get('TOOLKIT_SUPPORT_SOURCE', ROOT / 'scripts/release/console.ps1'))


def run_ps(tmp_path, code, timeout=15):
    if not PWSH:
        pytest.skip('requires PowerShell (set PWSH for portable installs)')
    script = tmp_path / 'probe.ps1'
    script.write_text("$ErrorActionPreference='Stop'\n. '" + str(CONSOLE) + "'\n" + code)
    with subprocess.Popen([PWSH, '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(script)],
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                          env={**os.environ, 'TMPDIR': str(tmp_path)}) as child:
        try:
            stdout, stderr = child.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name == 'posix':
                os.killpg(child.pid, signal.SIGKILL)
            else:
                child.kill()
            stdout, stderr = child.communicate(timeout=5)
            (tmp_path / 'timeout.stdout.txt').write_text(stdout)
            (tmp_path / 'timeout.stderr.txt').write_text(stderr)
            pytest.fail('bounded fixture timed out: ' + stdout + stderr)
        result = subprocess.CompletedProcess(child.args, child.returncode, stdout, stderr)
    (tmp_path / 'stdout.txt').write_text(result.stdout)
    (tmp_path / 'stderr.txt').write_text(result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_win11_compatibility_label_reaches_support_and_log(tmp_path):
    output = run_ps(tmp_path, r'''
$env:OS='Windows_NT'
$env:JIBO_DFU_SETUP_LOG=''
$global:RegistryCalls=0
function Get-ItemProperty([string]$Path) {
    if ($Path -ne 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion') { throw 'unexpected metadata boundary' }
    $global:RegistryCalls++
    return [pscustomobject]@{InstallationType='Client'; ProductName='Windows 10 Pro'; DisplayVersion='25H2'; CurrentBuild='26200'; UBR=9445}
}
Initialize-ToolkitLog -Root $PSScriptRoot -Build ('f'*40)
$expected='Windows 11 Pro 25H2 (build 26200.9445)'
if ($global:RegistryCalls -ne 1) { throw 'metadata fixture did not execute' }
if ($script:ToolkitWindowsVersion -ne $expected) { throw ('WIN11-LABEL: incorrect compatibility label: '+$script:ToolkitWindowsVersion) }
$details=Get-ToolkitSupportDetails 'Synthetic failure'
if (-not $details.Contains('Windows: '+$expected)) { throw 'support label mismatch' }
$start=Get-Content -Raw $script:ToolkitLogPath | ConvertFrom-Json
if ($start.windows -ne $expected) { throw 'setup-start label mismatch' }
Write-Output 'PASS WIN11-LABEL actual Initialize->support/log'
''')
    assert 'PASS WIN11-LABEL' in output


@pytest.mark.parametrize('metadata,expected', [
    ({'InstallationType': 'Client', 'ProductName': 'Windows 11 Pro', 'CurrentBuild': '26100', 'UBR': 9, 'DisplayVersion': '24H2'}, 'Windows 11 Pro 24H2 (build 26100.9)'),
    ({'InstallationType': 'Client', 'ProductName': 'Windows 10 Home', 'CurrentBuild': '19045', 'UBR': 7, 'DisplayVersion': '22H2'}, 'Windows 10 Home 22H2 (build 19045.7)'),
    ({'InstallationType': 'Server', 'ProductName': 'Windows Server 2025 Standard', 'CurrentBuild': '26100', 'UBR': 7}, 'Windows Server 2025 Standard (build 26100.7)'),
    ({'InstallationType': 'Server Core', 'ProductName': 'Windows Server 2022 Datacenter', 'CurrentBuild': '20348', 'UBR': 7}, 'Windows Server 2022 Datacenter (build 20348.7)'),
    ({'ProductName': 'Windows 10 Pro', 'CurrentBuild': '26200', 'UBR': 7}, 'Windows (build 26200.7)'),
    ({'InstallationType': 'Unknown', 'ProductName': 'Windows 11 Pro', 'CurrentBuild': '26200'}, 'Windows (build 26200)'),
    ({'InstallationType': 'Server', 'ProductName': 'Windows 10 Pro', 'CurrentBuild': '26100'}, 'Windows (build 26100)'),
    ({'InstallationType': 'Client', 'ProductName': 'Windows 11 Pro', 'CurrentBuild': '19045'}, 'Windows (build 19045)'),
    ({'InstallationType': 'Client', 'ProductName': 'Windows 10 Pro', 'CurrentBuild': 'not-a-build'}, 'Windows (version unavailable)'),
    ({'InstallationType': 'Client', 'ProductName': 'Windows 10 Pro'}, 'Windows (version unavailable)'),
    ({'InstallationType': 'Client', 'ProductName': 'Windows 10 Pro', 'CurrentBuild': '26200', 'UBR': -1, 'DisplayVersion': 'private-metadata'}, 'Windows 11 Pro (build 26200)'),
    ({'InstallationType': 'Client', 'ProductName': 'Windows 10 Pro', 'CurrentBuild': '26200', 'UBR': '9999999999'}, 'Windows 11 Pro (build 26200)'),
    ({}, 'Windows (version unavailable)'),
    (None, 'Windows (version unavailable)'),
], ids=['native-win11', 'true-win10', 'server-26100', 'server-core', 'missing-sku', 'unknown-sku',
        'contradictory-server', 'contradictory-client', 'bad-build', 'missing-build', 'bad-optional', 'overflow-ubr', 'empty', 'read-failed'])
def test_metadata_identity_is_truthful_through_support_log(tmp_path, metadata, expected):
    code = r'''
$env:OS='Windows_NT'; $env:JIBO_DFU_SETUP_LOG=''
$global:Metadata=ConvertFrom-Json 'METADATA'
$global:Reads=0; $global:Forbidden=0
function Get-ItemProperty { $global:Reads++; if ($null -eq $global:Metadata) { throw 'fixture unavailable' }; return $global:Metadata }
function Get-CimInstance { $global:Forbidden++; throw 'CIM forbidden before UI' }
function Get-WmiObject { $global:Forbidden++; throw 'WMI forbidden before UI' }
Initialize-ToolkitLog -Root $PSScriptRoot -Build ('f'*40)
$expected='EXPECTED'
if ($script:ToolkitWindowsVersion -ne $expected) { throw ('IDENTITY: '+$script:ToolkitWindowsVersion) }
if ($global:Reads -ne 1 -or $global:Forbidden -ne 0) { throw 'metadata boundary violated' }
if (-not (Get-ToolkitSupportDetails 'fixture').Contains('Windows: '+$expected)) { throw 'support identity mismatch' }
if ((Get-Content -Raw $script:ToolkitLogPath | ConvertFrom-Json).windows -ne $expected) { throw 'log identity mismatch' }
'''
    run_ps(tmp_path, code.replace('METADATA', json.dumps(metadata)).replace('EXPECTED', expected))


def extracted_ui():
    text = CONSOLE.read_text()
    def here(name):
        return text.split('$script:' + name + " = @'\n", 1)[1].split("\n'@", 1)[0] + '\n'
    return here('ToolkitSetupUiSource') + here('ToolkitQuestionModelSource')


def test_compiled_copy_faults_have_safe_provenance_and_recovery(tmp_path):
    if not all(shutil.which(tool) for tool in ['mcs', 'mono', 'Xvfb']):
        pytest.skip('portable C# control requires Mono and isolated Xvfb')
    fixture = ROOT / 'tests/fixtures/support_clipboard_faults.cs'
    source = tmp_path / 'probe.cs'
    source.write_text(extracted_ui() + fixture.read_text())
    binary = tmp_path / 'probe.exe'
    compiled = subprocess.run(['mcs', '-langversion:5', '-warnaserror', '-r:System.Windows.Forms', '-r:System.Drawing',
                               '-out:' + str(binary), str(source)], text=True, capture_output=True)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    with isolated_display(tmp_path) as environment:
        result = subprocess.run(['mono', str(binary)], text=True, capture_output=True, timeout=35, env=environment)
        (tmp_path / 'copy.stdout.txt').write_text(result.stdout)
        (tmp_path / 'copy.stderr.txt').write_text(result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
        assert 'PASS COMPILED-COPY-PROVENANCE' in result.stdout


@contextmanager
def isolated_display(tmp_path):
    rfd, wfd = os.pipe()
    with (tmp_path / 'xvfb.txt').open('w') as log:
        server = subprocess.Popen(['Xvfb', '-displayfd', str(wfd), '-screen', '0', '1280x900x24', '-nolisten', 'tcp', '-noreset'],
                                  pass_fds=(wfd,), stdout=log, stderr=log)
        os.close(wfd)
        try:
            assert select.select([rfd], [], [], 5)[0], 'Xvfb readiness timeout'
            display = os.read(rfd, 30).decode().strip()
            assert display.isdigit() and display != '99', 'refusing shared display'
            (tmp_path / 'display.json').write_text(json.dumps({'display': ':' + display, 'pid': server.pid, 'private': True, 'tcp': False}))
            yield {**os.environ, 'DISPLAY': ':' + display, 'TMPDIR': str(tmp_path)}
        finally:
            os.close(rfd)
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill(); server.wait(timeout=5)


def test_failure_wrapper_stop_start_details_persists_even_on_error(tmp_path, monkeypatch):
    if not all(shutil.which(tool) for tool in ['mcs', 'mono', 'Xvfb']):
        pytest.skip('portable controller bridge requires Mono and isolated Xvfb')
    # Mono lacks these two native APIs. These are platform-boundary shims, not
    # native viewport acceptance. Start/Stop/Details/Forms/DoCopy/queue remain
    # extracted source. Clipboard is an in-memory lexical fault adapter.
    ui = extracted_ui()
    native = '[DllImport("user32.dll")] static extern bool FlashWindowEx(ref FLASHWINFO info);'
    assert ui.count(native) == 1
    ui = ui.replace(native, 'static bool FlashWindowEx(ref FLASHWINFO info) { return false; }')
    client = '[DllImport("user32.dll")] static extern bool GetClientRect(IntPtr hwnd, out CLIENTRECT rect);'
    assert ui.count(client) == 1
    ui = ui.replace(client, 'static bool GetClientRect(IntPtr hwnd, out CLIENTRECT rect) { var size = Control.FromHandle(hwnd).ClientSize; rect = new CLIENTRECT(); rect.Right = size.Width; rect.Bottom = size.Height; return true; }')
    source = tmp_path / 'controller.cs'
    source.write_text(ui + (ROOT / 'tests/fixtures/support_clipboard_faults.cs').read_text()
                      + (ROOT / 'tests/fixtures/support_failure_controller.cs').read_text())
    binary = tmp_path / 'controller.exe'
    compiled = subprocess.run(['mcs', '-langversion:5', '-warnaserror', '-r:System.Windows.Forms', '-r:System.Drawing',
                               '-main:SupportFailureController', '-out:' + str(binary), str(source)], text=True, capture_output=True)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    scenario = ROOT / 'tests/fixtures/support_failure_scenarios.ps1'
    code = f"& '{scenario}' -Mode Portable -Scratch '{tmp_path / 'scenario'}' -ConsoleSource '{CONSOLE}' -ControllerBinary '{binary}'"
    with isolated_display(tmp_path) as environment:
        monkeypatch.setenv('DISPLAY', environment['DISPLAY'])
        output = run_ps(tmp_path, code, timeout=65)
    assert 'PASS WRAPPER-PERSISTENCE cases=5 events=4 forbidden=0' in output


def test_native_runner_refuses_wrong_platform_and_compiles_fixture(tmp_path):
    scenario = ROOT / 'tests/fixtures/support_failure_scenarios.ps1'
    run_ps(tmp_path, f'''
$tokens=$null; $parseErrors=$null
$null=[Management.Automation.Language.Parser]::ParseFile('{scenario}', [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) {{ throw 'native helper syntax failed' }}
$env:OS='fixture-not-windows'
$refused=$false
try {{ & '{scenario}' -Mode Native -Scratch '{tmp_path / 'must-not-exist'}' }} catch {{ $refused=$_.Exception.Message -match 'requires Windows PowerShell 5.1' }}
if (-not $refused -or (Test-Path '{tmp_path / 'must-not-exist'}')) {{ throw 'native helper bypassed platform guard' }}
''')
    if not shutil.which('mcs'):
        pytest.skip('native fixture C# 5 compile control requires mcs')
    join = 'dialogThread.Join(); // An explicit user decision, never a command timeout.'
    ui = extracted_ui()
    assert ui.count(join) == 1
    ui = ui.replace(join, join + '\n if (SupportFailureController.ThrowAfterDialog) throw new InvalidOperationException("synthetic wrapper error");')
    source = tmp_path / 'native-fixture.cs'
    source.write_text(ui + (ROOT / 'tests/fixtures/support_clipboard_faults.cs').read_text()
                      + (ROOT / 'tests/fixtures/support_failure_controller.cs').read_text())
    result = subprocess.run(['mcs', '-target:library', '-langversion:5', '-warnaserror', '-r:System.Windows.Forms', '-r:System.Drawing',
                             '-out:' + str(tmp_path / 'native-fixture.dll'), str(source)], text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
