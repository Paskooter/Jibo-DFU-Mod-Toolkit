# Preview-only supervisor. Each potentially blocking Windows/UI phase owns a fresh
# powershell.exe; no UI call can prevent later diagnostics or artifact upload.
param(
    [string]$OutDir,
    [switch]$HighContrast,
    [string]$LauncherTextFile = '',
    [string]$Filter = '',
    [switch]$SkipLive,
    [switch]$TimeoutProbe,
    [ValidateRange(1, 120)][int]$PhaseTimeoutSeconds = 60
)
$ErrorActionPreference = 'Stop'
if (-not [IO.Path]::IsPathRooted($OutDir)) { $OutDir = Join-Path (Get-Location) $OutDir }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$progressPath = Join-Path $OutDir 'progress.txt'
$utf8 = New-Object Text.UTF8Encoding($false)
function Trace-Preview([string]$Message) {
    $line = [DateTime]::UtcNow.ToString('o') + ' supervisor ' + $Message
    Write-Host $line
    [Console]::Out.Flush()
    [IO.File]::AppendAllText($progressPath, $line + [Environment]::NewLine, $utf8)
}
function Save-Receipt {
    $path = Join-Path $OutDir 'receipt.json'
    Trace-Preview "begin write $path"
    [IO.File]::WriteAllText($path, ($receipt | ConvertTo-Json -Depth 10), $utf8)
    Trace-Preview "end write $path"
}
function Quote-Argument([string]$Value) { return '"' + $Value.Replace('"', '\"') + '"' }
$receipt = [ordered]@{
    windows = [Environment]::OSVersion.VersionString
    powershell = $PSVersionTable.PSVersion.ToString()
    clr = [Environment]::Version.ToString()
    revision = $env:GITHUB_SHA
    high_contrast_requested = [bool]$HighContrast
    dpi_awareness = 'per-monitor (thread), rendered at forced 96/144/192 DPI'
    files = @(); problems = @(); live = [ordered]@{}; fallback = [ordered]@{}
    phases = [ordered]@{}
}
$phases = if ($TimeoutProbe) { @('timeout-probe', 'success-probe') }
elseif ($HighContrast) { @('setup-96', 'support-96', 'fallback-setup', 'launcher-text', 'fallback-launcher', 'clipboard','font-control') }
else { @('setup-96','setup-144','setup-192','support-96','support-144','support-192','simulated-hc','fallback-setup','launcher-text','fallback-launcher','clipboard','font-control') }
if (-not $TimeoutProbe -and -not $SkipLive -and -not $HighContrast) { $phases += 'live' }
$failed = $false
Save-Receipt
foreach ($phase in $phases) {
    $timeout = if ($phase -eq 'timeout-probe') { 2 } elseif ($TimeoutProbe) { 10 } else { $PhaseTimeoutSeconds }
    $stdout = Join-Path $OutDir "$phase.stdout.txt"
    $stderr = Join-Path $OutDir "$phase.stderr.txt"
    $phaseReceipt = Join-Path $OutDir "$phase.json"
    # Never accept a stale receipt from an earlier invocation.
    if (Test-Path $phaseReceipt) { Remove-Item -LiteralPath $phaseReceipt }
    $args = @('-NoProfile','-ExecutionPolicy','Bypass','-STA','-File', (Quote-Argument (Join-Path $PSScriptRoot 'render-setup-previews.ps1')), '-OutDir', (Quote-Argument $OutDir), '-Phase', $phase)
    if ($HighContrast) { $args += '-HighContrast' }
    if ($LauncherTextFile) { $args += @('-LauncherTextFile', (Quote-Argument $LauncherTextFile)) }
    if ($Filter) { $args += @('-Filter', (Quote-Argument $Filter)) }
    $result = [ordered]@{ status = 'running'; timeout_seconds = $timeout; started_utc = [DateTime]::UtcNow.ToString('o') }
    $receipt.phases[$phase] = $result
    Trace-Preview "begin phase $phase (budget ${timeout}s); begin write $stdout and $stderr"
    Save-Receipt
    $process = $null
    try {
        $process = Start-Process -FilePath "$PSHOME\powershell.exe" -ArgumentList $args -NoNewWindow -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
        $null = $process.Handle # Keep the native handle alive for ExitCode on PS 5.1.
        if (-not $process.WaitForExit($timeout * 1000)) {
            $result.status = 'timeout'
            Trace-Preview "timeout phase $phase; killing process tree $($process.Id)"
            $killer = Start-Process -FilePath "$env:SystemRoot\System32\taskkill.exe" -ArgumentList @('/PID', $process.Id, '/T', '/F') -NoNewWindow -PassThru
            if (-not $killer.WaitForExit(10000)) { $killer.Kill() }
            if (-not $process.WaitForExit(5000)) { throw "Phase $phase survived process-tree termination" }
            $failed = $true
        } else {
            # Refresh is necessary with Start-Process on Windows PowerShell 5.1.
            $process.Refresh()
            $result.exit_code = $process.ExitCode
            if (Test-Path $phaseReceipt) {
                $child = Get-Content -Raw -LiteralPath $phaseReceipt | ConvertFrom-Json
                $result.status = if ($child.failed -or $process.ExitCode -ne 0) { 'failed' } else { 'ok' }
                $receipt.files += @($child.files)
                $receipt.problems += @($child.problems)
                foreach ($key in @('live', 'fallback')) {
                    foreach ($p in $child.$key.PSObject.Properties) { $receipt[$key][$p.Name] = $p.Value }
                }
                foreach ($key in @('clipboard','font_lifetime_control','high_contrast_switched','high_contrast_active','high_contrast_restored')) {
                    if ($child.PSObject.Properties.Name -contains $key) { $receipt[$key] = $child.$key }
                }
                if ($child.error) { $result.error = $child.error }
            } else { $result.status = 'failed'; $result.error = 'Child exited without a receipt' }
            if ($result.status -ne 'ok') { $failed = $true }
        }
    } catch { $result.status = 'failed'; $result.error = $_.Exception.ToString(); $failed = $true }
    finally { if ($null -ne $process) { $process.Dispose() } }
    $result.finished_utc = [DateTime]::UtcNow.ToString('o')
    Trace-Preview "end phase $phase ($($result.status)); end write $stdout and $stderr"
    foreach ($path in @($stdout, $stderr)) { if (Test-Path $path) { Get-Content -LiteralPath $path | ForEach-Object { Write-Host $_ } } }
    Save-Receipt
}
$receipt.failed = $failed
Save-Receipt
Trace-Preview "finished: $($receipt.files.Count) renders, $($receipt.problems.Count) layout problems, failed=$failed"
if ($failed) { exit 1 }
exit 0
