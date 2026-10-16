# Synthetic console/WSL lifecycle; never invokes Win32, WSL, installers or USB.
param([string]$BootstrapPath = (Join-Path $PSScriptRoot 'bootstrap.ps1'), [string]$Case = 'classic')
$ErrorActionPreference = 'Stop'
if ($Case -eq 'setup-questions') {
    . (Join-Path (Split-Path $BootstrapPath) 'console.ps1')
    $source = [IO.File]::ReadAllText((Join-Path (Split-Path $BootstrapPath) 'console.ps1'))
    $questionPath = ($source -split 'function Show-ToolkitQuestion', 2)[1] -split 'function ', 2
    if ($questionPath[0] -match 'MessageBox' -or $questionPath[0] -notmatch 'AskQuestion') { throw 'Question path must use the embedded view, not MessageBox' }
    if ($env:OS -eq 'Windows_NT') { Start-ToolkitSetup }
    else { Add-Type -TypeDefinition $script:ToolkitQuestionModelSource }
    $q = New-Object ToolkitQuestionState
    $q.Begin('Set up Linux?', 0)
    if (-not $q.Pending -or $q.Answer -ne 'no' -or $q.KeysEnabled(0)) { throw 'Question must default to guarded No' }
    if ($q.Respond('yes', 0.1)) { throw 'Accidental early key answered a question' }
    if ($q.Remind(59) -or -not $q.Remind(60) -or $q.Remind(61)) { throw 'Reminder cadence is not 60 seconds' }
    if (-not $q.Respond('closed', 61) -or $q.Pending -or $q.Answer -ne 'closed') { throw 'Close must count as No/closed' }
    if ($env:OS -eq 'Windows_NT') {
        try {
            [ToolkitSetupStatus]::AskQuestion('Set up Linux?')
            [ToolkitSetupStatus]::AnswerQuestion('yes')
            if (-not [ToolkitSetupStatus]::QuestionPending) { throw 'Real view accepted a key before the guard' }
            $flags = [Reflection.BindingFlags]'Static,NonPublic'
            $window = [ToolkitSetupStatus].GetField('window',$flags).GetValue($null)
            $view = [ToolkitSetupStatus].GetField('view',$flags).GetValue($null)
            if (-not $window.TopMost -or -not $window.ControlBox -or $window.AcceptButton -ne $view.No -or $window.CancelButton -ne $view.No -or $view.No.Text -ne 'No' -or $view.Chip.Text -ne 'Your answer is needed' -or $view.Title.Text -notmatch 'Set up Linux.*' -or $view.Footnote.Text -notmatch 'Closing.*No' -or $view.Yes.Enabled) { throw 'Question view state/text/default was not visible' }
            # Advance the real question's own model, not the command/stall clock.
            $model = [ToolkitSetupStatus].GetField('question',$flags).GetValue($null)
            $clock = [ToolkitSetupStatus].GetField('clock',$flags).GetValue($null)
            $model.Begin('Reminder fixture', $clock.Elapsed.TotalSeconds - [ToolkitQuestionState]::ReminderSeconds - 1)
            Start-Sleep -Milliseconds 300
            if (@([ToolkitSetupStatus]::DrainEvents() | Where-Object Kind -eq 'question-reminder').Count -lt 1) { throw 'Real UI timer did not remind' }
            [ToolkitSetupStatus]::AnswerQuestion('closed')
            if ([ToolkitSetupStatus]::QuestionPending -or [ToolkitSetupStatus]::LastAnswer -ne 'closed' -or $view.Title.Text -ne 'You closed the question' -or $view.Description.Text -notmatch 'counts as') { throw 'Closed question state/text was lost' }
            [ToolkitSetupStatus]::UserWait($true, 'Waiting for you: approve Windows. Look for a flashing shield icon on the taskbar.')
            if ($view.Chip.Text -ne 'Waiting for you' -or $view.Steps.AccessibleName -notmatch 'shield icon') { throw 'External prompt instructions missing' }
            [ToolkitSetupStatus]::UserWait($false, '')
        } finally { Stop-ToolkitSetup }
    }
    Write-Output "PASS console: $Case"
    exit 0
}
if ($Case -eq 'setup-feedback') {
    . (Join-Path (Split-Path $BootstrapPath) 'console.ps1')
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('dfu-feedback-' + [guid]::NewGuid().ToString('N'))
    try {
        Initialize-ToolkitLog -Root $tmp -Build ('b' * 40)
        Set-ToolkitStep 'Downloading Ubuntu'
        Update-ToolkitSetup 'Downloading Ubuntu (120 of 345 MB)' 'Receiving bytes' 34
        if ($env:OS -eq 'Windows_NT') {
            Start-ToolkitSetup
            Update-ToolkitSetup 'Downloading Ubuntu (120 of 345 MB)' 'Receiving bytes' 34
            Start-Sleep -Milliseconds 300
            $flags = [Reflection.BindingFlags]'Static,NonPublic'
            $view = [ToolkitSetupStatus].GetField('view', $flags).GetValue($null)
            if ($view.Caption.Text -notmatch '120 MB of 345 MB' -or $view.Title.Text -notmatch '^Downloading the toolkit.s Linux$' -or $view.Progress.Value -ne 34 -or $view.Progress.Style -ne 'Continuous') { throw 'Real progress not painted' }
            Update-ToolkitSetup 'Checking the toolkit' 'Please wait'
            Start-Sleep -Milliseconds 300
            if ($view.Title.Text -ne 'Checking the toolkit' -or $view.Progress.Style -ne 'Marquee') { throw 'Step transition not painted' }
        }
        Complete-ToolkitStep failed 124 'Synthetic idle timeout'
        $text = Get-ToolkitSupportDetails 'Synthetic idle timeout'
        if ($text -notmatch 'Downloading Ubuntu' -or $text -notmatch 'Last log lines') { throw 'Missing support context' }
        Write-Output "PASS console: $Case"
    } finally { Stop-ToolkitSetup; if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp } }
    exit 0
}
if ($Case -eq 'setup-stalls') {
    . (Join-Path (Split-Path $BootstrapPath) 'console.ps1')
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('dfu-stalls-' + [guid]::NewGuid().ToString('N'))
    try {
        Initialize-ToolkitLog -Root $tmp -Build ('c' * 40)
        Set-ToolkitStep 'Checking the toolkit'
        $binary = (Get-Process -Id $PID).Path
        $timer = [Diagnostics.Stopwatch]::StartNew()
        $result = Invoke-ToolkitNative -File $binary -Arguments @('-NoProfile','-Command','Start-Sleep 60') -TimeoutSeconds 0.3
        if (-not $result.TimedOut -or $result.ExitCode -ne 124 -or $timer.Elapsed.TotalSeconds -gt 8) { throw 'Native stall was not killed within its deadline' }
        if (Test-ToolkitStall -ElapsedSeconds 9000 -IdleSeconds 1 -LimitSeconds 120 -ActivityBased) { throw 'Active download expired by total time' }
        if (-not (Test-ToolkitStall -ElapsedSeconds 121 -IdleSeconds 121 -LimitSeconds 120 -ActivityBased)) { throw 'Idle download not detected' }
        Write-ToolkitLog 'diagnostic' @{output='password=synthetic-password; ssid=synthetic-network'; error='Authorization: Bearer synthetic-token'}
        Complete-ToolkitStep failed 124 'Synthetic command stalled'
        $summary = Get-ToolkitSupportDetails 'Synthetic command stalled'
        $raw = [IO.File]::ReadAllText($script:ToolkitLogPath)
        if (($summary + $raw) -match 'synthetic-password|synthetic-network|synthetic-token') { throw 'Secret appeared in logs or copy text' }
        $rows = @(Get-Content $script:ToolkitLogPath | ForEach-Object { $_ | ConvertFrom-Json })
        $end = @($rows | Where-Object event -eq 'command-end')[-1]
        if ($end.exit_code -ne 124 -or $end.result -ne 'stalled' -or -not $end.start_utc -or -not $end.end_utc -or $summary -notmatch 'Checking the toolkit') { throw 'Command receipt/support text incomplete' }
        Set-ToolkitUserWait $true 'Waiting for you: finish the installer; look for its shield icon.'
        try {
            $waiting = Invoke-ToolkitNative -File $binary -Arguments @('-NoProfile','-Command','Start-Sleep -Milliseconds 400') -TimeoutSeconds 0.1
            if ($waiting.TimedOut -or $waiting.ExitCode -ne 0) { throw 'Waiting on a user was reported as a command stall' }
        } finally { Set-ToolkitUserWait $false }
        Write-Output "PASS console: $Case"
    } finally { Remove-Item -Recurse -Force $tmp }
    exit 0
}
if ($Case -eq 'setup-download') {
    . (Join-Path (Split-Path $BootstrapPath) 'console.ps1')
    Add-Type -TypeDefinition @'
using System;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
public class ToolkitHttpFixture : IDisposable {
    readonly TcpListener listener = new TcpListener(IPAddress.Loopback, 0);
    TcpClient client;
    public int Port { get { return ((IPEndPoint)listener.LocalEndpoint).Port; } }
    public ToolkitHttpFixture(string mode) {
        listener.Start();
        var thread = new Thread(delegate() {
            try {
                using (client = listener.AcceptTcpClient()) {
                    var stream = client.GetStream();
                    stream.Read(new byte[4096], 0, 4096);
                    if (mode == "headers") Thread.Sleep(2000);
                    byte[] header = Encoding.ASCII.GetBytes("HTTP/1.1 200 OK\r\nContent-Length: 524288\r\nConnection: close\r\n\r\n");
                    stream.Write(header, 0, header.Length);
                    byte[] block = new byte[65536];
                    for (int i = 0; i < 8; i++) {
                        stream.Write(block, 0, block.Length); stream.Flush();
                        Thread.Sleep(mode == "idle" ? 2000 : 100);
                    }
                }
            } catch { } // Client cancellation is the idle test's expected result.
        });
        thread.IsBackground = true; thread.Start();
    }
    public void Dispose() { listener.Stop(); if (client != null) client.Close(); }
}
'@
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('dfu-download-' + [guid]::NewGuid().ToString('N'))
    Initialize-ToolkitLog -Root $tmp -Build ('d' * 40)
    function Show-ToolkitSupportDialog { param($Message, [switch]$AllowWait); return $false }
    function Update-ToolkitSetup { param($Text,$Detail,$Percent); if ($Percent -gt 0 -and $Percent -lt 100) { $script:intermediate++ } }
    try {
        foreach ($mode in @('active','idle','headers')) {
            $script:intermediate = 0
            $server = New-Object ToolkitHttpFixture $mode
            $file = Join-Path $tmp ($mode + '.wsl')
            $stalled = $false
            try {
                Set-ToolkitStep 'Downloading Ubuntu'
                try { Receive-ToolkitDownload -Uri ("http://127.0.0.1:$($server.Port)/fixture") -OutFile $file -ExpectedBytes 524288 -IdleSeconds 0.4 }
                catch { if ($_.Exception.Message -notmatch 'stalled') { throw }; $stalled = $true }
                if ($stalled -ne ($mode -ne 'active')) { throw "Wrong download result: $mode" }
                if ($mode -eq 'active' -and ($script:intermediate -lt 1 -or (Get-Item $file).Length -ne 524288)) { throw 'Download lacked real byte progress' }
                if ($stalled -and (Test-Path $file)) { throw 'Stalled download was not removed' }
            } finally { $server.Dispose() }
        }
        Write-Output "PASS console: $Case"
    } finally { Remove-Item -Recurse -Force $tmp }
    exit 0
}
if ($Case -eq 'native-status') {
    . (Join-Path (Split-Path $BootstrapPath) 'console.ps1')
    if ($env:OS -eq 'Windows_NT') {
        Start-ToolkitSetup
        try {
            $field = [ToolkitSetupStatus].GetField('window', [Reflection.BindingFlags]'Static,NonPublic')
            $window = $field.GetValue($null)
            if (-not $window.Visible -or $window.ControlBox -or $window.Text -ne 'Jibo DFU Toolkit') { throw 'Native setup window was not visibly mapped' }
        } finally { Stop-ToolkitSetup }
        if (-not $window.IsDisposed) { throw 'Native setup window survived shutdown' }
    } else {
        $source = [IO.File]::ReadAllText((Join-Path (Split-Path $BootstrapPath) 'console.ps1'))
        if ($source -notmatch 'Application.Run\(window\)' -or $source -notmatch 'ApartmentState.STA') { throw 'Setup window needs its own STA message pump' }
    }
    Write-Output "PASS console: $Case"
    exit 0
}
if ($Case -eq 'native-command') {
    . (Join-Path (Split-Path $BootstrapPath) 'console.ps1')
    $hostBinary = (Get-Process -Id $PID).Path
    $code = @'
if ($env:OS -eq 'Windows_NT') {
    Add-Type -TypeDefinition 'using System; using System.Runtime.InteropServices; public class Probe { [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow(); }'
    if ([Probe]::GetConsoleWindow() -ne [IntPtr]::Zero) { exit 91 }
}
[Console]::Out.Write(('o' * 200000))
[Console]::Error.Write(('e' * 200000))
exit 7
'@
    $result = Invoke-ToolkitNative -File $hostBinary -Arguments @('-NoProfile', '-NonInteractive', '-Command', $code)
    if ($result.ExitCode -ne 7 -or $result.Output[0].Length -ne 200000 -or $result.Output[1].Length -ne 200000) { throw "Windowless native capture failed: $($result.ExitCode) $($result.StartError)" }
    Write-Output "PASS console: $Case"
    exit 0
}
if ($Case -eq 'native-distro-select') {
    # Root cause (TOOLKIT-FIX2 bug 1): run.ps1's Get-Wsl2Distributions regex
    # matches its row pattern per ITEM. The real windowless Invoke-ToolkitNative
    # must still hand it one array entry per output line -- not the whole
    # multi-line stdout blob as a single string -- or Select-Distribution finds
    # 0 distros and throws, which bootstrap.ps1 reports as "USB handoff failed
    # or administrator approval was declined." Exercise run.ps1's REAL
    # Get-Wsl2Distributions/Select-Distribution through the REAL
    # Invoke-ToolkitNative against a fake wsl.exe that writes a realistic
    # multi-line `wsl --list --verbose` table UTF-16LE-encoded (what wsl.exe
    # does when its output is redirected). Also prove multi-line usbipd `state`
    # JSON still parses through the same capture.
    $root = Split-Path (Split-Path (Split-Path $BootstrapPath))
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('dfu-native-distro-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        $fake = Join-Path $tmp 'wsl.exe'
        if ($env:OS -eq 'Windows_NT') {
            # A real Windows fake must behave like wsl.exe: UTF-16LE on stdout
            # when redirected. Emit via .NET so encoding is exact, not shell-dependent.
            $source = @'
using System;
using System.Text;
public class FakeWsl {
    public static void Main(string[] args) {
        var stdout = Console.OpenStandardOutput();
        if (args.Length > 0 && Array.IndexOf(args, "--list") >= 0) {
            string table = "  NAME              STATE           VERSION\r\n* Ubuntu-22.04      Running         2\r\n  Jibo-DFU-Toolkit  Stopped         2\r\n";
            byte[] data = Encoding.Unicode.GetBytes(table);
            stdout.Write(data, 0, data.Length);
        } else if (args.Length > 0 && Array.IndexOf(args, "state") >= 0) {
            string json = "{\"Devices\":[{\"BusId\":\"1-2\",\"InstanceId\":\"USB\\\\\\\\VID_0955\\u0026PID_7740\",\"Description\":\"Jibo\",\"ClientIPAddress\":null,\"PersistedGuid\":null}]}\r\n";
            byte[] data = Encoding.UTF8.GetBytes(json);
            stdout.Write(data, 0, data.Length);
        }
        Environment.Exit(0);
    }
}
'@
            $fakeExe = Join-Path $tmp 'wsl.exe'
            Add-Type -TypeDefinition $source -Language CSharp -OutputAssembly $fakeExe -OutputType ConsoleApplication
            $fake = $fakeExe
        } else {
            # Local Linux verification: produce the same UTF-16LE table bytes.
            $tableBytes = [Text.Encoding]::Unicode.GetBytes("  NAME              STATE           VERSION`r`n* Ubuntu-22.04      Running         2`r`n  Jibo-DFU-Toolkit  Stopped         2`r`n")
            $jsonBytes = [Text.Encoding]::UTF8.GetBytes('{"Devices":[{"BusId":"1-2","InstanceId":"USB\\VID_0955&PID_7740","Description":"Jibo","ClientIPAddress":null,"PersistedGuid":null}]}')
            $tableB64 = [Convert]::ToBase64String($tableBytes)
            $jsonB64 = [Convert]::ToBase64String($jsonBytes)
            $shim = @"
#!/bin/sh
case "`$*" in
  *--list*) printf '%s' '$tableB64' | base64 -d ;;
  *state*) printf '%s' '$jsonB64' | base64 -d ;;
  *) echo "fake wsl: `$*" ;;
