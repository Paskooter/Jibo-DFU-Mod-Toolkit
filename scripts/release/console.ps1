# Windowless release support. Source launchers retain their interactive console.
# Only setup diagnostics are collected: never command arguments or robot settings.
function Protect-ToolkitText([string]$Text) {
    $clean = $Text -replace "`0", ''
    $clean = [regex]::Replace($clean, '(?im)^.*(?:password|passwd|token|authorization|ssid|wi-?fi|psk|secret)\s*[:=].*$', '[private data removed]')
    $clean = $clean -replace '(?i)https?://[^\s"<>]+', '[URL removed]'
    foreach ($path in @($env:USERPROFILE, $env:LOCALAPPDATA, $env:TEMP, $env:HOME)) {
        if ($path -and $path.Length -gt 3) { $clean = $clean -replace [regex]::Escape($path), '[user folder]' }
    }
    $clean = $clean -replace '(?i)[A-Z]:\\Users\\[^\\\s]+', '[user folder]'
    $clean = $clean -replace '/(?:home|Users)/[^/\s]+', '[user folder]'
    return $clean
}
function Initialize-ToolkitLog([string]$Root, [string]$Build = 'unknown') {
    if (-not $Root) { $Root = $env:LOCALAPPDATA }
    if (-not $Root) { $Root = [IO.Path]::GetTempPath() }
    $script:ToolkitBuild = $Build
    $script:ToolkitWindowsVersion = [Environment]::OSVersion.VersionString
    $script:ToolkitWslVersion = 'not checked'
    $script:ToolkitStep = $null
    $script:ToolkitFailedStep = $null
    $script:ToolkitLogTail = New-Object 'Collections.Generic.Queue[string]'
    $name = 'setup-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmss.fffZ') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8) + '.log'
    $script:ToolkitLogPath = $null
    foreach ($base in @($Root, [IO.Path]::GetTempPath())) {
        try {
            $folder = Join-Path (Join-Path $base 'Jibo-DFU-Toolkit') 'logs'
            New-Item -ItemType Directory -Force -Path $folder | Out-Null
            $candidate = Join-Path $folder $name
            [IO.File]::WriteAllText($candidate, '', (New-Object Text.UTF8Encoding($false)))
            $script:ToolkitLogPath = $candidate
            break
        } catch { continue }
    }
    if (-not $script:ToolkitLogPath) { throw 'Cannot create a setup log in local app data or the temporary folder. Check free space and folder permissions; setup has not started.' }
    Write-ToolkitLog 'setup-start' @{ build=$Build; windows=$script:ToolkitWindowsVersion; wsl=$script:ToolkitWslVersion }
}
function Write-ToolkitLog([string]$Event, [hashtable]$Fields = @{}) {
    if (-not $script:ToolkitLogPath) { return }
    $record = [ordered]@{ utc=[DateTime]::UtcNow.ToString('o'); event=$Event }
    foreach ($key in $Fields.Keys) {
        $value = $Fields[$key]
        if ($value -is [string]) { $value = Protect-ToolkitText $value }
        $record[$key] = $value
    }
    $line = $record | ConvertTo-Json -Compress -Depth 4
    # Synchronous append per event: receipts survive a later crash or power loss.
    [IO.File]::AppendAllText($script:ToolkitLogPath, $line + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
    $script:ToolkitLogTail.Enqueue($line)
    while ($script:ToolkitLogTail.Count -gt 10) { $null = $script:ToolkitLogTail.Dequeue() }
}
function Get-ToolkitStepPolicy([string]$Name) {
    $seconds = 120; $hint = 'Close other WSL work, run wsl --shutdown in PowerShell, then reopen. Restart Windows if it happens again.'; $wait = $false
    switch ($Name) {
        'Checking Windows and WSL' { $seconds = 30; $hint = 'Restart Windows after installing WSL, then reopen the toolkit.' }
        'Downloading Ubuntu' { $seconds = 120; $hint = 'No download bytes arrived. Check your internet connection, then reopen the toolkit.'; $wait = $true }
        'Installing the toolkit''s Linux' { $seconds = 600; $wait = $true }
        'Preparing Linux' { $seconds = 600; $wait = $true }
        'Installing Windows components' { $seconds = 900; $hint = 'Check for an installer or Windows administrator approval prompt. Restart Windows if requested.'; $wait = $true }
        'Unpacking the toolkit' { $seconds = 300; $wait = $true }
        'Checking the toolkit' { $seconds = 120 }
        'Connecting USB' { $seconds = 180; $hint = 'Check for a Windows administrator approval prompt. Reconnect the USB cable and reopen the toolkit.'; $wait = $true }
        'Opening the toolkit' { $seconds = 120; $wait = $true }
        'Cleaning up USB support' { $seconds = 30 }
    }
    return [pscustomobject]@{ Seconds=$seconds; Hint=$hint; AllowWait=$wait }
}
function Set-ToolkitStep([string]$Name) {
    Complete-ToolkitStep
    $script:ToolkitStep = $Name
    $script:ToolkitStepStart = [DateTime]::UtcNow
    $script:ToolkitStepTimer = [Diagnostics.Stopwatch]::StartNew()
    $script:ToolkitPolicy = Get-ToolkitStepPolicy $Name
    Write-ToolkitLog 'step-start' @{ step=$Name; start_utc=$script:ToolkitStepStart.ToString('o'); limit_seconds=$script:ToolkitPolicy.Seconds }
    Write-Host $Name
    Update-ToolkitSetup $Name $script:ToolkitPolicy.Hint
}
function Complete-ToolkitStep([string]$Result = 'ok', [int]$ExitCode = 0, [string]$ErrorText = '') {
    if ($Result -ne 'ok' -and -not $script:ToolkitFailedStep) { $script:ToolkitFailedStep = $script:ToolkitStep }
    if (-not $script:ToolkitStepTimer) { return }
    Write-ToolkitLog 'step-end' @{ step=$script:ToolkitStep; start_utc=$script:ToolkitStepStart.ToString('o'); end_utc=[DateTime]::UtcNow.ToString('o'); duration_seconds=[Math]::Round($script:ToolkitStepTimer.Elapsed.TotalSeconds,3); result=$Result; exit_code=$ExitCode; error=$ErrorText }
    $script:ToolkitStepTimer = $null
}
function Get-ToolkitSupportDetails([string]$Message) {
    $step = if ($script:ToolkitFailedStep) { $script:ToolkitFailedStep } elseif ($script:ToolkitStep) { $script:ToolkitStep } else { 'Starting setup' }
    $tail = if ($script:ToolkitLogTail) { $script:ToolkitLogTail.ToArray() -join "`r`n" } else { 'Log unavailable.' }
    return (Protect-ToolkitText ("Jibo DFU Toolkit setup support`r`nBuild: $script:ToolkitBuild`r`nWindows: $script:ToolkitWindowsVersion`r`nWSL: $script:ToolkitWslVersion`r`nStep: $step`r`nError: $Message`r`nLast log lines:`r`n$tail"))
}
function Test-ToolkitStall([double]$ElapsedSeconds, [double]$IdleSeconds, [double]$LimitSeconds, [switch]$ActivityBased) {
    if ($ActivityBased) { return $IdleSeconds -ge $LimitSeconds }
    return $ElapsedSeconds -ge $LimitSeconds
}
function Initialize-ToolkitDownload {
    if ('ToolkitDownload' -as [type]) { return }
    # C# event handlers never execute PowerShell on a worker thread. WebClient
    # streams directly to disk; its legacy API is present on Windows PS 5.1.
    Add-Type -IgnoreWarnings -WarningAction SilentlyContinue -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Net;
using System.Threading;
public sealed class ToolkitDownload : IDisposable {
    readonly WebClient client = new WebClient();
    long bytes;
    public long Bytes { get { return Interlocked.Read(ref bytes); } }
    public volatile bool Done;
    public string Error;
    public ToolkitDownload() {
        client.DownloadProgressChanged += delegate(object sender, DownloadProgressChangedEventArgs e) { Interlocked.Exchange(ref bytes, e.BytesReceived); };
        client.DownloadFileCompleted += delegate(object sender, AsyncCompletedEventArgs e) {
            Error = e.Cancelled ? "Download cancelled" : (e.Error == null ? null : e.Error.Message);
            Done = true;
        };
    }
    public void Start(string uri, string file) { client.DownloadFileAsync(new Uri(uri), file); }
    public void Dispose() { client.CancelAsync(); client.Dispose(); }
}
'@
}
function Receive-ToolkitDownload([string]$Uri, [string]$OutFile, [long]$ExpectedBytes, [double]$IdleSeconds = 120) {
    Initialize-ToolkitDownload
    $transfer = New-Object ToolkitDownload
    $idle = [Diagnostics.Stopwatch]::StartNew()
    $logTimer = [Diagnostics.Stopwatch]::StartNew()
    $received = 0L; $success = $false
    try {
        $transfer.Start($Uri, $OutFile)
        while (-not $transfer.Done) {
            $bytes = $transfer.Bytes
            if ($bytes -ne $received) { $received = $bytes; $idle.Restart() }
            if ($bytes -gt $ExpectedBytes) { throw 'Ubuntu download exceeded the expected size. Nothing was registered.' }
            $percent = [int][Math]::Min(100, [Math]::Floor(100.0 * $bytes / $ExpectedBytes))
            $text = 'Downloading Ubuntu ({0:N1} of {1:N1} MB)' -f ($bytes / 1MB), ($ExpectedBytes / 1MB)
            Update-ToolkitSetup $text 'Only received bytes count as progress. You will be warned if no bytes arrive.' $percent
            if ($logTimer.Elapsed.TotalSeconds -ge 10) {
                Write-ToolkitLog 'download-progress' @{ step=$script:ToolkitStep; bytes=$bytes; expected_bytes=$ExpectedBytes }
                $logTimer.Restart()
            }
            if (Test-ToolkitStall -IdleSeconds $idle.Elapsed.TotalSeconds -LimitSeconds $IdleSeconds -ActivityBased) {
                $message = "Downloading Ubuntu stalled: no bytes arrived for $IdleSeconds seconds. Check your internet connection, then reopen the toolkit."
                Write-ToolkitLog 'stall' @{ step=$script:ToolkitStep; bytes=$bytes; error=$message }
                if (Show-ToolkitSupportDialog $message -AllowWait) { Write-ToolkitLog 'keep-waiting' @{step=$script:ToolkitStep}; $idle.Restart() }
                else { $error = New-Object TimeoutException $message; $error.Data['ToolkitStall'] = $true; throw $error }
            }
            Start-Sleep -Milliseconds 100
        }
        if ($transfer.Error) { throw "Ubuntu download failed: $($transfer.Error). Check your internet connection, then reopen." }
        $length = (Get-Item -LiteralPath $OutFile).Length
        if ($length -ne $ExpectedBytes) { throw 'Ubuntu image size mismatch. Nothing was registered.' }
        Update-ToolkitSetup 'Downloading Ubuntu (complete)' 'Download received. Checking the official image next.' 100
        Write-ToolkitLog 'download-progress' @{ step=$script:ToolkitStep; bytes=$length; expected_bytes=$ExpectedBytes }
        $success = $true
    } finally {
        $transfer.Dispose()
        # Cancellation closes the file on WebClient's worker. Give it a bounded
        # grace period before removing an incomplete archive.
        $stop = [Diagnostics.Stopwatch]::StartNew()
        while (-not $transfer.Done -and $stop.Elapsed.TotalSeconds -lt 5) { Start-Sleep -Milliseconds 50 }
        if (-not $success -and (Test-Path -LiteralPath $OutFile)) { Remove-Item -LiteralPath $OutFile -Force }
    }
}
function Initialize-ToolkitConsole {
    if ('ToolkitConsoleNative' -as [type]) { return $true }
    if ($env:OS -ne 'Windows_NT') { return $false }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class ToolkitConsoleNative {
    [DllImport("user32.dll", CharSet=CharSet.Unicode, EntryPoint="MessageBoxW")]
    public static extern int MessageBox(IntPtr hwnd, string text, string title, uint flags);
}
'@
    return $true
}
function Show-ToolkitQuestion([string]$Question) {
    if (-not (Initialize-ToolkitConsole)) { throw 'Windows setup dialogs are unavailable.' }
    # Owned, foreground, topmost/system-modal: a question must not hide behind
    # the status window or another app. No remains the default.
    $owner = [IntPtr]::Zero
    if ('ToolkitSetupStatus' -as [type]) { $owner = [ToolkitSetupStatus]::OwnerHandle }
    return ([ToolkitConsoleNative]::MessageBox($owner, $Question, 'Jibo DFU Toolkit', 0x00051124) -eq 6)
}
function Initialize-ToolkitStatus {
    if (-not ('ToolkitSetupStatus' -as [type])) {
        if ($env:OS -ne 'Windows_NT') { return }
        # Own UI thread: downloads/imports can block PowerShell for minutes without
        # starving the status window's message pump. It never runs toolkit work.
        Add-Type -ReferencedAssemblies System.Windows.Forms,System.Drawing -TypeDefinition @'
using System;
using System.Drawing;
using System.Threading;
using System.Windows.Forms;
public class ToolkitSetupStatus {
    static Form window;
    static Thread thread;
    static IntPtr ownerHandle;
    public static IntPtr OwnerHandle { get { return ownerHandle; } }
    static Label label, detail, elapsed;
    static ProgressBar progress;
    static DateTime stepStart;
    static string currentStep = "Checking Windows and WSL", currentDetail = "Preparing setup log";
    static int currentPercent = -1;
    static readonly ManualResetEvent ready = new ManualResetEvent(false);
    public static void Start() {
        if (thread != null && thread.IsAlive) return;
        ready.Reset();
        thread = new Thread(delegate() {
            window = new Form();
            window.Text = "Jibo DFU Toolkit";
            window.ClientSize = new Size(580, 190);
            window.StartPosition = FormStartPosition.CenterScreen;
            window.FormBorderStyle = FormBorderStyle.FixedDialog;
            window.ControlBox = false;
            label = new Label();
            label.SetBounds(20, 16, 540, 40);
            window.Controls.Add(label);
            progress = new ProgressBar();
            progress.SetBounds(20, 60, 540, 20);
            window.Controls.Add(progress);
            elapsed = new Label();
            elapsed.SetBounds(20, 90, 540, 22);
            window.Controls.Add(elapsed);
            detail = new Label();
            detail.SetBounds(20, 118, 540, 60);
            window.Controls.Add(detail);
            stepStart = DateTime.UtcNow;
            PaintStatus();
            var timer = new System.Windows.Forms.Timer();
            timer.Interval = 1000;
            timer.Tick += delegate { elapsed.Text = "This step: " + (int)(DateTime.UtcNow - stepStart).TotalSeconds + " seconds. Setup is supervised for stalls."; };
            timer.Start();
            window.FormClosed += delegate { timer.Dispose(); };
            window.Shown += delegate { ownerHandle = window.Handle; ready.Set(); };
            Application.Run(window);
        });
        thread.IsBackground = true;
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        if (!ready.WaitOne(10000)) throw new Exception("Could not show the toolkit setup window.");
    }
    static void PaintStatus() {
        label.Text = currentStep;
        detail.Text = currentDetail;
        progress.Style = currentPercent < 0 ? ProgressBarStyle.Marquee : ProgressBarStyle.Continuous;
        if (currentPercent >= 0) progress.Value = Math.Max(0, Math.Min(100, currentPercent));
    }
    public static void Update(string text, string hint, int percent) {
        // Queue ALL widget access on the owning STA thread.
        if (window != null && window.IsHandleCreated && !window.IsDisposed) {
            window.BeginInvoke(new Action(delegate {
                if (percent < 0) stepStart = DateTime.UtcNow;
                currentStep = text; currentDetail = hint; currentPercent = percent;
                PaintStatus();
            }));
        } else { currentStep = text; currentDetail = hint; currentPercent = percent; }
    }
    public static bool Details(string message, string summary, string folder, bool allowWait) {
        bool keepWaiting = false;
        var dialogThread = new Thread(delegate() {
            using (var form = new Form()) {
                form.Text = allowWait ? "Jibo DFU Toolkit - setup stalled" : "Jibo DFU Toolkit - setup stopped";
                form.ClientSize = new Size(680, 440);
                form.StartPosition = FormStartPosition.CenterScreen;
                form.FormBorderStyle = FormBorderStyle.FixedDialog;
                form.MaximizeBox = false; form.MinimizeBox = false; form.TopMost = true;
                var explanation = new Label();
                explanation.Text = message + "\r\nCopy details to paste in Phoenix Discord #help. Review before sharing.";
                explanation.SetBounds(16, 16, 648, 100); form.Controls.Add(explanation);
                var text = new TextBox();
                text.Multiline = true; text.ReadOnly = true; text.ScrollBars = ScrollBars.Both;
                text.WordWrap = false; text.Text = summary;
                text.SetBounds(16, 120, 648, 256); form.Controls.Add(text);
                var copy = new Button(); copy.Text = "Copy details"; copy.SetBounds(16, 396, 130, 28);
                copy.Click += delegate { try { Clipboard.SetText(summary); copy.Text = "Copied"; } catch { copy.Text = "Select text above"; } }; form.Controls.Add(copy);
                var open = new Button(); open.Text = "Open log folder"; open.SetBounds(156, 396, 140, 28);
                open.Enabled = !String.IsNullOrEmpty(folder);
                open.Click += delegate { try { System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(folder) { UseShellExecute = true }); } catch { open.Text = "Cannot open folder"; } }; form.Controls.Add(open);
                if (allowWait) {
                    var wait = new Button(); wait.Text = "Keep waiting"; wait.SetBounds(386, 396, 130, 28);
                    wait.Click += delegate { keepWaiting = true; form.Close(); }; form.Controls.Add(wait);
                }
                var close = new Button(); close.Text = allowWait ? "Stop setup" : "Close";
                close.SetBounds(526, 396, 138, 28); close.Click += delegate { form.Close(); }; form.Controls.Add(close);
                form.CancelButton = close; form.AcceptButton = close;
                form.ShowDialog();
            }
        });
        dialogThread.SetApartmentState(ApartmentState.STA);
        dialogThread.Start();
        dialogThread.Join(); // Deliberate user decision, not a command wait.
        return keepWaiting;
    }
    public static void Stop() {
        if (window != null && window.IsHandleCreated && !window.IsDisposed)
            window.BeginInvoke(new Action(delegate { window.Close(); }));
        if (thread != null) thread.Join(10000);
    }
}
'@
    }
}
function Start-ToolkitSetup {
    Initialize-ToolkitStatus
    if ('ToolkitSetupStatus' -as [type]) { [ToolkitSetupStatus]::Start() }
}
function Update-ToolkitSetup([string]$Text, [string]$Detail = '', [int]$Percent = -1) {
    if ('ToolkitSetupStatus' -as [type]) { [ToolkitSetupStatus]::Update($Text, $Detail, $Percent) }
}
function Show-ToolkitSupportDialog([string]$Message, [switch]$AllowWait) {
    $safe = Protect-ToolkitText $Message
    $summary = Get-ToolkitSupportDetails $safe
    if ($env:OS -eq 'Windows_NT') {
        Initialize-ToolkitStatus
        $folder = if ($script:ToolkitLogPath) { Split-Path $script:ToolkitLogPath } else { '' }
        return [ToolkitSetupStatus]::Details($safe, $summary, $folder, [bool]$AllowWait)
    }
    Write-Host $summary
    return $false
}
function Stop-ToolkitSetup {
    if ('ToolkitSetupStatus' -as [type]) { [ToolkitSetupStatus]::Stop() }
}
function Show-ToolkitFailure([string]$Message) {
    Stop-ToolkitSetup
    $safe = Protect-ToolkitText $Message
    Write-Host "Launcher: $safe" -ForegroundColor Red
    if ($script:ToolkitLogPath) {
        $null = Show-ToolkitSupportDialog $safe
    } elseif (Initialize-ToolkitConsole) {
        [ToolkitConsoleNative]::MessageBox([IntPtr]::Zero, $safe, 'Jibo DFU Toolkit', 0x00010010) | Out-Null
    }
}
function Start-ToolkitProcess([string]$FilePath, [string]$ArgumentList) {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $FilePath
    $info.Arguments = $ArgumentList
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    try {
        $null = $process.Start()
        $null = $process.Handle
        return $process
    } catch { $process.Dispose(); throw }
}
function ConvertTo-ToolkitNativeLines([string]$Text) {
    # Match the classic "& $FilePath @ArgumentList 2>&1" capture shape: one
    # array item per line, not the whole multi-line blob as a single string.
    # NULs are stripped (wsl.exe writes UTF-16LE when redirected;
    # StandardOutputEncoding=UTF8 turns each ASCII byte into a byte+NUL pair,
    # which run.ps1's per-item NUL strip already handles -- but only once each
    # *item* really is one line). A trailing newline must not create a
    # phantom empty trailing item; a genuinely empty interior line is kept.
    if (-not $Text) { return @() }
    $clean = $Text -replace "`0", ''
    $lines = [string[]]($clean -split "`r`n|`r|`n")
    if ($lines.Count -gt 0 -and $lines[-1] -eq '') {
        $lines = $lines[0..($lines.Count - 2)]
    }
    return @($lines)
}
function Invoke-ToolkitElevated([string]$File, [string]$Arguments, [double]$TimeoutSeconds = 180, [switch]$AllowWait) {
    $started = [DateTime]::UtcNow
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $code = 1; $message = ''; $process = $null
    Write-ToolkitLog 'command-start' @{step=$script:ToolkitStep; command=[IO.Path]::GetFileName($File); start_utc=$started.ToString('o'); limit_seconds=$TimeoutSeconds}
    try {
        $process = Start-Process -FilePath $File -ArgumentList $Arguments -Verb RunAs -WindowStyle Hidden -PassThru
        $null = $process.Handle
        if (-not (Wait-ToolkitProcess $process -TimeoutSeconds $TimeoutSeconds -AllowWait:$AllowWait)) {
            $code = 124
            $error = New-Object TimeoutException "$script:ToolkitStep stalled: administrator command did not finish. Check Windows prompts, then restart Windows and reopen."
            $error.Data['ToolkitStall'] = $true
            throw $error
        }
        $code = $process.ExitCode
        return [pscustomobject]@{ExitCode=$code}
    } catch { $message = $_.Exception.Message; throw }
    finally {
        Write-ToolkitLog 'command-end' @{step=$script:ToolkitStep; command=[IO.Path]::GetFileName($File); start_utc=$started.ToString('o'); end_utc=[DateTime]::UtcNow.ToString('o'); duration_seconds=[Math]::Round($timer.Elapsed.TotalSeconds,3); result=$(if ($code -eq 0) {'ok'} elseif ($code -eq 124) {'stalled'} else {'failed'}); exit_code=$code; error=$message}
        if ($process -is [Diagnostics.Process]) { $process.Dispose() }
    }
}
function Wait-ToolkitProcess($Process, [double]$TimeoutSeconds = 120, [switch]$AllowWait) {
    $timer = [Diagnostics.Stopwatch]::StartNew()
    while (-not $Process.WaitForExit(100)) {
        if (Test-ToolkitStall -ElapsedSeconds $timer.Elapsed.TotalSeconds -LimitSeconds $TimeoutSeconds) {
            $message = "$script:ToolkitStep stalled: no completion after $TimeoutSeconds seconds. $($script:ToolkitPolicy.Hint)"
            Write-ToolkitLog 'stall' @{ step=$script:ToolkitStep; error=$message; limit_seconds=$TimeoutSeconds }
            if ($AllowWait -and (Show-ToolkitSupportDialog $message -AllowWait)) {
                Write-ToolkitLog 'keep-waiting' @{ step=$script:ToolkitStep }
                $timer.Restart()
                continue
            }
            Stop-ToolkitProcess $Process
            return $false
        }
    }
    return $true
}
function Stop-ToolkitProcess($Process) {
    if (-not $Process -or $Process.HasExited) { return }
    try {
        # taskkill /T also closes the hidden PowerShell USB helper's children.
        # Never wait without a bound, including on the kill command itself.
        if ($env:OS -eq 'Windows_NT') {
            $kill = Start-ToolkitProcess -FilePath "$env:SystemRoot\System32\taskkill.exe" -ArgumentList "/PID $($Process.Id) /T /F"
            try { if (-not $kill.WaitForExit(5000)) { $kill.Kill() } } finally { $kill.Dispose() }
        } else { $Process.Kill() }
        if (-not $Process.WaitForExit(5000)) { throw 'Timed-out process did not stop. Restart Windows before trying again.' }
    } catch {
        # Killing a child which exited during the timeout check is harmless.
        if (-not $Process.HasExited) { throw }
    }
}
function Invoke-ToolkitNative([string]$File, [string[]]$Arguments, [double]$TimeoutSeconds = 120, [switch]$AllowWait) {
    $started = [DateTime]::UtcNow
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $commandStep = $script:ToolkitStep
    $result = $null
    Write-ToolkitLog 'command-start' @{ step=$commandStep; command=[IO.Path]::GetFileName($File); start_utc=$started.ToString('o'); limit_seconds=$TimeoutSeconds }
    # Never let PowerShell's native invocation allocate a console in a GUI launch.
    # Read both pipes concurrently so a verbose helper cannot deadlock on stderr.
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $File
    $info.Arguments = (($Arguments | ForEach-Object {
        '"' + ($_ -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
    }) -join ' ')
    # WSL parses its options before --exec without quote handling. Simple options
    # and distribution/user names therefore stay bare, unlike the command argv.
    if ([IO.Path]::GetFileName($File) -ieq 'wsl.exe') {
        $info.Arguments = (($Arguments | ForEach-Object {
            if ($_ -cmatch '^[A-Za-z0-9._/-]+$') { $_ }
            else { '"' + ($_ -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"' }
        }) -join ' ')
    }
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.StandardOutputEncoding = New-Object Text.UTF8Encoding($false)
    $info.StandardErrorEncoding = New-Object Text.UTF8Encoding($false)
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    try {
        $null = $process.Start()
        # wsl --list uses UTF-16LE when redirected. StreamReader defaults to UTF-8;
        # the existing Native NUL removal handles its ASCII distribution metadata.
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $completed = Wait-ToolkitProcess $process -TimeoutSeconds $TimeoutSeconds -AllowWait:$AllowWait
        # Pipe readers must also be bounded: descendants can inherit open handles.
        $pipesComplete = [Threading.Tasks.Task]::WaitAll([Threading.Tasks.Task[]]@($stdout, $stderr), 2000)
        $lines = @()
        if ($stdout.IsCompleted -and -not $stdout.IsFaulted) { $lines += @(ConvertTo-ToolkitNativeLines $stdout.GetAwaiter().GetResult()) }
        if ($stderr.IsCompleted -and -not $stderr.IsFaulted) { $lines += @(ConvertTo-ToolkitNativeLines $stderr.GetAwaiter().GetResult()) }
        if (-not $completed -or -not $pipesComplete) {
            $result = [pscustomobject]@{Output=$lines; ExitCode=124; TimedOut=$true; StartError="$commandStep stalled after $TimeoutSeconds seconds. $($script:ToolkitPolicy.Hint)"}
        } else {
            $result = [pscustomobject]@{Output=$lines; ExitCode=$process.ExitCode; TimedOut=$false; StartError=$null}
        }
    } catch {
        $result = [pscustomobject]@{Output=@(); ExitCode=1; TimedOut=$false; StartError=$_.Exception.Message}
    } finally {
        $process.Dispose()
        if ($result) {
            $output = if ($result.ExitCode -ne 0) { ($result.Output -join "`n") } else { '' }
            # Keep useful diagnostics bounded; never persist argument lists.
            if ($output.Length -gt 8000) { $output = $output.Substring($output.Length - 8000) }
            Write-ToolkitLog 'command-end' @{ step=$commandStep; command=[IO.Path]::GetFileName($File); start_utc=$started.ToString('o'); end_utc=[DateTime]::UtcNow.ToString('o'); duration_seconds=[Math]::Round($timer.Elapsed.TotalSeconds,3); result=$(if ($result.TimedOut) { 'stalled' } elseif ($result.ExitCode -eq 0) { 'ok' } else { 'failed' }); exit_code=$result.ExitCode; error=[string]$result.StartError; output=$output }
        }
    }
    return $result
}
