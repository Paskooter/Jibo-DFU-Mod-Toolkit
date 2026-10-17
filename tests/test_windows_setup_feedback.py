"""Executable PowerShell setup supervision checks; no WSL/USB or public network."""
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = Path(__file__).resolve().parents[1]
PWSH = os.environ.get('PWSH') or shutil.which('pwsh') or shutil.which('powershell.exe')


def run_ps(code, tmp_path, timeout=12):
    if not PWSH:
        pytest.skip('requires PowerShell (set PWSH for portable installs)')
    script = tmp_path / 'probe.ps1'
    script.write_text("$ErrorActionPreference='Stop'\n. '" + str(ROOT / 'scripts/release/console.ps1') + "'\n" + code)
    return subprocess.run([PWSH, '-NoProfile', '-File', str(script)], capture_output=True,
                          text=True, timeout=timeout, env={**os.environ, 'TMPDIR': str(tmp_path)})


def test_never_finishing_native_command_is_killed_within_step_limit(tmp_path):
    # The old no-argument WaitForExit is cut off by this independent outer bound.
    code = """
$timer = [Diagnostics.Stopwatch]::StartNew()
$result = Invoke-ToolkitNative -File (Get-Process -Id $PID).Path -Arguments @('-NoProfile','-Command','Start-Sleep 60') -TimeoutSeconds 0.3
if (-not $result.TimedOut -or $result.ExitCode -ne 124 -or $timer.Elapsed.TotalSeconds -gt 5) { throw 'Hung child was not diagnosed and killed within the step budget' }
Write-Output 'PASS bounded hung command'
"""
    try:
        result = run_ps(code, tmp_path, timeout=8)
    except subprocess.TimeoutExpired:
        pytest.fail('old native wait exceeded the independent 8-second safety bound')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PASS bounded hung command' in result.stdout