esac
"@
            [IO.File]::WriteAllText($fake, $shim)
            chmod +x $fake
        }
        $usbPath = Join-Path $root 'run.ps1'
        $tokens = $null; $usbErrors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile($usbPath, [ref]$tokens, [ref]$usbErrors)
        if ($usbErrors.Count) { throw ($usbErrors | Out-String) }
        foreach ($function in $ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $true)) {
            Invoke-Expression $function.Extent.Text
        }
        . (Join-Path (Split-Path $BootstrapPath) 'console.ps1')
        $ConsoleHelper = 'truthy'   # Only selects the Invoke-ToolkitNative capture path in Invoke-CapturedNativeCommand.
        $script:WslPath = $fake
        $script:UsbipdPath = $fake
        $distros = Get-Wsl2Distributions
        if ($distros.Count -ne 2) { throw "Expected 2 parsed WSL distro rows, got $($distros.Count)" }
        $UsbOnly = $true  # Release USB path, not a source build.
        $selected = Select-Distribution -Distributions $distros -Requested 'Jibo-DFU-Toolkit'
        if ($selected -ne 'Jibo-DFU-Toolkit') { throw "Select-Distribution returned '$selected', expected 'Jibo-DFU-Toolkit'" }
        $state = Get-UsbipdState
        if (-not $state -or -not $state.Devices -or $state.Devices.Count -ne 1) { throw 'usbipd state JSON did not parse through the real native capture' }
        if ($state.Devices[0].BusId -ne '1-2') { throw 'usbipd state device BusId was not preserved' }
    } finally {
        Remove-Item -Recurse -Force $tmp
    }
    Write-Output "PASS console: $Case"
    exit 0
}
if ($Case -eq 'all') {
    $hostBinary = (Get-Process -Id $PID).Path
    $passed = 0
    $failed = 0
    foreach ($scenario in @('setup-questions','setup-feedback','setup-stalls','setup-download','native-status','native-command','native-distro-select','classic','terminal','gui-success','gui-failed','never-ready','watcher-failed','monitor-failed','show-console','setup-decline','gui-start-failed','cleanup-failed','monitor-prompt','terminal-failed','hide-fallback','watcher-exit-race','monitor-exit-race','gui-handle','watcher-handle','gui-code-unavailable')) {
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
    $env:LOCALAPPDATA = $fixture
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
        File.AppendAllText(Environment.GetEnvironmentVariable("DFU_CONSOLE_TEST_TRACE"), "{\"Kind\":\"dialog\",\"Values\":[" + flags + "]}\n"); return 7;
    }
}
'@
    function Record($Kind, $Values) { [IO.File]::AppendAllText($global:ConsoleFixtureTrace, (([pscustomobject]@{Kind=$Kind; Values=@($Values)} | ConvertTo-Json -Compress) + "`n")) }
    if ($Case -in @('classic','terminal','hide-fallback')) {
        Add-Type -TypeDefinition @'
public class ToolkitSetupStatus {
    public static System.IntPtr OwnerHandle { get { return new System.IntPtr(42); } }
    public static bool QuestionPending { get { return false; } }
    public static string LastAnswer { get { return "no"; } }
    public static void Start() {} public static void Stop() {}
    public static void AskQuestion(string text) {
        System.IO.File.AppendAllText(System.Environment.GetEnvironmentVariable("DFU_CONSOLE_TEST_TRACE"), "{\"Kind\":\"question-view\",\"Values\":[]}\n");
    }
    public static object[] DrainEvents() { return new object[0]; }
}
'@
        $helper = Join-Path (Split-Path $BootstrapPath) 'console.ps1'
        if (-not (Test-Path $helper)) { throw 'Missing console controller' }
        . $helper
        [ToolkitConsoleNative]::Pseudo = $Case -eq 'terminal'
        [ToolkitConsoleNative]::IgnoreHide = $Case -eq 'hide-fallback'
        if (Show-ToolkitQuestion 'Setup?') { throw 'Dialog default must be No' }
        Show-ToolkitFailure 'Synthetic failure'
        $events = @(Get-Content $trace | ForEach-Object { $_ | ConvertFrom-Json })
        if ($events.Count -ne 2 -or $events[0].Kind -ne 'question-view' -or $events[1].Values[0] -ne 0x00010010) { throw 'Wrong question/error dialog flags or unexpected console operations' }
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
            param($FilePath,$ArgumentList,$Verb,[switch]$Wait,[switch]$PassThru,$WorkingDirectory,$WindowStyle)
            if ($WindowStyle -ne 'Hidden') { throw 'Elevated USB helper is visible' }
            Record 'admin' @($Verb)
            $p = [pscustomobject]@{ExitCode=0; Handle=1}
            $p | Add-Member ScriptMethod WaitForExit { param($ms); return $true }
            return $p
        }
        if (-not (Invoke-UacBind '1-2')) { throw 'USB bind failed' }
        $events = @(Get-Content $trace | ForEach-Object { $_ | ConvertFrom-Json })
        if ($events.Count -ne 1 -or $events[0].Kind -ne 'admin') { throw 'UAC handoff must not restore or create a console' }
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
        param($FilePath,$ArgumentList,$Verb,[switch]$Wait,[switch]$PassThru,[switch]$NoNewWindow,$WindowStyle)
        if ($WindowStyle -ne 'Hidden') { throw 'A background child was not hidden' }
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
    $mockHelper = [IO.File]::ReadAllText((Join-Path (Split-Path $BootstrapPath) 'console.ps1')) + @'

