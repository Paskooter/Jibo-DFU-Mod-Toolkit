param([switch]$UsbipdWorker)
# Shared, read-only usbipd CLI discovery for source and frozen Windows launchers.
# Installer source: dorssel/usbipd-win Installer/{Product,Server}.wxs: the normal
# location is ProgramFiles64Folder\usbipd-win; custom installs persist
# HKLM\SOFTWARE\usbipd-win\APPLICATIONFOLDER and append it to machine PATH.
function Test-UsbipdLocalPath([string]$Path) {
    if (-not $Path -or $Path -match '[\x00-\x1f"<>|]' -or $Path.StartsWith('\\')) { return $false }
    if ($env:OS -eq 'Windows_NT') { return ($Path -match '^[A-Za-z]:[\\/]') }
    return $Path.StartsWith('/') # Portable fixture host, never a Windows fallback.
}
function Get-UsbipdCandidates {
    foreach ($name in @('usbipd.exe', 'usbipd')) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue
        foreach ($entry in $command) { if ($entry.Source) { $entry.Source } }
    }
    foreach ($key in @('HKLM:\SOFTWARE\usbipd-win', 'HKLM:\SOFTWARE\WOW6432Node\usbipd-win')) {
        $value = Get-ItemProperty -LiteralPath $key -Name APPLICATIONFOLDER -ErrorAction SilentlyContinue
        if (Test-UsbipdLocalPath $value.APPLICATIONFOLDER) { Join-Path $value.APPLICATIONFOLDER 'usbipd.exe' }
    }
    foreach ($root in @($env:ProgramW6432, $env:ProgramFiles)) {
        if (Test-UsbipdLocalPath $root) { Join-Path (Join-Path $root 'usbipd-win') 'usbipd.exe' }
    }
    # Read current persisted PATH as data. Never change parent, user or machine PATH.
    foreach ($key in @('HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment', 'HKCU:\Environment')) {
        $value = Get-ItemProperty -LiteralPath $key -Name Path -ErrorAction SilentlyContinue
        foreach ($directory in ([string]$value.Path -split ';')) {
            $expanded = [Environment]::ExpandEnvironmentVariables($directory.Trim().Trim('"'))
            if (Test-UsbipdLocalPath $expanded) { Join-Path $expanded 'usbipd.exe' }
        }
    }
}
function Test-UsbipdWorkerProbe([string]$Path, [double]$TimeoutSeconds) {
    if (-not (Test-UsbipdLocalPath $Path) -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $Path
    $info.Arguments = '--version'
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    try {
        $null = $process.Start()
        $null = $process.Handle
        $streams = @($process.StandardOutput.BaseStream, $process.StandardError.BaseStream)
        $buffers = @((New-Object byte[] 512), (New-Object byte[] 512))
        $reads = @($streams[0].ReadAsync($buffers[0],0,512), $streams[1].ReadAsync($buffers[1],0,512))
        $ended = @($false,$false)
        $output = New-Object Text.StringBuilder
        $bytes = 0
        $timer = [Diagnostics.Stopwatch]::StartNew()
        while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
            foreach ($index in @(0,1)) {
                if (-not $ended[$index] -and $reads[$index].IsCompleted) {
                    $count = $reads[$index].GetAwaiter().GetResult()
                    if ($count -eq 0) { $ended[$index]=$true; continue }
                    $bytes += $count
                    if ($bytes -gt 4096) { return $false }
                    if ($index -eq 0) { $null=$output.Append([Text.Encoding]::UTF8.GetString($buffers[$index],0,$count)) }
                    $reads[$index] = $streams[$index].ReadAsync($buffers[$index],0,512)
                }
            }
            if ($process.HasExited -and $ended[0] -and $ended[1]) {
                return ($process.ExitCode -eq 0 -and $output.ToString().Trim() -match '^\d+\.\d+\.\d+(?:[+\-][A-Za-z0-9.+\-]+)?$')
            }
            [Threading.Thread]::Sleep(5)
        }
        return $false
    } catch { return $false }
    # The outer invocation owns the job/session, not just this immediate child.
    # Do not wait/Stop a blocked provider, reader, or runspace here. The owner
    # terminates the entire containment context before publishing any result.
    finally { $process.Dispose() }
}