def test_step_log_support_details_are_persistent_and_redacted(tmp_path):
    result = run_ps(r"""
Initialize-ToolkitLog -Root ([IO.Path]::GetTempPath()) -Build ('a' * 40)
Set-ToolkitStep 'Checking the toolkit'
$script:ToolkitWslVersion = 'WSL 2.4; WSLg 1.0'
Write-ToolkitLog 'diagnostic' @{message='password=secret-value; ssid=private-network'; output='Authorization: Bearer private-token'}
Complete-ToolkitStep -Result failed -ExitCode 7 -ErrorText 'C:\Users\private-owner\setup failed; https://example.invalid/?token=private-query'
$details = Get-ToolkitSupportDetails 'Synthetic failure'
$log = [IO.File]::ReadAllText($script:ToolkitLogPath)
if ($details -notmatch 'Checking the toolkit' -or $details -notmatch 'WSL 2.4' -or $details -notmatch ('a' * 40) -or $details -notmatch 'Last log lines') { throw 'Support details missing context' }
if (($details + $log) -match 'secret-value|private-network|private-token|private-owner|private-query') { throw 'Private data leaked' }
$rows = @(Get-Content $script:ToolkitLogPath | ForEach-Object { $_ | ConvertFrom-Json })
$end = @($rows | Where-Object event -eq 'step-end')[0]
if (-not $end.start_utc -or -not $end.end_utc -or $end.duration_seconds -lt 0 -or $end.exit_code -ne 7 -or $end.result -ne 'failed') { throw 'Missing step receipt' }
Write-Output 'PASS redacted log/support'
""", tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_failure_output_is_logged_and_stall_can_keep_waiting(tmp_path):
    result = run_ps("""
Initialize-ToolkitLog -Root ([IO.Path]::GetTempPath()) -Build ('c' * 40)
Set-ToolkitStep 'Connecting USB'
$script:decisions = 0
function Show-ToolkitSupportDialog { param($Message, [switch]$AllowWait); $script:decisions++; return $true }
$result = Invoke-ToolkitNative -File (Get-Process -Id $PID).Path -Arguments @('-NoProfile','-Command','Start-Sleep -Milliseconds 700; [Console]::Error.WriteLine("synthetic failure"); exit 7') -TimeoutSeconds 0.3 -AllowWait
$rows = Get-Content $script:ToolkitLogPath | ForEach-Object { $_ | ConvertFrom-Json }
$end = @($rows | Where-Object event -eq 'command-end')[-1]
if ($result.ExitCode -ne 7 -or $script:decisions -lt 1 -or $end.output -notmatch 'synthetic failure' -or $end.step -ne 'Connecting USB') { throw 'Failure output or wait extension was lost' }
Write-Output 'PASS native receipts/wait'
""", tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('mode', ['active', 'idle', 'headers'])
def test_real_download_reports_bytes_and_times_out_only_when_idle(tmp_path, mode):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            try:
                if mode == 'headers':
                    time.sleep(2)
                self.send_response(200)
                self.send_header('Content-Length', str(8 * 65536))
                self.end_headers()
                for _ in range(8):
                    self.wfile.write(b'x' * 65536)
                    self.wfile.flush()
                    time.sleep(2 if mode == 'idle' else 0.15)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        code = f"""
Initialize-ToolkitLog -Root ([IO.Path]::GetTempPath()) -Build ('d' * 40)
Set-ToolkitStep 'Downloading Ubuntu'
$script:updates = @()
function Update-ToolkitSetup {{ param($Text,$Detail,$Percent); $script:updates += [pscustomobject]@{{text=$Text; percent=$Percent}} }}
function Show-ToolkitSupportDialog {{ param($Message,[switch]$AllowWait); return $false }}
$file = Join-Path ([IO.Path]::GetTempPath()) 'download.wsl'
$failed = $false
try {{ Receive-ToolkitDownload -Uri 'http://127.0.0.1:{server.server_port}/fixture' -OutFile $file -ExpectedBytes 524288 -IdleSeconds 0.5 }} catch {{ if ($_.Exception.Message -notmatch 'stalled') {{ throw }}; $failed = $true }}
if ($failed -ne ${'false' if mode == 'active' else 'true'}) {{ throw 'Wrong download inactivity result' }}
if (-not $failed -and ((Get-Item $file).Length -ne 524288 -or @($script:updates | Where-Object {{ $_.percent -gt 0 -and $_.percent -lt 100 }}).Count -lt 1)) {{ throw 'No real intermediate byte progress' }}
if ($failed -and (Test-Path $file)) {{ throw 'Partial stalled download survived' }}
Write-Output 'PASS real download {mode}'
"""
        result = run_ps(code, tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.shutdown()
        server.server_close()


def test_gui_never_ready_is_bounded_without_timing_out_open_gui(tmp_path):
    code = """
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('BOOTSTRAP', [ref]$tokens, [ref]$errors)
foreach ($f in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]}, $true)) { Invoke-Expression $f.Extent.Text }
Initialize-ToolkitLog -Root ([IO.Path]::GetTempPath()) -Build ('e' * 40)
Set-ToolkitStep 'Opening the toolkit'
function Native { param($Arguments); return $Arguments[-1] }
function Invoke-ToolkitNative { return [pscustomobject]@{ExitCode=0; Output=@('shared')} }
$script:child = $null
function Start-ToolkitProcess { param($FilePath,$ArgumentList); $script:child = [Diagnostics.Process]::Start((Get-Process -Id $PID).Path, '-NoProfile -Command "Start-Sleep 60"'); return $script:child }
function Show-ToolkitSupportDialog { param($Message,[switch]$AllowWait); return $false }
$failed = $false
try { Invoke-WslGui -Name Ubuntu -User fixture -Target /target -Session /session -ReadyTimeoutSeconds 0.3 | Out-Null } catch { if ($_.Exception.Message -notmatch '^Windows toolkit window could not be verified within 0.3 seconds' -or -not $_.Exception.Data['ToolkitStall']) { throw }; $failed = $true }
if (-not $failed -or -not $script:child.HasExited) { throw 'Never-ready GUI was not stopped' }
Write-Output 'PASS GUI ready deadline'
""".replace('BOOTSTRAP', str(ROOT / 'scripts/release/bootstrap.ps1'))
    try:
        result = run_ps(code, tmp_path, timeout=8)
    except subprocess.TimeoutExpired:
        pytest.fail('old GUI readiness wait exceeded independent 8-second safety bound')
    assert result.returncode == 0, result.stdout + result.stderr


def test_elevated_setup_has_a_bounded_wait_without_changing_uac_scope(tmp_path):
    result = run_ps("""
Initialize-ToolkitLog -Root ([IO.Path]::GetTempPath()) -Build ('f' * 40)
Set-ToolkitStep 'Connecting USB'
$script:stopped = $false
function Start-Process { param($FilePath,$ArgumentList,$Verb,$WindowStyle,[switch]$PassThru); if ($Verb -ne 'RunAs' -or $WindowStyle -ne 'Hidden') { throw 'UAC scope changed' }; $p=[pscustomobject]@{HasExited=$false; Handle=1; Id=1}; $p | Add-Member ScriptMethod WaitForExit {param($ms); Start-Sleep -Milliseconds 50; return $false}; return $p }
function Stop-ToolkitProcess {param($Process); $script:stopped=$true}
function Show-ToolkitSupportDialog {param($Message,[switch]$AllowWait); return $false}
$failed=$false
try { Invoke-ToolkitElevated -File fixture -Arguments 'bind --busid 1-2' -TimeoutSeconds 0.2 | Out-Null } catch { $failed=$_.Exception.Message -match 'stalled' }
if (-not $failed -or -not $script:stopped) { throw 'Elevated hang was not stopped' }
Write-Output 'PASS elevated deadline'
""", tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('requested', ['', 'Jibo-DFU-Toolkit'])
def test_source_selection_does_not_use_exe_private_no_sudo_distro(tmp_path, requested):
    code = """
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('RUNSCRIPT', [ref]$tokens, [ref]$errors)
foreach ($f in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]}, $true)) { Invoke-Expression $f.Extent.Text }
$UsbOnly=$false
$rows=@([pscustomobject]@{Name='Jibo-DFU-Toolkit'; Version=2; IsDefault=$true})
$failed=$false
try { Select-Distribution -Distributions $rows -Requested 'REQUESTED' | Out-Null } catch { if ($_.Exception.Message -notmatch 'Jibo-DFU-Windows-x64.exe') { throw }; $failed=$true }
if (-not $failed) { throw 'Source route selected the private no-sudo Linux and would dead-end at sudo' }
$UsbOnly=$true
if ((Select-Distribution -Distributions $rows -Requested 'Jibo-DFU-Toolkit') -ne 'Jibo-DFU-Toolkit') { throw 'EXE USB route lost its own distro' }
Write-Output 'PASS private source guard'
""".replace('RUNSCRIPT', str(ROOT / 'run.ps1')).replace('REQUESTED', requested)
    result = run_ps(code, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_launcher_early_failure_always_leaves_redacted_support_log(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location('feedback_launcher', ROOT / 'scripts/release/windows_launcher.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    log = module.initialize_setup_log(tmp_path, 'a' * 40)
    module.write_setup_log(log, 'launcher-failed', r'C:\Users\private-name\failed password=private-secret')
    details = module.support_details(log, r'C:\Users\private-name\failed password=private-secret')
    assert log.is_file()
    assert 'a' * 40 in details
    assert 'Starting Windows launcher' in details
    assert 'WSL: not checked' in details
    assert 'private-name' not in details + log.read_text()
    assert 'private-secret' not in details + log.read_text()


def test_gui_ready_does_not_time_out_normal_toolkit_use(tmp_path):
    code = """
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('BOOTSTRAP', [ref]$tokens, [ref]$errors)
foreach ($f in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]}, $true)) { Invoke-Expression $f.Extent.Text }
Initialize-ToolkitLog -Root ([IO.Path]::GetTempPath()) -Build ('e' * 40)
Set-ToolkitStep 'Opening the toolkit'
$script:ready=$null
function Native { param($Arguments); $script:ready=$Arguments[-1]; return $script:ready }
function Invoke-ToolkitNative { return [pscustomobject]@{ExitCode=0; Output=@('shared')} }
$script:windowChecks=0
function Get-ToolkitGuiWindowState { param($Token); $script:windowChecks++; return 'visible' }
function Start-ToolkitProcess { param($FilePath,$ArgumentList); [IO.File]::WriteAllText($script:ready,'ready'); return [Diagnostics.Process]::Start((Get-Process -Id $PID).Path, '-NoProfile -Command "Start-Sleep -Milliseconds 900; exit 0"') }
function Show-ToolkitSupportDialog { param($Message,[switch]$AllowWait); throw 'Ready GUI was incorrectly diagnosed as stalled' }
$timer=[Diagnostics.Stopwatch]::StartNew()
$code=Invoke-WslGui -Name Ubuntu -User fixture -Target /target -Session /session -ReadyTimeoutSeconds 0.2
if ($code -ne 0 -or $script:windowChecks -ne 1 -or $timer.Elapsed.TotalSeconds -lt 0.8) { throw 'Normal toolkit lifetime was truncated' }
Write-Output 'PASS ready GUI lifetime'
""".replace('BOOTSTRAP', str(ROOT / 'scripts/release/bootstrap.ps1'))
    result = run_ps(code, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_gui_startup_failure_captures_real_stderr(tmp_path):
    code = """
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('BOOTSTRAP', [ref]$tokens, [ref]$errors)
foreach ($f in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]}, $true)) { Invoke-Expression $f.Extent.Text }
Initialize-ToolkitLog -Root ([IO.Path]::GetTempPath()) -Build ('e' * 40)
Set-ToolkitStep 'Opening the toolkit'
function Native { param($Arguments); return $Arguments[-1] }
function Invoke-ToolkitNative { return [pscustomobject]@{ExitCode=0; Output=@('shared')} }
$script:realStart=[scriptblock]::Create((Get-Command Start-ToolkitProcess).Definition)
function Start-ToolkitProcess { param($FilePath,$ArgumentList,[switch]$CaptureOutput); return (& $script:realStart -FilePath (Get-Process -Id $PID).Path -ArgumentList (ConvertTo-NativeArgumentLine @('-NoProfile','-Command','[Console]::Error.WriteLine("synthetic GUI stderr"); exit 7')) -CaptureOutput:$CaptureOutput) }
$code=Invoke-WslGui -Name Ubuntu -User fixture -Target /target -Session /session
if ($code -ne 7 -or [IO.File]::ReadAllText($script:ToolkitLogPath) -notmatch 'synthetic GUI stderr') { throw 'GUI startup error output was hidden' }
Write-Output 'PASS GUI stderr capture'
""".replace('BOOTSTRAP', str(ROOT / 'scripts/release/bootstrap.ps1'))
    result = run_ps(code, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_secret_json_fields_are_not_copied_or_logged(tmp_path):
    result = run_ps("""
Initialize-ToolkitLog -Root ([IO.Path]::GetTempPath()) -Build ('a' * 40)
Write-ToolkitLog 'diagnostic' @{output='{"ssid":"synthetic-network","password":"synthetic-password"}'}
$text=Get-ToolkitSupportDetails 'test'
if (($text + [IO.File]::ReadAllText($script:ToolkitLogPath)) -match 'synthetic-network|synthetic-password') { throw 'Quoted credential fields leaked' }
""", tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_winforms_failure_uses_copyable_native_fallback(tmp_path):
    result = run_ps("""
Initialize-ToolkitLog -Root ([IO.Path]::GetTempPath()) -Build ('a' * 40)
Set-ToolkitStep 'Checking the toolkit'
function Initialize-ToolkitStatus { throw 'Synthetic WinForms unavailable' }
function Initialize-ToolkitConsole { return $true }
function Get-ToolkitDialogOwner { return [IntPtr]42 }
Add-Type -TypeDefinition 'using System; public class ToolkitConsoleNative { public static int MessageBox(IntPtr h,string text,string title,uint flags) { if ((flags & 3) != 3 || !text.Contains("Copy details") || !text.Contains("Open log folder")) throw new Exception("Missing recovery choices"); return 6; } }'
$env:OS='Windows_NT'
$script:copied=''
function Set-Clipboard {param($Value); $script:copied=$Value}
Show-ToolkitSupportDialog 'Synthetic failure' | Out-Null
if ($script:copied -notmatch 'Checking the toolkit') { throw 'UI failure hid support details' }
""", tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_question_state_safe_default_guard_reminder_and_close(tmp_path):
    result = run_ps("""
Add-Type -TypeDefinition $script:ToolkitQuestionModelSource
$q=New-Object ToolkitQuestionState
$q.Begin('Set up Linux?', 0)
if (-not $q.Pending -or $q.Answer -ne 'no' -or $q.Prompt -ne 'Set up Linux?') { throw 'Question did not start in safe No state' }
if ($q.Respond('yes', [ToolkitQuestionState]::KeyGuardSeconds / 2)) { throw 'Early key answered a question' }
if ($q.Remind([ToolkitQuestionState]::ReminderSeconds - 1) -or -not $q.Remind([ToolkitQuestionState]::ReminderSeconds) -or $q.Remind([ToolkitQuestionState]::ReminderSeconds + 1)) { throw 'Question reminder cadence is wrong' }
if (-not $q.Respond('closed', 61) -or $q.Pending -or $q.Answer -ne 'closed') { throw 'Close did not count as No/closed' }
""", tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_confirm_setup_logs_closed_question_and_support_history(tmp_path):
    code = """
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile('BOOTSTRAP', [ref]$tokens, [ref]$errors)
$f=$ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Confirm-Setup'}, $true)[0]
Invoke-Expression $f.Extent.Text
Add-Type -TypeDefinition 'public class ToolkitSetupStatus { public static void Start() {} public static void AskQuestion(string text) {} public static bool QuestionPending { get { return false; } } public static string LastAnswer { get { return "closed"; } } public static object[] DrainEvents() { return new object[0]; } public static void Update(string t,string d,int p) {} }'
Initialize-ToolkitLog -Root ([IO.Path]::GetTempPath()) -Build ('b' * 40)
Set-ToolkitStep 'Finding Linux'
if (Confirm-Setup 'Set up a private Linux?') { throw 'Closed question was accepted' }
$rows=@(Get-Content $script:ToolkitLogPath | ForEach-Object {$_ | ConvertFrom-Json})
$start=@($rows | Where-Object event -eq 'question-start')[-1]
$end=@($rows | Where-Object event -eq 'question-end')[-1]
if ($start.question -ne 'Set up a private Linux?' -or $end.answer -ne 'closed' -or -not $end.utc -or $end.duration_seconds -lt 0) { throw 'Question/answer timing log incomplete' }
Set-ToolkitStep 'Checking the toolkit'
1..12 | ForEach-Object { Write-ToolkitLog 'other' @{message='later event'} }
$details=Get-ToolkitSupportDetails 'failure'
if ($details -notmatch 'Set up a private Linux' -or $details -notmatch 'closed') { throw 'Support text lost question history' }
""".replace('BOOTSTRAP', str(ROOT / 'scripts/release/bootstrap.ps1'))
    result = run_ps(code, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_question_path_never_uses_message_box():
    source = (ROOT / 'scripts/release/console.ps1').read_text()
    question = source.split('function Show-ToolkitQuestion', 1)[1].split('function ', 1)[0]
    assert 'MessageBox' not in question
    assert 'AskQuestion' in question


def test_download_inactivity_uses_changed_bytes_not_total_elapsed(tmp_path):
    result = run_ps("""
if (Test-ToolkitStall -ElapsedSeconds 4000 -IdleSeconds 1 -LimitSeconds 120 -ActivityBased) { throw 'Slow active download was timed out by total duration' }
if (-not (Test-ToolkitStall -ElapsedSeconds 120 -IdleSeconds 120 -LimitSeconds 120 -ActivityBased)) { throw 'Idle download was not detected' }
if (-not (Test-ToolkitStall -ElapsedSeconds 120 -IdleSeconds 1 -LimitSeconds 120)) { throw 'Silent native command has no total bound' }
Write-Output 'PASS activity deadline'
""", tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