function Start-ToolkitProcess { param($FilePath, $ArgumentList); return (Start-Process -FilePath $FilePath -ArgumentList $ArgumentList -WindowStyle Hidden -PassThru) }
function Invoke-ToolkitNative {
    param($File, [string[]]$Arguments)
    $output = if ($File -eq 'powershell.exe') { @(& powershell.exe @Arguments) } else { @(& wsl.exe @Arguments) }
    return [pscustomobject]@{Output=$output; ExitCode=$LASTEXITCODE; StartError=$null}
}
function Show-ToolkitQuestion { param($Question); Record 'question' @($Question); return $false }
function Start-ToolkitSetup { Record 'setup-start' @() }
function Stop-ToolkitSetup { Record 'setup-stop' @() }
function Show-ToolkitSupportDialog { param($Message, [switch]$AllowWait); [ToolkitConsoleNative]::MessageBox([IntPtr]::Zero, $Message, 'Support details', 0x00010010) | Out-Null; return $false }
'@
    [IO.File]::WriteAllText((Join-Path $fixture 'console.ps1'), $mockHelper)
    Copy-Item $BootstrapPath (Join-Path $fixture 'bootstrap.ps1')
    $BootstrapPath = Join-Path $fixture 'bootstrap.ps1'
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
    if ($hide.Count -or $show.Count -or $prompts.Count) { throw 'Windowless launcher touched a console or waited for console input' }
    $started = @($events | Where-Object Kind -eq 'setup-start')
    $stopped = @($events | Where-Object Kind -eq 'setup-stop')
    if ($started.Count -ne 1 -or -not $stopped.Count) { throw 'Setup status window was not supervised' }
    if ($success -and $Case -ne 'watcher-handle') {
        $ready = @($events | Where-Object Kind -eq 'ready')
        if ($ready.Count -ne 1 -or [array]::IndexOf($events,$stopped[0]) -le [array]::IndexOf($events,$ready[0])) { throw 'Setup window closed before the mapped GUI signal' }
    }
    if (-not $success) {
        $errors = @($events | Where-Object Kind -eq 'error')
        $dialogs = @($events | Where-Object Kind -eq 'dialog')
        if (-not $errors.Count -or -not $dialogs.Count) { throw 'Failure did not show a native error dialog' }
        foreach ($errorEvent in $errors) {
            $index = [array]::IndexOf($events,$errorEvent)
            if ($index -lt 1 -or $events[$index-1].Kind -ne 'setup-stop') { throw 'Error did not replace the setup status window' }
        }
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