if ($UsbipdWorker) {
    # No inventory, candidate filesystem access, or executable start is allowed
    # before the owner assigns containment and sends this private input gate.
    $ErrorActionPreference='Stop'
    try {
        [Console]::InputEncoding=New-Object Text.UTF8Encoding($false)
        [Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
        # Framework Process.StandardInput may emit a UTF-8 BOM on AutoFlush,
        # even before our raw write. Console.In does not consume that preamble.
        $request = [Console]::In.ReadLine().TrimStart([char]0xFEFF) | ConvertFrom-Json
        foreach ($variable in $request.Variables.PSObject.Properties) { Set-Variable -Scope Global -Name $variable.Name -Value $variable.Value }
        foreach ($function in $request.Functions.PSObject.Properties) { Set-Item -LiteralPath ('Function:global:'+$function.Name) -Value ([scriptblock]::Create($function.Value)) }
        $script:UsbipdResult=$null
        if ($request.PreferredPath) {
            if (Test-UsbipdWorkerProbe $request.PreferredPath $request.ProbeSeconds) { $script:UsbipdResult=$request.PreferredPath }
        } else {
            $seen=@{}
            try {
                # Streaming is necessary but not the bound: this whole worker is
                # disposable under the parent's single wall-clock deadline.
                Get-UsbipdCandidates | ForEach-Object {
                    if (-not $seen.ContainsKey($_)) {
                        $seen[$_]=$true
                        if (Test-UsbipdWorkerProbe $_ $request.ProbeSeconds) {
                            $script:UsbipdResult=$_
                            throw [OperationCanceledException]::new('usbipd candidate accepted')
                        }
                    }
                }
            } catch [OperationCanceledException] {}
        }
        [Console]::Out.WriteLine((@{Path=$script:UsbipdResult} | ConvertTo-Json -Compress))
    } catch { [Console]::Out.WriteLine('{"Path":null}') }
    # Retain the worker/group leader until its owner has finished cleanup.
    $null=[Console]::In.ReadLine()
    exit
}

$script:UsbipdHelperPath=$PSCommandPath
$script:UsbipdBudgetSeconds=15
if (-not ('UsbipdInvocation' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Runtime.InteropServices;
// Deliberately invocation-local: never accepts PIDs, process names or journals.
public sealed class UsbipdInvocation {
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr CreateJobObject(IntPtr a, string n);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr j, IntPtr p);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool TerminateJobObject(IntPtr j, uint c);
    [StructLayout(LayoutKind.Sequential)] struct Limits {
        public long ProcessTime, JobTime;
        public uint Flags;
        public UIntPtr MinWorking, MaxWorking;
        public uint ActiveLimit;
        public UIntPtr Affinity;
        public uint Priority, Scheduling;
    }
    [StructLayout(LayoutKind.Sequential)] struct Io {
        public ulong ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes;
    }
    [StructLayout(LayoutKind.Sequential)] struct ExtendedLimits {
        public Limits Basic;
        public Io Counters;
        public UIntPtr ProcessMemory, JobMemory, PeakProcess, PeakJob;
    }
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool SetInformationJobObject(IntPtr j, int c, ref ExtendedLimits limits, int size);
    [StructLayout(LayoutKind.Sequential)] struct Accounting {
        public long User, Kernel, PeriodUser, PeriodKernel;
        public uint Faults, Total, Active, Terminated;
    }
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool QueryInformationJobObject(IntPtr j, int c, out Accounting a, int size, IntPtr length);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
    [DllImport("libc", SetLastError=true)] static extern int kill(int p, int s);
    public bool ReadersReleased;
    public bool ContainmentStopped;
    public bool OverOutput;
    public bool TimedOut;
    public string Output;
    sealed class Capture {
        internal readonly MemoryStream Bytes=new MemoryStream();
        internal volatile bool Overflow;
        internal volatile bool Line;
        internal async Task Read(Stream stream) {
            byte[] b=new byte[512];
            for (;;) {
                int n=await stream.ReadAsync(b,0,b.Length).ConfigureAwait(false);
                if (n==0) return;
                lock(Bytes) {
                    if(Bytes.Length+n>8192) { Overflow=true; return; }
                    Bytes.Write(b,0,n);
                    for(int i=0;i<n;i++) if(b[i]==10) Line=true;
                }
            }
        }
        internal string Text { get { lock(Bytes) return Encoding.UTF8.GetString(Bytes.ToArray()); } }
    }
    static string Q(string s) { return "\""+s.Replace("\"","\\\"")+"\""; }
    static async Task Send(Stream stream, string input) {
        byte[] bytes=Encoding.UTF8.GetBytes(input+"\n");
        await stream.WriteAsync(bytes,0,bytes.Length).ConfigureAwait(false);
        await stream.FlushAsync().ConfigureAwait(false);
    }
    public void Run(string host, string helper, string input, int milliseconds) {
        Stopwatch clock=Stopwatch.StartNew();
        // Reserve cleanup inside, never after, the caller's total budget.
        int work=Math.Max(0,milliseconds-300);
        bool windows=Environment.OSVersion.Platform==PlatformID.Win32NT;
        IntPtr job=IntPtr.Zero;
        Process p=new Process();
        Capture stdout=new Capture(), stderr=new Capture();
        Task a=null,b=null,write=null;
        bool started=false, grouped=false;
        try {
            if(windows) {
                job=CreateJobObject(IntPtr.Zero,null);
                if(job==IntPtr.Zero) return;
                ExtendedLimits limits=new ExtendedLimits();
                limits.Basic.Flags=0x2000; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if(!SetInformationJobObject(job,9,ref limits,Marshal.SizeOf(typeof(ExtendedLimits)))) return;
            }
            ProcessStartInfo info=new ProcessStartInfo();
            info.UseShellExecute=false; info.CreateNoWindow=true;
            info.RedirectStandardInput=true; info.RedirectStandardOutput=true; info.RedirectStandardError=true;
            string args="-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "+Q(helper)+" -UsbipdWorker";
            if(windows) { info.FileName=host; info.Arguments=args; }
            else {
                // Portable control only: a session leader gates and then retains
                // ownership even after the actual PowerShell/CLI parent exits.
                info.FileName="/usr/bin/setsid";
                info.Arguments="/bin/sh -c "+Q("printf 'ready\\n' >&2; IFS= read -r gate; [ \"$gate\" = go ] || exit 91; \"$@\"; IFS= read -r hold")+" usbipd "+Q(host)+" "+args;
            }
            p.StartInfo=info; started=p.Start();
            IntPtr handle=p.Handle; // retain identity; no later PID lookup
            a=stdout.Read(p.StandardOutput.BaseStream); b=stderr.Read(p.StandardError.BaseStream);
            if(windows) {
                if(!AssignProcessToJobObject(job,handle)) return;
                grouped=true;
            } else {
                while(!stderr.Line && !p.HasExited && clock.ElapsedMilliseconds<work) Thread.Sleep(2);
                if(!stderr.Text.StartsWith("ready\n")) return;
                grouped=true;
                input="go\n"+input;
            }
            write=Send(p.StandardInput.BaseStream,input);
            while(clock.ElapsedMilliseconds<work && !stdout.Line && !p.HasExited && !stdout.Overflow && !stderr.Overflow) Thread.Sleep(2);
            TimedOut=!stdout.Line;
            OverOutput=stdout.Overflow || stderr.Overflow;
            if(!TimedOut && !OverOutput && write.IsCompleted && !write.IsFaulted) Output=stdout.Text;
        } catch { Output=null; }
        finally {
            // Always terminate containment, including a zero-exit CLI with held
            // pipes. Job handle authority survives the immediate parent's exit.
            if(grouped) {
                if(windows) {
                    bool sent=TerminateJobObject(job,1);
                    Accounting status;
                    while(sent && clock.ElapsedMilliseconds<milliseconds) {
                        if(!QueryInformationJobObject(job,1,out status,Marshal.SizeOf(typeof(Accounting)),IntPtr.Zero)) break;
                        if(status.Active==0) { ContainmentStopped=true; break; }
                        Thread.Sleep(2);
                    }
                }
                else ContainmentStopped=kill(-p.Id,9)==0;
            }
            if(started) {
                try { if(!p.HasExited) p.Kill(); } catch {}
                int left=Math.Max(0,milliseconds-(int)clock.ElapsedMilliseconds);
                try { p.WaitForExit(left); } catch {}
                left=Math.Max(0,milliseconds-(int)clock.ElapsedMilliseconds);
                try { ReadersReleased=a!=null && b!=null && Task.WaitAll(new Task[]{a,b},left); } catch {}
                // EOF must have released both readers BEFORE success is usable.
                if(!ReadersReleased || !ContainmentStopped) Output=null;
            }
            if(job!=IntPtr.Zero) CloseHandle(job);
            p.Dispose();
        }
    }
}
'@
}
function Invoke-UsbipdBounded([string]$PreferredPath, [double]$Seconds, [double]$ProbeSeconds) {
    $timer=[Diagnostics.Stopwatch]::StartNew()
    $functions=@{}; $variables=@{}
    # Preserve only the existing narrow, read-only inventory injection seams.
    # Transfer referenced primitive globals, not arbitrary caller state/closures.
    foreach ($name in @('Get-Command','Get-ItemProperty','Test-Path')) {
        $entry=$ExecutionContext.SessionState.InvokeCommand.GetCommand($name,[Management.Automation.CommandTypes]::Function)
        if ($entry) {
            $functions[$name]=$entry.Definition
            foreach ($match in [regex]::Matches($entry.Definition,'\$global:([A-Za-z_][A-Za-z0-9_]*)')) {
                $key=$match.Groups[1].Value
                $value=$ExecutionContext.SessionState.PSVariable.GetValue($key)
                if ($null -eq $value -or $value -is [string] -or $value -is [bool] -or $value -is [int]) { $variables[$key]=$value }
            }
        }
    }
    $inputText=@{PreferredPath=$PreferredPath; ProbeSeconds=$ProbeSeconds; Functions=$functions; Variables=$variables} | ConvertTo-Json -Compress -Depth 4
    if ($inputText.Length -gt 8192) { return $null }
    $hostName=if ($env:OS -ne 'Windows_NT') {'pwsh'} elseif ($PSVersionTable.PSEdition -eq 'Desktop') {'powershell.exe'} else {'pwsh.exe'}
    $supervisor=New-Object UsbipdInvocation
    $script:UsbipdLastSupervision=$supervisor
    $remaining=[int](1000*($Seconds-$timer.Elapsed.TotalSeconds))
    if ($remaining -le 300) { return $null }
    $supervisor.Run((Join-Path $PSHOME $hostName),$script:UsbipdHelperPath,$inputText,$remaining)
    if (-not $supervisor.Output) { return $null }
    try { return ($supervisor.Output | ConvertFrom-Json -ErrorAction Stop).Path } catch { return $null }
}
function Test-UsbipdExecutable([string]$Path, [double]$TimeoutSeconds=5) {
    if (-not (Test-UsbipdLocalPath $Path)) { return $false }
    # Direct probe API has a fixed, bounded 2s isolation-startup allowance; the
    # argument bounds entered worker execution, NOT worker admission/CLI entry.
    # Slow startup/filesystem work may exhaust the total without entering a CLI.
    # The resolver instead shares ONE 15s envelope including startup and cleanup.
    return [bool](Invoke-UsbipdBounded $Path ($TimeoutSeconds+2) $TimeoutSeconds)
}
function Resolve-UsbipdExecutable([string]$PreferredPath) {
    if ($PreferredPath -and -not (Test-UsbipdLocalPath $PreferredPath)) { return $null }
    return (Invoke-UsbipdBounded $PreferredPath $script:UsbipdBudgetSeconds 5)
}
