# Harmless in-memory support fixture only; no owner clipboard or setup operations.
# Run/Validate are native CI gates. Dot-sourcing exposes local testable primitives.
[CmdletBinding()]
param(
    [ValidateSet('Run','Validate','Check')][string]$Mode = 'Run',
    [string]$SourceRoot = '',
    [string]$OutputRoot = (Join-Path (Get-Location) 'support-native-receipts')
)
$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 may bind defaults before $PSScriptRoot is available.
if (-not $PSBoundParameters.ContainsKey('SourceRoot')) {
    $SourceRoot = Split-Path (Split-Path $PSScriptRoot)
}
$SupportRunner = $PSCommandPath
function Assert-Support($Condition, [string]$Reason) { if (-not $Condition) { throw $Reason } }
function Save-SupportJson([string]$Path, $Value) {
    [IO.File]::WriteAllText(($Path + '.writing'), ($Value | ConvertTo-Json -Depth 16), (New-Object Text.UTF8Encoding($false)))
    if ([IO.File]::Exists($Path)) { [IO.File]::Replace(($Path + '.writing'), $Path, [System.Management.Automation.Language.NullString]::Value) }
    else { [IO.File]::Move(($Path + '.writing'), $Path) }
}
function Read-SupportJson([string]$Path) { [IO.File]::ReadAllText($Path) | ConvertFrom-Json }
function Write-SupportProgress([string]$Root, [string]$Text) {
    [IO.File]::AppendAllText((Join-Path $Root 'progress.txt'), [DateTime]::UtcNow.ToString('o') + ' ' + $Text + [Environment]::NewLine)
}
function Initialize-SupportJobType {
    if ('SupportOwnedJob' -as [type]) { return }
    # Independent test containment; do not call the product's Job implementation.
    # C# 5 / .NET Framework APIs only. Job handle is never inherited by payloads.
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
public sealed class SupportOwnedJob : IDisposable {
    IntPtr job;
    [StructLayout(LayoutKind.Sequential)] struct BasicLimit {
        public long ProcessTime, JobTime; public uint Flags;
        public UIntPtr MinWorkingSet, MaxWorkingSet; public uint ActiveProcessLimit;
        public UIntPtr Affinity; public uint Priority, Scheduling;
    }
    [StructLayout(LayoutKind.Sequential)] struct IoCounters { public ulong ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes; }
    [StructLayout(LayoutKind.Sequential)] struct ExtendedLimit {
        public BasicLimit Basic; public IoCounters Io;
        public UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
    }
    [StructLayout(LayoutKind.Sequential)] struct Accounting {
        public long UserTime, KernelTime, PeriodUserTime, PeriodKernelTime;
        public uint PageFaults, TotalProcesses, ActiveProcesses, TerminatedProcesses;
    }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr CreateJobObject(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job, int info, ref ExtendedLimit value, uint length);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool QueryInformationJobObject(IntPtr job, int info, out Accounting value, uint length, IntPtr returned);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool TerminateJobObject(IntPtr job, uint code);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool CloseHandle(IntPtr handle);
    static void Check(bool ok) { if (!ok) throw new Win32Exception(Marshal.GetLastWin32Error()); }
    public SupportOwnedJob() {
        job=CreateJobObject(IntPtr.Zero, null);
        if (job==IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
        try {
            ExtendedLimit limit=new ExtendedLimit();
            limit.Basic.Flags=0x2000; // KILL_ON_JOB_CLOSE; no breakaway permission.
            Check(SetInformationJobObject(job, 9, ref limit, (uint)Marshal.SizeOf(typeof(ExtendedLimit))));
        } catch { Dispose(); throw; }
    }
    public void Assign(IntPtr process) { Check(AssignProcessToJobObject(job, process)); }
    public uint Count() {
        Accounting value;
        Check(QueryInformationJobObject(job, 1, out value, (uint)Marshal.SizeOf(typeof(Accounting)), IntPtr.Zero));
        return value.ActiveProcesses;
    }
    public void Terminate() { Check(TerminateJobObject(job, 137)); }
    public void Dispose() {
        if (job!=IntPtr.Zero) {
            IntPtr owned=job;
            Check(CloseHandle(owned)); job=IntPtr.Zero;
        }
        GC.SuppressFinalize(this);
    }
    ~SupportOwnedJob() { if (job!=IntPtr.Zero) CloseHandle(job); }
}
'@
}
function New-SupportContainment {
    # Do not trust env:OS (local fault controls deliberately spoof it).
    if ([Environment]::OSVersion.Platform -eq [PlatformID]::Win32NT) {
        Initialize-SupportJobType
        return [pscustomobject]@{kind='windows-job'; job=(New-Object SupportOwnedJob); assigned=$false}
    }
    Assert-Support ([Environment]::OSVersion.Platform -eq [PlatformID]::Unix -and (Test-Path '/proc/self/task')) 'Portable controls require Linux'
    $helper=Join-Path (Split-Path $SupportRunner) 'support_test_containment.py'
    if (-not (Test-Path -LiteralPath $helper)) { $helper=Join-Path $SourceRoot 'scripts/release/support_test_containment.py' }
    Assert-Support (Test-Path -LiteralPath $helper -PathType Leaf) 'Portable test containment helper absent'
    $python=(Get-Command python3 -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    return [pscustomobject]@{kind='linux-test-subreaper'; job=$null; assigned=$false; helper=$helper; python=$python}
}
function Get-SupportWait($Clock, [long]$Deadline) { return [int][Math]::Max(0, ($Deadline-$Clock.ElapsedMilliseconds)) }
function Admit-SupportChild($Containment, $Process, [string]$Root, $Clock, [long]$Deadline) {
    if ($Containment.kind -ceq 'windows-job') { $Containment.job.Assign($Process.Handle) }
    else {
        $ready=Join-Path $Root 'containment-ready.json'
        while (-not [IO.File]::Exists($ready)) {
            Assert-Support (-not $Process.HasExited -and (Get-SupportWait $Clock $Deadline) -gt 0) 'Owned containment admission deadline exceeded'
            Start-Sleep -Milliseconds 10
        }
        $receipt=Read-SupportJson $ready
        Assert-Support ($receipt.kind -ceq 'linux-test-subreaper' -and $receipt.ready -eq $true) 'Owned containment admission failed'
    }
    $Containment.assigned=$true
}
function Get-SupportOwnedCount($Containment, [string]$Root) {
    if ($Containment.kind -ceq 'windows-job') { return [long]$Containment.job.Count() }
    $receipt=Read-SupportJson (Join-Path $Root 'containment.json')
    Assert-Support ($receipt.kind -ceq 'linux-test-subreaper' -and $receipt.tree_empty -is [bool] -and $receipt.tree_empty -and ($receipt.owned_remaining -is [long] -or $receipt.owned_remaining -is [int])) 'Owned portable containment query failed'
    Assert-Support ($receipt.tree_empty -eq $true -and $receipt.owned_remaining -eq 0) 'Owned portable tree not empty'
    return [long]$receipt.owned_remaining
}
function Stop-SupportChild($Process, [string]$Root, $Containment, $Clock, [long]$Deadline) {
    # Containment, not parent lifetime, is the authority on every path.
    if ($Containment.kind -ceq 'windows-job') { $Containment.job.Terminate() }
    elseif (-not $Process.HasExited) { $Process.StandardInput.Close() }
    Assert-Support ($Process.WaitForExit((Get-SupportWait $Clock $Deadline))) 'Owned child survived cleanup'
    do {
        $count=Get-SupportOwnedCount $Containment $Root
        if ($count -eq 0) { return }
        Assert-Support ((Get-SupportWait $Clock $Deadline) -gt 0) 'Owned tree empty deadline exceeded'
        Start-Sleep -Milliseconds 10
    } while ($true)
}
function Close-SupportContainment($Containment, $Process, [bool]$Started, $Clock, [long]$Deadline) {
    # Not the overridable cleanup helper: independent final safety path.
    if ($Containment.kind -ceq 'windows-job') {
        try { $Containment.job.Terminate() } finally { $Containment.job.Dispose() }
    } elseif ($Started -and -not $Process.HasExited) { $Process.StandardInput.Close() }
    if ($Started -and -not $Containment.assigned -and -not $Process.HasExited) { $Process.StandardInput.Close(); $Process.Kill() }
    if ($Started) { Assert-Support ($Process.WaitForExit((Get-SupportWait $Clock $Deadline))) 'Owned containment final cleanup deadline exceeded' }
}
function Get-SupportBootstrap([string]$Binary, [string]$Arguments, [string]$Root, [string]$Nonce, [int]$Seconds, [string]$Candidate=$SourceRoot) {
    $request=[pscustomobject]@{binary=$Binary; arguments=$Arguments; root=$Root; nonce=$Nonce; seconds=$Seconds; working_directory=[IO.Path]::GetFullPath($Candidate)} | ConvertTo-Json -Compress
    $encoded=[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($request))
    # This is a fresh gating host, not the fixture. The inner ProcessStartInfo
    # invokes the original -File/-Mode Native argv, unchanged and unshimmed.
    $template=@'
$ErrorActionPreference='Stop'
$r=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('REQUEST')) | ConvertFrom-Json
if ([Console]::In.ReadLine() -cne $r.nonce) { exit 121 }
$child=New-Object Diagnostics.Process
$child.StartInfo=New-Object Diagnostics.ProcessStartInfo
$child.StartInfo.FileName=$r.binary; $child.StartInfo.Arguments=$r.arguments
$child.StartInfo.WorkingDirectory=$r.working_directory
$child.StartInfo.UseShellExecute=$false; $child.StartInfo.CreateNoWindow=$true
# Use explicit inner pipes, not implicit standard-handle inheritance under
# CREATE_NO_WINDOW. These copies preserve the fixture's raw stdout/stderr.
$child.StartInfo.RedirectStandardOutput=$true; $child.StartInfo.RedirectStandardError=$true
$environment=[ordered]@{}
foreach ($name in @('TEMP','TMP','TMPDIR','LOCALAPPDATA','JIBO_DFU_SETUP_LOG')) { $environment[$name]=[string][Environment]::GetEnvironmentVariable($name) }
$launch=[ordered]@{api='ProcessStartInfo';binary=$r.binary;binary_sha256=(Get-FileHash -LiteralPath $r.binary -Algorithm SHA256).Hash.ToLowerInvariant();arguments=$r.arguments;working_directory=$child.StartInfo.WorkingDirectory;use_shell_execute=$child.StartInfo.UseShellExecute;create_no_window=$child.StartInfo.CreateNoWindow;window_creation_flag='0x08000000';environment=[pscustomobject]$environment}
[IO.File]::WriteAllText((Join-Path $r.root 'fixture-launch.json'), ($launch | ConvertTo-Json -Depth 8), (New-Object Text.UTF8Encoding($false)))
$innerClock=[Diagnostics.Stopwatch]::StartNew()
$null=$child.Start(); $null=$child.Handle
$out=$child.StandardOutput.BaseStream.CopyToAsync([Console]::OpenStandardOutput())
$err=$child.StandardError.BaseStream.CopyToAsync([Console]::OpenStandardError())
$remaining=[int][Math]::Max(0,($r.seconds*1000-$innerClock.ElapsedMilliseconds))
if (-not $child.WaitForExit($remaining)) { exit 124 }
$code=$child.ExitCode
# Subordinate drain cap: the invocation's original outer clock/Job remains
# authoritative. A pipe holder cannot extend the 90s admission/execution budget.
$drain=[int][Math]::Max(0,[Math]::Min(2000,($r.seconds*1000-$innerClock.ElapsedMilliseconds)))
if (-not [Threading.Tasks.Task]::WaitAll([Threading.Tasks.Task[]]@($out,$err),$drain)) { exit 125 }
$child.Dispose(); exit $code
'@
    return $template.Replace('REQUEST',$encoded)
}
function Invoke-SupportChild([string]$Binary, [string]$Arguments, [string]$Root, [string]$Nonce, [ValidateRange(1,90)][int]$Seconds=90) {
    # One absolute clock: admission and execution <=90s; cleanup + stream drain
    # share at most another 10s. No phase/finally path resets either deadline.
    $clock=[Diagnostics.Stopwatch]::StartNew(); $executionDeadline=[long]($Seconds*1000); $totalDeadline=$executionDeadline+10000
    $null=[IO.Directory]::CreateDirectory($Root)
    $temp=Join-Path $Root ('support temp ' + $Nonce); $null=[IO.Directory]::CreateDirectory($temp)
    $phase=[ordered]@{run_id=$Nonce; state='running'; started_utc=[DateTime]::UtcNow.ToString('o'); ended_utc=$null; timed_out=$false; stopped=$false; exit_code=$null; pid=$null; launch=$null; budget_seconds=$Seconds; cleanup_allowance_seconds=10; admitted=$false; tree_empty=$false; owned_remaining=$null; containment_kind=$null; supervision=$null}
    Save-SupportJson (Join-Path $Root 'phase.json') $phase
    Write-SupportProgress $Root 'BEGIN support'
    $child=New-Object Diagnostics.Process
    $child.StartInfo=New-Object Diagnostics.ProcessStartInfo
    $child.StartInfo.WorkingDirectory=[IO.Path]::GetFullPath($SourceRoot)
    $child.StartInfo.UseShellExecute=$false; $child.StartInfo.CreateNoWindow=$true
    $child.StartInfo.RedirectStandardInput=$true
    $child.StartInfo.RedirectStandardOutput=$true; $child.StartInfo.RedirectStandardError=$true
    foreach ($name in @('TEMP','TMP','TMPDIR','LOCALAPPDATA')) { $child.StartInfo.EnvironmentVariables[$name]=$temp }
    $child.StartInfo.EnvironmentVariables['JIBO_DFU_SETUP_LOG']=''
    $started=$false; $containment=$null; $out=$null; $err=$null; $stdoutFile=$null; $stderrFile=$null
    try {
        $environment=[ordered]@{}
        foreach ($name in @('TEMP','TMP','TMPDIR','LOCALAPPDATA','JIBO_DFU_SETUP_LOG')) { $environment[$name]=$child.StartInfo.EnvironmentVariables[$name] }
        $phase.launch=[ordered]@{api='ProcessStartInfo'; binary=$Binary; binary_sha256=(Hash-SupportFile $Binary); arguments=$Arguments; working_directory=$child.StartInfo.WorkingDirectory; use_shell_execute=$false; create_no_window=$true; window_creation_flag='0x08000000'; environment=[pscustomobject]$environment}
        $containment=New-SupportContainment
        $phase.containment_kind=$containment.kind
        if ($containment.kind -ceq 'windows-job') {
            $bootstrap=Get-SupportBootstrap $Binary $Arguments $Root $Nonce $Seconds
            $child.StartInfo.FileName=(Get-Process -Id $PID).Path
            $child.StartInfo.Arguments='-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -EncodedCommand ' + [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($bootstrap))
            $phase.supervision=[ordered]@{api='ProcessStartInfo'; binary=$child.StartInfo.FileName; arguments=$child.StartInfo.Arguments; bootstrap_sha256=(Hash-SupportText $bootstrap); gated_stdin=$true}
        } else {
            Save-SupportJson (Join-Path $Root 'portable-request.json') ([ordered]@{binary=$Binary;arguments=$Arguments;working_directory=$child.StartInfo.WorkingDirectory;nonce=$Nonce;total_seconds=($Seconds+8)})
            $child.StartInfo.FileName=$containment.python
            $child.StartInfo.Arguments='"' + $containment.helper + '" "' + $Root + '"'
            $phase.supervision=[ordered]@{api='portable-test-only'; helper_sha256=(Hash-SupportFile $containment.helper); gated_stdin=$true}
        }
        Save-SupportJson (Join-Path $Root 'phase.json') $phase
        $stdoutFile=New-Object IO.FileStream -ArgumentList @((Join-Path $Root 'stdout.txt'),[IO.FileMode]::Create,[IO.FileAccess]::Write,[IO.FileShare]::ReadWrite,1)
        $stderrFile=New-Object IO.FileStream -ArgumentList @((Join-Path $Root 'stderr.txt'),[IO.FileMode]::Create,[IO.FileAccess]::Write,[IO.FileShare]::ReadWrite,1)
        $null=$child.Start(); $started=$true; $null=$child.Handle
        $phase.pid=$child.Id
        $out=$child.StandardOutput.BaseStream.CopyToAsync($stdoutFile)
        $err=$child.StandardError.BaseStream.CopyToAsync($stderrFile)
        Admit-SupportChild $containment $child $Root $clock $executionDeadline
        Assert-Support ($containment.assigned) 'Owned containment admission not confirmed'
        $phase.admitted=$true
        # Keep the exact nonce free of a .NET Framework StreamWriter BOM.
        $gateBytes=[Text.Encoding]::UTF8.GetBytes("$Nonce`n")
        $child.StandardInput.BaseStream.Write($gateBytes,0,$gateBytes.Length)
        $child.StandardInput.BaseStream.Flush()
        Save-SupportJson (Join-Path $Root 'phase.json') $phase
        $remaining=Get-SupportWait $clock $executionDeadline
        if ($remaining -le 0 -or -not $child.WaitForExit($remaining)) { $phase.timed_out=$true }
        Stop-SupportChild $child $Root $containment $clock $totalDeadline
        $phase.owned_remaining=Get-SupportOwnedCount $containment $Root
        Assert-Support ($phase.owned_remaining -eq 0) 'Owned tree not empty'
        $phase.tree_empty=$true; $phase.stopped=$true; $phase.exit_code=$child.ExitCode
        Assert-Support ([Threading.Tasks.Task]::WaitAll([Threading.Tasks.Task[]]@($out,$err),(Get-SupportWait $clock $totalDeadline))) 'Owned child output deadline exceeded'
        $phase.state='complete'
    } catch {
        $phase.state='failed'; $phase.stopped=$false
        [IO.File]::WriteAllText((Join-Path $Root 'supervisor-error.txt'), $_.Exception.ToString())
    } finally {
        if ($null -ne $containment) {
            try { Close-SupportContainment $containment $child $started $clock $totalDeadline }
            catch { $phase.state='failed'; $phase.stopped=$false; [IO.File]::AppendAllText((Join-Path $Root 'supervisor-error.txt'), $_.Exception.ToString()) }
        }
        # An unassigned Windows gate has no fixture and is not in the Job. Close
        # stdin to release it to refusal, then stop ONLY its original handle.
        if ($started -and -not $child.HasExited) {
            $phase.state='failed'; $phase.stopped=$false
            try {
                $child.StandardInput.Close()
                if ($containment.kind -ceq 'windows-job' -and -not $containment.assigned) { $child.Kill() }
                Assert-Support ($child.WaitForExit((Get-SupportWait $clock $totalDeadline))) 'Owned gate did not exit'
            } catch { [IO.File]::AppendAllText((Join-Path $Root 'supervisor-error.txt'), $_.Exception.ToString()) }
        }
        if ($started -and $null -ne $out -and $null -ne $err) {
            try { Assert-Support ([Threading.Tasks.Task]::WaitAll([Threading.Tasks.Task[]]@($out,$err),(Get-SupportWait $clock $totalDeadline))) 'Owned final stream deadline exceeded' }
            catch { $phase.state='failed'; $phase.stopped=$false; [IO.File]::AppendAllText((Join-Path $Root 'supervisor-error.txt'), $_.Exception.ToString()) }
        }
        if ($null -ne $stdoutFile) { $stdoutFile.Dispose() }
        if ($null -ne $stderrFile) { $stderrFile.Dispose() }
        $child.Dispose()
        $phase.ended_utc=[DateTime]::UtcNow.ToString('o')
        Write-SupportProgress $Root ('END support ' + $phase.state + ' timeout=' + $phase.timed_out)
        Save-SupportJson (Join-Path $Root 'phase.json') $phase
    }
    return [pscustomobject]$phase
}
function Hash-SupportFile([string]$Path) { (Microsoft.PowerShell.Utility\Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
function Assert-SupportNative($Runtime) {
    Assert-Support ($Runtime.os -ceq 'Windows_NT' -and $Runtime.windows -match '^Microsoft Windows NT ' -and $Runtime.powershell -match '^5\.1\.' -and $Runtime.edition -ceq 'Desktop' -and $Runtime.clr -match '^4\.0\.30319\.' -and $Runtime.host_name -ieq 'powershell.exe' -and $Runtime.framework -match '(?i)[/\\]Microsoft\.NET[/\\]Framework(64)?[/\\]v4\.0\.30319[/\\]?$') 'Native Windows PowerShell 5.1 and .NET Framework required'
}
function Get-SupportSources {
    $sources=[ordered]@{}
    foreach ($name in @('scripts/release/console.ps1','tests/test_windows_support_diagnostics.py','tests/fixtures/support_clipboard_faults.cs','tests/fixtures/support_failure_controller.cs','tests/fixtures/support_failure_proxy.cs','tests/fixtures/support_failure_scenarios.ps1','scripts/release/windows_launcher.py','.github/workflows/release.yml')) {
        $path=Join-Path $SourceRoot $name
        Assert-Support (Test-Path -LiteralPath $path -PathType Leaf) ('Required integrated input absent: ' + $name)
        if ($name.EndsWith('.ps1')) {
            $tokens=$null; $errors=$null
            $null=[Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$errors)
            Assert-Support ($errors.Count -eq 0) ('Integrated input does not parse: ' + $name)
        }
        $sources[$name]=Hash-SupportFile $path
    }
    return [pscustomobject]$sources
}
function Get-SupportArguments([string]$Root, [string]$Nonce, [string]$Candidate) {
    $fixture=Join-Path $Candidate 'tests/fixtures/support_failure_scenarios.ps1'
    $console=Join-Path $Candidate 'scripts/release/console.ps1'
    $scratch=Join-Path $Root ('support native ' + $Nonce)
    Assert-Support (($fixture+$console+$scratch) -notmatch '["\r\n]') 'Unsupported path quoting'
    return '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + $fixture + '" -Mode Native -Scratch "' + $scratch + '" -ConsoleSource "' + $console + '"'
}
function Get-SupportCompilation {
    # Read AST literals, never execute/source product code in the supervisor.
    $tokens=$null; $errors=$null
    $ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $SourceRoot 'scripts/release/console.ps1'),[ref]$tokens,[ref]$errors)
    Assert-Support ($errors.Count -eq 0) 'Compilation source does not parse'
    $parts=@{}
    foreach ($assignment in $ast.FindAll({param($node) $node -is [Management.Automation.Language.AssignmentStatementAst] -and $node.Left.Extent.Text -in @('$script:ToolkitSetupUiSource','$script:ToolkitQuestionModelSource')},$true)) {
        Assert-Support ($assignment.Right.Expression -is [Management.Automation.Language.StringConstantExpressionAst]) 'Compilation source must remain literal'
        $parts[$assignment.Left.Extent.Text]=$assignment.Right.Expression.Value
    }
    Assert-Support ($parts.Count -eq 2) 'Compilation source literals missing'
    $join='dialogThread.Join(); // An explicit user decision, never a command timeout.'
    $ui=$parts['$script:ToolkitSetupUiSource']
    Assert-Support ($ui.Contains($join)) 'Native post-Join compilation seam missing'
    $ui=$ui.Replace($join,$join + "`n" + '        if (SupportFailureController.ThrowAfterDialog) throw new InvalidOperationException("synthetic wrapper error");')
    return $ui + $parts['$script:ToolkitQuestionModelSource'] + (Get-Content -Raw -LiteralPath (Join-Path $SourceRoot 'tests/fixtures/support_clipboard_faults.cs')) + (Get-Content -Raw -LiteralPath (Join-Path $SourceRoot 'tests/fixtures/support_failure_controller.cs'))
}
function Hash-SupportText([string]$Text) {
    $sha=[Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text))).Replace('-','').ToLowerInvariant() }
    finally { $sha.Dispose() }
}
function Get-SupportContext {
    $binary=(Get-Process -Id $PID).Path
    $runtime=[pscustomobject]@{os=$env:OS; windows=[Environment]::OSVersion.VersionString; powershell=$PSVersionTable.PSVersion.ToString(); edition=$PSVersionTable.PSEdition; clr=[Environment]::Version.ToString(); framework=[Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory(); host_name=[IO.Path]::GetFileName($binary); host_path=$binary; host_sha256=(Hash-SupportFile $binary)}
    Assert-SupportNative $runtime
    Assert-Support ($env:GITHUB_RUN_ID -match '^\d+$' -and $env:GITHUB_RUN_ATTEMPT -match '^[1-9]\d*$' -and $env:GITHUB_JOB -match '^[A-Za-z_][A-Za-z0-9_-]*$') 'CI run/attempt/job identity required'
    $revision=(& git -C $SourceRoot rev-parse HEAD | Out-String).Trim()
    Assert-Support ($LASTEXITCODE -eq 0 -and $revision -cmatch '^[a-f0-9]{40}$' -and $revision -ceq $env:GITHUB_SHA) 'Exact CI checkout revision required'
    [pscustomobject]@{revision=$revision; ci_run_id=$env:GITHUB_RUN_ID; ci_attempt=$env:GITHUB_RUN_ATTEMPT; ci_job=$env:GITHUB_JOB; runtime=$runtime; source_root=[IO.Path]::GetFullPath($SourceRoot); runner_sha256=(Hash-SupportFile $SupportRunner); sources=(Get-SupportSources); compilation_sha256=(Hash-SupportText (Get-SupportCompilation))}
}
function Get-SupportInventory([string]$Root) {
    $Root=[IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar)
    $files=[ordered]@{}; $directories=@()
    foreach ($item in @((Get-Item -LiteralPath $Root -Force)) + @(Get-ChildItem -LiteralPath $Root -Recurse -Force | Sort-Object FullName)) {
        Assert-Support (-not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) 'Linked receipt paths are forbidden'
        if ($item.FullName -ceq $Root) { continue }
        $relative=$item.FullName.Substring($Root.Length+1).Replace('\','/')
        if ($item.PSIsContainer) { $directories+=@($relative) }
        elseif ($relative -cne 'run.json') { $files[$relative]=Hash-SupportFile $item.FullName }
    }
    [pscustomobject]@{files=[pscustomobject]$files; directories=@($directories | Sort-Object)}
}
function Assert-SupportFields($Value, [string[]]$Names, [string]$Reason) {
    Assert-Support ((@($Value.PSObject.Properties.Name | Sort-Object) -join '|') -ceq (@($Names | Sort-Object) -join '|')) $Reason
}
function Assert-SupportPhase($Phase, [string]$Nonce, $Started, $Ended) {
    Assert-Support ($Phase.run_id -ceq $Nonce) 'Stale phase receipt'
    Assert-Support ($Phase.state -ceq 'complete') 'Phase failed or incomplete'
    Assert-Support ($Phase.timed_out -is [bool] -and -not $Phase.timed_out) 'Child timed out'
    Assert-Support ($Phase.stopped -is [bool] -and $Phase.stopped) 'Child not stopped'
    Assert-Support (($Phase.exit_code -is [int] -or $Phase.exit_code -is [long]) -and $Phase.exit_code -eq 0) 'Child exit was not zero'
    Assert-Support ($Phase.admitted -is [bool] -and $Phase.admitted) 'Owned containment not admitted'
    Assert-Support ($Phase.tree_empty -is [bool] -and $Phase.tree_empty -and ($Phase.owned_remaining -is [int] -or $Phase.owned_remaining -is [long]) -and $Phase.owned_remaining -eq 0) 'Owned tree emptiness not proved'
    $start=[DateTimeOffset]::Parse($Phase.started_utc); $end=[DateTimeOffset]::Parse($Phase.ended_utc)
    Assert-Support ($start -ge [DateTimeOffset]::Parse($Started) -and $end -le [DateTimeOffset]::Parse($Ended) -and $start -le $end) 'Out-of-run phase timestamps'
}
function Validate-SupportEvidence([string]$Root, $Expected) {
    Assert-Support (Test-Path -LiteralPath (Join-Path $Root 'run.json') -PathType Leaf) 'Missing run receipt'
    $actual=Get-SupportInventory $Root
    $run=Read-SupportJson (Join-Path $Root 'run.json')
    Assert-Support ($run.schema -eq 1 -and $run.kind -ceq 'native-support') 'Not a native support receipt'
    Assert-Support ($run.state -ceq 'complete') 'Run incomplete'
    Assert-Support ($run.run_id -cmatch '^[a-f0-9]{32}$') 'Invalid run nonce'
    foreach ($context in @($run.context,$run.context_after)) {
        Assert-Support ($null -ne $context) 'Before/after context missing'
        Assert-Support ($context.ci_run_id -ceq $Expected.ci_run_id -and $context.ci_attempt -ceq $Expected.ci_attempt -and $context.ci_job -ceq $Expected.ci_job) 'CI run/attempt/job mismatch'
        Assert-Support ($context.revision -ceq $Expected.revision) 'Revision mismatch'
        Assert-Support ($context.runner_sha256 -ceq $Expected.runner_sha256) 'Runner fingerprint mismatch'
        Assert-Support ($context.source_root -ceq $Expected.source_root) 'Source root mismatch'
        Assert-Support ($context.compilation_sha256 -ceq $Expected.compilation_sha256) 'Compilation fingerprint mismatch'
        Assert-SupportFields $context.runtime @($Expected.runtime.PSObject.Properties.Name) 'Runtime identity shape mismatch'
        foreach ($field in $Expected.runtime.PSObject.Properties) { Assert-Support ($context.runtime.($field.Name) -ceq $field.Value) 'Runtime identity mismatch' }
        Assert-SupportNative $context.runtime
        Assert-SupportFields $context.sources @($Expected.sources.PSObject.Properties.Name) 'Source inventory mismatch'
        foreach ($source in $Expected.sources.PSObject.Properties) { Assert-Support ($context.sources.($source.Name) -ceq $source.Value) ('Source fingerprint mismatch: ' + $source.Name) }
    }
    $context=$run.context
    Assert-Support (Test-Path -LiteralPath (Join-Path $Root 'phase.json') -PathType Leaf) 'Missing phase receipt'
    $phase=Read-SupportJson (Join-Path $Root 'phase.json')
    Assert-SupportPhase $phase $run.run_id $run.started_utc $run.ended_utc
    Assert-Support ($phase.budget_seconds -eq 90) 'Wrong native child budget'
    $launch=$phase.launch
    Assert-Support ($launch.api -ceq 'ProcessStartInfo' -and $launch.use_shell_execute -is [bool] -and -not $launch.use_shell_execute -and $launch.create_no_window -is [bool] -and $launch.create_no_window -and $launch.window_creation_flag -ceq '0x08000000') 'Windowless process boundary mismatch'
    Assert-Support ($launch.binary -ceq $context.runtime.host_path -and $launch.binary_sha256 -ceq $context.runtime.host_sha256 -and $launch.working_directory -ceq $Expected.source_root -and $launch.arguments -ceq (Get-SupportArguments $Root $run.run_id $Expected.source_root)) 'Native child launch mismatch'
    Assert-SupportFields $launch.environment @('TEMP','TMP','TMPDIR','LOCALAPPDATA','JIBO_DFU_SETUP_LOG') 'Child environment shape mismatch'
    foreach ($name in @('TEMP','TMP','TMPDIR','LOCALAPPDATA')) { Assert-Support ($launch.environment.$name -ceq (Join-Path $Root ('support temp ' + $run.run_id))) 'Child temp ownership mismatch' }
    Assert-Support ($launch.environment.JIBO_DFU_SETUP_LOG -ceq '') 'Inherited setup log forbidden'
    Assert-Support ($phase.containment_kind -ceq 'windows-job' -and $phase.cleanup_allowance_seconds -eq 10 -and ([DateTimeOffset]::Parse($phase.ended_utc)-[DateTimeOffset]::Parse($phase.started_utc)).TotalSeconds -le 100) 'Native owned containment mismatch'
    $bootstrap=Get-SupportBootstrap $context.runtime.host_path (Get-SupportArguments $Root $run.run_id $Expected.source_root) $Root $run.run_id 90 $Expected.source_root
    $bootstrapArguments='-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -EncodedCommand ' + [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($bootstrap))
    $supervision=$phase.supervision
    Assert-SupportFields $supervision @('api','binary','arguments','bootstrap_sha256','gated_stdin') 'Native supervision shape mismatch'
    Assert-Support ($supervision.api -ceq 'ProcessStartInfo' -and $supervision.binary -ceq $context.runtime.host_path -and $supervision.arguments -ceq $bootstrapArguments -and $supervision.bootstrap_sha256 -ceq (Hash-SupportText $bootstrap) -and $supervision.gated_stdin -is [bool] -and $supervision.gated_stdin) 'Native gated bootstrap mismatch'
    Assert-Support (Test-Path -LiteralPath (Join-Path $Root 'fixture-launch.json') -PathType Leaf) 'Missing actual fixture launch'
    $fixtureLaunch=Read-SupportJson (Join-Path $Root 'fixture-launch.json')
    Assert-SupportFields $fixtureLaunch @($launch.PSObject.Properties.Name) 'Actual fixture launch shape mismatch'
    foreach ($name in @('api','binary','binary_sha256','arguments','working_directory','window_creation_flag')) { Assert-Support ($fixtureLaunch.$name -ceq $launch.$name) 'Actual fixture launch mismatch' }
    Assert-Support ($fixtureLaunch.use_shell_execute -is [bool] -and -not $fixtureLaunch.use_shell_execute -and $fixtureLaunch.create_no_window -is [bool] -and $fixtureLaunch.create_no_window) 'Actual fixture launch mismatch'
    Assert-SupportFields $fixtureLaunch.environment @($launch.environment.PSObject.Properties.Name) 'Actual fixture environment shape mismatch'
    foreach ($field in $launch.environment.PSObject.Properties) { Assert-Support ($fixtureLaunch.environment.($field.Name) -ceq $field.Value) 'Actual fixture environment mismatch' }

    Assert-Support (Test-Path -LiteralPath (Join-Path $Root 'fixture-compilation.cs') -PathType Leaf) 'Missing generated compilation source'
    Assert-Support ((Hash-SupportFile (Join-Path $Root 'fixture-compilation.cs')) -ceq $Expected.compilation_sha256) 'Generated compilation source mismatch'
    $scratch=Join-Path $Root ('support native ' + $run.run_id)
    $path=Join-Path $scratch 'receipt.json'
    Assert-Support (Test-Path -LiteralPath $path -PathType Leaf) 'Missing fixture receipt'
    $receipt=Read-SupportJson $path
    Assert-SupportFields $receipt @('mode','windows','powershell','clr','clipboard','cases','events','forbidden_calls','starts','stops','source_sha256','result') 'Fixture receipt shape mismatch'
    Assert-Support ($receipt.mode -ceq 'Native' -and $receipt.clipboard -ceq 'synthetic-memory-only') 'Wrong fixture mode or clipboard boundary'
    Assert-Support ($receipt.result -ceq 'ok') 'Fixture result not ok'
    foreach ($pair in @(@('cases',5),@('events',4),@('forbidden_calls',0),@('starts',6),@('stops',5))) {
        $value=$receipt.($pair[0])
        Assert-Support (($value -is [int] -or $value -is [long]) -and $value -eq $pair[1]) ('Fixture count mismatch: ' + $pair[0])
    }
    Assert-Support ($receipt.windows -ceq $context.runtime.windows -and $receipt.powershell -ceq $context.runtime.powershell -and $receipt.clr -ceq $context.runtime.clr) 'Fixture runtime mismatch'
    Assert-Support ($receipt.source_sha256 -match '^[a-fA-F0-9]{64}$' -and $receipt.source_sha256 -ieq $Expected.sources.'scripts/release/console.ps1') 'Fixture source fingerprint mismatch'
    $written=(Get-Item -LiteralPath $path).LastWriteTimeUtc
    Assert-Support ($written -ge [DateTimeOffset]::Parse($phase.started_utc).UtcDateTime -and $written -le [DateTimeOffset]::Parse($phase.ended_utc).UtcDateTime) 'Stale fixture receipt timestamp'
    foreach ($name in @('stdout.txt','stderr.txt','progress.txt')) { Assert-Support (Test-Path -LiteralPath (Join-Path $Root $name) -PathType Leaf) ('Missing diagnostic: ' + $name) }
    Assert-Support ([IO.File]::ReadAllText((Join-Path $Root 'stdout.txt')) -match '(?m)^PASS WRAPPER-PERSISTENCE cases=5 events=4 forbidden=0\r?$') 'Fixture success marker missing'
    $progress=[IO.File]::ReadAllText((Join-Path $Root 'progress.txt'))
    Assert-Support ($progress.Contains('BEGIN support') -and $progress.Contains('END support complete timeout=False')) 'Incomplete progress receipt'
    $logs=@(Get-ChildItem -LiteralPath $scratch -Recurse -Filter '*.log' -File)
    Assert-Support ($logs.Count -eq 1) 'Fixture log inventory mismatch'
    $logText=[IO.File]::ReadAllText($logs[0].FullName)
    Assert-Support ($logText -notmatch 'fixture[-_]private') 'Private fixture marker in JSONL'
    $rows=@([IO.File]::ReadAllLines($logs[0].FullName) | ForEach-Object { $_ | ConvertFrom-Json })
    foreach ($row in $rows) {
        switch -CaseSensitive ($row.event) {
            'setup-start' {
                Assert-SupportFields $row @('utc','event','build','windows','wsl') 'Setup-start JSONL shape mismatch'
                Assert-Support ($row.build -ceq ('f'*40) -and $row.windows -ceq 'Windows 11 Pro 25H2 (build 26200.9445)' -and $row.wsl -ceq 'not checked') 'Setup-start JSONL values mismatch'
            }
            'setup-end' {
                Assert-SupportFields $row @('utc','event','result','error') 'Setup-end JSONL shape mismatch'
                Assert-Support ($row.result -ceq 'failed' -and $row.error -ceq 'Synthetic failure') 'Setup-end JSONL values mismatch'
            }
            'clipboard-copy-failed' { }
            default { throw 'Unallowlisted JSONL event' }
        }
        Assert-Support ([DateTimeOffset]::Parse($row.utc) -ge [DateTimeOffset]::Parse($phase.started_utc) -and [DateTimeOffset]::Parse($row.utc) -le [DateTimeOffset]::Parse($phase.ended_utc)) 'Out-of-run JSONL timestamp'
    }
    $events=@($rows | Where-Object event -eq 'clipboard-copy-failed')
    Assert-Support (($events.category -join '|') -ceq 'external|thread-state|invalid-operation|external') 'Persisted clipboard event inventory mismatch'
    Assert-Support (($rows.event -join '|') -ceq 'setup-start|setup-end|clipboard-copy-failed|clipboard-copy-failed|clipboard-copy-failed|clipboard-copy-failed') 'JSONL event inventory mismatch'
    foreach ($event in $events) {
        Assert-SupportFields $event @('utc','event','at_utc','exception_type','category','hresult','apartment','managed_thread_id','summary_length') 'Persisted clipboard event shape mismatch'
        $type=switch ($event.category) { 'external' {'System.Runtime.InteropServices.ExternalException'} 'thread-state' {'System.Threading.ThreadStateException'} 'invalid-operation' {'System.InvalidOperationException'} }
        $hresult=switch ($event.category) { 'external' {'0x800401D0'} 'thread-state' {'0x80131520'} 'invalid-operation' {'0x80131509'} }
        Assert-Support ($event.exception_type -ceq $type -and $event.apartment -ceq 'STA' -and $event.hresult -ceq $hresult -and ($event.managed_thread_id -is [int] -or $event.managed_thread_id -is [long]) -and $event.managed_thread_id -gt 0 -and ($event.summary_length -is [int] -or $event.summary_length -is [long]) -and $event.summary_length -gt 0) 'Persisted clipboard provenance mismatch'
        Assert-Support ([DateTimeOffset]::Parse($event.at_utc) -ge [DateTimeOffset]::Parse($phase.started_utc) -and [DateTimeOffset]::Parse($event.at_utc) -le [DateTimeOffset]::Parse($event.utc)) 'Clipboard event timestamp mismatch'
    }
    Assert-SupportFields $run.inventory.files @($actual.files.PSObject.Properties.Name) 'Diagnostic file inventory mismatch'
    Assert-Support ((@($run.inventory.directories) -join '|') -ceq (@($actual.directories) -join '|')) 'Diagnostic directory inventory mismatch'
    foreach ($file in $actual.files.PSObject.Properties) { Assert-Support ($run.inventory.files.($file.Name) -ceq $file.Value) ('Diagnostic hash mismatch: ' + $file.Name) }
}
if ($MyInvocation.InvocationName -eq '.') { return }
try {
    if ($Mode -eq 'Check') { Get-SupportSources | ConvertTo-Json; exit 0 }
    $context=Get-SupportContext
    if ($Mode -eq 'Validate') { Validate-SupportEvidence $OutputRoot $context; Write-Output 'PASS strict native support receipt gate'; exit 0 }
    Assert-Support (-not (Test-Path -LiteralPath $OutputRoot)) 'Output must be fresh; refusing stale evidence'
    $OutputRoot=[IO.Path]::GetFullPath($OutputRoot)
    $null=[IO.Directory]::CreateDirectory($OutputRoot)
    $run=[ordered]@{schema=1; kind='native-support'; run_id=[guid]::NewGuid().ToString('N'); context=$context; context_after=$null; state='running'; started_utc=[DateTime]::UtcNow.ToString('o'); ended_utc=$null; inventory=$null}
    Save-SupportJson (Join-Path $OutputRoot 'run.json') $run
    try {
        [IO.File]::WriteAllText((Join-Path $OutputRoot 'fixture-compilation.cs'), (Get-SupportCompilation), (New-Object Text.UTF8Encoding($false)))
        $arguments=Get-SupportArguments $OutputRoot $run.run_id $context.source_root
        $null=Invoke-SupportChild (Get-Process -Id $PID).Path $arguments $OutputRoot $run.run_id 90
        $run.state='complete'
    } catch { $run.state='failed'; [IO.File]::WriteAllText((Join-Path $OutputRoot 'run-error.txt'), $_.Exception.ToString()) }
    finally {
        try { $run.context_after=Get-SupportContext }
        catch { $run.state='failed'; [IO.File]::WriteAllText((Join-Path $OutputRoot 'source-error.txt'), $_.Exception.ToString()) }
        $run.ended_utc=[DateTime]::UtcNow.ToString('o')
        $run.inventory=Get-SupportInventory $OutputRoot
        Save-SupportJson (Join-Path $OutputRoot 'run.json') $run
    }
    # Collect/retain even failed evidence; the separate post-upload Validate gates CI.
    Validate-SupportEvidence $OutputRoot $context
    Write-Output 'PASS bounded native support fixture'
    exit 0
} catch { Write-Error $_ -ErrorAction Continue; exit 1 }
