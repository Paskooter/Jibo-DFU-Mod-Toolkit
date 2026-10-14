# Synthetic console/WSL lifecycle; never invokes Win32, WSL, installers or USB.
param([string]$BootstrapPath = (Join-Path $PSScriptRoot 'bootstrap.ps1'), [string]$Case = 'classic')
$ErrorActionPreference = 'Stop'
if ($Case -eq 'all') {
    $hostBinary = (Get-Process -Id $PID).Path
    $passed = 0
    $failed = 0
    foreach ($scenario in @('classic','terminal','gui-success','gui-failed','never-ready','watcher-failed','monitor-failed','show-console','setup-decline','gui-start-failed','cleanup-failed','monitor-prompt','terminal-failed','hide-fallback','watcher-exit-race','monitor-exit-race','gui-handle','watcher-handle','gui-code-unavailable')) {
        $ErrorActionPreference = 'Continue'
        & $hostBinary -NoProfile -File $PSCommandPath -BootstrapPath $BootstrapPath -Case $scenario
        $code = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
        if ($code -eq 0) { $passed++ } else { $failed++ }
    }
    Write-Output "Console scenarios: $passed passed; $failed failed."
    if ($failed) { exit 1 }
    exit 0
}
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('dfu-console-test-' + [guid]::NewGuid())
New-Item -ItemType Directory $fixture | Out-Null
try {
    $trace = Join-Path $fixture 'trace.jsonl'
    $global:ConsoleFixtureTrace = $trace
    $env:DFU_CONSOLE_TEST_TRACE = $trace
    # The false ShowWindow return is the PREVIOUS visibility, not a failure.
    Add-Type -TypeDefinition @'
using System;
using System.IO;
public class ToolkitConsoleNative {
    public static bool Pseudo = false;
    public static bool Visible = true;
    public static bool IgnoreHide = false;
    public static IntPtr GetConsoleWindow() { return new IntPtr(42); }
    public static int DwmGetWindowAttribute(IntPtr hwnd, int attr, out int value, int size) { value = Pseudo ? 1 : 0; return 0; }
    public static bool IsWindowVisible(IntPtr hwnd) { return Visible; }
    public static bool IsIconic(IntPtr hwnd) { return false; }
    public static bool ShowWindow(IntPtr hwnd, int command) {
        Visible = command != 0 || IgnoreHide;
        File.AppendAllText(Environment.GetEnvironmentVariable("DFU_CONSOLE_TEST_TRACE"), "{\"Kind\":\"console\",\"Values\":[" + command + "]}\n");
        return false;
    }
    public static int MessageBox(IntPtr hwnd, string text, string title, uint flags) {
        File.AppendAllText(Environment.GetEnvironmentVariable("DFU_CONSOLE_TEST_TRACE"), "{\"Kind\":\"dialog\",\"Values\":[]}\n"); return 1;
    }
}
'@
    function Record($Kind, $Values) { [IO.File]::AppendAllText($global:ConsoleFixtureTrace, (([pscustomobject]@{Kind=$Kind; Values=@($Values)} | ConvertTo-Json -Compress) + "`n")) }
    if ($Case -in @('classic','terminal','hide-fallback')) {
        $helper = Join-Path (Split-Path $BootstrapPath) 'console.ps1'
        if (-not (Test-Path $helper)) { throw 'Missing console controller' }
        . $helper
        [ToolkitConsoleNative]::Pseudo = $Case -eq 'terminal'
        [ToolkitConsoleNative]::IgnoreHide = $Case -eq 'hide-fallback'
        Set-ToolkitConsole
        Set-ToolkitConsole -Visible
        $commands = @(Get-Content $trace | ForEach-Object { ($_ | ConvertFrom-Json).Values[0] })
        $expected = if ($Case -eq 'terminal') { @(6,9) } elseif ($Case -eq 'hide-fallback') { @(0,6,9) } else { @(0,9) }
        if (($commands -join ',') -ne ($expected -join ',')) { throw ('Wrong host operations: ' + ($commands -join ',')) }
        Write-Output "PASS console: $Case"
        exit 0
    }
    if ($Case -eq 'monitor-prompt') {
        $helper = Join-Path (Split-Path $BootstrapPath) 'console.ps1'
        if (Test-Path $helper) { . $helper }
        else {
            function Invoke-ToolkitConsolePrompt([scriptblock]$Action) {
                [ToolkitConsoleNative]::ShowWindow([IntPtr]42, 9) | Out-Null
                & $Action
            }
        }
        $usbPath = Join-Path (Split-Path (Split-Path (Split-Path $BootstrapPath))) 'run.ps1'
        $tokens=$null; $errors=$null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile($usbPath, [ref]$tokens, [ref]$errors)
        if ($errors.Count) { throw 'USB helper does not parse' }
        foreach ($function in $ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $true)) { Invoke-Expression $function.Extent.Text }
        $ConsoleHelper = Join-Path (Split-Path $BootstrapPath) 'console.ps1'
        function Test-IsAdministrator { return $false }
        function Start-Process {
            param($FilePath,$ArgumentList,$Verb,[switch]$Wait,[switch]$PassThru,$WorkingDirectory)
            Record 'admin' @($Verb)
            return [pscustomobject]@{ExitCode=0}
        }
        if (-not (Invoke-UacBind '1-2')) { throw 'USB bind failed' }
        $events = @(Get-Content $trace | ForEach-Object { $_ | ConvertFrom-Json })
        if ($events.Count -ne 2 -or $events[0].Kind -ne 'console' -or $events[0].Values[0] -ne 9 -or $events[1].Kind -ne 'admin') { throw 'UAC handoff preceded command-window restoration' }
        Write-Output "PASS console: $Case"
        exit 0
    }
    $global:ConsoleFixtureCase = $Case
    [ToolkitConsoleNative]::Pseudo = $Case -eq 'terminal-failed'
    function Write-Host {
        param($Object, $ForegroundColor)
        if ([string]$Object -like 'Launcher:*') { Record 'error' @($Object) }
        Microsoft.PowerShell.Utility\Write-Host $Object
    }
    $global:ConsoleFixtureTicks = 0
    $global:ConsoleFixtureReady = $null
    function wsl.exe {
        Record 'wsl' $args
        $global:LASTEXITCODE = 0
        if ($args[0] -eq '--version') { if ($global:ConsoleFixtureCase -eq 'setup-decline') { $global:LASTEXITCODE=1; return 'missing' }; return 'WSLg 1.0' }
        if ($args[0] -eq '--list') { return "NAME STATE VERSION`n* Ubuntu-22.04 Stopped 2" }
        if ($args -contains 'getconf') { return 'glibc 2.35' }
        if ($args -contains 'uname') { return 'x86_64' }
        if ($args -contains 'id') { if ($args[-1] -eq '-un') { return 'fixture' }; return '1000' }
        if ($args -contains 'cat') { return "ID=ubuntu`nVERSION_ID=22.04" }
        if ($args -contains 'printenv') { return '/home/fixture' }
        if ($args -contains 'wslpath') { return $args[-1].Replace('\', '/') }
        if ($args -contains 'rm' -and $global:ConsoleFixtureCase -eq 'cleanup-failed') { $global:LASTEXITCODE=2; return 'synthetic cleanup error' }
        if ($args -contains 'bash' -and ($args -join ' ') -match 'wsl-user-launch.sh') { $global:LASTEXITCODE=41 }
    }
    function powershell.exe { Record 'usb' $args; $global:LASTEXITCODE=0 }
    function Get-Command { param($Name,$ErrorAction); return [pscustomobject]@{Source='fixture-usbipd'} }
    function Read-Host { param($Prompt); Record 'prompt' @($Prompt); return 'n' }
    function Stop-Process { param($Id); Record 'stop' @($Id) }
    function Start-Process {
        param($FilePath,$ArgumentList,$Verb,[switch]$Wait,[switch]$PassThru,[switch]$NoNewWindow)
        Record 'start' @($FilePath,$ArgumentList,$Verb)
        if ($ArgumentList -match 'wsl-user-launch.sh') {
            if ($global:ConsoleFixtureCase -eq 'gui-start-failed') { throw 'synthetic GUI start failure' }
            # The production argument formatter must preserve the Linux command.
            $global:ConsoleFixtureReady = [regex]::Match($ArgumentList, '"([^"]+/ready)"$').Groups[1].Value
            if (-not $global:ConsoleFixtureReady) { throw 'GUI ready path was not forwarded' }
            $process = [pscustomobject]@{Id=4244; HasExited=$false; ExitCode=0; Cached=$false}
            if ($global:ConsoleFixtureCase -eq 'gui-handle') {
                $process.PSObject.Properties.Remove('ExitCode')
                $process | Add-Member NoteProperty RawExitCode 0
                $process | Add-Member ScriptProperty Handle { $this.Cached=$true; return 1 }
                $process | Add-Member ScriptProperty ExitCode { if ($this.Cached) { return $this.RawExitCode } } { param($Value); $this.RawExitCode=$Value }
            }
            $process | Add-Member ScriptMethod WaitForExit {
                param($Timeout)
                if ($Timeout -ne 100) { return $this.HasExited }
                $global:ConsoleFixtureTicks++
                Record 'tick' @($global:ConsoleFixtureTicks)
                if ($global:ConsoleFixtureTicks -eq 2 -and $global:ConsoleFixtureCase -ne 'never-ready') {
                    [IO.File]::WriteAllText($global:ConsoleFixtureReady, "ready`n")
                    Record 'ready' @()
                }
                if ($global:ConsoleFixtureTicks -eq 4) {
                    $this.HasExited = $true
                    $this.ExitCode = if ($global:ConsoleFixtureCase -eq 'gui-code-unavailable') { $null } elseif ($global:ConsoleFixtureCase -in @('gui-failed','never-ready','terminal-failed','gui-handle')) { 41 } else { 0 }
                }
                return $this.HasExited
            }
            return $process
        }
        $process = [pscustomobject]@{Id=$(if ($FilePath -match 'wsl.exe$') { 4242 } else { 4243 }); ExitCode=$(if ($global:ConsoleFixtureCase -eq 'watcher-exit-race') { 7 } else { 0 })}
        if ($global:ConsoleFixtureCase -eq 'watcher-handle' -and $process.Id -eq 4242) {
            $process.PSObject.Properties.Remove('ExitCode')
            $process | Add-Member NoteProperty Cached $false
            $process | Add-Member ScriptProperty Handle { $this.Cached=$true; return 1 }
            $process | Add-Member ScriptProperty ExitCode { if ($this.Cached) { return 0 } }
        }
        $process | Add-Member ScriptProperty HasExited {
            if ($global:ConsoleFixtureTicks -ge 4 -and $this.Id -eq 4242 -and $global:ConsoleFixtureCase -eq 'watcher-handle') { return $true }
            if ($global:ConsoleFixtureTicks -ge 4 -and (($this.Id -eq 4242 -and $global:ConsoleFixtureCase -eq 'watcher-exit-race') -or ($this.Id -eq 4243 -and $global:ConsoleFixtureCase -eq 'monitor-exit-race'))) { return $true }
            return ($global:ConsoleFixtureTicks -ge 3 -and (($this.Id -eq 4242 -and $global:ConsoleFixtureCase -eq 'watcher-failed') -or ($this.Id -eq 4243 -and $global:ConsoleFixtureCase -eq 'monitor-failed')))
        }
        $process | Add-Member ScriptMethod WaitForExit { param($Timeout); if ($Timeout) { return ($Timeout -ge 2000) } }
        return $process
    }
    foreach ($name in @('payload.tar.gz','run.ps1','wsl-usb-permissions.sh','wsl-user-launch.sh','wsl-provision.sh')) { [IO.File]::WriteAllText((Join-Path $fixture $name), 'fixture') }
    Copy-Item (Join-Path (Split-Path $BootstrapPath) 'console.ps1') $fixture -ErrorAction SilentlyContinue
    [IO.File]::WriteAllText((Join-Path $fixture 'revision.txt'), ('a' * 40))
    $parameters = @{PayloadRoot=$fixture}
    if ($Case -eq 'show-console') { $parameters.ShowConsole=$true }
    & $BootstrapPath @parameters
    $code = $LASTEXITCODE
    $events = @(Get-Content $trace | ForEach-Object { $_ | ConvertFrom-Json })
    $hide = @($events | Where-Object { $_.Kind -eq 'console' -and $_.Values[0] -in @(0,6) })
    $show = @($events | Where-Object { $_.Kind -eq 'console' -and $_.Values[0] -eq 9 })
    $prompts = @($events | Where-Object { $_.Kind -eq 'prompt' })
    $success = $Case -in @('gui-success','show-console','watcher-handle')
    if ($code -ne $(if ($success) { 0 } elseif ($Case -in @('gui-failed','never-ready','terminal-failed','gui-handle')) { 41 } else { 1 })) { throw "Wrong exit code: $code" }
    if ($Case -in @('never-ready','show-console','setup-decline','gui-start-failed')) {
        if ($hide.Count) { throw 'Console hidden without GUI readiness or despite ShowConsole' }
    } else {
        if ($hide.Count -ne 1) { throw 'Ready GUI did not hide console exactly once' }
        if ([array]::IndexOf($events,$hide[0]) -le [array]::IndexOf($events, @($events | Where-Object Kind -eq 'ready')[0])) { throw 'Console hidden before mapped-window signal' }
    }
    if (-not $success) {
        $errors = @($events | Where-Object Kind -eq 'error')
        if (-not $show.Count -or -not $prompts.Count -or -not $errors.Count) { throw 'Failure did not restore console and pause' }
        foreach ($errorEvent in $errors) {
            $index = [array]::IndexOf($events,$errorEvent)
            if ($index -lt 1 -or $events[$index-1].Kind -ne 'console' -or $events[$index-1].Values[0] -ne 9) { throw 'Error was printed before console restoration' }
        }
        if ([array]::IndexOf($events,$show[-1]) -ge [array]::IndexOf($events,$prompts[-1])) { throw 'Failure prompt preceded console restoration' }
    }
    if ($Case -eq 'terminal-failed' -and @($events | Where-Object Kind -eq 'dialog').Count -ne 1) { throw 'Terminal failure needs a visible dialog fallback' }
    if ($Case -notin @('setup-decline','gui-start-failed')) {
        if (@($events | Where-Object { $_.Kind -eq 'wsl' -and $_.Values -contains 'rm' }).Count -ne 1) { throw 'Session was not removed once' }
        if (@($events | Where-Object { $_.Kind -eq 'stop' -and $_.Values[0] -eq 4243 }).Count -ne $(if ($Case -in @('monitor-failed','monitor-exit-race')) { 0 } else { 1 })) { throw 'USB monitor not cleaned up' }
    }
    Write-Output "PASS console: $Case"
} finally {
    Remove-Item -Recurse -Force $fixture
}
