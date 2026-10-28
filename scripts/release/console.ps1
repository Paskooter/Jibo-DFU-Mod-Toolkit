# Windowless release support. Source launchers retain their interactive console.
# Only setup diagnostics are collected: never command arguments or robot settings.
function Protect-ToolkitText([string]$Text) {
    $clean = $Text -replace "`0", ''
    $clean = [regex]::Replace($clean, '(?im)^.*(?:password|passwd|token|authorization|ssid|wi-?fi|psk|secret)[\x22\x27]?\s*[:=].*$', '[private data removed]')
    $clean = $clean -replace '(?i)https?://[^\s"<>]+', '[URL removed]'
    foreach ($path in @($env:USERPROFILE, $env:LOCALAPPDATA, $env:TEMP, $env:HOME)) {
        if ($path -and $path.Length -gt 3) { $clean = $clean -replace [regex]::Escape($path), '[user folder]' }
    }
    $clean = $clean -replace '(?i)[A-Z]:\\Users\\[^\\\s]+', '[user folder]'
    $clean = $clean -replace '/(?:home|Users)/[^/\s]+', '[user folder]'
    return $clean
}
function Get-ToolkitWindowsLabel {
    if ($env:OS -ne 'Windows_NT') { return [Environment]::OSVersion.VersionString }
    # Local registry only: never block the first UI on a WMI/CIM provider. The
    # compatibility ProductName can say Windows 10 on Windows 11. Build alone
    # cannot distinguish a client from Server (which also uses build 26100).
    try { $windows = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion' -ErrorAction Stop }
    catch { return 'Windows (version unavailable)' }
    $build = 0; $revision = 0
    $hasBuild = [string]$windows.CurrentBuild -match '^[0-9]{4,6}$' -and [int]::TryParse([string]$windows.CurrentBuild, [ref]$build)
    $hasRevision = [string]$windows.UBR -match '^[0-9]{1,10}$' -and [int]::TryParse([string]$windows.UBR, [ref]$revision)
    $name = 'Windows'
    $product = [string]$windows.ProductName
    if ($hasBuild -and $windows.InstallationType -eq 'Client' -and $product -match '^Windows (10|11)( [A-Za-z0-9 .()-]{1,80})?$') {
        $edition = $Matches[2]
        if ($build -ge 22000) { $name = 'Windows 11' + $edition }
        elseif ($build -ge 10240 -and $Matches[1] -eq '10') { $name = $product }
    } elseif ($hasBuild -and $windows.InstallationType -in @('Server','Server Core') -and $product -match '^Windows Server [A-Za-z0-9 .()-]{1,80}$') {
        $name = $product
    }
    if (-not $hasBuild) { return 'Windows (version unavailable)' }
    if ([string]$windows.DisplayVersion -match '^[0-9]{2}H[12]$') { $name += ' ' + $windows.DisplayVersion }
    $number = [string]$build
    if ($hasRevision) { $number += '.' + $revision }
    return "$name (build $number)"
}
function Initialize-ToolkitLog([string]$Root, [string]$Build = 'unknown', [string]$ExistingPath = $env:JIBO_DFU_SETUP_LOG) {
    if (-not $Root) { $Root = $env:LOCALAPPDATA }
    if (-not $Root) { $Root = [IO.Path]::GetTempPath() }
    $script:ToolkitBuild = $Build
    $script:ToolkitWindowsVersion = Get-ToolkitWindowsLabel
    $script:ToolkitWslVersion = 'not checked'
    $script:ToolkitStep = $null
    $script:ToolkitFailedStep = $null
    $script:ToolkitLogTail = New-Object 'Collections.Generic.Queue[string]'
    $script:ToolkitQuestionHistory = New-Object 'Collections.Generic.Queue[string]'
    $script:ToolkitWaitingForUser = $false
    $name = 'setup-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmss.fffZ') + '-' + [guid]::NewGuid().ToString('N').Substring(0,8) + '.log'
    $script:ToolkitLogPath = $null
    # The EXE creates this before integrity checks and passes it only to its
    # bootstrap child. Append rather than losing early-launch diagnostics.
    if ($ExistingPath -and (Test-Path -LiteralPath $ExistingPath -PathType Leaf)) {
        try {
            [IO.File]::AppendAllText($ExistingPath, '')
            $script:ToolkitLogPath = $ExistingPath
        } catch { $script:ToolkitLogPath = $null }
    }
    foreach ($base in @($Root, [IO.Path]::GetTempPath())) {
        if ($script:ToolkitLogPath) { break }
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
    # Synchronous append per event: receipts are not held in a process buffer.
    [IO.File]::AppendAllText($script:ToolkitLogPath, $line + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
    $script:ToolkitLogTail.Enqueue($line)
    if ($Event -in @('question-start','question-end') -and $null -ne $script:ToolkitQuestionHistory) {
        $script:ToolkitQuestionHistory.Enqueue($line)
        while ($script:ToolkitQuestionHistory.Count -gt 20) { $null = $script:ToolkitQuestionHistory.Dequeue() }
    }
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
        'Fixing a Windows display problem' { $seconds = 75; $hint = 'Save your other Linux work, run wsl --shutdown in PowerShell (this closes all Linux apps), then open the toolkit again. Restart Windows if it still happens.' }
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
    Update-ToolkitSetup $Name ("Waiting for this step to finish. A stall warning appears after $($script:ToolkitPolicy.Seconds) seconds without completion.")
}
function Complete-ToolkitStep([string]$Result = 'ok', [int]$ExitCode = 0, [string]$ErrorText = '') {
    if ($Result -ne 'ok' -and -not $script:ToolkitFailedStep) { $script:ToolkitFailedStep = $script:ToolkitStep }
    if (-not $script:ToolkitStepTimer) { return }
    Write-ToolkitLog 'step-end' @{ step=$script:ToolkitStep; start_utc=$script:ToolkitStepStart.ToString('o'); end_utc=[DateTime]::UtcNow.ToString('o'); duration_seconds=[Math]::Round($script:ToolkitStepTimer.Elapsed.TotalSeconds,3); result=$Result; exit_code=$ExitCode; error=$ErrorText }
    $script:ToolkitStepTimer = $null
}
function Get-ToolkitSupportDetails([string]$Message) {
    $step = if ($script:ToolkitFailedStep) { $script:ToolkitFailedStep } elseif ($script:ToolkitStep) { $script:ToolkitStep } else { 'Starting setup' }
    $tail = if ($script:ToolkitLogTail) { ($script:ToolkitLogTail.ToArray() | ForEach-Object { if ($_.Length -gt 500) { $_.Substring(0,500) + ' [see full log]' } else { $_ } }) -join "`r`n" } else { 'Log unavailable.' }
    $questions = if ($script:ToolkitQuestionHistory) { $script:ToolkitQuestionHistory.ToArray() -join "`r`n" } else { 'None recorded.' }
    if ($Message.Length -gt 1200) { $Message = $Message.Substring(0,1200) + ' [see full log]' }
    return (Protect-ToolkitText ("Jibo DFU Toolkit setup support`r`nBuild: $script:ToolkitBuild`r`nWindows: $script:ToolkitWindowsVersion`r`nWSL: $script:ToolkitWslVersion`r`nStep: $step`r`nError: $Message`r`nSetup questions and answers:`r`n$questions`r`nLast log lines:`r`n$tail"))
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
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    static extern IntPtr CreateWindowEx(uint ex, string cls, string title, uint style, int x, int y, int w, int h, IntPtr parent, IntPtr menu, IntPtr instance, IntPtr param);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr hwnd, int command);
    [DllImport("user32.dll")] static extern bool DestroyWindow(IntPtr hwnd);
    static IntPtr owner;
    public static IntPtr CreateOwner() {
        if (owner == IntPtr.Zero) {
            owner = CreateWindowEx(0x00040000, "STATIC", "Jibo DFU Toolkit \u2013 setup stopped", 0x00C80000, 200, 200, 640, 180, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero);
            if (owner == IntPtr.Zero) throw new Exception("Could not create the setup error window.");
            ShowWindow(owner, 5);
        }
        return owner;
    }
    public static void CloseOwner() { if (owner != IntPtr.Zero) { DestroyWindow(owner); owner = IntPtr.Zero; } }
}
'@
    return $true
}
function Show-ToolkitQuestion([string]$Question) {
    Start-ToolkitSetup
    if (-not ('ToolkitSetupStatus' -as [type])) { throw 'The Windows setup question window is unavailable. No approval was given.' }
    $script:ToolkitWaitingForUser = $true
    try {
        [ToolkitSetupStatus]::AskQuestion($Question)
        while ([ToolkitSetupStatus]::QuestionPending) {
            Write-ToolkitInteractionEvents
            Start-Sleep -Milliseconds 100
        }
        Write-ToolkitInteractionEvents
        $script:ToolkitQuestionAnswer = [ToolkitSetupStatus]::LastAnswer
        return $script:ToolkitQuestionAnswer -eq 'yes'
    } finally { $script:ToolkitWaitingForUser = $false }
}
function Write-ToolkitInteractionEvents {
    if (-not ('ToolkitSetupStatus' -as [type])) { return }
    foreach ($event in [ToolkitSetupStatus]::DrainEvents()) {
        Write-ToolkitLog $event.Kind @{ at_utc=$event.Utc; question=$event.Message; step=$script:ToolkitStep }
    }
}
function Write-ToolkitClipboardEvents {
    if (-not ('ToolkitSetupStatus' -as [type]) -or -not [ToolkitSetupStatus].GetMethod('DrainClipboardFailures')) { return }
    foreach ($event in [ToolkitSetupStatus]::DrainClipboardFailures()) {
        Write-ToolkitLog 'clipboard-copy-failed' @{
            at_utc=$event.Utc; exception_type=$event.ExceptionType; category=$event.Category
            hresult=$event.HResult; apartment=$event.Apartment
            managed_thread_id=$event.ManagedThreadId; summary_length=$event.SummaryLength
        }
    }
}
function Set-ToolkitUserWait([bool]$Active, [string]$Instructions = '') {
    $script:ToolkitWaitingForUser = $Active
    if ('ToolkitSetupStatus' -as [type]) { [ToolkitSetupStatus]::UserWait($Active, $Instructions) }
    Write-ToolkitLog $(if ($Active) {'user-wait-start'} else {'user-wait-end'}) @{ step=$script:ToolkitStep; message=$Instructions }
    Write-ToolkitInteractionEvents
}
# The same model is compiled into the Windows UI and tested headlessly.
$script:ToolkitQuestionModelSource = @'
public class ToolkitQuestionState {
    public const double KeyGuardSeconds = 0.5;
    public const double ReminderSeconds = 60;
    public volatile bool Pending;
    public volatile string Answer = "no";
    public string Prompt;
    double opened, lastAttention;
    public void Begin(string prompt, double now) {
        Prompt = prompt; Answer = "no"; opened = lastAttention = now; Pending = true;
    }
    public bool KeysEnabled(double now) { return Pending && now - opened >= KeyGuardSeconds; }
    public bool Respond(string answer, double now) {
        if (!Pending || (answer != "closed" && !KeysEnabled(now))) return false;
        Answer = answer; Pending = false; return true;
    }
    public bool Remind(double now) {
        if (!Pending || now - lastAttention < ReminderSeconds) return false;
        lastAttention = now; return true;
    }
}
public class ToolkitInteractionEvent {
    public string Kind, Utc, Message;
    public ToolkitInteractionEvent(string kind, string message) {
        Kind = kind; Message = message; Utc = System.DateTime.UtcNow.ToString("o");
    }
}
// Copy diagnostics contain only bounded provenance, never an Exception object,
// message, stack trace or the support/clipboard text. Unknown/custom type names
// are not safe to log; the fixed category still distinguishes broad failures.
public sealed class ToolkitClipboardFailure {
    public readonly string Utc, ExceptionType, Category, HResult, Apartment;
    public readonly int ManagedThreadId, SummaryLength;
    public ToolkitClipboardFailure(System.Exception error, int summaryLength) {
        Utc = System.DateTime.UtcNow.ToString("o");
        var type = error.GetType();
        ExceptionType = type == typeof(System.Runtime.InteropServices.ExternalException) ? "System.Runtime.InteropServices.ExternalException" :
            type == typeof(System.Threading.ThreadStateException) ? "System.Threading.ThreadStateException" :
            type == typeof(System.InvalidOperationException) ? "System.InvalidOperationException" :
            type == typeof(System.ArgumentNullException) ? "System.ArgumentNullException" :
            type == typeof(System.ArgumentException) ? "System.ArgumentException" : "other";
        Category = error is System.Threading.ThreadStateException ? "thread-state" :
            error is System.Runtime.InteropServices.ExternalException ? "external" :
            error is System.InvalidOperationException ? "invalid-operation" :
            error is System.ArgumentException ? "argument" : "unexpected";
        HResult = "0x" + error.HResult.ToString("X8", System.Globalization.CultureInfo.InvariantCulture);
        Apartment = System.Threading.Thread.CurrentThread.GetApartmentState().ToString();
        ManagedThreadId = System.Threading.Thread.CurrentThread.ManagedThreadId;
        SummaryLength = summaryLength;
    }
}
// Plain-language presentation only. Keys are the exact logged strings (step names,
// question prompts, user-wait instructions, failure messages); the log and Copy
// details keep the original technical text. Unknown keys fall back to that text.
public sealed class ToolkitStepText {
    public string Title, Description;
    public int Phase;
}
public sealed class ToolkitQuestionText {
    public string Id, Title, Body, WhatHappens, HowLong, YesLabel, NoLabel, YesHelp, Footnote, Declined;
}
public sealed class ToolkitWaitText {
    public string Kind, Title, Hint;
    public string[] Steps;
}
public sealed class ToolkitFailureText {
    public string Tone, Headline, Step, StepKey, What, Try;
    public bool Decision; // Keep waiting / Stop setup is being offered.
}
public static class ToolkitSetupText {
    public const string Product = "Jibo DFU Toolkit";
    public const string Exe = "Jibo-DFU-Windows-x64.exe";
    public static readonly string[] Phases = { "Check Windows", "Set up Linux", "Prepare toolkit", "Connect USB", "Open toolkit" };
    public const string WorkingStatus = "Working \u2013 nothing for you to do right now";
    public const string QuestionStatus = "Your answer is needed";
    public const string ReminderStatus = "Still waiting for your answer";
    public const string WaitStatus = "Waiting for you";
    public const string WaitPaused = "Setup is paused until you do this. It has not stalled.";
    public const string ClosesItself = "This window closes by itself when the toolkit opens.";
    public const string HelpLine = "To get help, copy the details, press Open a support ticket in the #help channel on the Phoenix Discord, and paste them into your ticket. Look them over first.";
    public const string CopiedLine = "Copied. Paste it into your support ticket with Ctrl+V.";
    public const string CopyFailedLine = "Could not copy. Select the text below, then press Ctrl+C.";
    public const string ManualUsb = "download the file ending in x64.msi from https://github.com/dorssel/usbipd-win/releases, run it, restart Windows if it asks, then open the toolkit again.";
    static ToolkitStepText S(string title, string description, int phase) {
        var s = new ToolkitStepText(); s.Title = title; s.Description = description; s.Phase = phase; return s;
    }
    public static ToolkitStepText Step(string key, string questionId) {
        switch (key ?? "") {
            case "Starting setup": case "Checking embedded files": return S("Starting setup", "Checking the toolkit\u2019s own files.", 0);
            case "Checking Windows and WSL": return S("Checking Windows", "Making sure Windows has what the toolkit needs to run.", 0);
            case "Installing Windows components":
                if (questionId == "usb") return S("Installing USB support", "Installing usbipd-win, the free Windows tool that lets the toolkit talk to Jibo over USB.", 2);
                if (questionId == "wsl") return S("Installing Windows\u2019 Linux support", "Windows is installing its Linux feature (WSL) from Microsoft. This can take several minutes.", 1);
                return S("Installing Windows components", "Windows is installing what the toolkit needs. This can take several minutes.", 0);
            case "Finding Linux": return S("Looking for Linux on this PC", "Checking whether this PC already has a Linux the toolkit can use.", 1);
            case "Starting Linux": return S("Starting the toolkit\u2019s Linux", "Starting Linux and checking that the toolkit can use it.", 1);
            case "Downloading Ubuntu": return S("Downloading the toolkit\u2019s Linux", "A one-time download of Ubuntu Linux from Ubuntu\u2019s official site.", 1);
            case "Checking the Ubuntu image": return S("Checking the download", "Making sure the download is complete and is the official Ubuntu file.", 1);
            case "Installing the toolkit's Linux": return S("Installing the toolkit\u2019s Linux", "Setting up a private Linux just for the toolkit. This can take a few minutes.", 1);
            case "Preparing Linux": return S("Preparing the toolkit\u2019s Linux", "Finishing the Linux setup. No account or password is needed.", 1);
            case "Unpacking the toolkit": return S("Unpacking the toolkit", "Copying the toolkit into its Linux. This can take a few minutes.", 2);
            case "Checking the toolkit": return S("Checking the toolkit", "Making sure the toolkit starts correctly.", 2);
            case "Connecting USB": return S("Connecting USB", "Letting the toolkit use USB so it can talk to Jibo.", 3);
            case "Opening the toolkit": return S("Opening the toolkit", "The toolkit window appears in a moment.", 4);
            case "Fixing a Windows display problem": return S("Fixing a Windows display problem", "This is a known Windows issue. It takes a few seconds.", 4);
            case "Cleaning up USB support": return S("Closing USB support", "Releasing the USB connection.", 4);
        }
        return null;
    }
    public static string QuestionId(string prompt) {
        switch (prompt ?? "") {
            case "Install/update WSL 2 and WSLg? Windows will ask for administrator approval.": return "wsl";
            case "Set up a private Linux for the toolkit? One-time download of about 345 MB. No Linux account or password needed.": return "linux";
            case "Install usbipd-win using the interactive Windows installer?": return "usb";
        }
        return "";
    }
    static ToolkitQuestionText Q(string id, string title, string body, string what, string howLong, string yes, string yesHelp, string declined) {
        var q = new ToolkitQuestionText(); q.Id = id; q.Title = title; q.Body = body; q.WhatHappens = what; q.HowLong = howLong; q.Declined = declined;
        q.YesLabel = yes; q.NoLabel = "Not now"; q.YesHelp = yesHelp;
        q.Footnote = "Not now stops setup here and installs nothing. Closing this window also counts as Not now.";
        return q;
    }
    public static ToolkitQuestionText Question(string prompt) {
        switch (QuestionId(prompt)) {
            case "wsl": return Q("wsl", "Install Windows\u2019 Linux support?",
                "The toolkit runs on Linux inside Windows. Windows needs its Linux feature (WSL) installed or updated first.",
                "Windows installs it from Microsoft. Windows will ask for permission, and may ask you to restart.",
                "Several minutes, depending on your internet speed.", "Install", "Installs Windows\u2019 Linux feature from Microsoft",
                "Windows\u2019 Linux support was not installed.");
            case "linux": return Q("linux", "Set up the toolkit\u2019s Linux?",
                "The toolkit runs in its own private copy of Ubuntu Linux, kept separate from the rest of this PC.",
                "A one-time download of about 345 MB from Ubuntu\u2019s official site. No Linux account or password needed.",
                "Usually several minutes, depending on your internet speed.", "Set it up", "Downloads about 345 MB and sets up the toolkit\u2019s Linux",
                "Nothing was downloaded or installed.");
            case "usb": return Q("usb", "Install USB support?",
                "To talk to Jibo over USB, the toolkit needs usbipd-win, a free Windows tool.",
                "Its installer opens. Follow its steps and choose Yes if Windows asks for permission. Then open the toolkit again.",
                "Usually a minute or two.", "Install", "Opens the usbipd-win installer",
                "USB support was not installed.");
        }
        var other = new ToolkitQuestionText(); other.Id = ""; other.Title = prompt; other.YesLabel = "Yes"; other.NoLabel = "No";
        other.YesHelp = ""; other.Declined = ""; other.Footnote = "No stops setup here. Closing this window also counts as No.";
        return other;
    }
    static ToolkitWaitText W(string kind, string title, string hint, params string[] steps) {
        var w = new ToolkitWaitText(); w.Kind = kind; w.Title = title; w.Hint = hint; w.Steps = steps; return w;
    }
    public static ToolkitWaitText Wait(string instructions) {
        switch (instructions ?? "") {
            case "Waiting for you: approve the Windows administrator prompt. If you do not see it, look for a flashing shield icon on the taskbar.":
                return W("admin", "Approve the Windows prompt", "Don\u2019t see it? Click the flashing shield icon on the taskbar.",
                    "Windows is asking: \u201cDo you want to allow this app to make changes to your device?\u201d", "Choose Yes to continue setup.");
            case "Waiting for you: finish the usbipd-win installer. Look for its installer window or a flashing shield icon on the taskbar. Restart Windows if the installer asks.":
                return W("installer", "Finish the USB support installer", "Can\u2019t find it? Look for the installer or a flashing shield icon on the taskbar.",
                    "Find the usbipd-win installer window and follow its steps.", "If Windows asks for permission, choose Yes.", "Restart Windows only if the installer asks you to.");
            case "Waiting for you: restart Windows if the WSL installer requested it, then reopen Jibo-DFU-Windows-x64.exe. Check the installer window or flashing shield icon on the taskbar.":
                return W("restart", "Restart Windows, then open the toolkit again", "Not sure? Check the installer window or a flashing shield icon on the taskbar.",
                    "Restart Windows if the installer asked you to.", "Then open " + Exe + " again to finish setup.");
            case "Waiting for you: finish any usbipd-win installer window, restart Windows if requested, then reopen the EXE. Look for its installer window or flashing shield icon on the taskbar.":
                return W("restart", "Finish installing USB support", "Not sure? Check the installer window or a flashing shield icon on the taskbar.",
                    "Finish the usbipd-win installer, if it is still open.", "Restart Windows if it asks you to.", "Then open " + Exe + " again.");
            case "Waiting for you: approve USB sharing at the Windows administrator prompt. If it is hidden, look for a flashing shield icon on the taskbar.":
                return W("admin", "Approve USB sharing if Windows asks", "Don\u2019t see a prompt? Check the taskbar for a flashing shield icon. If there is none, setup continues by itself.",
                    "Windows may ask for permission to share USB with the toolkit.", "If it does, choose Yes.");
        }
        string text = instructions ?? "";
        if (text.StartsWith("Waiting for you: ")) text = text.Substring(17);
        if (text.Length > 0) text = char.ToUpperInvariant(text[0]) + text.Substring(1);
        return W("other", WaitStatus, "", text);
    }
    public static string Duration(double seconds) {
        int s = (int)System.Math.Round(System.Math.Max(0, seconds));
        if (s < 90) return s == 1 ? "1 second" : s + " seconds";
        int m = (int)System.Math.Round(s / 60.0);
        return m + " minutes";
    }
    public static string Clock(double seconds) {
        int s = (int)System.Math.Max(0, seconds);
        if (s >= 3600) return (s / 3600) + ":" + (s / 60 % 60).ToString("00") + ":" + (s % 60).ToString("00");
        return (s / 60) + ":" + (s % 60).ToString("00");
    }
    static string PlainTry(string hint) {
        switch ((hint ?? "").Trim()) {
            case "": return null;
            case "Close other WSL work, run wsl --shutdown in PowerShell, then reopen. Restart Windows if it happens again.":
            case "Restart Windows after installing WSL, then reopen the toolkit.":
                return "Restart Windows, then open the toolkit again.";
            case "No download bytes arrived. Check your internet connection, then reopen the toolkit.":
                return "Check your internet connection, then open the toolkit again.";
            case "Check for an installer or Windows administrator approval prompt. Restart Windows if requested.":
                return "Look for an installer window or a Windows prompt and finish it. Restart Windows if it asks you to.";
            case "Check for a Windows administrator approval prompt. Reconnect the USB cable and reopen the toolkit.":
                return "Look for a Windows prompt and choose Yes. Then unplug Jibo\u2019s USB cable, plug it back in and open the toolkit again.";
        }
        return hint.Trim();
    }
    static readonly string[] KnownHints = {
        "Close other WSL work, run wsl --shutdown in PowerShell, then reopen. Restart Windows if it happens again.",
        "Restart Windows after installing WSL, then reopen the toolkit.",
        "No download bytes arrived. Check your internet connection, then reopen the toolkit.",
        "Check for an installer or Windows administrator approval prompt. Restart Windows if requested.",
        "Check for a Windows administrator approval prompt. Reconnect the USB cable and reopen the toolkit." };
    static ToolkitFailureText F(string tone, string headline, string what, string tryText) {
        var f = new ToolkitFailureText(); f.Tone = tone; f.Headline = headline; f.What = what; f.Try = tryText; return f;
    }
    static string StepTitle(string step) {
        var known = Step(step, "");
        return known == null ? step : known.Title;
    }
    // message: the exact text given to the support dialog; allowWait: stall with Keep waiting.
    public static ToolkitFailureText Failure(string message, bool allowWait) {
        string text = (message ?? "").Replace("\r\n", "\n");
        bool closed = false;
        const string closedPrefix = "You closed the question. This counts as No; no approval was given.\n";
        if (text.StartsWith(closedPrefix)) { closed = true; text = text.Substring(closedPrefix.Length); }
        string step = null, hint = null;
        if (text.StartsWith("Step: ")) {
            int end = text.IndexOf('\n');
            if (end > 0) { step = text.Substring(6, end - 6); text = text.Substring(end + 1); }
        }
        int tryAt = text.LastIndexOf("\nTry: ");
        if (tryAt >= 0) { hint = text.Substring(tryAt + 6); text = text.Substring(0, tryAt); }
        text = text.Trim();
        var stall = System.Text.RegularExpressions.Regex.Match(text, "^(.+?) stalled(?::| after)");
        if (step == null && stall.Success) step = stall.Groups[1].Value;
        var seconds = System.Text.RegularExpressions.Regex.Match(text, "(?:for|after|within) ([0-9.]+) seconds");
        string waited = seconds.Success ? Duration(double.Parse(seconds.Groups[1].Value, System.Globalization.CultureInfo.InvariantCulture)) : null;
        foreach (string known in KnownHints) if (hint == null && text.EndsWith(known)) hint = known;
        string plainTry = PlainTry(hint);
        string declined = closed ? "You closed the question, which counts as Not now." : "You chose Not now.";
        string keepWaiting = "Choose Keep waiting to give it " + (waited == null ? "more time" : "another " + waited) + ", or Stop setup to end setup now.";
        ToolkitFailureText f;
        if (text.StartsWith("WSL setup declined."))
            f = F("info", "Setup stopped", declined + " Windows\u2019 Linux support was not installed, and nothing was changed.", "Open the toolkit again when you are ready to install it.");
        else if (text.StartsWith("Distribution setup declined."))
            f = F("info", "Setup stopped", declined + " Nothing was downloaded or installed.", "Open the toolkit again whenever you are ready to set up the toolkit\u2019s Linux.");
        else if (text.StartsWith("USB setup declined."))
            f = F("info", "Setup stopped", declined + " USB support was not installed.", "The toolkit needs USB support to talk to Jibo. Open the toolkit again when you are ready to install it.");
        else if (text.StartsWith("WSL setup requested. Restart Windows,"))
            f = F("info", "Restart Windows to finish setup", "Windows finished this part of its Linux setup. The toolkit will continue after a restart.", "Save your work, restart Windows, then open " + Exe + " again. The toolkit will check WSL and continue setup; it will not restart this PC for you.");
        else if (text.StartsWith("WSL setup requested."))
            f = F("info", "Open the toolkit again to finish setup", "Windows finished installing its Linux support.", "If Windows asked you to restart, restart first. Then open " + Exe + " again to continue setup.");
        else if (text.StartsWith("usbipd-win setup finished."))
            f = F("info", "Open the toolkit again to finish setup", "USB support (usbipd-win) was installed.", "Close this message, then open " + Exe + " again. Restart Windows first if the installer asked you to.");
        else if (text.IndexOf("canceled by the user", System.StringComparison.OrdinalIgnoreCase) >= 0)
            f = F("info", "Setup stopped", "The Windows permission prompt was declined, so nothing was installed.", "Open the toolkit again and choose Yes when Windows asks for permission.");
        else if (text.StartsWith("Downloading Ubuntu stalled"))
            f = F("stall", "The download seems stuck", "No data has arrived for " + (waited ?? "a while") + ". The internet connection may have dropped.",
                allowWait ? "Check your internet connection, then choose Keep waiting. If nothing arrives for another " + (waited ?? "while") + ", you will be asked again. Stop setup ends setup now."
                          : "Check your internet connection, then open the toolkit again.");
        else if (text.StartsWith("Windows toolkit window could not be verified"))
            f = F("error", "The toolkit window could not open", "Setup could not confirm a visible, responding toolkit window. No robot operation was enabled.",
                "Save and close your other Linux work, restart Windows, then open the toolkit again. Use Copy details if this happens again.");
        else if (text.StartsWith("Windows display is not ready"))
            f = F("error", "Windows could not display the toolkit",
                text.IndexOf("automatic fix", System.StringComparison.Ordinal) >= 0
                    ? "Windows reported WARN:COPY MODE, and the automatic fix didn\u2019t work. The toolkit window was not opened."
                    : "Windows reported WARN:COPY MODE. The toolkit window was not opened.",
                "Save your other Linux work, run wsl --shutdown in PowerShell (this closes all Linux apps), then open the toolkit again. Restart Windows if it still happens.");
        else if (text.StartsWith("Opening the toolkit stalled"))
            f = F("stall", "The toolkit window did not appear", "The toolkit has not opened after " + (waited ?? "a while") + ".",
                allowWait ? keepWaiting + " If you stop, restart Windows before opening the toolkit again." : "Restart Windows, then open the toolkit again.");
        else if (text.IndexOf("administrator command did not finish", System.StringComparison.Ordinal) >= 0)
            f = F("stall", "A Windows installer did not finish", "A Windows installer or permission prompt did not finish.", "Look for an installer window or a Windows prompt and finish it. If there is none, restart Windows and open the toolkit again.");
        else if (stall.Success && allowWait)
            f = F("stall", "Setup is taking longer than expected", StepTitle(step) + " has not finished" + (waited == null ? "." : " after " + waited + "."),
                keepWaiting + (plainTry == null ? "" : " If you stop: " + LowerFirst(plainTry)));
        else if (stall.Success)
            f = F("stall", "Setup stopped", StepTitle(step) + " did not finish" + (waited == null ? " in time." : " within " + waited + "."),
                text.IndexOf("Windows could not stop the command", System.StringComparison.Ordinal) >= 0 ? "Close any installer window, or restart Windows, before trying again." : (plainTry ?? "Restart Windows, then open the toolkit again."));
        else if (text.StartsWith("Ubuntu image checksum mismatch") || text.StartsWith("Ubuntu image size mismatch") || text.StartsWith("Ubuntu download exceeded"))
            f = F("error", "The download was damaged", "The downloaded Ubuntu file did not match the official one, so it was not used. Nothing was installed.", "Open the toolkit again to download it again.");
        else if (text.StartsWith("Ubuntu download failed"))
            f = F("error", "The download failed", "The Ubuntu download did not finish.", "Check your internet connection, then open the toolkit again.");
        else if (text.StartsWith("The private Linux is not ready.") || text.StartsWith("Private Linux did not start") || text.StartsWith("Private Linux compatibility check failed"))
            f = F("error", "The toolkit\u2019s Linux is not working", "The toolkit\u2019s private Linux is installed but did not start correctly.",
                "To set it up again, run wsl --unregister Jibo-DFU-Toolkit in PowerShell, then open the toolkit again. This deletes all files inside that Linux, including any backups saved there.");
        else if (text.StartsWith("Private Linux storage already exists"))
            f = F("error", "An old toolkit Linux folder is in the way", "A folder from an earlier toolkit Linux already exists, so setup stopped instead of replacing it.",
                "If you no longer need the files in it, move that folder somewhere else (its location is in the details), then open the toolkit again.");
        else if (text.StartsWith("USB handoff failed"))
            f = F("error", "USB could not be connected", "The toolkit could not get access to USB, or the Windows permission prompt was declined.", "Open the toolkit again and choose Yes when Windows asks for permission. Unplugging Jibo\u2019s USB cable and plugging it back in can also help.");
        else if (text.StartsWith("Embedded") || text.StartsWith("Invalid embedded revision"))
            f = F("error", "The toolkit download looks incomplete", "Some of the toolkit\u2019s own files are missing or damaged.", "Download " + Exe + " again, then open the new copy.");
        else if (text.StartsWith("The Windows setup question window is unavailable"))
            f = F("error", "The setup window could not open", "Setup could not show its question, so nothing was approved or installed.", "Restart Windows, then open the toolkit again.");
        else if (text.StartsWith("USB support stopped unexpectedly") || text.StartsWith("The WSL USB permission watcher"))
            f = F("error", "USB support stopped", "The toolkit\u2019s USB connection stopped unexpectedly.", "When nothing is being written to Jibo, close the toolkit window, then open the toolkit again.");
        else if (text.StartsWith("Install Microsoft App Installer"))
            f = F("error", "USB support cannot be installed automatically", "This PC is missing Microsoft\u2019s App Installer, which setup uses to install USB support.", "Install App Installer from the Microsoft Store, or install usbipd-win from its GitHub releases page. Then open the toolkit again.");
        else if (text.StartsWith("usbipd-win installer did not finish"))
            f = allowWait
                ? F("stall", "USB support is taking a long time to install", "The usbipd-win installer has not finished after " + (waited ?? "a few minutes") + ".",
                    "If you can see its window, finish it, then choose Keep waiting. If not, choose Stop setup and install it yourself: " + ManualUsb)
                : F("error", "USB support was not installed", "The usbipd-win installer did not finish within " + (waited ?? "a few minutes") + ", so setup stopped.",
                    "Install it yourself: " + ManualUsb + " If a usbipd-win window is still open, finish or close it first.");
        else if (text.StartsWith("usbipd-win installer exited with code -1978335230"))
            f = F("error", "USB support cannot be installed automatically", "Microsoft\u2019s App Installer on this PC is too old to install USB support.", "Install it yourself: " + ManualUsb);
        else if (text.StartsWith("usbipd-win setup returned")) {
            int checkAt = text.IndexOf(" Check usbipd --version", System.StringComparison.Ordinal);
            f = F("error", "USB support is not ready", checkAt < 0 ? text : text.Substring(0, checkAt),
                "Open a fresh PowerShell window and run usbipd --version. Share its output and these details when asking for help. No USB connection was attempted.");
        }
        else if (text.StartsWith("usbipd-win installer exited with code") && text.IndexOf("Restart Windows as requested", System.StringComparison.Ordinal) >= 0)
            f = F("info", "Restart Windows to finish USB setup", text.Substring(0, text.IndexOf('.') + 1), "Restart Windows as requested by the usbipd-win installer, then open the toolkit again.");
        else if (text.StartsWith("usbipd-win installer"))
            f = F("error", "USB support setup stopped", text, "Check the usbipd-win installer and Windows permission prompt. Share these details if it keeps failing.");
        else if (text.StartsWith("Setup exited with code"))
            f = F("error", "A Windows installer reported a problem", "The installer stopped with an error.", "Restart Windows if it asked you to, then open the toolkit again.");
        else if (text.StartsWith("GUI or USB support exited with code"))
            f = F("error", "The toolkit closed with an error", "The toolkit or its USB support stopped with an error.", "Open the toolkit again.");
        else if (text.StartsWith("Cannot create a setup log"))
            f = F("error", "Setup could not start", "The toolkit could not save its setup log.", "Make sure the drive has free space, then open the toolkit again.");
        else {
            f = F(allowWait ? "stall" : "error", allowWait ? "Setup is taking longer than expected" : "Setup stopped",
                (step == null ? "Setup" : StepTitle(step)) + (allowWait ? " has not finished yet." : " did not work."),
                allowWait ? keepWaiting : (plainTry ?? "Open the toolkit again. If it happens again, open a support ticket in #help on the Phoenix Discord."));
        }
        f.Step = step == null ? null : StepTitle(step);
        f.StepKey = step;
        f.Decision = allowWait;
        return f;
    }
    static string LowerFirst(string text) { return text.Length > 1 && char.IsLower(text[1]) ? char.ToLowerInvariant(text[0]) + text.Substring(1) : text; }
    // "While downloading the toolkit's Linux": step titles all start with a verb.
    public static string During(string stepTitle) { return string.IsNullOrEmpty(stepTitle) ? "" : "While " + LowerFirst(stepTitle); }
}
'@
# The Windows setup window and support dialog. Compiled together with the model
# above; previews (render-setup-previews.ps1) compile this exact source.
$script:ToolkitSetupUiSource = @'
using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Threading;
using System.Windows.Forms;
using System.Runtime.InteropServices;
using System.Collections.Concurrent;
// ---- Theme: palette, DPI-scaled fonts, drawing helpers. Presentation only. ----
// Same palette as the toolkit window (jibo_gui.py) so setup and the toolkit look like
// one app. High contrast uses Windows' system colors throughout.
public static class ToolkitTheme {
    public static int ForcedDpi;          // Previews only: render at 144/192 DPI. 0 = real monitor DPI.
    public static bool ForceHighContrast; // Previews only.
    public static bool ForceReducedMotion;
    public static Rectangle ForcedWorkArea;  // Previews only: the screen a forced DPI stands for.
    public static Rectangle WorkArea(Control control) {
        if (!ForcedWorkArea.IsEmpty) return ForcedWorkArea;
        try { return Screen.FromControl(control).WorkingArea; } catch (Exception) { return new Rectangle(0, 0, 1920, 1040); }
    }
    public static bool HighContrast { get { return ForceHighContrast || SystemInformation.HighContrast; } }
    static Color C(int rgb) { return Color.FromArgb(255, (rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255); }
    // Previews on a machine without high contrast: Windows' "Night sky" colors stand in
    // for the system colors. Real high contrast always uses the system colors.
    static Color Simulated(Color system) {
        if (system == SystemColors.Window) return C(0x000000);
        if (system == SystemColors.WindowText) return C(0xffffff);
        if (system == SystemColors.Highlight) return C(0x8ee3f0);
        if (system == SystemColors.HighlightText) return C(0x263b50);
        if (system == SystemColors.GrayText) return C(0xa6a6a6);
        return system;
    }
    static Color Pick(int rgb, Color system) {
        if (ForceHighContrast && !SystemInformation.HighContrast) return Simulated(system);
        return HighContrast ? system : C(rgb);
    }
    public static Color System(Color system) { return ForceHighContrast && !SystemInformation.HighContrast ? Simulated(system) : system; }
    public static Color Surface { get { return Pick(0xffffff, SystemColors.Window); } }
    public static Color Canvas { get { return Pick(0xf3f5f8, SystemColors.Window); } }
    public static Color Header { get { return Pick(0x141c26, SystemColors.Window); } }
    public static Color HeaderText { get { return Pick(0xeef2f6, SystemColors.WindowText); } }
    public static Color HeaderMuted { get { return Pick(0x97a5b6, SystemColors.WindowText); } }
    public static Color HeaderLine { get { return Pick(0x2f3b49, SystemColors.WindowText); } }
    public static Color Text { get { return Pick(0x17202b, SystemColors.WindowText); } }
    public static Color Muted { get { return Pick(0x566273, SystemColors.WindowText); } }
    public static Color Border { get { return Pick(0xd7dde5, SystemColors.WindowText); } }
    public static Color Trough { get { return Pick(0xdfe5ec, SystemColors.Window); } }
    public static Color Disabled { get { return Pick(0xa0aab6, SystemColors.GrayText); } }
    public static Color Accent { get { return Pick(0x2563c9, SystemColors.Highlight); } }
    public static Color AccentPressed { get { return Pick(0x1c4fa6, SystemColors.Highlight); } }
    public static Color AccentText { get { return Pick(0xffffff, SystemColors.HighlightText); } }
    public static Color AccentSoft { get { return Pick(0xe8effc, SystemColors.Window); } }
    public static Color AccentInk { get { return Pick(0x1d4ea3, SystemColors.WindowText); } }
    public static Color Attention { get { return Pick(0xb45309, SystemColors.Highlight); } }
    public static Color AttentionSoft { get { return Pick(0xfdf3e1, SystemColors.Window); } }
    public static Color AttentionInk { get { return Pick(0x8a3f06, SystemColors.WindowText); } }
    public static Color Danger { get { return Pick(0xc4302b, SystemColors.Highlight); } }
    public static Color DangerSoft { get { return Pick(0xfcebea, SystemColors.Window); } }
    public static Color DangerInk { get { return Pick(0xa2231f, SystemColors.WindowText); } }
    public static Color Success { get { return Pick(0x17804f, SystemColors.Highlight); } }
    public static Color Hover { get { return Pick(0xf0f4fa, SystemColors.Window); } }
    public static Color Pressed { get { return Pick(0xe3e9f1, SystemColors.Window); } }
    public static Color ToneColor(string tone) { return tone == "attention" || tone == "stall" ? Attention : tone == "error" ? Danger : tone == "done" ? Success : Accent; }
    public static Color ToneSoft(string tone) { return tone == "attention" || tone == "stall" ? AttentionSoft : tone == "error" ? DangerSoft : tone == "neutral" ? Canvas : AccentSoft; }
    public static Color ToneInk(string tone) { return tone == "attention" || tone == "stall" ? AttentionInk : tone == "error" ? DangerInk : tone == "neutral" ? Muted : AccentInk; }
    public static int S(int logical, int dpi) { return (int)Math.Round(logical * dpi / 96.0); }
    [DllImport("user32.dll")] static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll")] static extern uint GetDpiForWindow(IntPtr hwnd);
    [DllImport("user32.dll")] static extern bool SystemParametersInfo(uint action, uint param, ref bool value, uint flags);
    // Crisp text at 125-200%: windows created on this thread follow each monitor's DPI
    // (Windows 10 1703+, else 1607+). Older Windows keeps its own bitmap scaling.
    public static void UsePerMonitorDpi() {
        try { if (SetThreadDpiAwarenessContext(new IntPtr(-4)) == IntPtr.Zero) SetThreadDpiAwarenessContext(new IntPtr(-3)); }
        catch (Exception) { }
        try { Application.EnableVisualStyles(); } catch (Exception) { } // Modern scroll bars and text box.
    }
    public static int DpiOf(Control control) {
        if (ForcedDpi > 0) return ForcedDpi;
        try { if (control.IsHandleCreated) { uint dpi = GetDpiForWindow(control.Handle); if (dpi >= 72) return (int)dpi; } }
        catch (Exception) { }
        try { using (var g = control.CreateGraphics()) return Math.Max(96, (int)Math.Round(g.DpiX)); }
        catch (Exception) { return 96; }
    }
    public static bool ReducedMotion {
        get {
            if (ForceReducedMotion) return true;
            try { bool on = true; return SystemParametersInfo(0x1042, 0, ref on, 0) && !on; } // SPI_GETCLIENTAREAANIMATION
            catch (Exception) { return false; }
        }
    }
    public const TextFormatFlags Wrap = TextFormatFlags.WordBreak | TextFormatFlags.NoPrefix | TextFormatFlags.TextBoxControl | TextFormatFlags.NoPadding;
    public const TextFormatFlags Line = TextFormatFlags.SingleLine | TextFormatFlags.NoPrefix | TextFormatFlags.NoPadding | TextFormatFlags.VerticalCenter;
    public static int TextHeight(string text, Font font, int width) {
        if (String.IsNullOrEmpty(text)) return 0;
        // Bounded: odd font metrics must never overflow the layout arithmetic.
        return Math.Min(4000, TextRenderer.MeasureText(text, font, new Size(Math.Max(1, width), 100000), Wrap).Height);
    }
    public static int TextWidth(string text, Font font) {
        return String.IsNullOrEmpty(text) ? 0 : Math.Min(4000, TextRenderer.MeasureText(text, font, new Size(100000, 100000), Line).Width);
    }
    public static GraphicsPath Rounded(Rectangle r, int radius) {
        var path = new GraphicsPath();
        int d = Math.Max(1, Math.Min(radius * 2, Math.Min(r.Width, r.Height)));
        path.AddArc(r.X, r.Y, d, d, 180, 90);
        path.AddArc(r.Right - d, r.Y, d, d, 270, 90);
        path.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90);
        path.AddArc(r.X, r.Bottom - d, d, d, 90, 90);
        path.CloseFigure();
        return path;
    }
    public static void Fill(Graphics g, Color color, Rectangle r, int radius) {
        using (var path = Rounded(r, radius)) using (var brush = new SolidBrush(color)) g.FillPath(brush, path);
    }
    public static void Outline(Graphics g, Color color, Rectangle r, int radius, float width) {
        using (var path = Rounded(r, radius)) using (var pen = new Pen(color, width)) g.DrawPath(pen, path);
    }
    public static void Check(Graphics g, Color color, Rectangle r, float width) {
        using (var pen = new Pen(color, width)) {
            pen.StartCap = pen.EndCap = LineCap.Round; pen.LineJoin = LineJoin.Round;
            g.DrawLines(pen, new PointF[] {
                new PointF(r.X + r.Width * 0.22f, r.Y + r.Height * 0.52f),
                new PointF(r.X + r.Width * 0.42f, r.Y + r.Height * 0.72f),
                new PointF(r.X + r.Width * 0.78f, r.Y + r.Height * 0.30f) });
        }
    }
    // Round badge with a glyph: "!" attention, "i" information, "x" error, "?" question, "pause".
    public static void Badge(Graphics g, Rectangle r, Color fill, Color ink, string glyph) {
        using (var brush = new SolidBrush(fill)) g.FillEllipse(brush, r);
        float w = Math.Max(1.5f, r.Width / 9f);
        using (var pen = new Pen(ink, w)) using (var dot = new SolidBrush(ink)) {
            pen.StartCap = pen.EndCap = LineCap.Round;
            float cx = r.X + r.Width / 2f, cy = r.Y + r.Height / 2f, u = r.Width / 10f;
            if (glyph == "!") { g.DrawLine(pen, cx, cy - 2.6f * u, cx, cy + 0.6f * u); g.FillEllipse(dot, cx - 0.65f * u, cy + 1.7f * u, 1.3f * u, 1.3f * u); }
            else if (glyph == "i") { g.DrawLine(pen, cx, cy - 0.6f * u, cx, cy + 2.6f * u); g.FillEllipse(dot, cx - 0.65f * u, cy - 2.9f * u, 1.3f * u, 1.3f * u); }
            else if (glyph == "x") { g.DrawLine(pen, cx - 2f * u, cy - 2f * u, cx + 2f * u, cy + 2f * u); g.DrawLine(pen, cx + 2f * u, cy - 2f * u, cx - 2f * u, cy + 2f * u); }
            else if (glyph == "pause") { g.DrawLine(pen, cx - 1.2f * u, cy - 2.2f * u, cx - 1.2f * u, cy + 2.2f * u); g.DrawLine(pen, cx + 1.2f * u, cy - 2.2f * u, cx + 1.2f * u, cy + 2.2f * u); }
            else if (glyph == "check") Check(g, ink, Rectangle.Round(new RectangleF(r.X + r.Width * 0.12f, r.Y + r.Height * 0.12f, r.Width * 0.76f, r.Height * 0.76f)), w);
            else if (glyph == "?") {
                using (var font = new Font("Segoe UI Semibold", Math.Max(6f, r.Height * 0.62f), FontStyle.Regular, GraphicsUnit.Pixel))
                    TextRenderer.DrawText(g, "?", font, r, ink, Line | TextFormatFlags.HorizontalCenter);
            }
        }
    }
    [DllImport("comctl32.dll")] static extern int LoadIconWithScaleDown(IntPtr instance, IntPtr name, int cx, int cy, out IntPtr icon);
    [DllImport("user32.dll")] static extern bool DestroyIcon(IntPtr icon);
    public static void Shield(Graphics g, Rectangle r) {
        try {
            IntPtr handle; // IDI_SHIELD at the exact pixel size: crisp at 150% and 200%.
            if (LoadIconWithScaleDown(IntPtr.Zero, new IntPtr(32518), r.Width, r.Height, out handle) == 0 && handle != IntPtr.Zero) {
                try { using (var icon = Icon.FromHandle(handle)) g.DrawIcon(icon, r); } finally { DestroyIcon(handle); }
                return;
            }
        } catch (Exception) { }
        try { using (var icon = new Icon(SystemIcons.Shield, r.Size)) g.DrawIcon(icon, r); }
        catch (Exception) { Badge(g, r, Accent, AccentText, "!"); }
    }
}
// Fonts in pixels for one DPI, so text is the same physical size on every monitor.
public sealed class ToolkitFonts : IDisposable {
    public readonly int Dpi;
    public readonly Font Brand, Title, Body, BodyBold, Small, SmallBold, Caps, Button, Mono;
    static Font Make(string family, float points, int dpi) { return new Font(family, points * dpi / 72f, FontStyle.Regular, GraphicsUnit.Pixel); }
    public ToolkitFonts(int dpi) {
        Dpi = dpi;
        Brand = Make("Segoe UI Semibold", 10.5f, dpi); Title = Make("Segoe UI Semibold", 15f, dpi);
        Body = Make("Segoe UI", 10f, dpi); BodyBold = Make("Segoe UI Semibold", 10f, dpi);
        Small = Make("Segoe UI", 9f, dpi); SmallBold = Make("Segoe UI Semibold", 9f, dpi);
        Caps = Make("Segoe UI Semibold", 8f, dpi); Button = Make("Segoe UI Semibold", 10f, dpi);
        Mono = Make("Consolas", 9f, dpi);
    }
    public int S(int logical) { return ToolkitTheme.S(logical, Dpi); }
    // WinForms may retain an equal existing Font rather than the newly assigned
    // instance. Clear explicit references before assigning a new owned set;
    // inherited fonts then follow the new parent instead of a retired owner.
    public static void Detach(Control root) {
        root.Font = null;
        foreach (Control child in root.Controls) Detach(child);
    }
    public void Dispose() { foreach (var f in new Font[] { Brand, Title, Body, BodyBold, Small, SmallBold, Caps, Button, Mono }) f.Dispose(); }
}
// Static text measured and painted with the same flags, so wrapped text is never clipped.
public sealed class ToolkitText : Control {
    public TextFormatFlags Flags = ToolkitTheme.Wrap;
    public ToolkitText() {
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true);
        SetStyle(ControlStyles.Selectable, false);
        TabStop = false; AccessibleRole = AccessibleRole.StaticText;
    }
    public int Fit(int x, int y, int width) {
        int h = Text.Length > 0 ? ToolkitTheme.TextHeight(Text, Font, width) + 2 : 0;
        SetBounds(x, y, width, Math.Max(1, h));
        return h;
    }
    protected override void OnTextChanged(EventArgs e) { base.OnTextChanged(e); Invalidate(); }
    protected override void OnPaint(PaintEventArgs e) {
        // Opaque, in the parent's color: no transparency, so printing and captures match.
        using (var b = new SolidBrush(Parent != null ? Parent.BackColor : ToolkitTheme.Surface)) e.Graphics.FillRectangle(b, ClientRectangle);
        TextRenderer.DrawText(e.Graphics, Text, Font, ClientRectangle, ForeColor, Flags);
    }
    public void Announce() { AccessibilityNotifyClients(AccessibleEvents.NameChange, -1); AccessibilityNotifyClients(AccessibleEvents.SystemAlert, -1); }
}
// Flat button: "primary" (filled), "secondary" (outlined), "default" (the safe choice
// that Enter presses: soft accent fill, 2 px accent edge). Keeps Button semantics:
// AcceptButton/CancelButton, PerformClick, Enabled, keyboard and screen readers.
public sealed class ToolkitButton : Button {
    public string Kind = "secondary";
    public bool Shield, Done; // Done: a drawn check mark before the text (no font glyph needed).
    bool hover, down;
    public ToolkitButton() {
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true);
        UseMnemonic = false; // A typed letter must never press a setup button.
        FlatStyle = FlatStyle.Flat; Cursor = Cursors.Hand;
    }
    public int Measure(Font font, int dpi) {
        int text = ToolkitTheme.TextWidth(Text, font);
        return Math.Max(ToolkitTheme.S(112, dpi), text + ToolkitTheme.S(Shield || Done ? 58 : 36, dpi));
    }
    protected override void OnMouseEnter(EventArgs e) { hover = true; Invalidate(); base.OnMouseEnter(e); }
    protected override void OnMouseLeave(EventArgs e) { hover = down = false; Invalidate(); base.OnMouseLeave(e); }
    protected override void OnMouseDown(MouseEventArgs e) { down = true; Invalidate(); base.OnMouseDown(e); }
    protected override void OnMouseUp(MouseEventArgs e) { down = false; Invalidate(); base.OnMouseUp(e); }
    protected override void OnEnabledChanged(EventArgs e) { Invalidate(); base.OnEnabledChanged(e); }
    protected override void OnTextChanged(EventArgs e) { Invalidate(); base.OnTextChanged(e); }
    protected override void OnPaint(PaintEventArgs e) {
        var g = e.Graphics; g.SmoothingMode = SmoothingMode.AntiAlias;
        Color parent = Parent != null ? Parent.BackColor : ToolkitTheme.Surface;
        using (var b = new SolidBrush(parent)) g.FillRectangle(b, ClientRectangle);
        int dpi = ToolkitTheme.DpiOf(this); int radius = ToolkitTheme.S(4, dpi);
        var r = new Rectangle(0, 0, Width - 1, Height - 1);
        bool primary = Kind == "primary", safe = Kind == "default" || (IsDefault && !primary);
        bool hc = ToolkitTheme.HighContrast;
        Color fill, ink, line;
        if (!Enabled) { fill = primary && !hc ? ToolkitTheme.Disabled : ToolkitTheme.Surface; ink = primary && !hc ? ToolkitTheme.AccentText : ToolkitTheme.Disabled; line = ToolkitTheme.Disabled; }
        else if (primary) { fill = down ? ToolkitTheme.AccentPressed : ToolkitTheme.Accent; ink = ToolkitTheme.AccentText; line = fill; }
        else if (safe) { fill = down ? ToolkitTheme.Pressed : hover ? ToolkitTheme.Hover : ToolkitTheme.AccentSoft; ink = ToolkitTheme.AccentInk; line = ToolkitTheme.Accent; }
        else { fill = down ? ToolkitTheme.Pressed : hover ? ToolkitTheme.Hover : ToolkitTheme.Surface; ink = ToolkitTheme.Text; line = hover && !hc ? ToolkitTheme.Muted : ToolkitTheme.Border; }
        if (hc && Enabled && (hover || Focused)) { fill = ToolkitTheme.System(SystemColors.Highlight); ink = ToolkitTheme.System(SystemColors.HighlightText); line = ToolkitTheme.System(SystemColors.HighlightText); }
        ToolkitTheme.Fill(g, fill, r, radius);
        float width = safe && Enabled ? ToolkitTheme.S(2, dpi) : Math.Max(1, ToolkitTheme.S(1, dpi));
        Color edge = line;
        var inset = Rectangle.Inflate(r, -(int)(width / 2), -(int)(width / 2));
        ToolkitTheme.Outline(g, edge, inset, radius, width);
        var content = new Rectangle(0, 0, Width, Height);
        if (Shield || Done) {
            int icon = ToolkitTheme.S(16, dpi);
            int textWidth = ToolkitTheme.TextWidth(Text, Font);
            int start = (Width - icon - ToolkitTheme.S(8, dpi) - textWidth) / 2;
            var box = new Rectangle(start, (Height - icon) / 2, icon, icon);
            if (Shield) ToolkitTheme.Shield(g, box); else ToolkitTheme.Check(g, ink, box, Math.Max(1.5f, ToolkitTheme.S(2, dpi)));
            content = new Rectangle(start + icon + ToolkitTheme.S(8, dpi), 0, textWidth + 2, Height);
            TextRenderer.DrawText(g, Text, Font, content, ink, ToolkitTheme.Line);
        } else TextRenderer.DrawText(g, Text, Font, content, ink, ToolkitTheme.Line | TextFormatFlags.HorizontalCenter);
        if (Focused && ShowFocusCues) {
            var focus = Rectangle.Inflate(r, -ToolkitTheme.S(3, dpi), -ToolkitTheme.S(3, dpi));
            using (var pen = new Pen(primary ? ToolkitTheme.AccentText : ToolkitTheme.Text, 1)) { pen.DashStyle = DashStyle.Dot; g.DrawRectangle(pen, focus); }
        }
    }
}
// Accent bar on a rounded track. Marquee animates; Windows' reduced-motion setting
// draws a calm static stripe instead. Still a real ProgressBar for screen readers.
public sealed class ToolkitProgress : ProgressBar {
    readonly System.Windows.Forms.Timer animation = new System.Windows.Forms.Timer();
    readonly System.Diagnostics.Stopwatch since = System.Diagnostics.Stopwatch.StartNew();
    public double Frame = -1; // Previews pin the marquee position (0..1).
    public ToolkitProgress() {
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true);
        animation.Interval = 33;
        animation.Tick += delegate { if (Style == ProgressBarStyle.Marquee && Visible) Invalidate(); };
        animation.Start();
    }
    protected override void Dispose(bool disposing) { if (disposing) animation.Dispose(); base.Dispose(disposing); }
    protected override void OnPaint(PaintEventArgs e) {
        var g = e.Graphics; g.SmoothingMode = SmoothingMode.AntiAlias;
        Color parent = Parent != null ? Parent.BackColor : ToolkitTheme.Surface;
        using (var b = new SolidBrush(parent)) g.FillRectangle(b, ClientRectangle);
        var track = new Rectangle(0, 0, Width - 1, Height - 1);
        int radius = Height / 2;
        ToolkitTheme.Fill(g, ToolkitTheme.Trough, track, radius);
        if (ToolkitTheme.HighContrast) ToolkitTheme.Outline(g, ToolkitTheme.System(SystemColors.WindowText), track, radius, 1);
        Rectangle bar;
        if (Style == ProgressBarStyle.Marquee) {
            int segment = Math.Max(Height * 4, Width / 4);
            if (ToolkitTheme.ReducedMotion && Frame < 0) {
                using (var hatch = new HatchBrush(HatchStyle.WideUpwardDiagonal, ToolkitTheme.Accent, ToolkitTheme.Trough))
                using (var path = ToolkitTheme.Rounded(track, radius)) g.FillPath(hatch, path);
                return;
            }
            double t = Frame >= 0 ? Frame : (since.ElapsedMilliseconds % 1600) / 1600.0;
            double eased = 0.5 - Math.Cos(Math.PI * t) / 2;
            int x = (int)(-segment + eased * (Width + segment));
            bar = new Rectangle(x, 0, segment, Height - 1);
            using (var clip = ToolkitTheme.Rounded(track, radius)) {
                g.SetClip(clip);
                ToolkitTheme.Fill(g, ToolkitTheme.Accent, bar, radius);
                g.ResetClip();
            }
            return;
        }
        int span = Maximum - Minimum;
        if (span <= 0 || Value <= Minimum) return;
        int w = (int)Math.Round((double)(Width - 1) * (Value - Minimum) / span);
        bar = new Rectangle(0, 0, Math.Max(Height, w), Height - 1);
        ToolkitTheme.Fill(g, ToolkitTheme.Accent, bar, radius);
    }
}
// The five setup phases: done (check), now (number, filled), later (outlined number).
public sealed class ToolkitPhaseRail : Control {
    int current = 0;
    bool attention;
    public ToolkitFonts Fonts;
    public ToolkitPhaseRail() {
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true);
        SetStyle(ControlStyles.Selectable, false); TabStop = false;
        AccessibleRole = AccessibleRole.StaticText;
        SetPhase(0, false);
    }
    public int Current { get { return current; } }
    public void SetPhase(int phase, bool needsYou) {
        phase = Math.Max(0, Math.Min(ToolkitSetupText.Phases.Length - 1, phase));
        if (phase == current && needsYou == attention && AccessibleName != null) return;
        current = phase; attention = needsYou;
        AccessibleName = "Setup step " + (current + 1) + " of " + ToolkitSetupText.Phases.Length + ": " + ToolkitSetupText.Phases[current];
        Invalidate();
    }
    protected override void OnPaint(PaintEventArgs e) {
        if (Fonts == null) return;
        var g = e.Graphics; g.SmoothingMode = SmoothingMode.AntiAlias;
        using (var b = new SolidBrush(BackColor)) g.FillRectangle(b, ClientRectangle);
        int n = ToolkitSetupText.Phases.Length;
        int d = Fonts.S(22), slot = Width / n, line = Math.Max(1, Fonts.S(2));
        for (int i = 0; i < n; i++) {
            int cx = slot * i + slot / 2;
            var circle = new Rectangle(cx - d / 2, 0, d, d);
            if (i < n - 1) {
                using (var pen = new Pen(i < current ? ToolkitTheme.Accent : ToolkitTheme.HeaderLine, line))
                    g.DrawLine(pen, cx + d / 2 + Fonts.S(6), d / 2, cx + slot - d / 2 - Fonts.S(6), d / 2);
            }
            Color tone = attention ? ToolkitTheme.Attention : ToolkitTheme.Accent;
            if (i < current) ToolkitTheme.Badge(g, circle, ToolkitTheme.Accent, ToolkitTheme.AccentText, "check");
            else if (i == current) {
                using (var brush = new SolidBrush(tone)) g.FillEllipse(brush, circle);
                TextRenderer.DrawText(g, (i + 1).ToString(), Fonts.SmallBold, circle, ToolkitTheme.AccentText, ToolkitTheme.Line | TextFormatFlags.HorizontalCenter);
            } else {
                using (var pen = new Pen(ToolkitTheme.HeaderLine, line)) g.DrawEllipse(pen, Rectangle.Inflate(circle, -line / 2 - 1, -line / 2 - 1));
                TextRenderer.DrawText(g, (i + 1).ToString(), Fonts.SmallBold, circle, ToolkitTheme.HeaderMuted, ToolkitTheme.Line | TextFormatFlags.HorizontalCenter);
            }
            var label = new Rectangle(slot * i, d + Fonts.S(6), slot, Height - d - Fonts.S(6));
            TextRenderer.DrawText(g, ToolkitSetupText.Phases[i], i == current ? Fonts.SmallBold : Fonts.Small, label,
                i <= current ? ToolkitTheme.HeaderText : ToolkitTheme.HeaderMuted, ToolkitTheme.Line | TextFormatFlags.HorizontalCenter | TextFormatFlags.Top);
        }
    }
}
// Status pill: what the window needs from the person right now. A spinner while
// working (calm static dot with reduced motion), a glyph while waiting for them.
public sealed class ToolkitChip : Control {
    public string Tone = "working", Glyph = "";
    public ToolkitFonts Fonts;
    public double Frame = -1;
    readonly System.Windows.Forms.Timer animation = new System.Windows.Forms.Timer();
    readonly System.Diagnostics.Stopwatch since = System.Diagnostics.Stopwatch.StartNew();
    public ToolkitChip() {
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.SupportsTransparentBackColor, true);
        SetStyle(ControlStyles.Selectable, false); TabStop = false; AccessibleRole = AccessibleRole.StaticText;
        animation.Interval = 50;
        animation.Tick += delegate { if (Tone == "working" && Visible) Invalidate(); };
        animation.Start();
    }
    protected override void Dispose(bool disposing) { if (disposing) animation.Dispose(); base.Dispose(disposing); }
    public int Fit(int x, int y, int maxWidth) {
        if (Fonts == null) return 0;
        int h = Fonts.S(28);
        SetBounds(x, y, Math.Min(maxWidth, h + ToolkitTheme.TextWidth(Text, Fonts.SmallBold) + Fonts.S(14)), h);
        return h;
    }
    protected override void OnTextChanged(EventArgs e) { base.OnTextChanged(e); Invalidate(); }
    public void Announce() { AccessibilityNotifyClients(AccessibleEvents.NameChange, -1); AccessibilityNotifyClients(AccessibleEvents.SystemAlert, -1); }
    protected override void OnPaint(PaintEventArgs e) {
        if (Fonts == null) return;
        var g = e.Graphics; g.SmoothingMode = SmoothingMode.AntiAlias;
        Color parent = Parent != null ? Parent.BackColor : ToolkitTheme.Surface;
        using (var b = new SolidBrush(parent)) g.FillRectangle(b, ClientRectangle);
        var r = new Rectangle(0, 0, Width - 1, Height - 1);
        Color tone = ToolkitTheme.ToneColor(Tone), soft = ToolkitTheme.ToneSoft(Tone), ink = ToolkitTheme.ToneInk(Tone);
        ToolkitTheme.Fill(g, soft, r, Height / 2);
        if (ToolkitTheme.HighContrast) ToolkitTheme.Outline(g, ToolkitTheme.System(SystemColors.WindowText), r, Height / 2, 1);
        int icon = Fonts.S(16), pad = (Height - icon) / 2;
        var box = new Rectangle(pad + Fonts.S(2), pad, icon, icon);
        if (Tone == "working") {
            float w = Math.Max(2f, Fonts.S(2));
            var ring = Rectangle.Inflate(box, -(int)w / 2 - 1, -(int)w / 2 - 1);
            using (var pen = new Pen(Color.FromArgb(ToolkitTheme.HighContrast ? 255 : 90, tone), w)) g.DrawEllipse(pen, ring);
            if (!ToolkitTheme.ReducedMotion || Frame >= 0) {
                double t = Frame >= 0 ? Frame : (since.ElapsedMilliseconds % 1000) / 1000.0;
                using (var pen = new Pen(tone, w)) { pen.StartCap = pen.EndCap = LineCap.Round; g.DrawArc(pen, ring, (float)(t * 360 - 90), 100); }
            }
        } else if (Glyph == "neutral") {
            using (var brush = new SolidBrush(ToolkitTheme.Muted)) g.FillEllipse(brush, Rectangle.Inflate(box, -Fonts.S(4), -Fonts.S(4)));
        } else ToolkitTheme.Badge(g, box, tone, ToolkitTheme.AccentText, Glyph);
        var text = new Rectangle(box.Right + Fonts.S(8), 0, Width - box.Right - Fonts.S(8), Height);
        TextRenderer.DrawText(g, Text, Fonts.SmallBold, text, ink, ToolkitTheme.Line | TextFormatFlags.EndEllipsis);
    }
}
// A soft rounded box behind grouped content (what happens / numbered steps).
public sealed class ToolkitPanel : Panel {
    public Color Fill = Color.Empty, Line = Color.Empty;
    public int Radius = 6;
    public ToolkitPanel() { SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true); }
    protected override void OnPaint(PaintEventArgs e) {
        var g = e.Graphics; g.SmoothingMode = SmoothingMode.AntiAlias;
        Color parent = Parent != null ? Parent.BackColor : ToolkitTheme.Surface;
        using (var b = new SolidBrush(parent)) g.FillRectangle(b, ClientRectangle);
        var r = new Rectangle(0, 0, Width - 1, Height - 1);
        ToolkitTheme.Fill(g, Fill, r, Radius);
        if (!Line.IsEmpty) ToolkitTheme.Outline(g, Line, r, Radius, 1);
    }
}
// Label/value pairs in a soft box: "What happens", "How long".
public sealed class ToolkitFacts : Control {
    public ToolkitFonts Fonts;
    public string[] Labels = new string[0], Values = new string[0];
    public ToolkitFacts() {
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);
        SetStyle(ControlStyles.Selectable, false); TabStop = false; AccessibleRole = AccessibleRole.StaticText;
    }
    public void Set(string[] labels, string[] values) {
        Labels = labels; Values = values;
        var parts = new System.Collections.Generic.List<string>();
        for (int i = 0; i < labels.Length; i++) if (!String.IsNullOrEmpty(values[i])) parts.Add(labels[i] + ": " + values[i]);
        AccessibleName = String.Join(" ", parts.ToArray());
        Invalidate();
    }
    int Pad { get { return Fonts.S(14); } }
    public int Measure(int width) {
        int h = Pad, inner = width - 2 * Pad;
        for (int i = 0; i < Labels.Length; i++) {
            if (String.IsNullOrEmpty(Values[i])) continue;
            if (h > Pad) h += Fonts.S(10);
            h += Fonts.Caps.Height + Fonts.S(2) + ToolkitTheme.TextHeight(Values[i], Fonts.Body, inner);
        }
        return h + Pad;
    }
    protected override void OnPaint(PaintEventArgs e) {
        if (Fonts == null) return;
        var g = e.Graphics; g.SmoothingMode = SmoothingMode.AntiAlias;
        Color parent = Parent != null ? Parent.BackColor : ToolkitTheme.Surface;
        using (var b = new SolidBrush(parent)) g.FillRectangle(b, ClientRectangle);
        var box = new Rectangle(0, 0, Width - 1, Height - 1);
        ToolkitTheme.Fill(g, ToolkitTheme.Canvas, box, Fonts.S(6));
        ToolkitTheme.Outline(g, ToolkitTheme.Border, box, Fonts.S(6), 1);
        int y = Pad, inner = Width - 2 * Pad;
        for (int i = 0; i < Labels.Length; i++) {
            if (String.IsNullOrEmpty(Values[i])) continue;
            if (y > Pad) y += Fonts.S(10);
            TextRenderer.DrawText(g, Labels[i].ToUpperInvariant(), Fonts.Caps, new Rectangle(Pad, y, inner, Fonts.Caps.Height), ToolkitTheme.Muted, ToolkitTheme.Line);
            y += Fonts.Caps.Height + Fonts.S(2);
            int h = ToolkitTheme.TextHeight(Values[i], Fonts.Body, inner);
            TextRenderer.DrawText(g, Values[i], Fonts.Body, new Rectangle(Pad, y, inner, h + 2), ToolkitTheme.Text, ToolkitTheme.Wrap);
            y += h;
        }
    }
}
// Numbered "do this" steps for a person, in the attention color.
public sealed class ToolkitSteps : Control {
    public ToolkitFonts Fonts;
    public string[] Items = new string[0];
    public ToolkitSteps() {
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw, true);
        SetStyle(ControlStyles.Selectable, false); TabStop = false; AccessibleRole = AccessibleRole.StaticText;
    }
    public void Set(string[] items) {
        Items = items ?? new string[0];
        var parts = new string[Items.Length];
        for (int i = 0; i < Items.Length; i++) parts[i] = (Items.Length > 1 ? (i + 1) + ". " : "") + Items[i];
        AccessibleName = String.Join(" ", parts);
        Invalidate();
    }
    int Pad { get { return Fonts.S(14); } }
    int Number { get { return Fonts.S(22); } }
    int TextLeft { get { return Items.Length > 1 ? Pad + Number + Fonts.S(12) : Pad; } }
    public int Measure(int width) {
        int h = Pad, inner = width - TextLeft - Pad;
        for (int i = 0; i < Items.Length; i++) {
            if (i > 0) h += Fonts.S(8);
            h += Math.Max(Items.Length > 1 ? Number : 0, ToolkitTheme.TextHeight(Items[i], Fonts.Body, inner));
        }
        return h + Pad;
    }
    protected override void OnPaint(PaintEventArgs e) {
        if (Fonts == null) return;
        var g = e.Graphics; g.SmoothingMode = SmoothingMode.AntiAlias;
        Color parent = Parent != null ? Parent.BackColor : ToolkitTheme.Surface;
        using (var b = new SolidBrush(parent)) g.FillRectangle(b, ClientRectangle);
        var box = new Rectangle(0, 0, Width - 1, Height - 1);
        ToolkitTheme.Fill(g, ToolkitTheme.AttentionSoft, box, Fonts.S(6));
        if (ToolkitTheme.HighContrast) ToolkitTheme.Outline(g, ToolkitTheme.System(SystemColors.WindowText), box, Fonts.S(6), 1);
        int y = Pad, left = TextLeft, inner = Width - left - Pad;
        for (int i = 0; i < Items.Length; i++) {
            if (i > 0) y += Fonts.S(8);
            int h = ToolkitTheme.TextHeight(Items[i], Fonts.Body, inner);
            int row = Math.Max(Items.Length > 1 ? Number : 0, h);
            if (Items.Length > 1) {
                var circle = new Rectangle(Pad, y + Math.Max(0, (Math.Min(row, Fonts.Body.Height + Fonts.S(4)) - Number) / 2), Number, Number);
                using (var brush = new SolidBrush(ToolkitTheme.Attention)) g.FillEllipse(brush, circle);
                TextRenderer.DrawText(g, (i + 1).ToString(), Fonts.SmallBold, circle, ToolkitTheme.AccentText, ToolkitTheme.Line | TextFormatFlags.HorizontalCenter);
            }
            int ty = y + (row > h ? (Math.Min(row, Number) - Math.Min(h, Fonts.Body.Height + 2)) / 2 : 0);
            TextRenderer.DrawText(g, Items[i], Fonts.Body, new Rectangle(left, ty, inner, h + 2), ToolkitTheme.Text, ToolkitTheme.Wrap);
            y += row;
        }
    }
}
// "Where to look": the taskbar shield icon next to a short hint.
public sealed class ToolkitHint : Control {
    public ToolkitFonts Fonts;
    public ToolkitHint() {
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.ResizeRedraw | ControlStyles.SupportsTransparentBackColor, true);
        SetStyle(ControlStyles.Selectable, false); TabStop = false; AccessibleRole = AccessibleRole.StaticText;
    }
    int Icon { get { return Fonts.S(20); } }
    public int Measure(int width) {
        if (String.IsNullOrEmpty(Text)) return 0;
        return Math.Max(Icon, ToolkitTheme.TextHeight(Text, Fonts.Body, width - Icon - Fonts.S(10)));
    }
    protected override void OnTextChanged(EventArgs e) { base.OnTextChanged(e); AccessibleName = Text; Invalidate(); }
    protected override void OnPaint(PaintEventArgs e) {
        if (Fonts == null) return;
        var g = e.Graphics; g.SmoothingMode = SmoothingMode.AntiAlias;
        Color parent = Parent != null ? Parent.BackColor : ToolkitTheme.Surface;
        using (var b = new SolidBrush(parent)) g.FillRectangle(b, ClientRectangle);
        int h = ToolkitTheme.TextHeight(Text, Fonts.Body, Width - Icon - Fonts.S(10));
        int iconTop = Math.Max(0, (Math.Min(h, Fonts.Body.Height + 2) - Icon) / 2);
        ToolkitTheme.Shield(g, new Rectangle(0, iconTop, Icon, Icon));
        TextRenderer.DrawText(g, Text, Fonts.Body, new Rectangle(Icon + Fonts.S(10), 0, Width - Icon - Fonts.S(10), h + 2), ToolkitTheme.Text, ToolkitTheme.Wrap);
    }
}
// Everything the setup window shows comes from this snapshot. The controller fills it
// from the same strings it logs; previews fill it with sample data.
public sealed class ToolkitViewState {
    public string Mode = "working";   // working | question | answered | waiting | support
    public string Step = "", Hint = "", Prompt = "", QuestionId = "", Answer = "", Wait = "";
    public int Percent = -1;
    public bool KeysEnabled, Reminder;
    public double Seconds;            // this step / waiting for you / question open
    public ToolkitFailureText Support;
    public ToolkitViewState Copy() { return (ToolkitViewState)MemberwiseClone(); }
}
public sealed class ToolkitSetupView : Panel {
    public ToolkitFonts Fonts;
    public readonly Panel Band = new Panel(), Content = new Panel(), Footer = new Panel();
    public readonly ToolkitPhaseRail Rail = new ToolkitPhaseRail();
    public readonly ToolkitText Brand = new ToolkitText(), StepCount = new ToolkitText();
    public readonly ToolkitChip Chip = new ToolkitChip();
    public readonly ToolkitText Title = new ToolkitText(), Description = new ToolkitText(), Caption = new ToolkitText(),
        Clock = new ToolkitText(), Note = new ToolkitText(), Footnote = new ToolkitText(), FooterText = new ToolkitText();
    public readonly ToolkitProgress Progress = new ToolkitProgress();
    public readonly ToolkitFacts Facts = new ToolkitFacts();
    public readonly ToolkitSteps Steps = new ToolkitSteps();
    public readonly ToolkitHint Where = new ToolkitHint();
    public readonly ToolkitButton Yes = new ToolkitButton(), No = new ToolkitButton();
    ToolkitViewState shown;
    // Intended visibility: Control.Visible reads false until the window is shown.
    readonly System.Collections.Generic.List<Control> on = new System.Collections.Generic.List<Control>();
    void Vis(Control c, bool visible) { on.Remove(c); if (visible) on.Add(c); c.Visible = visible; }
    public bool IsOn(Control c) { return on.Contains(c); }
    public const int LogicalWidth = 560;
    public bool Compact; // Short screens: no phase rail, so the buttons stay on screen.
    static readonly System.Text.RegularExpressions.Regex DownloadCaption = new System.Text.RegularExpressions.Regex("^Downloading Ubuntu \\((.+) of (.+) MB\\)$");
    public ToolkitSetupView() {
        SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint, true);
        Band.Controls.Add(Rail); Band.Controls.Add(Brand); Band.Controls.Add(StepCount);
        Brand.Text = ToolkitSetupText.Product; Brand.Flags = ToolkitTheme.Line; StepCount.Flags = ToolkitTheme.Line | TextFormatFlags.Right;
        foreach (Control c in new Control[] { Chip, Title, Description, Progress, Caption, Clock, Facts, Steps, Where, Note, Footnote }) Content.Controls.Add(c);
        Footer.Controls.Add(FooterText); Footer.Controls.Add(Yes); Footer.Controls.Add(No);
        Controls.Add(Footer); Controls.Add(Content); Controls.Add(Band);
        Yes.TabIndex = 0; No.TabIndex = 1; Yes.Kind = "secondary"; No.Kind = "default";
        Yes.Visible = No.Visible = false;
        Clock.Flags = ToolkitTheme.Line | TextFormatFlags.Right;
        Caption.Flags = ToolkitTheme.Line;
        Progress.Minimum = 0; Progress.Maximum = 100; Progress.MarqueeAnimationSpeed = 30;
        ApplyDpi(96);
    }
    public void ApplyDpi(int dpi) {
        if (Fonts != null && Fonts.Dpi == dpi) return;
        var old = Fonts;
        ToolkitFonts.Detach(this);
        Fonts = new ToolkitFonts(dpi);
        Rail.Fonts = Chip.Fonts = Facts.Fonts = Steps.Fonts = Where.Fonts = Fonts;
        Font = Fonts.Body;
        Brand.Font = Fonts.Brand; StepCount.Font = Fonts.Small;
        Title.Font = Fonts.Title; Description.Font = Fonts.Body; Caption.Font = Fonts.SmallBold; Clock.Font = Fonts.Small;
        Note.Font = Fonts.Small; Footnote.Font = Fonts.Small; FooterText.Font = Fonts.Small;
        Yes.Font = No.Font = Fonts.Button; Where.Font = Fonts.Body;
        if (shown != null) Show(shown);
        if (old != null) old.Dispose();
    }
    protected override void Dispose(bool disposing) {
        base.Dispose(disposing); // Destroy descendants before releasing their fonts.
        if (disposing && Fonts != null) { Fonts.Dispose(); Fonts = null; }
    }
    int S(int logical) { return Fonts.S(logical); }
    public void Restyle() { if (shown != null) Show(shown); }
    public static string StepKey(string step) {
        if (step == "Downloading Ubuntu (complete)" || DownloadCaption.IsMatch(step ?? "")) return "Downloading Ubuntu";
        return step ?? "";
    }
    string ProgressText(ToolkitViewState s) {
        if (s.Step == "Downloading Ubuntu (complete)") return "Download complete";
        var m = DownloadCaption.Match(s.Step ?? "");
        if (m.Success) return m.Groups[1].Value + " MB of " + System.Text.RegularExpressions.Regex.Replace(m.Groups[2].Value, "[.,]0$", "") + " MB  \u00b7  " + Math.Max(0, Math.Min(100, s.Percent)) + "%";
        return s.Percent >= 0 ? Math.Max(0, Math.Min(100, s.Percent)) + "%" : "";
    }
    public ToolkitStepText StepText(ToolkitViewState s) {
        var known = ToolkitSetupText.Step(StepKey(s.Step), s.QuestionId);
        if (known != null) return known;
        var other = new ToolkitStepText(); other.Title = s.Step; other.Description = s.Hint; other.Phase = Rail.Current; return other;
    }
    static int QuestionPhase(string id, int fallback) { return id == "wsl" || id == "linux" ? 1 : id == "usb" ? 2 : fallback; }
    // Text that changes every tick (clock, reminder) without a full relayout.
    public void Tick(ToolkitViewState s) {
        if (shown == null || s.Mode != shown.Mode) { Show(s); return; }
        string clock = ClockText(s);
        if (Clock.Text != clock) Clock.Text = clock;
        if (s.Mode == "question" && (s.Reminder != shown.Reminder || s.KeysEnabled != shown.KeysEnabled)) { Show(s); return; }
        shown.Seconds = s.Seconds;
    }
    string ClockText(ToolkitViewState s) {
        string t = ToolkitSetupText.Clock(s.Seconds);
        if (s.Mode == "working") return "Running for " + t;
        if (s.Mode == "waiting") return "Paused for " + t;
        if (s.Mode == "question") return "Asked " + t + " ago";
        return "";
    }
    public void Show(ToolkitViewState s) {
        shown = s.Copy();
        SuspendLayout();
        BackColor = ToolkitTheme.Surface;
        Band.BackColor = Rail.BackColor = ToolkitTheme.Header; Footer.BackColor = ToolkitTheme.Canvas; Content.BackColor = ToolkitTheme.Surface;
        Brand.ForeColor = ToolkitTheme.HeaderText; StepCount.ForeColor = ToolkitTheme.HeaderMuted;
        Title.ForeColor = ToolkitTheme.Text; Description.ForeColor = s.Mode == "question" ? ToolkitTheme.Text : ToolkitTheme.Muted;
        Caption.ForeColor = ToolkitTheme.Text; Clock.ForeColor = ToolkitTheme.Muted; Note.ForeColor = ToolkitTheme.Muted;
        Footnote.ForeColor = ToolkitTheme.Muted; FooterText.ForeColor = ToolkitTheme.Muted;
        foreach (Control c in new Control[] { Progress, Caption, Clock, Facts, Steps, Where, Note, Footnote, Yes, No, FooterText }) Vis(c, false);
        var step = StepText(s);
        int phase = step.Phase;
        string chip, tone, glyph = "", title, description, footer = "";
        if (s.Mode == "question") {
            var q = ToolkitSetupText.Question(s.Prompt);
            phase = QuestionPhase(q.Id, phase);
            tone = "attention"; glyph = "?";
            chip = s.Reminder ? ToolkitSetupText.ReminderStatus : ToolkitSetupText.QuestionStatus;
            title = q.Title; description = q.Body ?? "";
            Facts.Set(new string[] { "What happens", "How long" }, new string[] { q.WhatHappens, q.HowLong });
            Vis(Facts, !String.IsNullOrEmpty(q.WhatHappens) || !String.IsNullOrEmpty(q.HowLong));
            Footnote.Text = q.Footnote; Vis(Footnote, true);
            Yes.Text = q.YesLabel; No.Text = q.NoLabel;
            Yes.AccessibleName = q.YesLabel; No.AccessibleName = q.NoLabel;
            Yes.AccessibleDescription = q.YesHelp;
            No.AccessibleDescription = q.Title + " " + description + " " + q.Footnote;
            Yes.Shield = q.Id == "wsl" || q.Id == "usb";
            Yes.Enabled = No.Enabled = s.KeysEnabled;
            Vis(Yes, true); Vis(No, true);
            // Once the key guard lifts, keyboard focus rests on the safe answer.
            if (s.KeysEnabled && !Yes.Focused && !No.Focused) No.Focus();
            Vis(Clock, s.Reminder);
        } else if (s.Mode == "answered") {
            var q = ToolkitSetupText.Question(s.Prompt);
            phase = QuestionPhase(q.Id, phase);
            if (s.Answer == "yes") {
                tone = "working"; chip = "Continuing setup";
                title = "Thanks \u2013 continuing setup"; description = "You chose \u201c" + q.YesLabel + "\u201d.";
                footer = ToolkitSetupText.ClosesItself;
            } else {
                tone = "neutral"; glyph = "neutral"; chip = "Setup is stopping";
                title = s.Answer == "closed" ? "You closed the question" : "You chose \u201c" + q.NoLabel + "\u201d";
                description = (s.Answer == "closed" ? "That counts as \u201c" + q.NoLabel + "\u201d. " : "") + "Setup stops here." + (q.Declined.Length > 0 ? " " + q.Declined : "");
            }
        } else if (s.Mode == "waiting") {
            var w = ToolkitSetupText.Wait(s.Wait);
            tone = "attention"; glyph = "pause"; chip = ToolkitSetupText.WaitStatus;
            title = w.Title; description = "";
            Steps.Set(w.Steps); Vis(Steps, w.Steps.Length > 0);
            Where.Text = w.Hint; Vis(Where, !String.IsNullOrEmpty(w.Hint));
            footer = ToolkitSetupText.WaitPaused;
            Vis(Clock, true);
        } else if (s.Mode == "support" && s.Support != null) {
            tone = s.Support.Tone == "info" ? "neutral" : s.Support.Tone; glyph = s.Support.Tone == "info" ? "i" : s.Support.Tone == "error" ? "x" : "!";
            chip = s.Support.Decision ? "Setup needs your decision" : "Setup stopped";
            title = s.Support.Headline; description = s.Support.What;
            // The failed step, not the cleanup step that may have started since.
            var failed = ToolkitSetupText.Step(s.Support.StepKey, s.QuestionId);
            if (failed != null) phase = failed.Phase;
            footer = "The message in front of this window says what to do next.";
        } else {
            tone = "working"; chip = ToolkitSetupText.WorkingStatus;
            title = step.Title; description = step.Description ?? "";
            Vis(Progress, true); Vis(Clock, true);
            if (s.Percent >= 0) { Progress.Style = ProgressBarStyle.Continuous; Progress.Value = Math.Max(0, Math.Min(100, s.Percent)); }
            else Progress.Style = ProgressBarStyle.Marquee;
            Caption.Text = ProgressText(s); Vis(Caption, Caption.Text.Length > 0);
            Note.Text = "If anything takes much longer than expected, this window will tell you what to do."; Vis(Note, true);
            footer = ToolkitSetupText.ClosesItself;
        }
        Rail.SetPhase(phase, tone == "attention");
        StepCount.Text = "Step " + (Rail.Current + 1) + " of " + ToolkitSetupText.Phases.Length;
        StepCount.AccessibleName = Rail.AccessibleName;
        bool chipChanged = Chip.Text != chip || Chip.Tone != tone;
        Chip.Tone = tone; Chip.Glyph = glyph; Chip.Text = chip; Chip.AccessibleName = chip;
        Title.Text = title; Title.AccessibleName = title;
        Description.Text = description; Vis(Description, description.Length > 0); Description.AccessibleName = description;
        Clock.Text = ClockText(s);
        FooterText.Text = footer; Vis(FooterText, footer.Length > 0 && s.Mode != "question");
        Arrange(s);
        ResumeLayout(true);
        Invalidate(true);
        if (chipChanged && (tone == "attention" || s.Mode == "support")) Chip.Announce();
    }
    // Layout in device pixels from the current DPI. Returns the full view size.
    void Arrange(ToolkitViewState s) {
        int width = S(LogicalWidth), margin = S(28), inner = width - 2 * margin;
        // Dark header: product name and step count, then the phase rail.
        int brandHeight = Fonts.Brand.Height + S(4);
        int countWidth = ToolkitTheme.TextWidth("Step 5 of 5", Fonts.Small) + S(4);
        Brand.SetBounds(margin, S(14), inner - countWidth - S(8), brandHeight);
        StepCount.SetBounds(margin + inner - countWidth, S(14), countWidth, brandHeight);
        int railHeight = S(22) + S(6) + RailLabelHeight(inner);
        Rail.Visible = !Compact;
        Rail.SetBounds(margin - S(10), Brand.Bottom + S(14), inner + S(20), railHeight);
        Band.SetBounds(0, 0, width, (Compact ? Brand.Bottom : Rail.Bottom) + S(14));
        // Content.
        int y = S(22);
        y += Chip.Fit(margin, y, inner - ClockReserve(s)) + S(14);
        y += Title.Fit(margin, y, inner);
        if (IsOn(Description)) { y += S(6); y += Description.Fit(margin, y, inner); }
        if (s.Mode == "working") {
            y += S(22);
            Progress.SetBounds(margin, y, inner, S(8)); y += S(8) + S(10);
            int clockWidth = ToolkitTheme.TextWidth("Running for 00:00:00", Clock.Font) + S(4);
            Clock.SetBounds(margin + inner - clockWidth, y, clockWidth, Fonts.Small.Height + S(4));
            Caption.SetBounds(margin, y, inner - clockWidth - S(8), Fonts.SmallBold.Height + S(4));
            y += Math.Max(Fonts.Small.Height, Fonts.SmallBold.Height) + S(4) + S(18);
            y += Note.Fit(margin, y, inner);
        } else if (s.Mode == "question") {
            if (IsOn(Facts)) { y += S(16); int h = Facts.Measure(inner); Facts.SetBounds(margin, y, inner, h); y += h; }
            y += S(12); y += Footnote.Fit(margin, y, inner);
        } else if (s.Mode == "waiting") {
            if (IsOn(Steps)) { y += S(16); int h = Steps.Measure(inner); Steps.SetBounds(margin, y, inner, h); y += h; }
            if (IsOn(Where)) { y += S(14); int h = Where.Measure(inner); Where.SetBounds(margin, y, inner, h); y += h; }
        }
        if (IsOn(Clock) && s.Mode != "working") {
            int clockWidth = ToolkitTheme.TextWidth("Asked 00:00:00 ago", Clock.Font) + S(4);
            Clock.SetBounds(margin + inner - clockWidth, S(22), clockWidth, Chip.Height);
        }
        y += S(24);
        int contentHeight = Math.Max(y, S(s.Mode == "working" ? 196 : 150));
        Content.SetBounds(0, Band.Bottom + 1, width, contentHeight);
        // Footer: buttons for a question, otherwise one calm line.
        int footerHeight;
        if (s.Mode == "question") {
            int bh = S(34), gap = S(10);
            int noWidth = No.Measure(Fonts.Button, Fonts.Dpi), yesWidth = Yes.Measure(Fonts.Button, Fonts.Dpi);
            footerHeight = S(16) * 2 + bh;
            No.SetBounds(width - margin - noWidth, S(16), noWidth, bh);
            Yes.SetBounds(No.Left - gap - yesWidth, S(16), yesWidth, bh);
        } else if (IsOn(FooterText)) {
            int h = ToolkitTheme.TextHeight(FooterText.Text, FooterText.Font, inner);
            footerHeight = h + S(12) * 2;
            FooterText.SetBounds(margin, S(12), inner, h + 2);
        } else footerHeight = S(12);
        Footer.SetBounds(0, Content.Bottom + 1, width, footerHeight);
        Size = new Size(width, Footer.Bottom);
    }
    // Room kept beside the chip for "Asked 1:04 ago" / "Paused for 0:37".
    int ClockReserve(ToolkitViewState s) {
        return s.Mode == "question" || s.Mode == "waiting" ? ToolkitTheme.TextWidth("Asked 00:00:00 ago", Fonts.Small) + S(16) : 0;
    }
    int RailLabelHeight(int inner) {
        int slot = (inner + S(20)) / ToolkitSetupText.Phases.Length, h = 0;
        foreach (string phase in ToolkitSetupText.Phases) h = Math.Max(h, ToolkitTheme.TextHeight(phase, Fonts.SmallBold, slot - S(4)));
        return h;
    }
    protected override void OnPaint(PaintEventArgs e) {
        base.OnPaint(e);
        using (var pen = new Pen(ToolkitTheme.Border)) e.Graphics.DrawLine(pen, 0, Content.Bottom, Width, Content.Bottom);
    }
}
// Stall / failure dialog: what happened, what to try, then one obvious Copy details
// action. The summary text is shown and copied exactly as given (never reworded).
public sealed class ToolkitSupportView : Panel {
    public readonly ToolkitButton Copy = new ToolkitButton(), Open = new ToolkitButton(), KeepWaiting = new ToolkitButton(), CloseButton = new ToolkitButton();
    public readonly ToolkitText StepLine = new ToolkitText(), Headline = new ToolkitText(), What = new ToolkitText(), TryLabel = new ToolkitText(),
        TryText = new ToolkitText(), HelpLabel = new ToolkitText(), HelpLine = new ToolkitText(), DetailsLabel = new ToolkitText();
    public readonly ToolkitPanel TryBox = new ToolkitPanel(), DetailsBox = new ToolkitPanel(), Footer = new ToolkitPanel();
    public readonly TextBox Details = new TextBox();
    readonly Panel icon = new Panel();
    readonly ToolkitFailureText text;
    readonly string summary;
    readonly bool allowWait;
    ToolkitFonts fonts;
    public string CopyState = ""; // "", "copied", "failed" (previews set it directly)
    public const int LogicalWidth = 600;
    public ToolkitSupportView(ToolkitFailureText text, string summary, string folder, bool allowWait) {
        this.text = text; this.summary = summary; this.allowWait = allowWait;
        SetStyle(ControlStyles.OptimizedDoubleBuffer | ControlStyles.AllPaintingInWmPaint, true);
        icon.Paint += delegate(object sender, PaintEventArgs e) {
            e.Graphics.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            string tone = text.Tone == "info" ? "accent" : text.Tone;
            string glyph = text.Tone == "error" ? "x" : text.Tone == "info" ? "i" : "!";
            ToolkitTheme.Badge(e.Graphics, new Rectangle(0, 0, icon.Width - 1, icon.Height - 1), ToolkitTheme.ToneColor(tone), ToolkitTheme.AccentText, glyph);
        };
        foreach (Control c in new Control[] { icon, StepLine, Headline, What, TryBox, HelpLabel, HelpLine, Copy, Open, DetailsLabel, DetailsBox, Footer }) Controls.Add(c);
        TryBox.Controls.Add(TryLabel); TryBox.Controls.Add(TryText);
        DetailsBox.Controls.Add(Details);
        Footer.Controls.Add(KeepWaiting); Footer.Controls.Add(CloseButton);
        StepLine.Flags = ToolkitTheme.Line; TryLabel.Flags = HelpLabel.Flags = DetailsLabel.Flags = ToolkitTheme.Line;
        StepLine.Text = ToolkitSetupText.During(text.Step);
        Headline.Text = text.Headline; What.Text = text.What;
        TryLabel.Text = "WHAT TO TRY"; TryText.Text = text.Try; TryText.AccessibleName = "What to try: " + text.Try;
        HelpLabel.Text = "GET HELP"; DetailsLabel.Text = "DETAILS FOR SUPPORT (THIS IS WHAT GETS COPIED)";
        Copy.Text = "Copy details"; Copy.Kind = "primary";
        // Read first by screen readers, since focus starts here.
        Copy.AccessibleDescription = text.Headline + ". " + text.What + " What to try: " + text.Try + " " + ToolkitSetupText.HelpLine;
        Open.Text = "Open log folder"; Open.Enabled = !String.IsNullOrEmpty(folder);
        KeepWaiting.Text = "Keep waiting"; KeepWaiting.Visible = allowWait; KeepWaiting.AccessibleDescription = "Give this step more time.";
        CloseButton.Text = allowWait ? "Stop setup" : "Close";
        if (allowWait) CloseButton.AccessibleDescription = "Stop setup now. You can open the toolkit again later.";
        // Wrapped, so nothing is hidden off to the right; copied text is unchanged.
        Details.Multiline = true; Details.ReadOnly = true; Details.ScrollBars = ScrollBars.Vertical; Details.WordWrap = true;
        Details.BorderStyle = BorderStyle.None; Details.Text = summary; Details.AccessibleName = "Details for support";
        // Tab order: Copy details, Open log folder, the details text, then the decision.
        int order = 10;
        foreach (Control c in Controls) c.TabIndex = order++;
        Copy.TabIndex = 0; Open.TabIndex = 1; DetailsBox.TabIndex = 2; Footer.TabIndex = 3; KeepWaiting.TabIndex = 0; CloseButton.TabIndex = 1;
        Copy.Click += delegate { DoCopy(); };
        Open.Click += delegate {
            try { System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(folder) { UseShellExecute = true }); }
            catch (Exception) { Open.Text = "Could not open the folder"; Relayout(); }
        };
        ApplyDpi(96);
    }
    public void DoCopy() {
        try { Clipboard.SetText(summary); CopyState = "copied"; }
        catch (Exception error) {
            CopyState = "failed";
            ToolkitSetupStatus.RecordClipboardFailure(error, summary == null ? 0 : summary.Length);
        }
        Relayout();
        if (CopyState == "failed") { Details.Focus(); Details.SelectAll(); }
        HelpLine.Announce();
    }
    public void ApplyDpi(int dpi) {
        if (fonts != null && fonts.Dpi == dpi) return;
        var old = fonts;
        ToolkitFonts.Detach(this);
        fonts = new ToolkitFonts(dpi);
        Font = fonts.Body;
        StepLine.Font = fonts.SmallBold; Headline.Font = fonts.Title; What.Font = fonts.Body;
        TryLabel.Font = HelpLabel.Font = DetailsLabel.Font = fonts.Caps; TryText.Font = fonts.Body; HelpLine.Font = fonts.Body;
        Copy.Font = Open.Font = KeepWaiting.Font = CloseButton.Font = fonts.Button;
        Details.Font = fonts.Mono;
        Relayout();
        if (old != null) old.Dispose();
    }
    protected override void Dispose(bool disposing) {
        base.Dispose(disposing);
        if (disposing && fonts != null) { fonts.Dispose(); fonts = null; }
    }
    int S(int logical) { return fonts.S(logical); }
    public void Relayout() {
        SuspendLayout();
        BackColor = ToolkitTheme.Surface;
        string tone = text.Tone == "info" ? "accent" : text.Tone;
        StepLine.ForeColor = ToolkitTheme.Muted; Headline.ForeColor = ToolkitTheme.Text; What.ForeColor = ToolkitTheme.Text;
        TryLabel.ForeColor = ToolkitTheme.ToneInk(tone); TryText.ForeColor = ToolkitTheme.Text;
        HelpLabel.ForeColor = DetailsLabel.ForeColor = ToolkitTheme.Muted;
        TryBox.Fill = ToolkitTheme.ToneSoft(tone); TryBox.Line = ToolkitTheme.HighContrast ? ToolkitTheme.System(SystemColors.WindowText) : Color.Empty;
        DetailsBox.Fill = ToolkitTheme.Canvas; DetailsBox.Line = ToolkitTheme.Border;
        Footer.Fill = ToolkitTheme.Canvas; Footer.Radius = 0;
        TryBox.BackColor = TryBox.Fill; DetailsBox.BackColor = DetailsBox.Fill; Footer.BackColor = Footer.Fill; // Children paint this color.
        Details.BackColor = ToolkitTheme.Canvas; Details.ForeColor = ToolkitTheme.Text;
        TryBox.Radius = DetailsBox.Radius = S(6);
        Copy.Done = CopyState == "copied";
        if (CopyState == "copied") { HelpLine.Text = ToolkitSetupText.CopiedLine; HelpLine.ForeColor = ToolkitTheme.Success; Copy.Text = "Copied"; }
        else if (CopyState == "failed") { HelpLine.Text = ToolkitSetupText.CopyFailedLine; HelpLine.ForeColor = ToolkitTheme.DangerInk; Copy.Text = "Copy again"; }
        else { HelpLine.Text = ToolkitSetupText.HelpLine; HelpLine.ForeColor = ToolkitTheme.Text; Copy.Text = "Copy details"; }
        HelpLine.AccessibleName = HelpLine.Text;
        int width = S(LogicalWidth), margin = S(28), iconSize = S(36), gap = S(16);
        int textLeft = margin + iconSize + gap, textWidth = width - textLeft - margin, inner = width - 2 * margin;
        int y = S(24);
        icon.SetBounds(margin, y + S(2), iconSize, iconSize);
        if (StepLine.Text.Length > 0) { StepLine.SetBounds(textLeft, y, textWidth, fonts.SmallBold.Height + S(2)); y += fonts.SmallBold.Height + S(6); }
        y += Headline.Fit(textLeft, y, textWidth) + S(6);
        y += What.Fit(textLeft, y, textWidth);
        y = Math.Max(y, icon.Bottom) + S(18);
        // What to try.
        int pad = S(14), tryInner = inner - 2 * pad;
        TryLabel.SetBounds(pad, pad, tryInner, fonts.Caps.Height + S(2));
        int tryHeight = TryText.Fit(pad, TryLabel.Bottom + S(4), tryInner);
        TryBox.SetBounds(margin, y, inner, TryLabel.Bottom + S(4) + tryHeight + pad);
        y = TryBox.Bottom + S(22);
        // Get help: one line, then Copy details (primary) and Open log folder.
        HelpLabel.SetBounds(margin, y, inner, fonts.Caps.Height + S(2)); y += fonts.Caps.Height + S(6);
        y += HelpLine.Fit(margin, y, inner) + S(10);
        int bh = S(34);
        int copyWidth = Math.Max(Copy.Measure(fonts.Button, fonts.Dpi), ToolkitTheme.TextWidth("Copy details", fonts.Button) + S(36));
        Copy.SetBounds(margin, y, copyWidth, bh);
        Open.SetBounds(Copy.Right + S(10), y, Open.Measure(fonts.Button, fonts.Dpi), bh);
        y += bh + S(20);
        // The exact text that is copied, small and scrollable.
        DetailsLabel.SetBounds(margin, y, inner, fonts.Caps.Height + S(2)); y += fonts.Caps.Height + S(6);
        int lines = 6;
        if (ToolkitTheme.WorkArea(this).Height < y + fonts.Mono.Height * 6 + S(150)) lines = 3;
        int detailsHeight = fonts.Mono.Height * lines + S(16);
        DetailsBox.SetBounds(margin, y, inner, detailsHeight);
        Details.SetBounds(S(8), S(8), inner - S(16), detailsHeight - S(16));
        y += detailsHeight + S(24);
        // Decision footer.
        int footerHeight = bh + 2 * S(16);
        Footer.SetBounds(0, y, width, footerHeight);
        int closeWidth = CloseButton.Measure(fonts.Button, fonts.Dpi);
        CloseButton.SetBounds(width - margin - closeWidth, S(16), closeWidth, bh);
        if (allowWait) {
            int waitWidth = KeepWaiting.Measure(fonts.Button, fonts.Dpi);
            KeepWaiting.SetBounds(CloseButton.Left - S(10) - waitWidth, S(16), waitWidth, bh);
        }
        Size = new Size(width, Footer.Bottom);
        ResumeLayout(true);
        Invalidate(true);
    }
    protected override void OnPaint(PaintEventArgs e) {
        base.OnPaint(e);
        using (var pen = new Pen(ToolkitTheme.Border)) e.Graphics.DrawLine(pen, 0, Footer.Top - 1, Width, Footer.Top - 1);
    }
}
// Keep the reviewed view at its DPI size. On a smaller working area, native
// scrollbars (also scrolled by keyboard focus) expose every action instead of
// letting Windows silently clamp away the footer. No content or choices change.
public class ToolkitViewportForm : Form {
    [StructLayout(LayoutKind.Sequential)] struct CLIENTRECT { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] static extern bool GetClientRect(IntPtr hwnd, out CLIENTRECT rect);
    Size fittedView;
    Rectangle fittedArea;
    int fittedDpi;
    bool fitting;
    protected void FitViewport(Control view) {
        if (fitting) return;
        fitting = true;
        try {
            var area = ToolkitTheme.WorkArea(this);
            int dpi = IsHandleCreated ? ToolkitTheme.DpiOf(this) : 0;
            if (fittedView != view.Size || fittedArea != area || fittedDpi != dpi) {
                AutoScroll = false; AutoScrollMinSize = Size.Empty;
                view.Location = Point.Empty;
                Size client = NativeClientSize();
                int maxW = Math.Max(1, area.Width - (Width - client.Width));
                int maxH = Math.Max(1, area.Height - (Height - client.Height));
                bool vertical = view.Height > maxH;
                bool horizontal = view.Width + (vertical ? SystemInformation.VerticalScrollBarWidth : 0) > maxW;
                vertical = view.Height + (horizontal ? SystemInformation.HorizontalScrollBarHeight : 0) > maxH;
                ClientSize = new Size(Math.Min(maxW, view.Width + (vertical ? SystemInformation.VerticalScrollBarWidth : 0)),
                    Math.Min(maxH, view.Height + (horizontal ? SystemInformation.HorizontalScrollBarHeight : 0)));
                AutoScrollMinSize = view.Size;
                AutoScroll = true;
                fittedView = view.Size; fittedArea = area; fittedDpi = dpi;
            }
            Location = new Point(Math.Min(Math.Max(area.Left, Left), Math.Max(area.Left, area.Right - Width)),
                Math.Min(Math.Max(area.Top, Top), Math.Max(area.Top, area.Bottom - Height)));
        } finally { fitting = false; }
    }
    Size NativeClientSize() {
        CLIENTRECT rect;
        return IsHandleCreated && GetClientRect(Handle, out rect) ? new Size(rect.Right - rect.Left, rect.Bottom - rect.Top) : ClientSize;
    }
    protected override Point ScrollToControl(Control activeControl) {
        // Framework Form.ClientSize can retain the pre-scrollbar dimensions.
        // Use the real native viewport so keyboard focus never hides the bottom
        // or right edge of a decision behind a scrollbar.
        Size client = NativeClientSize();
        Rectangle bounds = RectangleToClient(activeControl.Parent.RectangleToScreen(activeControl.Bounds));
        Point next = AutoScrollPosition;
        if (bounds.Left < 0) next.X -= bounds.Left;
        else if (bounds.Right > client.Width) next.X -= bounds.Right - client.Width;
        if (bounds.Top < 0) next.Y -= bounds.Top;
        else if (bounds.Bottom > client.Height) next.Y -= bounds.Bottom - client.Height;
        return new Point(Math.Min(0, next.X), Math.Min(0, next.Y));
    }
}
// Window for the support view: owned by the setup window, sized to the view. Never
// TopMost: an installer the user must finish has to be able to come in front of it.
public sealed class ToolkitSupportForm : ToolkitViewportForm {
    public readonly ToolkitSupportView View;
    public ToolkitButton KeepWaiting { get { return View.KeepWaiting; } }
    public ToolkitButton CloseButton { get { return View.CloseButton; } }
    [StructLayout(LayoutKind.Sequential)] struct RECT { public int Left, Top, Right, Bottom; }
    public ToolkitSupportForm(ToolkitFailureText text, string summary, string folder, bool allowWait) {
        View = new ToolkitSupportView(text, summary, folder, allowWait);
        Text = ToolkitSetupText.Product + (allowWait ? " \u2013 setup needs a decision" : " \u2013 setup stopped");
        AccessibleName = text.Headline; AccessibleRole = AccessibleRole.Dialog;
        AccessibleDescription = text.What + " " + text.Try;
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false; MinimizeBox = false; TopMost = false;
        AutoScaleMode = AutoScaleMode.None; // Layout is already in device pixels.
        Controls.Add(View);
        AcceptButton = View.Copy; CancelButton = View.CloseButton;
        View.SizeChanged += delegate { Fit(); };
        Fit();
    }
    void Fit() {
        FitViewport(View);
    }
    protected override void OnLoad(EventArgs e) {
        View.ApplyDpi(ToolkitTheme.DpiOf(this));
        Fit();
        if (StartPosition == FormStartPosition.CenterScreen) CenterToScreen();
        Fit(); // Centering must not put a clamped window outside the working area.
        base.OnLoad(e);
        ActiveControl = View.Copy;
    }
    protected override void WndProc(ref Message m) {
        if (m.Msg == 0x02E0) {
            var r = (RECT)Marshal.PtrToStructure(m.LParam, typeof(RECT));
            if (ToolkitTheme.ForcedDpi == 0) View.ApplyDpi((int)((long)m.WParam & 0xffff));
            Location = new Point(r.Left, r.Top);
            Fit();
            m.Result = IntPtr.Zero;
            return;
        }
        base.WndProc(ref m);
        if (m.Msg == 0x0015 || m.Msg == 0x001A) View.Relayout();
    }
}
// Window that hosts the setup view: keeps its size in step with the view, follows
// monitor DPI changes, and repaints when Windows switches high contrast on or off.
public sealed class ToolkitSetupForm : ToolkitViewportForm {
    public readonly ToolkitSetupView View = new ToolkitSetupView();
    [StructLayout(LayoutKind.Sequential)] struct RECT { public int Left, Top, Right, Bottom; }
    public ToolkitSetupForm() {
        Text = ToolkitSetupText.Product;
        AccessibleName = ToolkitSetupText.Product + " setup";
        StartPosition = FormStartPosition.CenterScreen;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false; MinimizeBox = false;
        BackColor = ToolkitTheme.Surface;
        AutoScaleMode = AutoScaleMode.None; // Layout is already in device pixels.
        Controls.Add(View);
    }
    // Client area exactly the view's size. Non-client size can differ per monitor DPI,
    // so measure the real client area and correct; never leave the work area.
    public void FitView() {
        var area = ToolkitTheme.WorkArea(this);
        bool compact = View.Height + (Height - ClientSize.Height) > area.Height && !View.Compact;
        if (compact) { View.Compact = true; View.Restyle(); }
        FitViewport(View);
    }
    protected override void OnLoad(EventArgs e) {
        View.ApplyDpi(ToolkitTheme.DpiOf(this));
        FitView();
        if (StartPosition == FormStartPosition.CenterScreen) CenterToScreen();
        FitView();
        base.OnLoad(e);
    }
    protected override void WndProc(ref Message m) {
        if (m.Msg == 0x02E0) { // WM_DPICHANGED: lay out again for the new monitor.
            int dpi = (int)((long)m.WParam & 0xffff);
            var r = (RECT)Marshal.PtrToStructure(m.LParam, typeof(RECT));
            if (ToolkitTheme.ForcedDpi == 0) { View.Compact = false; View.ApplyDpi(dpi); }
            Location = new Point(r.Left, r.Top);
            FitView();
            m.Result = IntPtr.Zero;
            return;
        }
        base.WndProc(ref m);
        if (m.Msg == 0x0015 || m.Msg == 0x001A) View.Restyle(); // WM_SYSCOLORCHANGE, WM_SETTINGCHANGE
    }
}
public class ToolkitSetupStatus {
    static ToolkitSetupForm window;
    static ToolkitSetupView view;
    static Thread thread;
    static IntPtr ownerHandle;
    public static IntPtr OwnerHandle { get { return ownerHandle; } }
    static readonly ToolkitQuestionState question = new ToolkitQuestionState();
    static readonly ConcurrentQueue<ToolkitInteractionEvent> events = new ConcurrentQueue<ToolkitInteractionEvent>();
    static readonly ConcurrentQueue<ToolkitClipboardFailure> clipboardFailures = new ConcurrentQueue<ToolkitClipboardFailure>();
    static readonly System.Diagnostics.Stopwatch clock = System.Diagnostics.Stopwatch.StartNew();
    static double stepStart, lastExternalAttention, externalStart, questionStart;
    static bool externalWait, forceClose, reminded;
    static string beforeWaitStep, beforeWaitDetail;
    static int beforeWaitPercent;
    static string currentStep = "Checking Windows and WSL", currentDetail = "Preparing setup log";
    static int currentPercent = -1;
    // Presentation only: what the window shows besides the logged step text.
    static string waitInstructions = "", lastPrompt = "", lastQuestionId = "", answered = "", questionStep = "";
    static ToolkitFailureText support;
    static readonly ManualResetEvent ready = new ManualResetEvent(false);
    public static bool QuestionPending { get { return question.Pending; } }
    public static string LastAnswer { get { return question.Answer; } }
    [StructLayout(LayoutKind.Sequential)]
    struct FLASHWINFO { public uint cbSize; public IntPtr hwnd; public uint dwFlags, uCount, dwTimeout; }
    [DllImport("user32.dll")] static extern bool FlashWindowEx(ref FLASHWINFO info);
    static void Flash(bool active) {
        var info = new FLASHWINFO(); info.cbSize = (uint)Marshal.SizeOf(info); info.hwnd = ownerHandle;
        info.dwFlags = active ? 15u : 0u; info.uCount = active ? UInt32.MaxValue : 0;
        FlashWindowEx(ref info);
    }
    // The setup window is never TopMost and never takes the front while the user is
    // busy in another window (UAC, the usbipd-win or WSL installer): a TopMost window
    // covers them and even a taskbar click cannot raise them above it.
    // Remind: flash the taskbar button and play a sound; no activation, no z-order change.
    static void Remind() {
        Flash(!window.ContainsFocus);
        System.Media.SystemSounds.Question.Play();
    }
    // Only the toolkit's own question comes forward, once, with normal activation.
    static void AskAttention() {
        if (window.WindowState == FormWindowState.Minimized) window.WindowState = FormWindowState.Normal;
        window.Show(); window.Activate();
        Remind();
    }
    static void OnUi(Action action) {
        if (window != null && window.IsHandleCreated && !window.IsDisposed) window.Invoke(action);
        else throw new InvalidOperationException("The setup window is unavailable.");
    }
    public static ToolkitInteractionEvent[] DrainEvents() {
        var result = new System.Collections.Generic.List<ToolkitInteractionEvent>();
        ToolkitInteractionEvent item;
        while (events.TryDequeue(out item)) result.Add(item);
        return result.ToArray();
    }
    public static void RecordClipboardFailure(Exception error, int summaryLength) {
        clipboardFailures.Enqueue(new ToolkitClipboardFailure(error, summaryLength));
    }
    public static ToolkitClipboardFailure[] DrainClipboardFailures() {
        var result = new System.Collections.Generic.List<ToolkitClipboardFailure>();
        ToolkitClipboardFailure item;
        while (clipboardFailures.TryDequeue(out item)) result.Add(item);
        return result.ToArray();
    }
    public static void Start() {
        if (thread != null && thread.IsAlive) return;
        ready.Reset(); forceClose = false;
        thread = new Thread(delegate() {
            ToolkitTheme.UsePerMonitorDpi();
            window = new ToolkitSetupForm(); view = window.View;
            window.ControlBox = false; window.KeyPreview = true;
            view.Yes.Click += delegate { AnswerOnUi("yes"); }; view.No.Click += delegate { AnswerOnUi("no"); };
            window.KeyDown += delegate(object sender, KeyEventArgs e) {
                if (question.Pending && !question.KeysEnabled(clock.Elapsed.TotalSeconds)) { e.SuppressKeyPress = true; e.Handled = true; }
            };
            window.Activated += delegate { Flash(false); };
            window.FormClosing += delegate(object sender, FormClosingEventArgs e) {
                if (question.Pending && !forceClose) { e.Cancel = true; AnswerOnUi("closed"); }
            };
            stepStart = clock.Elapsed.TotalSeconds; PaintStatus();
            var timer = new System.Windows.Forms.Timer(); timer.Interval = 100;
            timer.Tick += delegate {
                double now = clock.Elapsed.TotalSeconds;
                if (question.Pending) {
                    if (question.Remind(now)) { reminded = true; events.Enqueue(new ToolkitInteractionEvent("question-reminder", question.Prompt)); Remind(); }
                } else if (externalWait) {
                    if (now - lastExternalAttention >= ToolkitQuestionState.ReminderSeconds) {
                        lastExternalAttention = now; events.Enqueue(new ToolkitInteractionEvent("user-wait-reminder", currentDetail)); Remind();
                    }
                }
                view.Tick(State(now));
                window.FitView();
            };
            timer.Start();
            window.FormClosed += delegate { timer.Dispose(); ownerHandle = IntPtr.Zero; };
            window.Shown += delegate { ownerHandle = window.Handle; ready.Set(); };
            Application.Run(window);
        });
        thread.IsBackground = true; thread.SetApartmentState(ApartmentState.STA); thread.Start();
        if (!ready.WaitOne(10000)) throw new Exception("Could not show the toolkit setup window.");
    }
    static ToolkitViewState State(double now) {
        var s = new ToolkitViewState();
        s.Step = currentStep; s.Hint = currentDetail; s.Percent = currentPercent; s.QuestionId = lastQuestionId;
        s.Prompt = lastPrompt; s.Answer = answered; s.Wait = waitInstructions;
        if (support != null) { s.Mode = "support"; s.Support = support; }
        else if (question.Pending) {
            s.Mode = "question"; s.KeysEnabled = question.KeysEnabled(now); s.Reminder = reminded; s.Seconds = now - questionStart;
        } else if (externalWait) { s.Mode = "waiting"; s.Seconds = now - externalStart; }
        else if (answered.Length > 0) s.Mode = "answered";
        else { s.Mode = "working"; s.Seconds = now - stepStart; }
        return s;
    }
    static void PaintStatus() {
        view.Show(State(clock.Elapsed.TotalSeconds));
        bool asking = question.Pending;
        view.Yes.Enabled = view.No.Enabled = asking && question.KeysEnabled(clock.Elapsed.TotalSeconds);
        window.FitView();
    }
    public static void Update(string text, string hint, int percent) {
        if (window != null && window.IsHandleCreated && !window.IsDisposed) {
            OnUi(delegate {
                if (question.Pending || externalWait) return;
                if (percent < 0) stepStart = clock.Elapsed.TotalSeconds;
                // Confirm-Setup refreshes the same step after a No; keep showing that answer.
                if (!(answered == "no" && text == questionStep)) answered = "";
                currentStep = text; currentDetail = hint; currentPercent = percent; PaintStatus();
            });
        } else { currentStep = text; currentDetail = hint; currentPercent = percent; answered = ""; }
    }
    public static void AskQuestion(string prompt) {
        OnUi(delegate {
            question.Begin(prompt, clock.Elapsed.TotalSeconds);
            questionStart = clock.Elapsed.TotalSeconds; reminded = false; answered = ""; questionStep = currentStep;
            lastPrompt = prompt; lastQuestionId = ToolkitSetupText.QuestionId(prompt);
            window.ControlBox = true; window.AcceptButton = view.No; window.CancelButton = view.No;
            PaintStatus(); AskAttention(); view.No.Focus();
        });
    }
    static void AnswerOnUi(string answer) {
        if (!question.Respond(answer, clock.Elapsed.TotalSeconds)) return;
        window.AcceptButton = window.CancelButton = null;
        window.ControlBox = false; Flash(false);
        answered = answer;
        currentStep = answer == "closed" ? "Question closed - this counts as No" : "Answer received: " + answer;
        currentDetail = answer == "closed" ? "You closed the question. Setup will stop; no approval was given." : "Continuing setup.";
        PaintStatus();
    }
    // Also used by keyboard/accessibility clients; same guard as button clicks.
    public static void AnswerQuestion(string answer) { OnUi(delegate { AnswerOnUi(answer); }); }
    public static void UserWait(bool active, string instructions) {
        OnUi(delegate {
            if (active && !externalWait) { beforeWaitStep = currentStep; beforeWaitDetail = currentDetail; beforeWaitPercent = currentPercent; externalStart = clock.Elapsed.TotalSeconds; }
            externalWait = active; window.ControlBox = false;
            if (active) {
                currentStep = "Waiting for you"; currentDetail = instructions; waitInstructions = instructions; answered = "";
                // The other window is in front now; stay behind it. Reminders only flash.
                lastExternalAttention = clock.Elapsed.TotalSeconds; PaintStatus();
            } else { currentStep = beforeWaitStep; currentDetail = beforeWaitDetail; currentPercent = beforeWaitPercent; Flash(false); PaintStatus(); }
        });
    }
    sealed class Owner : IWin32Window { readonly IntPtr handle; public Owner(IntPtr h) { handle = h; } public IntPtr Handle { get { return handle; } } }
    // The setup window behind the dialog says the same thing in one line.
    static void ShowSupport(ToolkitFailureText text) {
        try { if (window != null && window.IsHandleCreated && !window.IsDisposed) OnUi(delegate { support = text; PaintStatus(); }); }
        catch (Exception) { support = text; } // Presentation only; the dialog still appears.
    }
    public static bool Details(string message, string summary, string folder, bool allowWait) {
        bool keepWaiting = false;
        var text = ToolkitSetupText.Failure(message, allowWait);
        ShowSupport(text);
        var dialogThread = new Thread(delegate() {
            ToolkitTheme.UsePerMonitorDpi();
            using (var form = new ToolkitSupportForm(text, summary, folder, allowWait)) {
                form.KeepWaiting.Click += delegate { keepWaiting = true; form.Close(); };
                form.CloseButton.Click += delegate { form.Close(); };
                form.ShowDialog(new Owner(ownerHandle));
            }
        });
        dialogThread.SetApartmentState(ApartmentState.STA); dialogThread.Start();
        dialogThread.Join(); // An explicit user decision, never a command timeout.
        ShowSupport(null);
        return keepWaiting;
    }
    public static void Stop() {
        if (window != null && window.IsHandleCreated && !window.IsDisposed) {
            OnUi(delegate { forceClose = true; window.Close(); });
        }
        if (thread != null) thread.Join(10000);
    }
}
'@
function Initialize-ToolkitStatus {
    if ('ToolkitSetupStatus' -as [type]) { return }
    if ($env:OS -ne 'Windows_NT') { return }
    Add-Type -ReferencedAssemblies System.Windows.Forms,System.Drawing -TypeDefinition ($script:ToolkitSetupUiSource + $script:ToolkitQuestionModelSource)
}
function Start-ToolkitSetup {
    Initialize-ToolkitStatus
    if ('ToolkitSetupStatus' -as [type]) { [ToolkitSetupStatus]::Start() }
}
function Update-ToolkitSetup([string]$Text, [string]$Detail = '', [int]$Percent = -1) {
    if ('ToolkitSetupStatus' -as [type]) { [ToolkitSetupStatus]::Update($Text, $Detail, $Percent) }
}
function Get-ToolkitDialogOwner {
    if ('ToolkitSetupStatus' -as [type]) {
        if ([ToolkitSetupStatus]::OwnerHandle -ne [IntPtr]::Zero) { return [ToolkitSetupStatus]::OwnerHandle }
    }
    if (-not (Initialize-ToolkitConsole)) { throw 'The Windows setup error window is unavailable.' }
    return [ToolkitConsoleNative]::CreateOwner()
}
# Last-resort box when the setup window cannot open. Its buttons are Windows' own
# Yes/No/Cancel, so the text says what each one does. Display only.
function Get-ToolkitFallbackText([string]$Safe) {
    $dash = [char]0x2013
    return "Setup stopped.`r`n`r`n$Safe`r`n`r`nTo get help, copy the details, press Open a support ticket in the #help channel on the Phoenix Discord, and paste them into your ticket.`r`n`r`n    Yes  $dash  Copy details`r`n    No  $dash  Open log folder`r`n    Cancel  $dash  Close"
}
function Get-ToolkitFallbackTitle { return "Jibo DFU Toolkit $([char]0x2013) setup stopped" }
# Windows-side half of first-open readiness. Match a random per-invocation title
# token, not an old toolkit window. No activation, close, kill or compositor API.
$script:ToolkitWindowProbeSource = @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class ToolkitWindowProbe {
    delegate bool EnumProc(IntPtr h, IntPtr p);
    [StructLayout(LayoutKind.Sequential)] struct Rect { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc callback, IntPtr p);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr h, StringBuilder text, int n);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] static extern bool GetClientRect(IntPtr h, out Rect rect);
    [DllImport("user32.dll", SetLastError=true)] static extern IntPtr SendMessageTimeout(IntPtr h, uint m, UIntPtr w, IntPtr l, uint flags, uint timeout, out UIntPtr result);
    public static string State(string token) {
        if (String.IsNullOrEmpty(token)) return "missing";
        string state = "missing";
        EnumWindows(delegate(IntPtr h, IntPtr p) {
            var text = new StringBuilder(2048);
            GetWindowText(h, text, text.Capacity);
            string title = text.ToString();
            if (title.IndexOf("Jibo DFU Toolkit", StringComparison.Ordinal) < 0 ||
                title.IndexOf(" [startup " + token + "]", StringComparison.Ordinal) < 0) return true;
            if (title.StartsWith("[WARN:COPY MODE]", StringComparison.Ordinal)) { state="copy"; return false; }
            Rect rect; UIntPtr result;
            if (IsWindowVisible(h) && !IsIconic(h) && GetClientRect(h, out rect) &&
                rect.Right > rect.Left && rect.Bottom > rect.Top &&
                SendMessageTimeout(h, 0, UIntPtr.Zero, IntPtr.Zero, 2, 200, out result) != IntPtr.Zero) state="visible";
            return false;
        }, IntPtr.Zero);
        return state;
    }
}
'@

