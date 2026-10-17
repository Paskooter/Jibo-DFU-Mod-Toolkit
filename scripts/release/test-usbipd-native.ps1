# Synthetic process fixtures only. Run/Validate require native Windows PowerShell 5.1.
# Check/SelfTest are portable structural/negative controls, NOT native acceptance.
[CmdletBinding()]
param(
    [ValidateSet('Run','Validate','Check','SelfTest','Phase')][string]$Mode = 'Run',
    [string]$ToolkitRoot = '',
    [string]$OutputRoot = (Join-Path (Get-Location) 'usbipd-native-receipts'),
    [string]$RunId,
    [string]$PhaseName,
    [string]$ScratchRoot
)
$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 may bind defaults before $PSScriptRoot is available.
if (-not $PSBoundParameters.ContainsKey('ToolkitRoot')) {
    $ToolkitRoot = Split-Path (Split-Path $PSScriptRoot)
}
Import-Module Microsoft.PowerShell.Utility
Import-Module Microsoft.PowerShell.Management
$SelfPath = $PSCommandPath
$HostBinary = (Get-Process -Id $PID).Path
$HarnessRoot = Split-Path (Split-Path $PSScriptRoot)

function Assert-True($Value, [string]$Message) { if (-not $Value) { throw $Message } }
function Assert-Rejection([scriptblock]$Action, [string]$Expected) {
    try { $null=& $Action }
    catch { Assert-True ($_.Exception.Message -ceq $Expected) ('Wrong rejection reason; expected ' + $Expected + '; got ' + $_.Exception.Message); return }
    throw ('Expected rejection absent: ' + $Expected)
}
function Save-Json([string]$Path, $Value) {
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 16), (New-Object Text.UTF8Encoding($false)))
}
function Read-Json([string]$Path) { return ([IO.File]::ReadAllText($Path) | ConvertFrom-Json) }
function Hash-File([string]$Path) { return (Microsoft.PowerShell.Utility\Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
function Write-ProgressLine([string]$Root, [string]$Message) {
    $line = [DateTime]::UtcNow.ToString('o') + ' ' + $Message
    [IO.File]::AppendAllText((Join-Path $Root 'progress.txt'), $line + [Environment]::NewLine)
    Write-Host $line
}
function Quote-Arg([string]$Value) {
    Assert-True ($Value -notmatch '["\r\n]') 'Harness paths must not contain quotes or newlines'
    return '"' + $Value + '"'
}
function Get-Specs {
    @(
        [pscustomobject]@{Id='compile'; Seconds=60; ExpectedExit=0; Timeout=$false}
        [pscustomobject]@{Id='bootstrap-usb'; Seconds=240; ExpectedExit=0; Timeout=$false}
        [pscustomobject]@{Id='resolver-stale'; Seconds=20; ExpectedExit=0; Timeout=$false}
        [pscustomobject]@{Id='usb-console'; Seconds=20; ExpectedExit=0; Timeout=$false}
        [pscustomobject]@{Id='usb-legacy'; Seconds=20; ExpectedExit=0; Timeout=$false}
        [pscustomobject]@{Id='monitor-console'; Seconds=15; ExpectedExit=$null; Timeout=$true}
        [pscustomobject]@{Id='monitor-legacy'; Seconds=15; ExpectedExit=$null; Timeout=$true}
        [pscustomobject]@{Id='deadline-native'; Seconds=6; ExpectedExit=$null; Timeout=$true}
        [pscustomobject]@{Id='after-deadline'; Seconds=20; ExpectedExit=0; Timeout=$false}
    )
    foreach ($bad in @('missing','directory','relative','unc','invalid-file','malformed','nonzero','hang')) {
        foreach ($entry in @('usb','monitor')) {
            [pscustomobject]@{Id=('bad-' + $bad + '-' + $entry); Seconds=20; ExpectedExit=2; Timeout=$false}
        }
    }
    foreach ($case in @('healthy','broken','pipe-preflight','hang','pipe','noisy-stderr','blocked-output','remove-cap','first-fast','blocked-discovery','blocked-filesystem','shared-remaining','shared-cutoff','preferred-missing','preferred-relative','preferred-unc','preferred-quote','preferred-exact','source-registry64','source-registry32','source-program64','source-program32','source-machine','source-user')) {
        [pscustomobject]@{Id=('bounds-' + $case); Seconds=20; ExpectedExit=0; Timeout=$false}
    }
}
function Assert-WindowsIdentity($Runtime) {
    Assert-True ($Runtime.os -eq 'Windows_NT' -and $Runtime.ps -match '^5\.1\.' -and $Runtime.edition -eq 'Desktop' -and $Runtime.host_name -ieq 'powershell.exe') 'Native acceptance requires Windows PowerShell 5.1 Desktop powershell.exe'
}
function Runtime-Identity {
    [pscustomobject]@{os=$env:OS; ps=$PSVersionTable.PSVersion.ToString(); edition=$PSVersionTable.PSEdition; host_name=[IO.Path]::GetFileName($HostBinary); host_sha256=(Hash-File $HostBinary); clr=[Environment]::Version.ToString(); framework=[Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory()}
}
function Check-Sources {
    $files = @('usbipd.ps1','run.ps1','scripts/release/bootstrap.ps1','scripts/release/console.ps1','scripts/release/test-bootstrap.ps1')
    $hashes = [ordered]@{}
    foreach ($name in $files) {
        $path = Join-Path $ToolkitRoot $name
        Assert-True (Test-Path -LiteralPath $path -PathType Leaf) ('Required integrated candidate source absent: ' + $name)
        $tokens=$null; $errors=$null
        $null = [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
        Assert-True ($errors.Count -eq 0) ('PowerShell parser rejected: ' + $name + ' ' + ($errors | Out-String))
        $hashes[$name] = Hash-File $path
    }
    foreach ($name in @('tests/fixtures/usbipd_bounds_native.cs','tests/fixtures/usbipd_bounds_native.md','tests/fixtures/usbipd_native_job.cs')) {
        $hashes[$name] = Hash-File (Join-Path $HarnessRoot $name)
    }
    $tokens=$null; $errors=$null
    $null = [Management.Automation.Language.Parser]::ParseFile($SelfPath, [ref]$tokens, [ref]$errors)
    Assert-True ($errors.Count -eq 0) 'Harness does not parse'
    Assert-True (-not ([IO.File]::ReadAllBytes($SelfPath) | Where-Object { $_ -gt 127 })) 'Harness must be ASCII for Windows PowerShell 5.1'
    return $hashes
}
function Get-FixtureSource {
@'
using System;
using System.IO;
using System.Text;
using System.Diagnostics;
using System.Reflection;
using System.Threading;
public static class SyntheticNative {
    static string receipt;
    static string prefix;
    static string Q(string value) { return "\"" + value.Replace("\\", "\\\\").Replace("\"", "\\\"").Replace("\r", "\\r").Replace("\n", "\\n").Replace("\t", "\\t") + "\""; }
    static int Finish(int code) {
        File.WriteAllText(receipt + ".writing", prefix + ",\"complete\":true,\"exit_code\":" + code + "}", new UTF8Encoding(false));
        File.Replace(receipt + ".writing", receipt, null);
        return code;
    }
    public static int Main(string[] args) {
        string exe = Assembly.GetExecutingAssembly().Location;
        string dir = Environment.GetEnvironmentVariable("USBIPD_NATIVE_TRACE");
        string run = Environment.GetEnvironmentVariable("USBIPD_NATIVE_RUN");
        if (String.IsNullOrEmpty(dir) || String.IsNullOrEmpty(run) || !Directory.Exists(dir)) return 99;
        string mode = File.Exists(exe + ".mode") ? File.ReadAllText(exe + ".mode").Trim() : "good";
        string[] quoted = Array.ConvertAll(args, delegate(string a) { return Q(a); });
        int pid = Process.GetCurrentProcess().Id;
        receipt = Path.Combine(dir, "native-" + pid + ".json");
        prefix = "{\"run_id\":" + Q(run) + ",\"pid\":" + pid + ",\"utc\":" + Q(DateTime.UtcNow.ToString("o")) + ",\"exe\":" + Q(exe) + ",\"mode\":" + Q(mode) + ",\"clr\":" + Q(Environment.Version.ToString()) + ",\"args\":[" + String.Join(",", quoted) + "]";
        File.WriteAllText(receipt + ".writing", prefix + ",\"complete\":false}", new UTF8Encoding(false));
        File.Move(receipt + ".writing", receipt);
        if (Path.GetFileName(exe).Equals("wsl.exe", StringComparison.OrdinalIgnoreCase)) {
            if (args.Length != 2 || args[0] != "--list" || args[1] != "--verbose") return Finish(90);
            // Real redirected wsl.exe emits UTF-16LE, not pre-split mock rows.
            byte[] table = Encoding.Unicode.GetBytes("  NAME                    STATE           VERSION\r\n* Legacy-Fixture          Stopped         1\r\n  Ubuntu-22.04            Stopped         2\r\n  Other-Fixture           Stopped         2\r\n");
            Stream output = Console.OpenStandardOutput();
            output.Write(table, 0, table.Length); output.Flush();
            return Finish(0);
        }
        if (args.Length == 1 && args[0] == "--version") {
            if (mode == "hang") { Thread.Sleep(60000); return Finish(88); }
            if (mode == "malformed") { Console.WriteLine("not a semantic version"); return Finish(0); }
            if (mode == "nonzero") { Console.WriteLine("token=fixture-secret"); Console.Error.WriteLine("synthetic probe stderr"); return Finish(17); }
            Console.WriteLine("5.3.0+synthetic"); return Finish(0);
        }
        // Synthetic state is already shared: neither bind nor UAC is needed.
        if (args.Length == 1 && args[0] == "state") {
            Console.WriteLine("{\r\n  \"Devices\": [{\"BusId\":\"1-2\",\"InstanceId\":\"USB\\\\VID_0955&PID_7740\\\\SYNTHETIC\",\"Description\":\"Synthetic device\",\"PersistedGuid\":\"synthetic\",\"ClientIPAddress\":null}]\r\n}");
            return Finish(0);
        }
        if (args.Length == 5 && args[0] == "attach" && args[1] == "--wsl" && args[2] == "Ubuntu-22.04" && args[3] == "--busid" && args[4] == "1-2") return Finish(0);
        return Finish(91);
    }
}
'@
}
function Fixture-Paths {
    $program = Join-Path $ScratchRoot 'Program Files'
    [pscustomobject]@{Program=$program; Usb=(Join-Path (Join-Path $program 'usbipd-win') 'usbipd.exe'); Wsl=(Join-Path (Join-Path $ScratchRoot 'WSL fixtures') 'wsl.exe'); Fallback=(Join-Path (Join-Path $ScratchRoot 'Fallback CLI') 'usbipd.exe')}
}
function Assert-FixtureOwnership([bool]$Compiled) {
    Assert-True ($RunId -match '^[a-f0-9]{32}$' -and (Split-Path $ScratchRoot -Leaf) -ceq ('usbipd native ' + $RunId)) 'Refusing unowned native fixture directory'
    $owner=Read-Json (Join-Path $ScratchRoot 'owner.json')
    Assert-True ($owner.run_id -ceq $RunId -and $owner.harness_sha256 -ceq (Hash-File $SelfPath)) 'Native fixture ownership marker mismatch'
    if ($Compiled) {
        $inventory=Read-Json (Join-Path $ScratchRoot 'binaries.json')
        Assert-True ($inventory.run_id -ceq $RunId -and $inventory.exe_sha256 -match '^[a-f0-9]{64}$') 'Missing owned compiled fixture inventory'
        $paths=Fixture-Paths
        foreach ($path in @($paths.Usb,$paths.Wsl,$paths.Fallback) + @('malformed','nonzero','hang' | ForEach-Object { Join-Path (Join-Path $ScratchRoot ('Bad ' + $_)) 'usbipd.exe' })) {
            Assert-True ((Hash-File $path) -ceq $inventory.exe_sha256) 'Refusing executable outside the compiled fixture byte set'
        }
        Assert-True ((Hash-File (Join-Path $ScratchRoot 'bounds-job.dll')) -ceq $inventory.bounds_job_sha256) 'Bounds enclosing Job byte set changed'
        foreach ($path in @(Bounds-FixturePaths)) { Assert-True ((Hash-File $path) -ceq $inventory.bounds_sha256) 'Bounds compiled fixture byte set changed' }
        foreach ($bad in @('malformed','nonzero','hang')) {
            Assert-True ([IO.File]::ReadAllText((Join-Path (Join-Path $ScratchRoot ('Bad ' + $bad)) 'usbipd.exe.mode')) -ceq $bad) 'Bad-version sidecar changed'
        }
        Assert-True ([IO.File]::ReadAllText((Join-Path $ScratchRoot 'invalid fixture.exe')) -ceq 'synthetic invalid executable') 'Invalid-file fixture changed'
    }
}
function Record-Boundary([string]$Kind, [bool]$Forbidden, [string]$Detail) {
    $entry = [pscustomobject]@{run_id=$RunId; kind=$Kind; forbidden=$Forbidden; detail=$Detail}
    [IO.File]::AppendAllText((Join-Path $OutputRoot 'boundaries.jsonl'), ($entry | ConvertTo-Json -Compress) + "`n")
}
function Assert-Boundaries([string]$Root, [string]$Token) {
    $path = Join-Path $Root 'boundaries.jsonl'
    if (-not (Test-Path -LiteralPath $path)) { return }
    foreach ($line in [IO.File]::ReadAllLines($path)) {
        $entry = $line | ConvertFrom-Json
        Assert-True ($entry.run_id -ceq $Token) 'Stale boundary receipt'
        Assert-True ($entry.forbidden -eq $false) ('Forbidden boundary entered, even if product caught it: ' + $entry.kind)
    }
}
function Native-Records([string]$Root) {
    foreach ($path in [IO.Directory]::GetFiles($Root, 'native-*.json')) { Read-Json $path }
}
function Test-RecordedProcessAlive($Record) {
    $process = $null
    try {
        try { $process = [Diagnostics.Process]::GetProcessById([int]$Record.pid) }
        catch [ArgumentException] { return $false } # PID is absent, not a query failure.
        # Read-only survivor check. A journal is never authority to signal a PID.
        # ConvertFrom-Json may materialize ISO UTC as DateTime. Parsing that
        # again via a culture string discards Kind and invents a timezone shift.
        $started=if ($Record.utc -is [DateTime]) {$Record.utc.ToUniversalTime()} else {[DateTime]::Parse([string]$Record.utc).ToUniversalTime()}
        return (-not $process.HasExited -and $process.MainModule.FileName -ieq $Record.exe -and [Math]::Abs(($process.StartTime.ToUniversalTime() - $started).TotalSeconds) -lt 10)
    }
    finally { if ($process) { $process.Dispose() } }
}
function Stop-OwnedTree($Process, [string]$Root) {
    if (-not $Process.HasExited) {
        if ($env:OS -eq 'Windows_NT') {
            $kill = New-Object Diagnostics.Process
            $info = New-Object Diagnostics.ProcessStartInfo
            $info.FileName = Join-Path $env:SystemRoot 'System32/taskkill.exe'
            $info.Arguments = '/PID ' + $Process.Id + ' /T /F'
            $info.UseShellExecute=$false; $info.CreateNoWindow=$true
            $info.RedirectStandardOutput=$true; $info.RedirectStandardError=$true
            $kill.StartInfo=$info
            try {
                $null=$kill.Start(); $out=$kill.StandardOutput.ReadToEndAsync(); $err=$kill.StandardError.ReadToEndAsync()
                Assert-True ($kill.WaitForExit(5000)) 'Owned taskkill did not finish'
                Assert-True ([Threading.Tasks.Task]::WaitAll([Threading.Tasks.Task[]]@($out,$err),1000)) 'Owned taskkill streams stalled'
                [IO.File]::WriteAllText((Join-Path $Root 'tree-stop.txt'), $out.Result + $err.Result)
            } finally { if (-not $kill.HasExited) { $kill.Kill() }; $kill.Dispose() }
        } else { $Process.Kill() } # Only same-process local guard, no native fixture.
    }
    Assert-True ($Process.WaitForExit(5000)) 'Owned phase did not stop'
    foreach ($record in @(Native-Records $Root)) {
        # Only the supervisor object we started above may authorize tree cleanup.
        # Never reacquire/signal a journal PID; fail if tree cleanup left a survivor.
        Assert-True (-not (Test-RecordedProcessAlive $record)) 'Owned native fixture survived tree stop'
    }
}
function Invoke-OwnedPhase([string]$Name, [int]$Seconds, [string]$Root) {
    $bounded=$Name -like 'bounds-*'
    $outerClock=[Diagnostics.Stopwatch]::StartNew()
    $outer=$null
    $start = [DateTime]::UtcNow.ToString('o')
    $receipt = [ordered]@{run_id=$RunId; id=$Name; started_utc=$start; state='running'; ended_utc=$null; timed_out=$false; stopped=$false; exit_code=$null}
    Save-Json (Join-Path $Root 'phase.json') $receipt
    Write-ProgressLine (Split-Path $Root) ('BEGIN ' + $Name)
    $process = New-Object Diagnostics.Process
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $HostBinary
    $info.Arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' + (Quote-Arg $SelfPath) + ' -Mode Phase -ToolkitRoot ' + (Quote-Arg $ToolkitRoot) + ' -OutputRoot ' + (Quote-Arg $Root) + ' -RunId ' + (Quote-Arg $RunId) + ' -PhaseName ' + (Quote-Arg $Name) + ' -ScratchRoot ' + (Quote-Arg $ScratchRoot)
    $info.UseShellExecute=$false; $info.CreateNoWindow=$true
    $info.RedirectStandardOutput=$true; $info.RedirectStandardError=$true
    # Existing bootstrap fixtures also allocate temp/log files. Keep all of them
    # in this run's owned tree, never an inherited operational log or other lane.
    $info.EnvironmentVariables['TEMP']=$ScratchRoot
    $info.EnvironmentVariables['TMP']=$ScratchRoot
    $info.EnvironmentVariables['TMPDIR']=$ScratchRoot
    $info.EnvironmentVariables['LOCALAPPDATA']=(Join-Path $ScratchRoot 'Local fixture')
    $info.EnvironmentVariables['JIBO_DFU_SETUP_LOG']=''
    $info.EnvironmentVariables['USBIPD_FIXTURE_MODE']=''
    if ($bounded) { $info.RedirectStandardInput=$true }
    $process.StartInfo=$info
    try {
        if ($bounded) { Import-BoundsTypes; $outer=New-Object NativeBoundsJob }
        $null=$process.Start(); $null=$process.Handle
        if ($bounded) {
            $outer.Assign($process)
            $receipt.outer_assigned_before_gate=$true
            $process.StandardInput.WriteLine('outer-job:'+$RunId); $process.StandardInput.Flush()
        }
        $out=$process.StandardOutput.ReadToEndAsync(); $err=$process.StandardError.ReadToEndAsync()
        $receipt.pid=$process.Id
        Save-Json (Join-Path $Root 'phase.json') $receipt
        $wait=if ($bounded) {[Math]::Max(0,($Seconds-2)*1000-[int]$outerClock.ElapsedMilliseconds)} else {$Seconds*1000}
        if (-not $process.WaitForExit($wait)) { $receipt.timed_out=$true }
        if ($bounded) { $receipt.outer_active_zero=$outer.Stop([Math]::Max(0,$Seconds*1000-[int]$outerClock.ElapsedMilliseconds)); Assert-True $receipt.outer_active_zero 'Bounds outer Job retained processes'; Assert-True ($process.WaitForExit(1000)) 'Bounds outer phase did not exit' }
        else { Stop-OwnedTree $process $Root }
        $receipt.stopped=$process.HasExited
        $receipt.exit_code=$process.ExitCode
        $streamWait=if ($bounded) {[Math]::Max(0,$Seconds*1000-[int]$outerClock.ElapsedMilliseconds)} else {2000}
        Assert-True ([Threading.Tasks.Task]::WaitAll([Threading.Tasks.Task[]]@($out,$err),$streamWait)) 'Owned phase output streams did not close'
        [IO.File]::WriteAllText((Join-Path $Root 'stdout.txt'), $out.Result)
        [IO.File]::WriteAllText((Join-Path $Root 'stderr.txt'), $err.Result)
        $receipt.state='complete'
    } catch {
        $receipt.state='failed'
        [IO.File]::WriteAllText((Join-Path $Root 'supervisor-error.txt'), $_.Exception.ToString())
    } finally {
        try {
            if ($bounded) {
                $receipt.outer_active_zero=$outer.Stop([Math]::Max(0,$Seconds*1000-[int]$outerClock.ElapsedMilliseconds))
                if (-not $process.HasExited) { $process.Kill(); $null=$process.WaitForExit(1000) } # retained invocation-owned supervisor only
                Assert-True $receipt.outer_active_zero 'Bounds outer Job retained processes'
                $outer.Dispose()
            } else { Stop-OwnedTree $process $Root }
        } catch { $receipt.state='failed'; if ($outer) {$outer.Dispose()} }
        $process.Dispose()
        $receipt.ended_utc=[DateTime]::UtcNow.ToString('o')
        Save-Json (Join-Path $Root 'phase.json') $receipt
        Write-ProgressLine (Split-Path $Root) ('END ' + $Name + ' ' + $receipt.state + ' timeout=' + $receipt.timed_out)
    }
    return [pscustomobject]$receipt
}
function Assert-PhaseEvidence($Spec, $Receipt, [string]$Root, [string]$Token) {
    Assert-True ($Receipt.id -ceq $Spec.Id -and $Receipt.run_id -ceq $Token -and $Receipt.state -ceq 'complete' -and $Receipt.stopped -eq $true) ('Incomplete/stale phase: ' + $Spec.Id)
    Assert-True ($Receipt.timed_out -eq $Spec.Timeout) ('Unexpected timeout result: ' + $Spec.Id)
    if (-not $Spec.Timeout) { Assert-True ($null -ne $Receipt.exit_code -and $Receipt.exit_code -eq $Spec.ExpectedExit) ('Wrong native phase exit: ' + $Spec.Id) }
    Assert-Boundaries $Root $Token
    $stdout=[IO.File]::ReadAllText((Join-Path $Root 'stdout.txt'))
    $stderr=[IO.File]::ReadAllText((Join-Path $Root 'stderr.txt'))
    Assert-True (($stdout + $stderr) -notmatch 'fixture-secret|FAIL scenario:') 'Unsafe output or failing bootstrap scenario'
    if ($Spec.Id -like 'bounds-*') {
        Assert-True (($stdout+$stderr) -notmatch '(?m)^5\.3\.0|x{100}') 'Bounds raw native output entered diagnostics'
        Assert-True ($Receipt.outer_assigned_before_gate -eq $true -and $Receipt.outer_active_zero -eq $true) 'Bounds enclosing Job not verified'
        Assert-True ([IO.File]::Exists((Join-Path $Root 'bounds.json'))) 'Missing bounds API observation'
        $value=Read-Json (Join-Path $Root 'bounds.json')
        Assert-BoundsEvidence $value $Spec.Id.Substring(7) $Token
        $compiled=Read-Json (Join-Path (Join-Path (Split-Path $Root) 'compile') 'bounds-compiler.json')
        Assert-True ($value.fixture_sha256 -ceq $compiled.exe_sha256) 'Bounds executed PE fingerprint mismatch'
        $helper=Join-Path $ToolkitRoot 'usbipd.ps1'
        if ($Spec.Id -in @('bounds-pipe-preflight','bounds-remove-cap')) {
            $expectedText=Get-BoundsMutant $Spec.Id.Substring(7) $helper
            $helper=Join-Path $Root 'private-mutant.ps1'
            Assert-True ([IO.File]::ReadAllText($helper) -ceq $expectedText) 'Bounds private mutant differs from exact controlled mutation'
        }
        Assert-True ($value.helper_sha256 -ceq (Hash-File $helper)) 'Bounds executed helper fingerprint mismatch'
        Assert-True ($compiled.run_id -ceq $Token -and $compiled.flags -ceq '/nologo /target:exe /langversion:5 /warnaserror+' -and $compiled.supplied_source_sha256 -ceq (Hash-File (Join-Path $HarnessRoot 'tests/fixtures/usbipd_bounds_native.cs')) -and $compiled.job_source_sha256 -ceq (Hash-File (Join-Path $HarnessRoot 'tests/fixtures/usbipd_native_job.cs'))) 'Bounds compiler/source binding mismatch'
        Assert-True ($compiled.adapted_source_sha256 -ceq (Hash-File (Join-Path (Join-Path (Split-Path $Root) 'compile') 'bounds-fixture.cs'))) 'Bounds adapted compiler input mismatch'
        $inventory=if ([IO.File]::Exists((Join-Path $Root 'inventory.txt'))) {@([IO.File]::ReadAllLines((Join-Path $Root 'inventory.txt')))} else {@()}
        Assert-True ($inventory.Count -eq $value.inventory_entries -and @($inventory | Where-Object {$_ -like 'registry:*'}).Count -eq $value.registry_entries) 'Bounds read journal disagrees with result'
        Assert-True ([IO.File]::Exists((Join-Path $Root 'filesystem.entered')) -eq $value.filesystem_entered -and [IO.File]::Exists((Join-Path $Root 'poison.entered')) -eq $value.poison_entered) 'Bounds boundary sentinels disagree with result'
        if ($Spec.Id -in @('bounds-hang','bounds-pipe','bounds-blocked-output','bounds-pipe-preflight')) {
            Assert-True ([int][IO.File]::ReadAllText((Join-Path $Root 'parent.pid')) -eq $value.parent_id -and [IO.File]::ReadAllText((Join-Path $Root 'parent.observed')) -ceq 'retained') 'Bounds parent observation inventory missing'
            if ($Spec.Id -ne 'bounds-blocked-output') {
                Assert-True ([int][IO.File]::ReadAllText((Join-Path $Root 'child.pid')) -eq $value.child_id -and [IO.File]::ReadAllText((Join-Path $Root 'child.observed')) -ceq 'retained') 'Bounds child observation inventory missing'
            }
        }
        $entries=if ([IO.File]::Exists((Join-Path $Root 'probe.entries'))) {@([IO.File]::ReadAllLines((Join-Path $Root 'probe.entries')))} else {@()}
        Assert-True ($entries.Count -eq $value.entries -and @($entries | Where-Object {$_ -cne 'version'}).Count -eq 0) 'Bounds probe journal disagrees with result'
        return
    }
    if ($Spec.Id -eq 'compile') {
        $compile=Read-Json (Join-Path $Root 'compiler.json')
        Assert-True ($compile.run_id -ceq $Token -and $compile.exit_code -eq 0 -and $compile.flags -ceq '/nologo /target:exe /langversion:5 /warnaserror+' -and $compile.compiler_sha256 -match '^[a-f0-9]{64}$' -and $compile.exe_sha256 -match '^[a-f0-9]{64}$') 'Missing compiler identity, C#5/warnings gate or executable fingerprint'
        return
    }
    if ($Spec.Id -eq 'bootstrap-usb') {
        foreach ($name in @('usb-installed-stale-path','usb-installed-current-path','usb-noop-ready','usb-noop-missing','usb-noop-broken','usb-fresh-ready','usb-fresh-missing','usb-reboot-ready','usb-winget-reboot','usb-failure-ready','usb-uac-cancelled','usb-install-refused','usb-winget-missing','usb-declined')) {
            Assert-True ($stdout -match ('(?m)^PASS scenario: ' + [regex]::Escape($name) + '\r?$')) ('Existing bootstrap case missing: ' + $name)
        }
        Assert-True ($stdout -match 'Bootstrap scenarios: 14 passed; 0 failed') 'Existing bootstrap USB inventory incomplete'
        return
    }
    $records=@(Native-Records $Root)
    Assert-True ($records.Count -gt 0) ('Native fixture did not run: ' + $Spec.Id)
    foreach ($record in $records) {
        Assert-True ($record.run_id -ceq $Token) 'Stale native journal'
        Assert-True (-not (Test-RecordedProcessAlive $record)) 'Native fixture is still alive'
        Assert-True ($record.clr -match '^4\.') 'Fixture was not executed by .NET Framework'
    }
    $paths=Fixture-Paths
    $wsl=@($records | Where-Object { $_.exe -ieq $paths.Wsl })
    $usb=@($records | Where-Object { $_.exe -ine $paths.Wsl })
    foreach ($record in $wsl) {
        Assert-True (($record.args -join '|') -ceq '--list|--verbose' -and $record.complete -eq $true -and $record.exit_code -eq 0) 'Unexpected fake WSL operation'
    }
    if ($Spec.Id -eq 'deadline-native') {
        Assert-True ($records.Count -eq 1 -and $usb.Count -eq 1 -and $usb[0].mode -ceq 'hang' -and ($usb[0].args -join '|') -ceq '--version' -and $usb[0].complete -eq $false) 'Intentional native timeout did not enter and kill the hanging fixture'
        return
    }
    $boundaryPath=Join-Path $Root 'boundaries.jsonl'
    Assert-True (Test-Path -LiteralPath $boundaryPath -PathType Leaf) 'Boundary guard never ran'
    $boundaries=@([IO.File]::ReadAllLines($boundaryPath) | ForEach-Object { $_ | ConvertFrom-Json })
    if ($Spec.Id -in @('resolver-stale','after-deadline')) {
        Assert-True (@($boundaries | Where-Object { $_.kind -eq 'lookup' }).Count -eq 2 -and @($boundaries | Where-Object { $_.kind -eq 'registry-data' }).Count -eq 2) 'Real candidate discovery did not exercise synthetic lookup and registry boundaries'
        Assert-True ($records.Count -eq 1 -and $usb.Count -eq 1 -and $usb[0].exe -ieq $paths.Usb -and ($usb[0].args -join '|') -ceq '--version' -and $usb[0].complete -eq $true -and $usb[0].exit_code -eq 0) 'Resolver did not validate the exact compiled executable'
        $result=Read-Json (Join-Path $Root 'resolver.json')
        Assert-True ($result.run_id -ceq $Token -and $result.path_unchanged -eq $true -and $result.resolved_exact -eq $true -and $result.stale_path -eq $true) 'Stale-PATH discovery failed or changed PATH'
        return
    }
    Assert-True (@($boundaries | Where-Object { $_.kind -eq 'lookup' -and $_.detail -ceq 'wsl.exe' }).Count -eq 1 -and @($boundaries | Where-Object { $_.kind -eq 'lookup' -and $_.detail -cne 'wsl.exe' }).Count -eq 0) 'Explicit handoff attempted fallback command discovery'
    Assert-True ($wsl.Count -eq 1) 'Real distro table parser was not exercised exactly once'
    if ($Spec.Id -like 'bad-*') {
        foreach ($record in $usb) {
            Assert-True (($record.args -join '|') -ceq '--version' -and $record.exe -ine $paths.Fallback) 'Bad explicit executable entered state/attach or silently switched to fallback'
        }
        $mode=$Spec.Id -replace '^bad-','' -replace '-(usb|monitor)$',''
        $expectedCount=if ($mode -in @('malformed','nonzero','hang')) {1} else {0}
        Assert-True ($usb.Count -eq $expectedCount) 'Version probe did not execute the expected invalid fixture'
        if ($mode -in @('malformed','nonzero','hang')) {
            Assert-True ($usb[0].exe -ieq (Join-Path $ScratchRoot ('Bad ' + $mode + '/usbipd.exe'))) 'Wrong invalid executable was probed'
            if ($mode -eq 'hang') { Assert-True ($usb[0].complete -eq $false) 'Timed-out version fixture was not killed by the real resolver' }
            else { Assert-True ($usb[0].complete -eq $true -and $usb[0].exit_code -eq $(if ($mode -eq 'nonzero') {17} else {0})) 'Native invalid version exit mismatch' }
        }
        return
    }
    foreach ($record in $usb) {
        Assert-True ($record.exe -ieq $paths.Usb -and $record.mode -ceq 'good') 'Explicit executable changed'
        Assert-True (($record.args -join '|') -cin @('--version','state','attach|--wsl|Ubuntu-22.04|--busid|1-2')) 'Unexpected USB command or argument loss'
        if ($record.complete -eq $true) { Assert-True ($record.exit_code -eq 0) 'Native fixture command failed' }
        else { Assert-True ($Spec.Timeout -and $record.args[0] -in @('state','attach')) 'Unexpected unfinished native fixture' }
    }
    $versions=@($usb | Where-Object { ($_.args -join '|') -ceq '--version' })
    $states=@($usb | Where-Object { ($_.args -join '|') -ceq 'state' -and $_.complete -eq $true })
    $attaches=@($usb | Where-Object { $_.args[0] -ceq 'attach' -and $_.complete -eq $true })
    Assert-True ($versions.Count -eq 1) 'Exact supplied path was not version-validated once'
    $minimum=if ($Spec.Timeout) {2} else {1}
    Assert-True (@($usb | Where-Object { $_.complete -ne $true }).Count -le 1) 'More than one native command was left unfinished'
    Assert-True ($states.Count -ge $minimum -and $attaches.Count -ge $minimum) 'Actual USB-only/monitor state and attach cycles did not run'
    if (-not $Spec.Timeout) { Assert-True ($states.Count -eq 1 -and $attaches.Count -eq 1) 'One-shot USB-only entry did not exit after one cycle' }
}
function Assert-Manifest($Manifest) {
    Assert-True ($Manifest.kind -ceq 'native-usbipd' -and $Manifest.schema -eq 1 -and $Manifest.state -ceq 'complete') 'Not a completed native acceptance manifest'
    Assert-WindowsIdentity $Manifest.runtime
    Assert-True ($Manifest.run_id -match '^[a-f0-9]{32}$') 'Missing run nonce'
    $names=@(Get-Specs | ForEach-Object { $_.Id })
    Assert-True ($Manifest.phases.Count -eq $names.Count) 'Missing/extra native phases'
    Assert-True ((@($Manifest.phases | ForEach-Object { $_.id }) -join '|') -ceq ($names -join '|')) 'Wrong/duplicate phase inventory'
    Assert-True ($Manifest.revision -match '^[a-f0-9]{40}$' -and $Manifest.harness_sha256 -ceq (Hash-File $SelfPath)) 'Revision/harness fingerprint mismatch'
}
function Validate-Run([string]$Root) {
    Assert-WindowsIdentity (Runtime-Identity)
    $manifest=Read-Json (Join-Path $Root 'run.json')
    Assert-Manifest $manifest
    $runtime=Runtime-Identity
    Assert-WindowsIdentity $runtime
    Assert-True ($manifest.runtime.host_sha256 -ceq $runtime.host_sha256) 'Receipt host binary differs from validation host'
    if ($env:GITHUB_RUN_ID) {
        Assert-True ($manifest.ci_run_id -ceq $env:GITHUB_RUN_ID -and $manifest.ci_attempt -ceq $env:GITHUB_RUN_ATTEMPT -and $manifest.ci_job -ceq $env:GITHUB_JOB) 'Receipt belongs to another CI run/attempt/job'
    }
    $revision=(& git -C $ToolkitRoot rev-parse HEAD | Out-String).Trim()
    Assert-True ($LASTEXITCODE -eq 0 -and $revision -ceq $manifest.revision) 'Receipt revision differs from integrated checkout'
    if ($env:GITHUB_SHA) { Assert-True ($manifest.revision -ceq $env:GITHUB_SHA) 'Receipt revision differs from exact CI commit' }
    $current=Check-Sources
    foreach ($name in $current.Keys) { Assert-True ($manifest.source_sha256.$name -ceq $current[$name]) ('Integrated source changed since execution: ' + $name) }
    $actualDirs=@([IO.Directory]::GetDirectories($Root) | ForEach-Object { [IO.Path]::GetFileName($_) } | Sort-Object)
    $expectedDirs=@(Get-Specs | ForEach-Object { $_.Id } | Sort-Object)
    Assert-True (($actualDirs -join '|') -ceq ($expectedDirs -join '|')) 'Unaccounted root phase directories'
    $actualFiles=@([IO.Directory]::GetFiles($Root) | ForEach-Object { [IO.Path]::GetFileName($_) } | Sort-Object)
    Assert-True (($actualFiles -join '|') -ceq 'progress.txt|run.json') 'Unaccounted root diagnostics'
    # run.json is the fixed metadata container, never its own hashed leaf.
    Assert-True ((@($manifest.root_files.PSObject.Properties.Name) -join '|') -ceq 'progress.txt') 'Wrong root diagnostic hash inventory'
    Assert-True ($manifest.root_files.'progress.txt' -ceq (Hash-File (Join-Path $Root 'progress.txt'))) 'Root diagnostic file hash mismatch'
    $savedScratch=$script:ScratchRoot
    $script:ScratchRoot=$manifest.scratch_root
    try {
        foreach ($spec in @(Get-Specs)) {
            $entry=@($manifest.phases | Where-Object { $_.id -ceq $spec.Id })[0]
            $dir=Join-Path $Root $spec.Id
            Assert-True ($entry.state -ceq 'pass') ('Phase failed: ' + $spec.Id)
            Assert-True ([IO.Directory]::GetDirectories($dir).Count -eq 0) 'Nested phase output is not allowed'
            foreach ($file in $entry.files.PSObject.Properties) {
                Assert-True ($file.Name -notmatch '[/\\]' -and $file.Value -ceq (Hash-File (Join-Path $dir $file.Name))) 'Diagnostic file hash mismatch'
            }
            $actual=@([IO.Directory]::GetFiles($dir) | ForEach-Object { [IO.Path]::GetFileName($_) } | Sort-Object)
            $expected=@($entry.files.PSObject.Properties.Name | Sort-Object)
            Assert-True (($actual -join '|') -ceq ($expected -join '|')) 'Unaccounted phase diagnostics'
            foreach ($required in @('phase.json','stdout.txt','stderr.txt')) { Assert-True ($expected -contains $required) ('Missing receipt file: ' + $required) }
            $receipt=Read-Json (Join-Path $dir 'phase.json')
            Assert-True ([DateTime]::Parse($receipt.started_utc) -ge [DateTime]::Parse($manifest.started_utc) -and [DateTime]::Parse($receipt.ended_utc) -le [DateTime]::Parse($manifest.ended_utc)) 'Stale/out-of-run phase timestamps'
            Assert-PhaseEvidence $spec $receipt $dir $manifest.run_id
        }
    } finally { $script:ScratchRoot=$savedScratch }
    Write-Output ('PASS native receipt gate: ' + $manifest.phases.Count + ' complete phases; revision ' + $manifest.revision)
}
function Install-PhaseGuards([bool]$Discover, [bool]$AllowUsb) {
    $global:NativeFixturePaths=Fixture-Paths
    $global:NativeFixtureDiscover=$Discover
    $global:NativeFixtureAllowUsb=$AllowUsb
    $global:NativeFixtureRoot=$OutputRoot
    $global:NativeFixtureToken=$RunId
    $env:PATH=(Split-Path $global:NativeFixturePaths.Wsl) + ';' + (Split-Path $global:NativeFixturePaths.Fallback)
    $env:ProgramFiles=$global:NativeFixturePaths.Program; $env:ProgramW6432=$env:ProgramFiles
    $env:USBIPD_NATIVE_TRACE=$OutputRoot; $env:USBIPD_NATIVE_RUN=$RunId
    # No registry inventory, UAC, installers, real WSL, network or clipboard.
    function global:Get-Command {
        param($Name,$CommandType,$ErrorAction)
        $bad=($Name -notin @('wsl.exe','usbipd.exe','usbipd') -or ($Name -ne 'wsl.exe' -and -not $global:NativeFixtureDiscover))
        # This primitive/env-only seam is snapshotted into the private worker.
        $entry=@{run_id=$env:USBIPD_NATIVE_RUN; kind='lookup'; forbidden=$bad; detail=$Name}
        [IO.File]::AppendAllText((Join-Path $env:USBIPD_NATIVE_TRACE 'boundaries.jsonl'),($entry | ConvertTo-Json -Compress)+"`n")
        if ($bad) { throw 'Forbidden command lookup/fallback' }
        return (Microsoft.PowerShell.Core\Get-Command -Name $Name -CommandType Application -ErrorAction SilentlyContinue)
    }
    function global:Get-ItemProperty {
        param($LiteralPath,$Name,$ErrorAction)
        $entry=@{run_id=$env:USBIPD_NATIVE_RUN; kind='registry-data'; forbidden=$false; detail=$Name}
        [IO.File]::AppendAllText((Join-Path $env:USBIPD_NATIVE_TRACE 'boundaries.jsonl'),($entry | ConvertTo-Json -Compress)+"`n")
        return $null
    }
    function global:Start-Process { Record-Boundary 'start-process' $true ''; throw 'No installer, UAC or unrelated process may start' }
    function global:Invoke-ToolkitElevated { Record-Boundary 'elevated' $true ''; throw 'No elevation in fixture' }
    function global:Read-Host { Record-Boundary 'prompt' $true ''; throw 'No interactive input in fixture' }
    function global:Set-Clipboard { Record-Boundary 'clipboard-write' $true ''; throw 'No clipboard access in fixture' }
    function global:Get-Clipboard { Record-Boundary 'clipboard-read' $true ''; throw 'No clipboard access in fixture' }
    function global:Invoke-WebRequest { Record-Boundary 'network' $true ''; throw 'No network in fixture' }
    function global:Invoke-RestMethod { Record-Boundary 'network' $true ''; throw 'No network in fixture' }
}
function Bounds-FixturePaths {
    $main=Bounds-Path; $folder=Split-Path $main
    $main
    Join-Path $folder 'first.exe'
    Join-Path $folder 'second.exe'
    Join-Path $folder 'poison.exe'
    foreach ($name in @('registry64','registry32','machine','user')) { Join-Path (Join-Path $folder $name) 'usbipd.exe' }
    foreach ($name in @('program64','program32')) { Join-Path (Join-Path (Join-Path $folder $name) 'usbipd-win') 'usbipd.exe' }
}
function Bounds-Path { return (Join-Path $ScratchRoot ('Bounds ' + [char]0x03a9 + ' fixtures/usbipd.exe')) }
function Get-BoundsFixtureSource {
    $text=[IO.File]::ReadAllText((Join-Path $HarnessRoot 'tests/fixtures/usbipd_bounds_native.cs'))
    # Narrow fixture-only seam: per-executable delays/modes and a live-handle
    # observation barrier. The supplied fixture bytes remain unchanged on disk.
    $needle='string mode=Environment.GetEnvironmentVariable("USBIPD_BOUNDS_MODE") ?? "healthy";'
    Assert-True ($text.Contains($needle)) 'Bounds fixture mode seam changed'
    $text=$text.Replace($needle, $needle + @'

        string exe=Assembly.GetExecutingAssembly().Location;
        if(File.Exists(exe+".mode")) mode=File.ReadAllText(exe+".mode");
        if(Path.GetFileName(exe)=="poison.exe") { File.WriteAllText(Path.Combine(folder,"poison.entered"),"forbidden executable entered"); return 97; }
'@)
    $needle='if(mode=="hang" || mode=="pipe") {'
    Assert-True ($text.Contains($needle)) 'Bounds fixture observation seam changed'
    $text=$text.Replace($needle,@'
        if(Environment.GetEnvironmentVariable("USBIPD_BOUNDS_OBSERVE")=="yes") {
            Stopwatch gate=Stopwatch.StartNew();
            while(!File.Exists(Path.Combine(folder,"parent.observed")) && gate.ElapsedMilliseconds<2000) Thread.Sleep(1);
            if(!File.Exists(Path.Combine(folder,"parent.observed"))) return 95;
        }
        if(File.Exists(exe+".delay")) Thread.Sleep(Int32.Parse(File.ReadAllText(exe+".delay")));
        if(mode=="hang" || mode=="pipe") {
'@)
    $needle='if(mode=="hang") { child.WaitForExit(); return 94; }'
    Assert-True ($text.Contains($needle)) 'Bounds fixture child barrier seam changed'
    return $text.Replace($needle,@'
            Stopwatch observed=Stopwatch.StartNew();
            while(!File.Exists(Path.Combine(folder,"child.observed")) && observed.ElapsedMilliseconds<2000) Thread.Sleep(1);
            if(!File.Exists(Path.Combine(folder,"child.observed"))) return 96;
            if(mode=="hang") { child.WaitForExit(); return 94; }
'@)
}
function Import-BoundsTypes {
    Assert-FixtureOwnership $true
    # Load the already hash-checked image, not a file-backed assembly that
    # Windows keeps locked until this supervisor exits and prevents cleanup.
    if (-not ('NativeBoundsJob' -as [type])) { $null=[Reflection.Assembly]::Load([IO.File]::ReadAllBytes((Join-Path $ScratchRoot 'bounds-job.dll'))) }
}
function Assert-BoundsEvidence($Value, [string]$Case, [string]$Token) {
    Assert-True ($Value.run_id -ceq $Token -and $Value.id -ceq $Case -and $Value.before_outer_cleanup -eq $true) 'Bounds stale/missing API observation'
    Assert-True ($Value.helper_sha256 -match '^[a-f0-9]{64}$' -and $Value.fixture_sha256 -match '^[a-f0-9]{64}$') 'Bounds fingerprints missing'
    $limit=if ($Case -in @('healthy','broken','hang','pipe','pipe-preflight')) {4000} elseif ($Case -in @('noisy-stderr','blocked-output','remove-cap')) {3000} elseif ($Case -like 'shared-*') {3300} elseif ($Case -eq 'first-fast') {6000} else {15300}
    Assert-True ($Value.limit_ms -eq $limit -and $Value.elapsed_ms -ge 0 -and $Value.elapsed_ms -le $limit) 'Bounds total deadline exceeded'
    Assert-True ($Value.raw_output_leaked -eq $false) 'Bounds raw output escaped'
    if ($Case -eq 'pipe-preflight') {
        Assert-True ($Value.parent_observed -eq $true -and $Value.child_observed -eq $true -and $Value.parent_exited -eq $true -and $Value.child_exited -eq $false -and ($Value.stdout_pending -eq $true -or $Value.stderr_pending -eq $true)) 'Bounds pipe inheritance preflight not proven'
        Assert-True ($Value.containment_stopped -eq $false) 'Bounds cleanup-withheld mutant did not run'
    } else {
        if ($Case -notin @('preferred-relative','preferred-unc','preferred-quote')) {
            Assert-True ($Value.readers_released -eq $true -and $Value.containment_stopped -eq $true) 'Bounds product cleanup incomplete'
        }
        if ($Case -in @('hang','pipe','blocked-output')) {
            Assert-True ($Value.parent_observed -eq $true -and $Value.parent_exited -eq $true) 'Bounds exact parent survived API return'
        }
        if ($Case -in @('hang','pipe')) {
            Assert-True ($Value.child_observed -eq $true -and $Value.child_exited -eq $true) 'Bounds exact descendant survived API return'
        }
    }
    $accept=$Case -in @('healthy','first-fast','shared-remaining','preferred-exact','source-registry64','source-registry32','source-program64','source-program32','source-machine','source-user','remove-cap')
    Assert-True ($Value.accepted -eq $accept) 'Bounds readiness decision incorrect'
    if ($accept) { Assert-True ($Value.resolved_exact -eq $true) 'Bounds exact path/precedence lost' }
    $entries=if ($Case -like 'preferred-*' -and $Case -ne 'preferred-exact' -or $Case -like 'blocked-*' -and $Case -ne 'blocked-output') {0} elseif ($Case -like 'shared-*') {2} else {1}
    Assert-True ($Value.entries -eq $entries) 'Bounds actual probe entry inventory incorrect'
    if ($Case -like 'preferred-*') { Assert-True ($Value.inventory_entries -eq 0) 'Bounds preferred path fell back to inventory' }
    if ($Case -eq 'first-fast') { Assert-True ($Value.registry_entries -eq 0 -and $Value.inventory_entries -eq 1) 'Bounds healthy first consulted delayed provider' }
    if ($Case -eq 'blocked-discovery') { Assert-True ($Value.registry_entries -eq 1 -and $Value.inventory_entries -eq 3) 'Bounds blocked discovery was not entered' }
    if ($Case -eq 'blocked-filesystem') { Assert-True ($Value.filesystem_entered -eq $true) 'Bounds blocked filesystem was not entered' }
    Assert-True ($Value.path_unchanged -eq $true -and $Value.poison_entered -eq $false) 'Bounds PATH/poison boundary violated'
    if ($Case -eq 'remove-cap') { Assert-True ($Value.mutation_detected -eq $true) 'Bounds output-cap removal not detected' }
}
function Get-BoundsMutant([string]$case, [string]$helper) {
        $text=[IO.File]::ReadAllText($helper)
        if ($case -eq 'remove-cap') {
            Assert-True ($text.Contains('if ($bytes -gt 4096)')) 'Bounds cap mutation seam changed'
            $text=$text.Replace('if ($bytes -gt 4096)','if ($bytes -gt 1048576)')
        } else {
            # Deliberately withhold BOTH product Job termination and kill-on-close.
            # The independent outer Job remains assigned before this phase's gate.
            Assert-True ($text.Contains('if(grouped) {') -and $text.Contains('limits.Basic.Flags=0x2000;')) 'Bounds containment mutation seam changed'
            $text=$text.Replace('if(grouped) {','if(grouped && milliseconds<0) {').Replace('limits.Basic.Flags=0x2000;','limits.Basic.Flags=0;')
            $needle="        return `$false`n    } catch { return `$false }"
            Assert-True ($text.Contains($needle)) 'Bounds pipe preflight instrumentation seam changed'
            $text=$text.Replace($needle,@'
        [IO.File]::WriteAllText((Join-Path $env:USBIPD_BOUNDS_DIRECTORY 'pipe-pending.json'),(@{stdout_pending=(-not $ended[0] -and -not $reads[0].IsCompleted); stderr_pending=(-not $ended[1] -and -not $reads[1].IsCompleted); parent_exited=$process.HasExited} | ConvertTo-Json -Compress))
        return $false
    } catch { return $false }
'@)
        }
    return $text
}
function Run-BoundsCase {
    $case=$PhaseName.Substring(7)
    $fixture=Bounds-Path
    $env:USBIPD_BOUNDS_DIRECTORY=$OutputRoot
    $env:USBIPD_BOUNDS_CASE=$case
    $env:USBIPD_BOUNDS_EXE=$fixture
    $env:USBIPD_BOUNDS_MODE=if ($case -in @('healthy','broken','hang','pipe','noisy-stderr','blocked-output')) {$case} elseif ($case -eq 'pipe-preflight') {'pipe'} elseif ($case -eq 'remove-cap') {'noisy-stderr'} else {'healthy'}
    $env:USBIPD_BOUNDS_OBSERVE=if ($case -in @('hang','pipe','blocked-output','pipe-preflight')) {'yes'} else {''}
    $env:USBIPD_BOUNDS_FIRST=Join-Path (Split-Path $fixture) 'first.exe'
    $env:USBIPD_BOUNDS_SECOND=Join-Path (Split-Path $fixture) 'second.exe'
    # The supplied fixture's observation files are phase-private and initially absent.
    Assert-True ([IO.Directory]::GetFiles($OutputRoot,'*.pid').Count -eq 0 -and -not [IO.File]::Exists((Join-Path $OutputRoot 'probe.entries'))) 'Bounds stale entry observations'
    [IO.File]::WriteAllText(($env:USBIPD_BOUNDS_FIRST+'.mode'),'broken')
    [IO.File]::WriteAllText(($env:USBIPD_BOUNDS_FIRST+'.delay'),'1000')
    [IO.File]::WriteAllText(($env:USBIPD_BOUNDS_SECOND+'.delay'),$(if ($case -eq 'shared-cutoff') {'2000'} else {'0'}))
    $env:ProgramW6432=Join-Path (Split-Path $fixture) 'absent64'
    $env:ProgramFiles=Join-Path (Split-Path $fixture) 'absent32'
    foreach ($name in @('program64','program32')) {
        if ($case -eq ('source-'+$name) -or $case -in @('source-registry64','source-registry32') -or $case -eq 'source-program64') {
            if ($name -eq 'program64') { $env:ProgramW6432=Join-Path (Split-Path $fixture) $name } else { $env:ProgramFiles=Join-Path (Split-Path $fixture) $name }
        }
    }
    Save-Json (Join-Path $OutputRoot 'discovery-parent.json') @{program64=$env:ProgramW6432; program=$env:ProgramFiles; process64=[Environment]::Is64BitProcess; expected_program32=(Join-Path (Join-Path (Join-Path (Split-Path $fixture) 'program32') 'usbipd-win') 'usbipd.exe')}
    $before=@([Environment]::GetEnvironmentVariable('Path','Process'),[Environment]::GetEnvironmentVariable('Path','User'),[Environment]::GetEnvironmentVariable('Path','Machine')) | ConvertTo-Json -Compress
    # Only the documented narrow read seams enter the product's private worker.
    function Get-Command {
        param($Name,$CommandType,$ErrorAction)
        if ($CommandType -cne 'Application' -or $Name -notin @('usbipd.exe','usbipd')) { [IO.File]::WriteAllText((Join-Path $env:USBIPD_BOUNDS_DIRECTORY 'poison.entered'),'bad lookup'); throw 'Forbidden bounds lookup' }
        [IO.File]::AppendAllText((Join-Path $env:USBIPD_BOUNDS_DIRECTORY 'discovery-worker.jsonl'),((@{name=$Name; program64=$env:ProgramW6432; program=$env:ProgramFiles; process64=[Environment]::Is64BitProcess} | ConvertTo-Json -Compress)+"`n"))
        [IO.File]::AppendAllText((Join-Path $env:USBIPD_BOUNDS_DIRECTORY 'inventory.txt'),('lookup:'+ $Name + "`n"))
        if ($env:USBIPD_BOUNDS_CASE -like 'source-*' -or $env:USBIPD_BOUNDS_CASE -eq 'blocked-discovery') { return $null }
        if ($env:USBIPD_BOUNDS_CASE -like 'shared-*') {
            $path=if ($Name -eq 'usbipd.exe') {$env:USBIPD_BOUNDS_FIRST} else {$env:USBIPD_BOUNDS_SECOND}
            return [pscustomobject]@{Source=$path}
        }
        if ($Name -eq 'usbipd.exe') { return [pscustomobject]@{Source=$env:USBIPD_BOUNDS_EXE} }
    }
    function Get-ItemProperty {
        param($LiteralPath,$Name,$ErrorAction)
        [IO.File]::AppendAllText((Join-Path $env:USBIPD_BOUNDS_DIRECTORY 'inventory.txt'),('registry:'+ $LiteralPath + ':' + $Name + "`n"))
        if ($env:USBIPD_BOUNDS_CASE -in @('first-fast','blocked-discovery')) { Start-Sleep -Milliseconds 16200; return $null }
        $folder=Split-Path $env:USBIPD_BOUNDS_EXE
        $target=$env:USBIPD_BOUNDS_CASE -replace '^source-',''
        if ($Name -eq 'APPLICATIONFOLDER') {
            $view=if ($LiteralPath -ceq 'HKLM:\SOFTWARE\usbipd-win') {'registry64'} else {'registry32'}
            $value=if ($target -eq $view -or ($target -eq 'registry64' -and $view -eq 'registry32')) {Join-Path $folder $view} else {'relative-poison'}
            return [pscustomobject]@{APPLICATIONFOLDER=$value; UninstallString=('"'+(Join-Path $folder 'poison.exe')+'" --version')}
        }
        if ($Name -eq 'Path') {
            $view=if ($LiteralPath -like 'HKLM:*') {'machine'} else {'user'}
            $value=if ($target -eq $view -or ($target -eq 'machine' -and $view -eq 'user')) {Join-Path $folder $view} else {'relative;\\synthetic.invalid\share'}
            return [pscustomobject]@{Path=$value; UninstallString=('"'+(Join-Path $folder 'poison.exe')+'" --version')}
        }
        [IO.File]::WriteAllText((Join-Path $env:USBIPD_BOUNDS_DIRECTORY 'poison.entered'),'bad registry'); throw 'Forbidden registry inventory'
    }
    if ($case -like 'source-*') {
        function Test-Path {
            param($LiteralPath,$PathType)
            $exists=Microsoft.PowerShell.Management\Test-Path -LiteralPath $LiteralPath -PathType $PathType
            [IO.File]::AppendAllText((Join-Path $env:USBIPD_BOUNDS_DIRECTORY 'discovery-filesystem.jsonl'),((@{path=$LiteralPath; exists=$exists} | ConvertTo-Json -Compress)+"`n"))
            return $exists
        }
    }
    if ($case -eq 'blocked-filesystem') {
        function Test-Path {
            param($LiteralPath,$PathType)
            [IO.File]::WriteAllText((Join-Path $env:USBIPD_BOUNDS_DIRECTORY 'filesystem.entered'),'entered')
            Start-Sleep -Milliseconds 16200
            return $true
        }
    }
    $helper=Join-Path $ToolkitRoot 'usbipd.ps1'
    if ($case -in @('pipe-preflight','remove-cap')) {
        $text=Get-BoundsMutant $case $helper
        $helper=Join-Path $OutputRoot 'private-mutant.ps1'
        [IO.File]::WriteAllText($helper,$text,[Text.Encoding]::ASCII)
    }
    . $helper
    if ($case -like 'shared-*') { $script:UsbipdBudgetSeconds=3 }
    $preferred=if ($case -like 'preferred-*') {
        switch ($case) {
            'preferred-missing' {$fixture+'.missing'}
            'preferred-relative' {'usbipd.exe'}
            'preferred-unc' {'\\synthetic.invalid\share\usbipd.exe'}
            'preferred-quote' {$fixture+'"'}
            default {$fixture}
        }
    } else {$null}
    $observer=$null
    if ($env:USBIPD_BOUNDS_OBSERVE -eq 'yes') { Import-BoundsTypes; $observer=New-Object NativeBoundsObserver($OutputRoot,$fixture) }
    try {
        $timer=[Diagnostics.Stopwatch]::StartNew()
        $direct=$case -in @('healthy','broken','hang','pipe','noisy-stderr','blocked-output','pipe-preflight','remove-cap')
        if ($direct) { $accepted=Test-UsbipdExecutable $fixture -TimeoutSeconds 2; $resolved=if ($accepted) {$fixture} else {$null} }
        else { $resolved=Resolve-UsbipdExecutable -PreferredPath $preferred; $accepted=[bool]$resolved }
        $elapsed=$timer.Elapsed.TotalMilliseconds
        if ($observer) { $observer.Finish(); Assert-True (-not $observer.Error) ('Bounds observation query failed: '+$observer.Error) }
        $supervision=$script:UsbipdLastSupervision
        $inventory=if ([IO.File]::Exists((Join-Path $OutputRoot 'inventory.txt'))) {@([IO.File]::ReadAllLines((Join-Path $OutputRoot 'inventory.txt')))} else {@()}
        $entries=if ([IO.File]::Exists((Join-Path $OutputRoot 'probe.entries'))) {@([IO.File]::ReadAllLines((Join-Path $OutputRoot 'probe.entries')))} else {@()}
        Assert-True (@($entries | Where-Object {$_ -cne 'version'}).Count -eq 0) 'Bounds unexpected native operation'
        $expectedPath=if ($case -eq 'shared-remaining') {$env:USBIPD_BOUNDS_SECOND} elseif ($case -like 'source-*') {
            $name=$case.Substring(7)
            if ($name -like 'program*') {Join-Path (Join-Path (Join-Path (Split-Path $fixture) $name) 'usbipd-win') 'usbipd.exe'} else {Join-Path (Join-Path (Split-Path $fixture) $name) 'usbipd.exe'}
        } else {$fixture}
        $limit=if ($direct) {4000} elseif ($case -like 'shared-*') {3300} elseif ($case -eq 'first-fast') {6000} else {15300}
        if ($case -in @('noisy-stderr','blocked-output','remove-cap')) {$limit=3000}
        $after=@([Environment]::GetEnvironmentVariable('Path','Process'),[Environment]::GetEnvironmentVariable('Path','User'),[Environment]::GetEnvironmentVariable('Path','Machine')) | ConvertTo-Json -Compress
        $value=[ordered]@{run_id=$RunId; id=$case; before_outer_cleanup=$true; helper_sha256=(Hash-File $helper); fixture_sha256=(Hash-File $fixture); elapsed_ms=$elapsed; limit_ms=$limit; accepted=$accepted; resolved_exact=($resolved -ceq $expectedPath); entries=$entries.Count; inventory_entries=$inventory.Count; registry_entries=@($inventory | Where-Object {$_ -like 'registry:*'}).Count; filesystem_entered=[IO.File]::Exists((Join-Path $OutputRoot 'filesystem.entered')); readers_released=($supervision.ReadersReleased -eq $true); containment_stopped=($supervision.ContainmentStopped -eq $true); parent_observed=$false; child_observed=$false; parent_exited=$false; child_exited=$false; path_unchanged=($before -ceq $after); poison_entered=[IO.File]::Exists((Join-Path $OutputRoot 'poison.entered')); raw_output_leaked=([string]$supervision.Output -match 'fixture-secret|5\.3\.0|xxxxx'); mutation_detected=$false}
        if ($observer) {
            $value.parent_observed=$observer.ParentObserved; $value.child_observed=$observer.ChildObserved
            $value.parent_exited=$observer.ParentExited; $value.child_exited=$observer.ChildExited
            $value.parent_id=$observer.ParentId; $value.child_id=$observer.ChildId
        }
        if ($case -eq 'pipe-preflight') {
            $pending=Read-Json (Join-Path $OutputRoot 'pipe-pending.json')
            Assert-True ($pending.parent_exited -eq $true) 'Bounds pipe parent did not exit'
            $value.stdout_pending=$pending.stdout_pending; $value.stderr_pending=$pending.stderr_pending
        }
        if ($case -eq 'remove-cap') {
            Assert-Rejection { Assert-True (-not $accepted) 'Bounds output cap readiness assertion' } 'Bounds output cap readiness assertion'
            $value.mutation_detected=$true
        }
        Save-Json (Join-Path $OutputRoot 'bounds.json') $value
        Assert-BoundsEvidence ([pscustomobject]$value) $case $RunId
    } finally { if ($observer) {$observer.Dispose()} }
    exit 0
}
function SelfTest-Bounds {
    $names=@(Get-Specs | ForEach-Object {$_.Id})
    Assert-True ($names.Count -eq 49 -and @($names | Select-Object -Unique).Count -eq 49 -and @($names | Where-Object {$_ -notlike 'bounds-*'}).Count -eq 25) 'Bounds/legacy phase inventory changed'
    # Complete positive synthetic observations; never emit a native receipt.
    $baseline=@{run_id=$RunId; id='pipe'; before_outer_cleanup=$true; helper_sha256=('a'*64); fixture_sha256=('b'*64); elapsed_ms=2100; limit_ms=4000; accepted=$false; resolved_exact=$false; entries=1; inventory_entries=0; registry_entries=0; readers_released=$true; containment_stopped=$true; parent_observed=$true; child_observed=$true; parent_exited=$true; child_exited=$true; path_unchanged=$true; poison_entered=$false; raw_output_leaked=$false}
    Assert-BoundsEvidence ([pscustomobject]$baseline) 'pipe' $RunId
    $controls=@(
        @('run_id','stale','Bounds stale/missing API observation'),
        @('elapsed_ms',4400,'Bounds total deadline exceeded'),
        @('readers_released',$false,'Bounds product cleanup incomplete'),
        @('containment_stopped',$false,'Bounds product cleanup incomplete'),
        @('parent_exited',$false,'Bounds exact parent survived API return'),
        @('child_exited',$false,'Bounds exact descendant survived API return'),
        @('accepted',$true,'Bounds readiness decision incorrect'),
        @('entries',0,'Bounds actual probe entry inventory incorrect'),
        @('path_unchanged',$false,'Bounds PATH/poison boundary violated'),
        @('poison_entered',$true,'Bounds PATH/poison boundary violated'),
        @('raw_output_leaked',$true,'Bounds raw output escaped')
    )
    $tested=New-Object 'Collections.Generic.List[string]'
    foreach ($control in $controls) {
        $value=$baseline.Clone(); $value[$control[0]]=$control[1]
        Assert-Rejection { Assert-BoundsEvidence ([pscustomobject]$value) 'pipe' $RunId } $control[2]
        Write-Output ('PASS bounds-reject-'+$control[0]+': '+$control[2])
        $tested.Add('reject-'+$control[0])
    }
    # Exercise each new case's complete positive predicate without running native
    # product code or inventing native receipts. These are labelled unit values.
    $make={ param($case)
        $v=$baseline.Clone(); $v.id=$case; $v.elapsed_ms=1000
        $v.accepted=$case -in @('healthy','first-fast','shared-remaining','preferred-exact','source-registry64','source-registry32','source-program64','source-program32','source-machine','source-user','remove-cap')
        $v.resolved_exact=$v.accepted
        $v.limit_ms=if ($case -in @('healthy','broken','hang','pipe','pipe-preflight')) {4000} elseif ($case -in @('noisy-stderr','blocked-output','remove-cap')) {3000} elseif ($case -like 'shared-*') {3300} elseif ($case -eq 'first-fast') {6000} else {15300}
        $v.entries=if ($case -like 'preferred-*' -and $case -ne 'preferred-exact' -or $case -like 'blocked-*' -and $case -ne 'blocked-output') {0} elseif ($case -like 'shared-*') {2} else {1}
        $v.filesystem_entered=$case -eq 'blocked-filesystem'
        $v.inventory_entries=if ($case -eq 'first-fast') {1} elseif ($case -eq 'blocked-discovery') {3} else {0}
        $v.registry_entries=if ($case -eq 'blocked-discovery') {1} else {0}
        $v.mutation_detected=$case -eq 'remove-cap'
        if ($case -eq 'pipe-preflight') {$v.child_exited=$false; $v.containment_stopped=$false; $v.stdout_pending=$true; $v.stderr_pending=$true}
        return $v
    }
    foreach ($spec in @(Get-Specs | Where-Object {$_.Id -like 'bounds-*'})) {
        $v=& $make $spec.Id.Substring(7)
        Assert-BoundsEvidence ([pscustomobject]$v) $v.id $RunId
        $tested.Add('positive-'+$v.id)
    }
    foreach ($control in @(
        @('pipe-preflight','stdout_pending',$false,'Bounds pipe inheritance preflight not proven'),
        @('preferred-missing','inventory_entries',1,'Bounds preferred path fell back to inventory'),
        @('first-fast','registry_entries',1,'Bounds healthy first consulted delayed provider'),
        @('blocked-discovery','registry_entries',0,'Bounds blocked discovery was not entered'),
        @('blocked-filesystem','filesystem_entered',$false,'Bounds blocked filesystem was not entered'),
        @('source-registry64','resolved_exact',$false,'Bounds exact path/precedence lost'),
        @('remove-cap','mutation_detected',$false,'Bounds output-cap removal not detected'),
        @('pipe','helper_sha256','missing','Bounds fingerprints missing'),
        @('pipe','before_outer_cleanup',$false,'Bounds stale/missing API observation')
    )) {
        $v=& $make $control[0]; $v[$control[1]]=$control[2]
        if ($control[0] -eq 'pipe-preflight') {$v.stderr_pending=$false}
        Assert-Rejection { Assert-BoundsEvidence ([pscustomobject]$v) $v.id $RunId } $control[3]
        $tested.Add('reject-'+$control[0]+'-'+$control[1])
    }
    # Otherwise-valid new-phase evidence tests the SAME post-upload predicate.
    $unit=Join-Path $OutputRoot 'SYNTHETIC-bounds-phase-NOT-NATIVE'
    $dir=Join-Path $unit 'bounds-healthy'; $compile=Join-Path $unit 'compile'
    $null=New-Item -ItemType Directory -Path $dir,$compile
    [IO.File]::WriteAllText((Join-Path $dir 'stdout.txt'),'')
    [IO.File]::WriteAllText((Join-Path $dir 'stderr.txt'),'')
    [IO.File]::WriteAllText((Join-Path $dir 'probe.entries'),"version`n")
    [IO.File]::WriteAllText((Join-Path $compile 'bounds-fixture.cs'),'SYNTHETIC compiler input, never executed')
    Save-Json (Join-Path $compile 'bounds-compiler.json') @{run_id=$RunId; flags='/nologo /target:exe /langversion:5 /warnaserror+'; exe_sha256=('b'*64); adapted_source_sha256=(Hash-File (Join-Path $compile 'bounds-fixture.cs')); supplied_source_sha256=(Hash-File (Join-Path $HarnessRoot 'tests/fixtures/usbipd_bounds_native.cs')); job_source_sha256=(Hash-File (Join-Path $HarnessRoot 'tests/fixtures/usbipd_native_job.cs'))}
    $v=& $make 'healthy'; $v.helper_sha256=Hash-File (Join-Path $ToolkitRoot 'usbipd.ps1')
    Save-Json (Join-Path $dir 'bounds.json') $v
    $spec=Get-Specs | Where-Object {$_.Id -ceq 'bounds-healthy'}
    $receipt=@{id=$spec.Id; run_id=$RunId; state='complete'; stopped=$true; timed_out=$false; exit_code=0; outer_assigned_before_gate=$true; outer_active_zero=$true}
    Assert-PhaseEvidence $spec ([pscustomobject]$receipt) $dir $RunId
    $tested.Add('positive-final-phase-predicate')
    foreach ($key in @('outer_assigned_before_gate','outer_active_zero')) {
        $r=$receipt.Clone(); $r[$key]=$false
        Assert-Rejection { Assert-PhaseEvidence $spec ([pscustomobject]$r) $dir $RunId } 'Bounds enclosing Job not verified'
        $tested.Add('reject-'+$key)
    }
    Remove-Item -LiteralPath (Join-Path $dir 'bounds.json')
    Assert-Rejection { Assert-PhaseEvidence $spec ([pscustomobject]$receipt) $dir $RunId } 'Missing bounds API observation'
    Save-Json (Join-Path $dir 'bounds.json') $v
    $tested.Add('reject-missing-bounds-observation')
    foreach ($control in @(@('run_id','stale','Bounds stale/missing API observation'),@('helper_sha256',('0'*64),'Bounds executed helper fingerprint mismatch'),@('fixture_sha256',('0'*64),'Bounds executed PE fingerprint mismatch'))) {
        $mutated=$v.Clone(); $mutated[$control[0]]=$control[1]
        Save-Json (Join-Path $dir 'bounds.json') $mutated
        Assert-Rejection { Assert-PhaseEvidence $spec ([pscustomobject]$receipt) $dir $RunId } $control[2]
        $tested.Add('reject-phase-'+$control[0])
    }
    Save-Json (Join-Path $dir 'bounds.json') $v
    [IO.File]::WriteAllText((Join-Path $dir 'probe.entries'),'')
    Assert-Rejection { Assert-PhaseEvidence $spec ([pscustomobject]$receipt) $dir $RunId } 'Bounds probe journal disagrees with result'
    $tested.Add('reject-tampered-entry-inventory')
    [IO.File]::WriteAllText((Join-Path $dir 'probe.entries'),"version`n")
    [IO.File]::WriteAllText((Join-Path $dir 'stdout.txt'),"5.3.0`n")
    Assert-Rejection { Assert-PhaseEvidence $spec ([pscustomobject]$receipt) $dir $RunId } 'Bounds raw native output entered diagnostics'
    [IO.File]::WriteAllText((Join-Path $dir 'stdout.txt'),'')
    $tested.Add('reject-raw-phase-output')
    $compilerPath=Join-Path $compile 'bounds-compiler.json'
    $compilerValue=Read-Json $compilerPath
    $original=$compilerValue.job_source_sha256; $compilerValue.job_source_sha256=('0'*64)
    Save-Json $compilerPath $compilerValue
    Assert-Rejection { Assert-PhaseEvidence $spec ([pscustomobject]$receipt) $dir $RunId } 'Bounds compiler/source binding mismatch'
    $compilerValue.job_source_sha256=$original
    Save-Json $compilerPath $compilerValue
    $tested.Add('reject-tampered-job-source')
    [IO.File]::WriteAllText((Join-Path $compile 'bounds-fixture.cs'),'TAMPERED synthetic compiler input')
    Assert-Rejection { Assert-PhaseEvidence $spec ([pscustomobject]$receipt) $dir $RunId } 'Bounds adapted compiler input mismatch'
    $tested.Add('reject-tampered-compiler-input')
    Save-Json (Join-Path $OutputRoot 'bounds-controls.json') @{kind='local-controls'; native_acceptance=$false; tests=$tested.ToArray()}
    Write-Output ('PASS '+$tested.Count+' bounds unit controls; NOT native Windows execution')
}

function Run-Phase {
    if ($PhaseName -eq 'local-hang') { [IO.File]::WriteAllText((Join-Path $OutputRoot 'entered.txt'), 'owned local hang entered'); Start-Sleep -Seconds 60; exit 89 }
    if ($PhaseName -eq 'local-after') { Write-Output 'owned local guard recovery'; exit 0 }
    if ($PhaseName -like 'bounds-*') {
        # The gate is UTF-8 (Framework writer, BOM included). This windowless
        # child's console input code page is the OEM default (e.g. 437), which
        # would decode the BOM as three text characters; declare the protocol
        # encoding first, as usbipd.ps1's worker does, then drop only U+FEFF.
        [Console]::InputEncoding=New-Object Text.UTF8Encoding($false)
        Assert-True ([Console]::In.ReadLine().TrimStart([char]0xFEFF) -ceq ('outer-job:'+$RunId)) 'Bounds outer Job gate missing'
    }
    Assert-WindowsIdentity (Runtime-Identity)
    Assert-True (@(Get-Specs | Where-Object { $_.Id -ceq $PhaseName }).Count -eq 1) 'Unknown native fixture phase'
    Assert-FixtureOwnership ($PhaseName -ne 'compile')
    $paths=Fixture-Paths
    if ($PhaseName -eq 'compile') {
        $source=Join-Path $OutputRoot 'fixture.cs'
        [IO.File]::WriteAllText($source, (Get-FixtureSource), [Text.Encoding]::ASCII)
        $compiler=Join-Path ([Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory()) 'csc.exe'
        Assert-True (Test-Path -LiteralPath $compiler -PathType Leaf) 'Available .NET Framework compiler required; no downloads'
        $exe=Join-Path $ScratchRoot 'compiled fixture.exe'
        $flags='/nologo /target:exe /langversion:5 /warnaserror+'
        & $compiler /nologo /target:exe /langversion:5 /warnaserror+ ('/out:' + $exe) $source
        $code=$LASTEXITCODE
        Assert-True ($code -eq 0) 'C#5 native fixture compiler failed'
        foreach ($destination in @($paths.Usb,$paths.Wsl,$paths.Fallback)) {
            $null=New-Item -ItemType Directory -Force -Path (Split-Path $destination)
            Copy-Item -LiteralPath $exe -Destination $destination
        }
        foreach ($bad in @('malformed','nonzero','hang')) {
            $dir=Join-Path $ScratchRoot ('Bad ' + $bad)
            $null=New-Item -ItemType Directory -Path $dir
            $target=Join-Path $dir 'usbipd.exe'
            Copy-Item -LiteralPath $exe -Destination $target
            [IO.File]::WriteAllText(($target + '.mode'), $bad)
        }
        [IO.File]::WriteAllText((Join-Path $ScratchRoot 'invalid fixture.exe'), 'synthetic invalid executable')
        $boundsSource=Join-Path $OutputRoot 'bounds-fixture.cs'
        [IO.File]::WriteAllText($boundsSource,(Get-BoundsFixtureSource),[Text.Encoding]::ASCII)
        $boundsExe=Bounds-Path
        $null=New-Item -ItemType Directory -Force -Path (Split-Path $boundsExe)
        & $compiler /nologo /target:exe /langversion:5 /warnaserror+ ('/out:' + $boundsExe) $boundsSource
        Assert-True ($LASTEXITCODE -eq 0) 'C#5 bounds fixture compiler failed'
        foreach ($target in @(Bounds-FixturePaths | Select-Object -Skip 1)) {
            $null=New-Item -ItemType Directory -Force -Path (Split-Path $target)
            Copy-Item -LiteralPath $boundsExe -Destination $target
        }
        $jobDll=Join-Path $ScratchRoot 'bounds-job.dll'
        & $compiler /nologo /target:library /langversion:5 /warnaserror+ ('/out:' + $jobDll) (Join-Path $HarnessRoot 'tests/fixtures/usbipd_native_job.cs')
        Assert-True ($LASTEXITCODE -eq 0) 'C#5 enclosing Job compiler failed'
        Save-Json (Join-Path $OutputRoot 'bounds-compiler.json') @{run_id=$RunId; job_dll_sha256=(Hash-File $jobDll); flags=$flags; exe_sha256=(Hash-File $boundsExe); adapted_source_sha256=(Hash-File $boundsSource); supplied_source_sha256=(Hash-File (Join-Path $HarnessRoot 'tests/fixtures/usbipd_bounds_native.cs')); job_source_sha256=(Hash-File (Join-Path $HarnessRoot 'tests/fixtures/usbipd_native_job.cs'))}
        Save-Json (Join-Path $OutputRoot 'compiler.json') @{run_id=$RunId; compiler=$compiler; compiler_version=[Diagnostics.FileVersionInfo]::GetVersionInfo($compiler).FileVersion; compiler_sha256=(Hash-File $compiler); flags=$flags; exit_code=$code; exe_sha256=(Hash-File $exe); clr=[Environment]::Version.ToString()}
        Save-Json (Join-Path $ScratchRoot 'binaries.json') @{run_id=$RunId; exe_sha256=(Hash-File $exe); bounds_sha256=(Hash-File $boundsExe); bounds_job_sha256=(Hash-File $jobDll)}
        exit 0
    }
    if ($PhaseName -like 'bounds-*') { Run-BoundsCase; exit 0 }
    if ($PhaseName -eq 'bootstrap-usb') {
        # Reuse the existing full bootstrap algorithm and its consent/reboot mocks.
        # All external operations in that harness are synthetic; no installer runs.
        & $HostBinary -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $ToolkitRoot 'scripts/release/test-bootstrap.ps1') -BootstrapPath (Join-Path $ToolkitRoot 'scripts/release/bootstrap.ps1') -Scenario 'usb-*'
        exit $LASTEXITCODE
    }
    $discover=$PhaseName -in @('resolver-stale','after-deadline')
    $bad=$PhaseName -like 'bad-*'
    Install-PhaseGuards -Discover $discover -AllowUsb (-not $bad)
    if ($discover) {
        # Remove the valid fallback from inherited PATH. Installed location still exists.
        $env:PATH=Split-Path $paths.Wsl
        $before=$env:PATH
        $onPath=Microsoft.PowerShell.Core\Get-Command usbipd.exe -CommandType Application -ErrorAction SilentlyContinue
        Assert-True (-not $onPath) 'Stale inherited PATH control is not stale'
        . (Join-Path $ToolkitRoot 'usbipd.ps1')
        $resolved=Resolve-UsbipdExecutable
        Save-Json (Join-Path $OutputRoot 'resolver.json') @{run_id=$RunId; stale_path=(-not $onPath); path_unchanged=($before -ceq $env:PATH); resolved_exact=($resolved -ceq $paths.Usb)}
        Assert-True ($resolved -ceq $paths.Usb -and $before -ceq $env:PATH) 'Installed-location discovery failed or mutated inherited PATH'
        exit 0
    }
    if ($PhaseName -eq 'deadline-native') {
        & (Join-Path $ScratchRoot 'Bad hang/usbipd.exe') --version
        exit $LASTEXITCODE
    }
    $preferred=$paths.Usb
    if ($bad) {
        $kind=$PhaseName -replace '^bad-','' -replace '-(usb|monitor)$',''
        $preferred=switch ($kind) {
            'missing' { Join-Path $ScratchRoot 'missing usbipd.exe' }
            'directory' { Split-Path $paths.Usb }
            'relative' { 'usbipd.exe' }
            'unc' { '\\synthetic.invalid\share\usbipd.exe' }
            'invalid-file' { Join-Path $ScratchRoot 'invalid fixture.exe' }
            default { Join-Path $ScratchRoot ('Bad ' + $kind + '/usbipd.exe') }
        }
    }
    $parameters=@{UsbOnly=$true; Distro='Ubuntu-22.04'; UsbipdPath=$preferred}
    if ($PhaseName -like '*monitor*') { $parameters.MonitorUsb=$true }
    if ($PhaseName -notlike '*legacy') {
        # Load the REAL windowless native helper. The wrapper checks boundaries,
        # never manufactures output arrays; compiled fixtures provide all bytes.
        $helper=Join-Path $OutputRoot 'capture-guard.ps1'
        $helperText=@'
. (Join-Path $env:USBIPD_NATIVE_TOOLKIT 'scripts/release/console.ps1')
$global:ActualFixtureCapture=(Get-Item Function:\Invoke-ToolkitNative).ScriptBlock
function Invoke-ToolkitNative {
    param([string]$File,[string[]]$Arguments)
    $wsl=($File -ceq $global:NativeFixturePaths.Wsl -and ($Arguments -join '|') -ceq '--list|--verbose')
    $usb=($global:NativeFixtureAllowUsb -and $File -ceq $global:NativeFixturePaths.Usb -and ($Arguments -join '|') -cin @('state','attach|--wsl|Ubuntu-22.04|--busid|1-2'))
    Record-Boundary 'native-capture' (-not ($wsl -or $usb)) ([IO.Path]::GetFileName($File) + ':' + ($Arguments -join '|'))
    if (-not ($wsl -or $usb)) { throw 'Forbidden native capture boundary' }
    return (& $global:ActualFixtureCapture -File $File -Arguments $Arguments -TimeoutSeconds 5)
}
function Invoke-ToolkitElevated { Record-Boundary 'elevated' $true ''; throw 'No elevation in fixture' }
'@
        [IO.File]::WriteAllText($helper,$helperText,[Text.Encoding]::ASCII)
        $env:USBIPD_NATIVE_TOOLKIT=$ToolkitRoot
        $parameters.ConsoleHelper=$helper
    }
    & (Join-Path $ToolkitRoot 'run.ps1') @parameters
    exit $LASTEXITCODE
}
function SelfTest-Guards {
    Assert-True (-not (Test-Path -LiteralPath $OutputRoot)) 'SelfTest output must be fresh'
    $null=New-Item -ItemType Directory -Path $OutputRoot
    $script:RunId=[guid]::NewGuid().ToString('N')
    $script:ScratchRoot=$OutputRoot
    $tested=New-Object 'Collections.Generic.List[string]'
    $rejected=$false
    try { Assert-FixtureOwnership $false } catch { $rejected=$_.Exception.Message -eq 'Refusing unowned native fixture directory'; Write-Output ('PASS reject-unowned-native-directory: ' + $_.Exception.Message) }
    Assert-True $rejected 'Fixture ownership guard accepted an unrelated directory'
    $tested.Add('reject-unowned-native-directory')
    $cases=@(
        @{Name='reject-control-as-native'; Error='Not a completed native acceptance manifest'; Value=@{kind='local-controls'; schema=1; state='complete'}}
        @{Name='reject-running'; Error='Not a completed native acceptance manifest'; Value=@{kind='native-usbipd'; schema=1; state='running'}}
        @{Name='reject-non-Windows-runtime'; Error='Native acceptance requires Windows PowerShell 5.1 Desktop powershell.exe'; Value=@{kind='native-usbipd'; schema=1; state='complete'; runtime=@{os='Linux'; ps='7.5.4'; edition='Core'; host_name='pwsh'}}}
        @{Name='reject-missing-phases'; Error='Missing/extra native phases'; Value=@{kind='native-usbipd'; schema=1; state='complete'; runtime=@{os='Windows_NT'; ps='5.1.0'; edition='Desktop'; host_name='powershell.exe'}; run_id=$RunId; phases=@()}}
    )
    foreach ($case in $cases) {
        Assert-Rejection { Assert-Manifest ([pscustomobject]$case.Value) } $case.Error
        Write-Output ('PASS ' + $case.Name + ': ' + $case.Error)
        $tested.Add($case.Name)
    }
    # Shape-only controls use explicitly synthetic inputs, never native evidence.
    $template=@{kind='native-usbipd'; schema=1; state='complete'; runtime=@{os='Windows_NT'; ps='5.1.0'; edition='Desktop'; host_name='powershell.exe'}; run_id=$RunId; phases=@(Get-Specs | ForEach-Object { @{id=$_.Id} }); revision=('a' * 40); harness_sha256=(Hash-File $SelfPath)}
    Assert-Manifest ([pscustomobject]$template)
    foreach ($name in @('reject-duplicate-phase','reject-extra-phase','reject-bad-nonce','reject-harness-substitution')) {
        $mutated=($template | ConvertTo-Json -Depth 10 | ConvertFrom-Json)
        switch ($name) {
            'reject-duplicate-phase' { $mutated.phases[1].id=$mutated.phases[0].id; $expected='Wrong/duplicate phase inventory' }
            'reject-extra-phase' { $mutated.phases+=@{id='unknown'}; $expected='Missing/extra native phases' }
            'reject-bad-nonce' { $mutated.run_id='stale'; $expected='Missing run nonce' }
            'reject-harness-substitution' { $mutated.harness_sha256=('0' * 64); $expected='Revision/harness fingerprint mismatch' }
        }
        Assert-Rejection { Assert-Manifest $mutated } $expected
        Write-Output ('PASS ' + $name + ': ' + $expected)
        $tested.Add($name)
    }
    # Complete SYNTHETIC compile evidence: no missing-file exception can stand in
    # for an intended phase guard. This is never a native acceptance receipt.
    $phaseControl=Join-Path $OutputRoot 'SYNTHETIC-phase-controls-NOT-NATIVE'
    $null=New-Item -ItemType Directory -Path $phaseControl
    [IO.File]::WriteAllText((Join-Path $phaseControl 'stdout.txt'),'')
    [IO.File]::WriteAllText((Join-Path $phaseControl 'stderr.txt'),'')
    Save-Json (Join-Path $phaseControl 'compiler.json') @{run_id=$RunId; exit_code=0; flags='/nologo /target:exe /langversion:5 /warnaserror+'; compiler_sha256=('0'*64); exe_sha256=('0'*64); synthetic_control=$true; native_acceptance=$false}
    $spec=[pscustomobject]@{Id='compile'; Timeout=$false; ExpectedExit=0}
    $receipt=[pscustomobject]@{id='compile'; run_id=$RunId; state='complete'; stopped=$true; timed_out=$false; exit_code=0}
    Assert-PhaseEvidence $spec $receipt $phaseControl $RunId
    $tested.Add('valid-phase-evidence-baseline')
    foreach ($name in @('reject-wrong-exit','reject-unexpected-timeout','reject-stale-phase','reject-running-phase','reject-live-phase')) {
        $receipt=[pscustomobject]@{id='compile'; run_id=$RunId; state='complete'; stopped=$true; timed_out=$false; exit_code=0}
        $expected='Incomplete/stale phase: compile'
        switch ($name) {
            'reject-wrong-exit' { $receipt.exit_code=2; $expected='Wrong native phase exit: compile' }
            'reject-unexpected-timeout' { $receipt.timed_out=$true; $expected='Unexpected timeout result: compile' }
            'reject-stale-phase' { $receipt.run_id='stale' }
            'reject-running-phase' { $receipt.state='running' }
            'reject-live-phase' { $receipt.stopped=$false }
        }
        Assert-Rejection { Assert-PhaseEvidence $spec $receipt $phaseControl $RunId } $expected
        Write-Output ('PASS ' + $name + ': ' + $expected)
        $tested.Add($name)
    }
    # A caught exception cannot hide entry into this forbidden boundary.
    Save-Json (Join-Path $OutputRoot 'boundary-control.json') @{synthetic=$true; acceptance=$false}
    try { Record-Boundary 'state-after-bad-version' $true 'synthetic negative control'; throw 'synthetic swallowed fixture failure' } catch { }
    $entry=@{run_id=$RunId; kind='state-after-bad-version'; forbidden=$true; detail='synthetic negative control'}
    Assert-Rejection { Assert-Boundaries $OutputRoot $RunId } 'Forbidden boundary entered, even if product caught it: state-after-bad-version'
    $tested.Add('reject-caught-forbidden-boundary')
    $entry.forbidden=$false; $entry.run_id='stale'
    [IO.File]::WriteAllText((Join-Path $OutputRoot 'boundaries.jsonl'), ($entry | ConvertTo-Json -Compress) + "`n")
    Assert-Rejection { Assert-Boundaries $OutputRoot $RunId } 'Stale boundary receipt'
    $tested.Add('reject-stale-boundary')
    $nativeControl=Join-Path $OutputRoot 'synthetic-native-rejection'
    $null=New-Item -ItemType Directory -Path $nativeControl
    $paths=Fixture-Paths
    Save-Json (Join-Path $nativeControl 'native-1.json') @{run_id=$RunId; pid=0; utc=[DateTime]::UtcNow.ToString('o'); exe=$paths.Wsl; clr='4.0.30319'; args=@('--list','--verbose'); complete=$true; exit_code=0; synthetic_control=$true}
    Save-Json (Join-Path $nativeControl 'native-2.json') @{run_id=$RunId; pid=0; utc=[DateTime]::UtcNow.ToString('o'); exe=(Join-Path $OutputRoot 'bad synthetic.exe'); clr='4.0.30319'; args=@('state'); complete=$true; exit_code=0; synthetic_control=$true}
    [IO.File]::WriteAllText((Join-Path $nativeControl 'stdout.txt'),'')
    [IO.File]::WriteAllText((Join-Path $nativeControl 'stderr.txt'),'')
    [IO.File]::WriteAllText((Join-Path $nativeControl 'boundaries.jsonl'), (@{run_id=$RunId; kind='lookup'; forbidden=$false; detail='wsl.exe'} | ConvertTo-Json -Compress) + "`n")
    $negative=Get-Specs | Where-Object { $_.Id -eq 'bad-nonzero-usb' }
    $receipt=[pscustomobject]@{id=$negative.Id; run_id=$RunId; state='complete'; stopped=$true; timed_out=$false; exit_code=2}
    $rejected=$false
    try { Assert-PhaseEvidence $negative $receipt $nativeControl $RunId }
    catch { $rejected=$_.Exception.Message -match 'Bad explicit executable entered state/attach'; Write-Output ('PASS reject-native-state-despite-caught-exit2: ' + $_.Exception.Message) }
    Assert-True $rejected 'Native journal failed to catch forbidden state despite expected product failure exit'
    $tested.Add('reject-native-state-despite-caught-exit2')
    foreach ($id in @('local-hang','local-after')) { $null=New-Item -ItemType Directory -Path (Join-Path $OutputRoot $id) }
    $hang=Invoke-OwnedPhase 'local-hang' 5 (Join-Path $OutputRoot 'local-hang')
    $after=Invoke-OwnedPhase 'local-after' 10 (Join-Path $OutputRoot 'local-after')
    Assert-True ($hang.timed_out -and $hang.stopped -and $hang.state -eq 'complete' -and [IO.File]::ReadAllText((Join-Path $OutputRoot 'local-hang/entered.txt')) -ceq 'owned local hang entered' -and -not $after.timed_out -and $after.exit_code -eq 0 -and $after.state -eq 'complete') 'Real local timeout/kill/recovery control failed'
    $tested.Add('real-owned-child-timeout-kill-and-recovery')
    Save-Json (Join-Path $OutputRoot 'controls.json') @{kind='local-controls'; acceptance=$false; runtime=(Runtime-Identity); tests=$tested.ToArray(); hung=$hang; after=$after}
    Write-Output ('PASS ' + $tested.Count + ' local guard controls; NOT native Windows execution')
}
try {
    if ($Mode -eq 'Check') { $hashes=Check-Sources; Write-Output ($hashes | ConvertTo-Json); Write-Output ('PASS source/parser checks on ' + $PSVersionTable.PSVersion + '; NOT Windows PowerShell 5.1 acceptance'); exit 0 }
    if ($Mode -eq 'SelfTest') { $null=Check-Sources; SelfTest-Guards; SelfTest-Bounds; exit 0 }
    if ($Mode -eq 'Validate') { Validate-Run $OutputRoot; exit 0 }
    if ($Mode -eq 'Phase') { Run-Phase; exit 0 }
    Assert-WindowsIdentity (Runtime-Identity)
    $sources=Check-Sources
    Assert-True (Test-Path -LiteralPath (Join-Path $env:SystemRoot 'System32/taskkill.exe') -PathType Leaf) 'Bounded Windows tree termination prerequisite unavailable'
    Assert-True (-not (Test-Path -LiteralPath $OutputRoot)) 'Native receipts output must be fresh; refusing stale results'
    $null=New-Item -ItemType Directory -Path $OutputRoot
    $OutputRoot=[IO.Path]::GetFullPath($OutputRoot)
    $RunId=[guid]::NewGuid().ToString('N')
    $ScratchRoot=Join-Path ([IO.Path]::GetTempPath()) ('usbipd native ' + $RunId)
    $null=New-Item -ItemType Directory -Path $ScratchRoot
    Save-Json (Join-Path $ScratchRoot 'owner.json') @{run_id=$RunId; harness_sha256=(Hash-File $SelfPath)}
    $revision=(& git -C $ToolkitRoot rev-parse HEAD | Out-String).Trim()
    Assert-True ($LASTEXITCODE -eq 0 -and $revision -match '^[a-f0-9]{40}$') 'Cannot identify integrated checkout revision'
    if ($env:GITHUB_SHA) { Assert-True ($revision -ceq $env:GITHUB_SHA) 'CI checkout differs from exact requested revision' }
    $manifest=[ordered]@{schema=1; kind='native-usbipd'; run_id=$RunId; revision=$revision; ci_run_id=$env:GITHUB_RUN_ID; ci_attempt=$env:GITHUB_RUN_ATTEMPT; ci_job=$env:GITHUB_JOB; runtime=(Runtime-Identity); harness_sha256=(Hash-File $SelfPath); source_sha256=$sources; scratch_root=$ScratchRoot; started_utc=[DateTime]::UtcNow.ToString('o'); ended_utc=$null; state='running'; root_files=[ordered]@{}; phases=@()}
    Save-Json (Join-Path $OutputRoot 'run.json') $manifest
    try {
        foreach ($spec in @(Get-Specs)) {
            $dir=Join-Path $OutputRoot $spec.Id
            $null=New-Item -ItemType Directory -Path $dir
            $null=Invoke-OwnedPhase $spec.Id $spec.Seconds $dir
            $state='pass'
            try { Assert-PhaseEvidence $spec (Read-Json (Join-Path $dir 'phase.json')) $dir $RunId }
            catch { $state='fail'; [IO.File]::WriteAllText((Join-Path $dir 'assertion.txt'), $_.Exception.Message) }
            $files=[ordered]@{}
            foreach ($file in [IO.Directory]::GetFiles($dir)) { $files[[IO.Path]::GetFileName($file)]=Hash-File $file }
            $manifest.phases+=@([pscustomobject]@{id=$spec.Id; state=$state; files=$files})
            Save-Json (Join-Path $OutputRoot 'run.json') $manifest
        }
        $manifest.state='complete'; $manifest.ended_utc=[DateTime]::UtcNow.ToString('o')
        $manifest.root_files['progress.txt']=Hash-File (Join-Path $OutputRoot 'progress.txt')
        Save-Json (Join-Path $OutputRoot 'run.json') $manifest
    } finally {
        # Every phase has stopped its owned supervisor tree and checked journals read-only.
        # Keep all diagnostics; compiled fixtures live only in this owned temp tree.
        if (Test-Path -LiteralPath $ScratchRoot) { Remove-Item -LiteralPath $ScratchRoot -Recurse -Force }
    }
    Validate-Run $OutputRoot
    exit 0
} catch { Write-Error $_ -ErrorAction Continue; exit 1 }
