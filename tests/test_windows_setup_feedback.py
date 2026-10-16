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
        def log_message(self, *_):
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
$script:child = $null
function Start-ToolkitProcess { param($FilePath,$ArgumentList); $script:child = [Diagnostics.Process]::Start((Get-Process -Id $PID).Path, '-NoProfile -Command "Start-Sleep 60"'); return $script:child }
function Show-ToolkitSupportDialog { param($Message,[switch]$AllowWait); return $false }
$failed = $false
try { Invoke-WslGui -Name Ubuntu -User fixture -Target /target -Session /session -ReadyTimeoutSeconds 0.3 | Out-Null } catch { if ($_.Exception.Message -notmatch 'Opening the toolkit.*stalled') { throw }; $failed = $true }
if (-not $failed -or -not $script:child.HasExited) { throw 'Never-ready GUI was not stopped' }
Write-Output 'PASS GUI ready deadline'
""".replace('BOOTSTRAP', str(ROOT / 'scripts/release/bootstrap.ps1'))
    try:
        result = run_ps(code, tmp_path, timeout=8)
    except subprocess.TimeoutExpired:
        pytest.fail('old GUI readiness wait exceeded independent 8-second safety bound')
    assert result.returncode == 0, result.stdout + result.stderr


def test_download_inactivity_uses_changed_bytes_not_total_elapsed(tmp_path):
    result = run_ps("""
if (Test-ToolkitStall -ElapsedSeconds 4000 -IdleSeconds 1 -LimitSeconds 120 -ActivityBased) { throw 'Slow active download was timed out by total duration' }
if (-not (Test-ToolkitStall -ElapsedSeconds 120 -IdleSeconds 120 -LimitSeconds 120 -ActivityBased)) { throw 'Idle download was not detected' }
if (-not (Test-ToolkitStall -ElapsedSeconds 120 -IdleSeconds 1 -LimitSeconds 120)) { throw 'Silent native command has no total bound' }
Write-Output 'PASS activity deadline'
""", tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