function Show-ToolkitSupportDialog([string]$Message, [switch]$AllowWait) {
    $safe = Protect-ToolkitText $Message
    $summary = Get-ToolkitSupportDetails $safe
    if ($env:OS -eq 'Windows_NT') {
        $folder = if ($script:ToolkitLogPath) { Split-Path $script:ToolkitLogPath } else { '' }
        try {
            Start-ToolkitSetup
            return [ToolkitSetupStatus]::Details($safe, $summary, $folder, [bool]$AllowWait)
        } catch {
            # Even a missing/broken WinForms runtime must leave an owned, visible
            # recovery dialog. It never turns a question into implicit approval.
            $owner = Get-ToolkitDialogOwner
            # 0x00010213: Yes/No/Cancel, error icon, Cancel default, set foreground. No MB_TOPMOST.
            $choice = [ToolkitConsoleNative]::MessageBox($owner, (Get-ToolkitFallbackText $safe), (Get-ToolkitFallbackTitle), 0x00010213)
            try {
                if ($choice -eq 6) { Set-Clipboard -Value $summary }
                if ($choice -eq 7 -and $folder) { Start-Process -FilePath explorer.exe -ArgumentList ('"' + $folder + '"') | Out-Null }
            } catch {
                $where = if ($folder) { "The setup log is in $folder" } else { 'The setup log folder is unknown' }
                [ToolkitConsoleNative]::MessageBox($owner, "Could not copy the details or open the log folder. $where. Look it over before sharing it.", 'Jibo DFU Toolkit', 0x00010010) | Out-Null
            }
            return $false
        } finally {
            # Details joins its STA dialog thread. Persist only after it returns,
            # on this PowerShell runspace, including fallback/wrapper errors.
            # Diagnostics must not replace the user's decision on a log I/O error.
            try { Write-ToolkitClipboardEvents } catch { }
        }
    }
    Write-Host $summary
    return $false
}
function Stop-ToolkitSetup {
    if ('ToolkitSetupStatus' -as [type]) { [ToolkitSetupStatus]::Stop() }
    if (('ToolkitConsoleNative' -as [type]) -and [ToolkitConsoleNative].GetMethod('CloseOwner')) { [ToolkitConsoleNative]::CloseOwner() }
}
function Show-ToolkitFailure([string]$Message) {
    Stop-ToolkitSetup
    $safe = Protect-ToolkitText $Message
    if ($script:ToolkitStep) {
        $step = if ($script:ToolkitFailedStep) { $script:ToolkitFailedStep } else { $script:ToolkitStep }
        $policy = Get-ToolkitStepPolicy $step
        $safe = "Step: $step`r`n$safe`r`nTry: $($policy.Hint)"
        if ($script:ToolkitQuestionAnswer -eq 'closed') { $safe = "You closed the question. This counts as No; no approval was given.`r`n$safe" }
    }
    Write-Host "Launcher: $safe" -ForegroundColor Red
    if ($script:ToolkitLogPath) {
        $null = Show-ToolkitSupportDialog $safe
    } elseif (Initialize-ToolkitConsole) {
        Start-ToolkitSetup
        [ToolkitConsoleNative]::MessageBox([ToolkitSetupStatus]::OwnerHandle, $safe, 'Jibo DFU Toolkit', 0x00010010) | Out-Null
    }
}
function Start-ToolkitProcess([string]$FilePath, [string]$ArgumentList, [switch]$CaptureOutput) {
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $FilePath
    $info.Arguments = $ArgumentList
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    if ($CaptureOutput) {
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        $info.StandardOutputEncoding = New-Object Text.UTF8Encoding($false)
        $info.StandardErrorEncoding = New-Object Text.UTF8Encoding($false)
    }
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    try {
        $null = $process.Start()
        $null = $process.Handle
        if ($CaptureOutput) {
            $process | Add-Member NoteProperty ToolkitStdout $process.StandardOutput.ReadToEndAsync()
            $process | Add-Member NoteProperty ToolkitStderr $process.StandardError.ReadToEndAsync()
        }
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
# The usbipd-win way out when its installer cannot finish. Logged and copied text;
# no https:// because Protect-ToolkitText removes full URLs from logs and details.
$script:ToolkitUsbipdManualInstall = 'download the x64 .msi from github.com/dorssel/usbipd-win/releases, run it, restart Windows if it asks, then reopen the toolkit.'
# Longest wait for the toolkit's own Linux display to come back after the
# automatic copy-mode fix (bootstrap.ps1 Repair-ToolkitDisplay).
$script:ToolkitDisplayRepairWaitSeconds = 30
# Seconds after Windows approval before setup asks about a usbipd-win install that
# has not finished: winget's first-run source update and the ~4.5 MB MSI download
# usually take well under a minute, and the short usbipd-win wizard about another
# minute. Keep waiting restarts it; nothing is stopped without the user's choice.
$script:ToolkitUsbipdInstallerWaitSeconds = 180
# InstallerWaitSeconds bounds how long an approved --interactive installer may run
# while setup shows "Waiting for you". 0 keeps the old unbounded user wait.
function Invoke-ToolkitElevated([string]$File, [string]$Arguments, [double]$TimeoutSeconds = 180, [switch]$AllowWait, [double]$InstallerWaitSeconds = 0) {
    $started = [DateTime]::UtcNow
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $code = 1; $message = ''; $process = $null
    Write-ToolkitLog 'command-start' @{step=$script:ToolkitStep; command=[IO.Path]::GetFileName($File); start_utc=$started.ToString('o'); limit_seconds=$TimeoutSeconds}
    try {
        $interactive = $Arguments -match '--interactive'
        Set-ToolkitUserWait $true 'Waiting for you: approve the Windows administrator prompt. If you do not see it, look for a flashing shield icon on the taskbar.'
        try {
            $process = Start-Process -FilePath $File -ArgumentList $Arguments -Verb RunAs -WindowStyle Hidden -PassThru
        } finally {
            Write-ToolkitInteractionEvents
            if (-not $interactive) { Set-ToolkitUserWait $false }
        }
        if ($interactive) { Set-ToolkitUserWait $true 'Waiting for you: finish the usbipd-win installer. Look for its installer window or a flashing shield icon on the taskbar. Restart Windows if the installer asks.' }
        $null = $process.Handle
        $installerWait = if ($interactive) { $InstallerWaitSeconds } else { 0 }
        $installerMessage = "usbipd-win installer did not finish within $installerWait seconds after Windows approval. If its installer window is open, finish it. Otherwise install usbipd-win yourself: $script:ToolkitUsbipdManualInstall"
        if (-not (Wait-ToolkitProcess $process -TimeoutSeconds $TimeoutSeconds -AllowWait:$AllowWait -UserWaitSeconds $installerWait -UserWaitMessage $installerMessage)) {
            $code = 124
            $error = New-Object TimeoutException "$script:ToolkitStep stalled: administrator command did not finish. Check Windows prompts, then restart Windows and reopen."
            $error.Data['ToolkitStall'] = $true
            throw $error
        }
        $code = $process.ExitCode
        return [pscustomobject]@{ExitCode=$code}
    } catch {
        $message = $_.Exception.Message
        if ($_.Exception.Data['ToolkitStall']) { $code = 124 }
        throw
    }
    finally {
        if ($script:ToolkitWaitingForUser) { Set-ToolkitUserWait $false }
        Write-ToolkitLog 'command-end' @{step=$script:ToolkitStep; command=[IO.Path]::GetFileName($File); start_utc=$started.ToString('o'); end_utc=[DateTime]::UtcNow.ToString('o'); duration_seconds=[Math]::Round($timer.Elapsed.TotalSeconds,3); result=$(if ($code -eq 0) {'ok'} elseif ($code -eq 124) {'stalled'} else {'failed'}); exit_code=$code; error=$message}
        if ($process -is [Diagnostics.Process]) { $process.Dispose() }
    }
}
function Wait-ToolkitProcess($Process, [double]$TimeoutSeconds = 120, [switch]$AllowWait, [double]$UserWaitSeconds = 0, [string]$UserWaitMessage = '') {
    $timer = [Diagnostics.Stopwatch]::StartNew()
    $userTimer = [Diagnostics.Stopwatch]::StartNew()
    while (-not $Process.WaitForExit(100)) {
        Write-ToolkitInteractionEvents
        if ($script:ToolkitWaitingForUser) {
            $timer.Restart()
            # A user wait is never a stall, but an installer that cannot be seen
            # must not keep setup paused forever. Ask; never kill it unasked.
            if ($UserWaitSeconds -le 0 -or $userTimer.Elapsed.TotalSeconds -lt $UserWaitSeconds) { continue }
            Write-ToolkitLog 'stall' @{ step=$script:ToolkitStep; error=$UserWaitMessage; limit_seconds=$UserWaitSeconds; waiting_for='installer' }
            if (Show-ToolkitSupportDialog $UserWaitMessage -AllowWait) {
                Write-ToolkitLog 'keep-waiting' @{ step=$script:ToolkitStep }
                $userTimer.Restart()
                continue
            }
            if ($Process.WaitForExit(0)) { return $true }
            # Stop setup leaves the elevated installer alone: stopping it could
            # interrupt a driver install, and a normal user cannot stop it anyway.
            Write-ToolkitLog 'installer-wait-stopped' @{ step=$script:ToolkitStep; limit_seconds=$UserWaitSeconds; installer_left_running=$true }
            $exception = New-Object TimeoutException $UserWaitMessage
            $exception.Data['ToolkitStall'] = $true
            throw $exception
        }
        if (Test-ToolkitStall -ElapsedSeconds $timer.Elapsed.TotalSeconds -LimitSeconds $TimeoutSeconds) {
            $message = "$script:ToolkitStep stalled: no completion after $TimeoutSeconds seconds. $($script:ToolkitPolicy.Hint)"
            Write-ToolkitLog 'stall' @{ step=$script:ToolkitStep; error=$message; limit_seconds=$TimeoutSeconds }
            if ($AllowWait -and (Show-ToolkitSupportDialog $message -AllowWait)) {
                Write-ToolkitLog 'keep-waiting' @{ step=$script:ToolkitStep }
                $timer.Restart()
                continue
            }
            try { Stop-ToolkitProcess $Process } catch {
                $exception = New-Object TimeoutException "$message Windows could not stop the command. Close the installer or restart Windows before trying again."
                $exception.Data['ToolkitStall'] = $true
                throw $exception
            }
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
# Read the WSL version probe while it runs: the inbox stub can print installation
# advice and then wait for a key. Never send a key (that can approve an install).
# Microsoft WSL's install.cpp also has a one-minute keypress wait; generic nonzero
# exits, including -1, are not a unique "not installed" signal. See docs/release.md.
$script:ToolkitWslProbeSource = @'
using System.IO;
using System.Text;
using System.Threading.Tasks;
public sealed class ToolkitWslProbeOutput {
    readonly StringBuilder text = new StringBuilder();
    readonly object gate = new object();
    public Task Completion { get; private set; }
    public string Text { get { lock (gate) { return text.ToString(); } } }
    public ToolkitWslProbeOutput(StreamReader reader) { Completion = Read(reader); }
    async Task Read(StreamReader reader) {
        var buffer = new char[512];
        int count;
        while ((count = await reader.ReadAsync(buffer, 0, buffer.Length).ConfigureAwait(false)) > 0) {
            lock (gate) {
                // Keep the first 16 KiB, where WSL emits its diagnostic. Continue
                // draining both pipes even if an unexpected program is noisy.
                int keep = System.Math.Min(count, 16384 - text.Length);
                if (keep > 0) text.Append(buffer, 0, keep);
            }
        }
    }
}
'@
function Test-ToolkitWslSetupOutput([string]$Text) {
    # The command spelling is invariant across localized WSL messages. Do not
    # match "not installed" alone: a missing distro is not a missing WSL runtime.
    $clean = $Text -replace "`0", ''
    return ($clean -match '(?i)\bwsl\.exe[ \t]+--(?:install|update)\b' -or
        $clean -match '(?i)The Windows Subsystem for Linux is not installed\.' -or
        $clean -match '\bWSL_E_WSL_OPTIONAL_COMPONENT_REQUIRED\b')
}
function Invoke-ToolkitNative([string]$File, [string[]]$Arguments, [double]$TimeoutSeconds = 120, [switch]$AllowWait, [switch]$WatchWslSetup) {
    # Early termination is allowed only for this read-only probe, never an
    # install, distro command, or the USB helpers using this same capture helper.
    if ($WatchWslSetup -and ([IO.Path]::GetFileName($File) -ine 'wsl.exe' -or $Arguments.Count -ne 1 -or $Arguments[0] -cne '--version')) { throw 'WSL setup detection is only allowed for wsl.exe --version.' }
    if ($WatchWslSetup -and -not ('ToolkitWslProbeOutput' -as [type])) { Add-Type -TypeDefinition $script:ToolkitWslProbeSource }
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
    $info.RedirectStandardInput = [bool]$WatchWslSetup
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    try {
        $null = $process.Start()
        if ($WatchWslSetup) {
            $process.StandardInput.Close()
            $probeOut = New-Object ToolkitWslProbeOutput($process.StandardOutput)
            $probeErr = New-Object ToolkitWslProbeOutput($process.StandardError)
            $setupRequired = $false; $completed = $true; $stoppedProbe = $false
            while (-not $process.WaitForExit(100)) {
                if ((Test-ToolkitWslSetupOutput $probeOut.Text) -or (Test-ToolkitWslSetupOutput $probeErr.Text)) {
                    # Stop only the read-only probe that emitted the advice. No
                    # installer has started, and no permission has been granted.
                    Stop-ToolkitProcess $process
                    $setupRequired = $true; $stoppedProbe = $true
                    break
                }
                if (Test-ToolkitStall -ElapsedSeconds $timer.Elapsed.TotalSeconds -LimitSeconds $TimeoutSeconds) {
                    Stop-ToolkitProcess $process
                    $completed = $false
                    break
                }
            }
            $pipesComplete = [Threading.Tasks.Task]::WaitAll([Threading.Tasks.Task[]]@($probeOut.Completion, $probeErr.Completion), 2000)
            $lines = @((ConvertTo-ToolkitNativeLines $probeOut.Text)) + @((ConvertTo-ToolkitNativeLines $probeErr.Text))
            # A normally exiting stub can emit the same message between polls.
            $setupRequired = $setupRequired -or (Test-ToolkitWslSetupOutput $probeOut.Text) -or (Test-ToolkitWslSetupOutput $probeErr.Text)
            if (-not $pipesComplete -or (-not $completed -and -not $setupRequired)) {
                $result = [pscustomobject]@{Output=$lines; ExitCode=124; TimedOut=$true; WslSetupRequired=$false; StartError="$commandStep stalled after $TimeoutSeconds seconds. $($script:ToolkitPolicy.Hint)"}
            } else {
                $result = [pscustomobject]@{Output=$lines; ExitCode=$(if ($stoppedProbe) {$null} else {$process.ExitCode}); TimedOut=$false; WslSetupRequired=$setupRequired; StartError=$null}
            }
        } else {
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
        }
    } catch {
        $stalled = [bool]$_.Exception.Data['ToolkitStall']
        $result = [pscustomobject]@{Output=@(); ExitCode=$(if ($stalled) {124} else {1}); TimedOut=$stalled; StartError=$_.Exception.Message}
    } finally {
        $process.Dispose()
        if ($result) {
            $output = if ($result.WslSetupRequired -or $result.ExitCode -ne 0) { ($result.Output -join "`n") } else { '' }
            # Redact BEFORE truncating: otherwise a long secret could lose its key.
            $output = Protect-ToolkitText $output
            # Keep useful diagnostics bounded; never persist argument lists.
            if ($output.Length -gt 8000) { $output = $output.Substring($output.Length - 8000) }
            Write-ToolkitLog 'command-end' @{ step=$commandStep; command=[IO.Path]::GetFileName($File); start_utc=$started.ToString('o'); end_utc=[DateTime]::UtcNow.ToString('o'); duration_seconds=[Math]::Round($timer.Elapsed.TotalSeconds,3); result=$(if ($result.TimedOut) { 'stalled' } elseif ($result.WslSetupRequired) { 'setup-required' } elseif ($result.ExitCode -eq 0) { 'ok' } else { 'failed' }); exit_code=$result.ExitCode; error=[string]$result.StartError; output=$output }
        }
    }
    return $result
}
