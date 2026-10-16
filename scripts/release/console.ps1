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
function Initialize-ToolkitLog([string]$Root, [string]$Build = 'unknown', [string]$ExistingPath = $env:JIBO_DFU_SETUP_LOG) {
    if (-not $Root) { $Root = $env:LOCALAPPDATA }
    if (-not $Root) { $Root = [IO.Path]::GetTempPath() }
    $script:ToolkitBuild = $Build
    $script:ToolkitWindowsVersion = [Environment]::OSVersion.VersionString
    if ($env:OS -eq 'Windows_NT') {
        try {
            $windows = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
            $script:ToolkitWindowsVersion = "$($windows.ProductName) $($windows.DisplayVersion) (build $($windows.CurrentBuild).$($windows.UBR))"
        } catch { } # Older Windows still has the CLR version string.
    }
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
            owner = CreateWindowEx(0x00040000, "STATIC", "Jibo DFU Toolkit - setup stopped", 0x00C80000, 200, 200, 640, 180, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero, IntPtr.Zero);
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
// Plain-language presentation only. Keys are the exact logged strings (step names,
// question prompts, user-wait instructions, failure messages); the log and Copy
// details keep the original technical text. Unknown keys fall back to that text.
public sealed class ToolkitStepText {
    public string Title, Description;
    public int Phase;
}
public sealed class ToolkitQuestionText {
    public string Id, Title, Body, WhatHappens, HowLong, YesLabel, NoLabel, YesHelp, Footnote;
}
public sealed class ToolkitWaitText {
    public string Kind, Title, Hint;
    public string[] Steps;
}
public sealed class ToolkitFailureText {
    public string Tone, Headline, Step, What, Try;
}
public static class ToolkitSetupText {
    public const string Product = "Jibo DFU Toolkit";
    public const string Exe = "Jibo-DFU-Windows-x64.exe";
    public static readonly string[] Phases = { "Check Windows", "Set up Linux", "Prepare the toolkit", "Connect USB", "Open the toolkit" };
    public const string WorkingStatus = "Working \u2013 nothing for you to do right now";
    public const string QuestionStatus = "Your answer is needed";
    public const string ReminderStatus = "Still waiting for your answer";
    public const string WaitStatus = "Waiting for you";
    public const string WaitPaused = "Setup is paused until you do this. It has not stalled.";
    public const string ClosesItself = "This window closes by itself when the toolkit opens.";
    public const string HelpLine = "Copy the details and paste them in the #help channel on the Phoenix Discord. Please look them over before posting.";
    public const string CopiedLine = "Copied. Paste it in #help with Ctrl+V.";
    public const string CopyFailedLine = "Could not copy. Select the text below, then press Ctrl+C.";
    static ToolkitStepText S(string title, string description, int phase) {
        var s = new ToolkitStepText(); s.Title = title; s.Description = description; s.Phase = phase; return s;
    }
    public static ToolkitStepText Step(string key, string questionId) {
        switch (key ?? "") {
            case "Starting setup": case "Checking embedded files": return S("Starting setup", "Checking the toolkit\u2019s own files.", 0);
            case "Checking Windows and WSL": return S("Checking Windows", "Making sure Windows has what the toolkit needs to run.", 0);
            case "Installing Windows components":
                if (questionId == "usb") return S("Installing USB support", "Installing usbipd-win, the free Windows tool that lets the toolkit talk to Jibo over USB.", 2);
                if (questionId == "wsl") return S("Installing Windows\u2019 Linux support", "Windows is installing its Linux feature (WSL) from Microsoft. This can take several minutes.", 0);
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
    static ToolkitQuestionText Q(string id, string title, string body, string what, string howLong, string yes, string yesHelp) {
        var q = new ToolkitQuestionText(); q.Id = id; q.Title = title; q.Body = body; q.WhatHappens = what; q.HowLong = howLong;
        q.YesLabel = yes; q.NoLabel = "Not now"; q.YesHelp = yesHelp;
        q.Footnote = "Not now stops setup here and installs nothing. Closing this window also counts as Not now.";
        return q;
    }
    public static ToolkitQuestionText Question(string prompt) {
        switch (QuestionId(prompt)) {
            case "wsl": return Q("wsl", "Install Windows\u2019 Linux support?",
                "The toolkit runs on Linux inside Windows. Windows needs its Linux feature (WSL) installed or updated first.",
                "Windows installs it from Microsoft. Windows will ask for permission, and may ask you to restart.",
                "Several minutes, depending on your internet speed.", "Install", "Installs Windows\u2019 Linux feature from Microsoft");
            case "linux": return Q("linux", "Set up the toolkit\u2019s Linux?",
                "The toolkit runs in its own private copy of Ubuntu Linux, kept separate from the rest of this PC.",
                "A one-time download of about 345 MB from Ubuntu\u2019s official site. No Linux account or password needed.",
                "Usually several minutes, depending on your internet speed.", "Set it up", "Downloads about 345 MB and sets up the toolkit\u2019s Linux");
            case "usb": return Q("usb", "Install USB support?",
                "To talk to Jibo over USB, the toolkit needs usbipd-win, a free Windows tool.",
                "Its installer opens. Follow its steps and choose Yes if Windows asks for permission. Then open the toolkit again.",
                "Usually a minute or two.", "Install", "Opens the usbipd-win installer");
        }
        var other = new ToolkitQuestionText(); other.Id = ""; other.Title = prompt; other.YesLabel = "Yes"; other.NoLabel = "No";
        other.YesHelp = ""; other.Footnote = "No stops setup here. Closing this window also counts as No.";
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
                return W("restart", "Finish installing USB support, then open the toolkit again", "Not sure? Check the installer window or a flashing shield icon on the taskbar.",
                    "Finish the usbipd-win installer, if it is still open.", "Restart Windows if it asks you to.", "Then open " + Exe + " again.");
            case "Waiting for you: approve USB sharing at the Windows administrator prompt. If it is hidden, look for a flashing shield icon on the taskbar.":
                return W("admin", "Approve USB sharing if Windows asks", "Don\u2019t see a prompt? Check the taskbar for a flashing shield icon. If there is none, setup continues by itself.",
                    "Windows may ask for permission to share USB with the toolkit.", "If it does, choose Yes.");
        }
        string text = instructions ?? "";
        if (text.StartsWith("Waiting for you: ")) text = text.Substring(17);
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
        string keepWaiting = "Choose Keep waiting to give it more time, or Stop setup to end setup now.";
        ToolkitFailureText f;
        if (text.StartsWith("WSL setup declined."))
            f = F("info", "Setup stopped", declined + " Windows\u2019 Linux support was not installed, and nothing was changed.", "Open the toolkit again when you are ready to install it.");
        else if (text.StartsWith("Distribution setup declined."))
            f = F("info", "Setup stopped", declined + " Nothing was downloaded or installed.", "Open the toolkit again when you are ready to set up its Linux.");
        else if (text.StartsWith("USB setup declined."))
            f = F("info", "Setup stopped", declined + " USB support was not installed.", "The toolkit needs USB support to talk to Jibo. Open the toolkit again when you are ready to install it.");
        else if (text.StartsWith("WSL setup requested."))
            f = F("info", "Restart Windows to finish setup", "Windows finished installing its Linux support.", "Restart Windows if it asked you to. Then open " + Exe + " again to continue setup.");
        else if (text.StartsWith("usbipd-win setup finished."))
            f = F("info", "Open the toolkit again to finish setup", "USB support (usbipd-win) was installed.", "Close this message, then open " + Exe + " again. Restart Windows first if the installer asked you to.");
        else if (text.IndexOf("canceled by the user", System.StringComparison.OrdinalIgnoreCase) >= 0)
            f = F("info", "Setup stopped", "The Windows permission prompt was declined, so nothing was installed.", "Open the toolkit again and choose Yes when Windows asks for permission.");
        else if (text.StartsWith("Downloading Ubuntu stalled"))
            f = F("stall", "The download stopped", "No data has arrived for " + (waited ?? "a while") + ". The internet connection may have dropped.",
                allowWait ? "Check your internet connection. " + keepWaiting : "Check your internet connection, then open the toolkit again.");
        else if (text.StartsWith("Opening the toolkit stalled"))
            f = F("stall", "The toolkit window did not appear", "The toolkit has not opened after " + (waited ?? "a while") + ".",
                allowWait ? keepWaiting + " If you stop, restart Windows before opening the toolkit again." : "Restart Windows, then open the toolkit again.");
        else if (text.IndexOf("administrator command did not finish", System.StringComparison.Ordinal) >= 0)
            f = F("stall", "A Windows installer did not finish", "A Windows installer or permission prompt did not finish.", "Look for an installer window or a Windows prompt and finish it. If there is none, restart Windows and open the toolkit again.");
        else if (stall.Success)
            f = F("stall", "Setup is taking longer than expected", StepTitle(step) + " has not finished" + (waited == null ? "." : " after " + waited + "."),
                allowWait ? keepWaiting + (plainTry == null ? "" : " If you stop: " + LowerFirst(plainTry)) : (plainTry ?? "Restart Windows, then open the toolkit again."));
        else if (text.StartsWith("Ubuntu image checksum mismatch") || text.StartsWith("Ubuntu image size mismatch") || text.StartsWith("Ubuntu download exceeded"))
            f = F("error", "The download was damaged", "The downloaded Ubuntu file did not match the official one, so it was not used. Nothing was installed.", "Open the toolkit again to download it again.");
        else if (text.StartsWith("Ubuntu download failed"))
            f = F("error", "The download failed", "The Ubuntu download did not finish.", "Check your internet connection, then open the toolkit again.");
        else if (text.StartsWith("The private Linux is not ready."))
            f = F("error", "The toolkit\u2019s Linux is not working", "The toolkit\u2019s private Linux is installed but did not start correctly.",
                "To set it up again, run wsl --unregister Jibo-DFU-Toolkit in PowerShell, then open the toolkit again. This deletes all files inside that Linux, including any backups saved there.");
        else if (text.StartsWith("Private Linux storage already exists"))
            f = F("error", "An old toolkit Linux folder is in the way", "A folder from an earlier toolkit Linux already exists, so setup stopped instead of replacing it.",
                "If you no longer need the files in it, move that folder somewhere else (its location is in the details), then open the toolkit again.");
        else if (text.StartsWith("USB handoff failed"))
            f = F("error", "USB could not be connected", "Sharing USB with the toolkit failed, or the Windows permission prompt was declined.", "Open the toolkit again and choose Yes when Windows asks for permission. Unplugging Jibo\u2019s USB cable and plugging it back in can also help.");
        else if (text.StartsWith("USB support stopped unexpectedly"))
            f = F("error", "USB support stopped", "The toolkit\u2019s USB connection stopped unexpectedly.", "When nothing is being written to Jibo, close the toolkit window, then open the toolkit again.");
        else if (text.StartsWith("Install Microsoft App Installer"))
            f = F("error", "USB support cannot be installed automatically", "This PC is missing Microsoft\u2019s App Installer, which setup uses to install USB support.", "Install App Installer from the Microsoft Store, or install usbipd-win from its GitHub releases page. Then open the toolkit again.");
        else if (text.StartsWith("Setup exited with code"))
            f = F("error", "A Windows installer reported a problem", "The installer stopped with an error.", "Restart Windows if it asked you to, then open the toolkit again.");
        else if (text.StartsWith("GUI or USB support exited with code"))
            f = F("error", "The toolkit closed with an error", "The toolkit or its USB support stopped with an error.", "Open the toolkit again.");
        else if (text.StartsWith("Cannot create a setup log"))
            f = F("error", "Setup could not start", "The toolkit could not save its setup log.", "Make sure the drive has free space, then open the toolkit again.");
        else
            f = F(allowWait ? "stall" : "error", allowWait ? "Setup is taking longer than expected" : "Setup stopped", text, allowWait ? keepWaiting : (plainTry ?? "Open the toolkit again. If it happens again, ask for help in #help."));
        f.Step = step == null ? null : StepTitle(step);
        return f;
    }
    static string LowerFirst(string text) { return text.Length > 1 ? char.ToLowerInvariant(text[0]) + text.Substring(1) : text; }
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
    public static bool HighContrast { get { return ForceHighContrast || SystemInformation.HighContrast; } }
    static Color C(int rgb) { return Color.FromArgb(255, (rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255); }
    static Color Pick(int rgb, Color system) { return HighContrast ? system : C(rgb); }
    public static Color Surface { get { return Pick(0xffffff, SystemColors.Window); } }
    public static Color Canvas { get { return Pick(0xf3f5f8, SystemColors.Window); } }
    public static Color Header { get { return Pick(0x141c26, SystemColors.Window); } }
    public static Color HeaderText { get { return Pick(0xeef2f6, SystemColors.WindowText); } }
    public static Color HeaderMuted { get { return Pick(0xa9b6c6, SystemColors.WindowText); } }
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
        return TextRenderer.MeasureText(text, font, new Size(Math.Max(1, width), int.MaxValue), Wrap).Height;
    }
    public static int TextWidth(string text, Font font) {
        return String.IsNullOrEmpty(text) ? 0 : TextRenderer.MeasureText(text, font, new Size(int.MaxValue, int.MaxValue), Line).Width;
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
    public static void Shield(Graphics g, Rectangle r) {
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
    public void Dispose() { foreach (var f in new Font[] { Brand, Title, Body, BodyBold, Small, SmallBold, Caps, Button, Mono }) f.Dispose(); }
}
// Static text measured and painted with the same flags, so wrapped text is never clipped.
public sealed class ToolkitText : Control {
    public TextFormatFlags Flags = ToolkitTheme.Wrap;
    public ToolkitText() {
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer | ControlStyles.SupportsTransparentBackColor, true);
        SetStyle(ControlStyles.Selectable, false);
        TabStop = false; AccessibleRole = AccessibleRole.StaticText; BackColor = Color.Transparent;
    }
    public int Fit(int x, int y, int width) {
        int h = Visible && Text.Length > 0 ? ToolkitTheme.TextHeight(Text, Font, width) + 2 : 0;
        SetBounds(x, y, width, Math.Max(1, h));
        return h;
    }
    protected override void OnTextChanged(EventArgs e) { base.OnTextChanged(e); Invalidate(); }
    protected override void OnPaint(PaintEventArgs e) {
        TextRenderer.DrawText(e.Graphics, Text, Font, ClientRectangle, ForeColor, Flags);
    }
    public void Announce() { AccessibilityNotifyClients(AccessibleEvents.NameChange, -1); AccessibilityNotifyClients(AccessibleEvents.SystemAlert, -1); }
}
// Flat button: "primary" (filled), "secondary" (outlined). Keeps Button semantics:
// AcceptButton/CancelButton, PerformClick, Enabled, keyboard and screen readers.
public sealed class ToolkitButton : Button {
    public string Kind = "secondary";
    public bool Shield;
    bool hover, down;
    public ToolkitButton() {
        SetStyle(ControlStyles.UserPaint | ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer, true);
        UseMnemonic = false; // A typed letter must never press a setup button.
        FlatStyle = FlatStyle.Flat; Cursor = Cursors.Hand;
    }
    public int Measure(Font font, int dpi) {
        int text = ToolkitTheme.TextWidth(Text, font);
        return Math.Max(ToolkitTheme.S(112, dpi), text + ToolkitTheme.S(Shield ? 58 : 36, dpi));
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
        bool primary = Kind == "primary";
        bool hc = ToolkitTheme.HighContrast;
        Color fill, ink, line;
        if (!Enabled) { fill = primary && !hc ? ToolkitTheme.Disabled : ToolkitTheme.Surface; ink = primary && !hc ? ToolkitTheme.AccentText : ToolkitTheme.Disabled; line = ToolkitTheme.Disabled; }
        else if (primary) { fill = down ? ToolkitTheme.AccentPressed : ToolkitTheme.Accent; ink = ToolkitTheme.AccentText; line = fill; }
        else { fill = down ? ToolkitTheme.Pressed : hover ? ToolkitTheme.Hover : ToolkitTheme.Surface; ink = ToolkitTheme.Text; line = hover && !hc ? ToolkitTheme.Muted : ToolkitTheme.Border; }
        if (hc && Enabled && (hover || Focused)) { fill = SystemColors.Highlight; ink = SystemColors.HighlightText; line = SystemColors.HighlightText; }
        ToolkitTheme.Fill(g, fill, r, radius);
        float width = IsDefault && Enabled && !primary ? ToolkitTheme.S(2, dpi) : Math.Max(1, ToolkitTheme.S(1, dpi));
        Color edge = IsDefault && Enabled && !primary ? ToolkitTheme.Accent : line;
        var inset = Rectangle.Inflate(r, -(int)(width / 2), -(int)(width / 2));
        ToolkitTheme.Outline(g, edge, inset, radius, width);
        var content = new Rectangle(0, 0, Width, Height);
        if (Shield) {
            int icon = ToolkitTheme.S(16, dpi);
            int textWidth = ToolkitTheme.TextWidth(Text, Font);
            int start = (Width - icon - ToolkitTheme.S(8, dpi) - textWidth) / 2;
            ToolkitTheme.Shield(g, new Rectangle(start, (Height - icon) / 2, icon, icon));
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
        if (ToolkitTheme.HighContrast) ToolkitTheme.Outline(g, SystemColors.WindowText, track, radius, 1);
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
                using (var pen = new Pen(i < current ? ToolkitTheme.Accent : ToolkitTheme.Border, line))
                    g.DrawLine(pen, cx + d / 2 + Fonts.S(6), d / 2, cx + slot - d / 2 - Fonts.S(6), d / 2);
            }
            Color tone = attention ? ToolkitTheme.Attention : ToolkitTheme.Accent;
            if (i < current) ToolkitTheme.Badge(g, circle, ToolkitTheme.Accent, ToolkitTheme.AccentText, "check");
            else if (i == current) {
                using (var brush = new SolidBrush(tone)) g.FillEllipse(brush, circle);
                TextRenderer.DrawText(g, (i + 1).ToString(), Fonts.SmallBold, circle, ToolkitTheme.AccentText, ToolkitTheme.Line | TextFormatFlags.HorizontalCenter);
            } else {
                using (var brush = new SolidBrush(ToolkitTheme.Surface)) g.FillEllipse(brush, circle);
                using (var pen = new Pen(ToolkitTheme.Border, line)) g.DrawEllipse(pen, Rectangle.Inflate(circle, -line / 2 - 1, -line / 2 - 1));
                TextRenderer.DrawText(g, (i + 1).ToString(), Fonts.SmallBold, circle, ToolkitTheme.Muted, ToolkitTheme.Line | TextFormatFlags.HorizontalCenter);
            }
            var label = new Rectangle(slot * i, d + Fonts.S(6), slot, Height - d - Fonts.S(6));
            TextRenderer.DrawText(g, ToolkitSetupText.Phases[i], i == current ? Fonts.SmallBold : Fonts.Small, label,
                i <= current ? ToolkitTheme.Text : ToolkitTheme.Muted, ToolkitTheme.Line | TextFormatFlags.HorizontalCenter | TextFormatFlags.Top);
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
    public int Fit(int x, int y) {
        if (Fonts == null) return 0;
        int h = Fonts.S(28);
        SetBounds(x, y, h + ToolkitTheme.TextWidth(Text, Fonts.SmallBold) + Fonts.S(14), h);
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
        if (ToolkitTheme.HighContrast) ToolkitTheme.Outline(g, SystemColors.WindowText, r, Height / 2, 1);
        int icon = Fonts.S(16), pad = (Height - icon) / 2;
        var box = new Rectangle(pad + Fonts.S(2), pad, icon, icon);
        if (Tone == "working") {
            float w = Math.Max(2f, Fonts.S(2));
            var ring = Rectangle.Inflate(box, -(int)w / 2 - 1, -(int)w / 2 - 1);
            using (var pen = new Pen(Color.FromArgb(ToolkitTheme.HighContrast ? 255 : 60, tone), w)) g.DrawEllipse(pen, ring);
            if (!ToolkitTheme.ReducedMotion || Frame >= 0) {
                double t = Frame >= 0 ? Frame : (since.ElapsedMilliseconds % 1000) / 1000.0;
                using (var pen = new Pen(tone, w)) { pen.StartCap = pen.EndCap = LineCap.Round; g.DrawArc(pen, ring, (float)(t * 360 - 90), 100); }
            }
        } else if (Glyph == "neutral") {
            using (var brush = new SolidBrush(ToolkitTheme.Muted)) g.FillEllipse(brush, Rectangle.Inflate(box, -Fonts.S(4), -Fonts.S(4)));
        } else ToolkitTheme.Badge(g, box, tone, ToolkitTheme.AccentText, Glyph);
        var text = new Rectangle(box.Right + Fonts.S(8), 0, Width - box.Right - Fonts.S(8), Height);
        TextRenderer.DrawText(g, Text, Fonts.SmallBold, text, ink, ToolkitTheme.Line);
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
public class ToolkitSetupStatus {
    static Form window;
    static Thread thread;
    static IntPtr ownerHandle;
    public static IntPtr OwnerHandle { get { return ownerHandle; } }
    static Label label, detail, elapsed;
    static ProgressBar progress;
    static Button yes, no;
    static readonly ToolkitQuestionState question = new ToolkitQuestionState();
    static readonly ConcurrentQueue<ToolkitInteractionEvent> events = new ConcurrentQueue<ToolkitInteractionEvent>();
    static readonly System.Diagnostics.Stopwatch clock = System.Diagnostics.Stopwatch.StartNew();
    static double stepStart, lastExternalAttention;
    static bool externalWait, forceClose;
    static string beforeWaitStep, beforeWaitDetail;
    static int beforeWaitPercent;
    static string currentStep = "Checking Windows and WSL", currentDetail = "Preparing setup log";
    static int currentPercent = -1;
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
    static void Attention() {
        if (window.WindowState == FormWindowState.Minimized) window.WindowState = FormWindowState.Normal;
        window.Show(); window.TopMost = true; window.BringToFront(); window.Activate();
        Flash(!window.ContainsFocus);
        System.Media.SystemSounds.Question.Play();
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
    public static void Start() {
        if (thread != null && thread.IsAlive) return;
        ready.Reset(); forceClose = false;
        thread = new Thread(delegate() {
            window = new Form(); window.Text = "Jibo DFU Toolkit";
            window.ClientSize = new Size(580, 310);
            window.StartPosition = FormStartPosition.CenterScreen;
            window.FormBorderStyle = FormBorderStyle.FixedDialog;
            window.ControlBox = false; window.KeyPreview = true;
            label = new Label(); label.SetBounds(20, 16, 540, 44); window.Controls.Add(label);
            progress = new ProgressBar(); progress.SetBounds(20, 70, 540, 20); window.Controls.Add(progress);
            elapsed = new Label(); elapsed.SetBounds(20, 100, 540, 24); window.Controls.Add(elapsed);
            detail = new Label(); detail.SetBounds(20, 132, 540, 110); window.Controls.Add(detail);
            yes = new Button(); yes.Text = "Yes"; yes.SetBounds(340, 260, 100, 30);
            no = new Button(); no.Text = "No"; no.SetBounds(450, 260, 110, 30);
            yes.Visible = no.Visible = false; window.Controls.Add(yes); window.Controls.Add(no);
            yes.Click += delegate { AnswerOnUi("yes"); }; no.Click += delegate { AnswerOnUi("no"); };
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
                    yes.Enabled = no.Enabled = question.KeysEnabled(now);
                    elapsed.Text = "Waiting for your answer. No is the safe default; closing counts as No.";
                    if (question.Remind(now)) { events.Enqueue(new ToolkitInteractionEvent("question-reminder", question.Prompt)); Attention(); }
                } else if (externalWait) {
                    elapsed.Text = "Waiting for you - this is not a setup stall.";
                    if (now - lastExternalAttention >= ToolkitQuestionState.ReminderSeconds) {
                        lastExternalAttention = now; events.Enqueue(new ToolkitInteractionEvent("user-wait-reminder", currentDetail)); Attention();
                    }
                } else { elapsed.Text = "This step: " + (int)(now - stepStart) + " seconds. Setup is supervised for stalls."; }
            };
            timer.Start();
            window.FormClosed += delegate { timer.Dispose(); ownerHandle = IntPtr.Zero; };
            window.Shown += delegate { ownerHandle = window.Handle; ready.Set(); };
            Application.Run(window);
        });
        thread.IsBackground = true; thread.SetApartmentState(ApartmentState.STA); thread.Start();
        if (!ready.WaitOne(10000)) throw new Exception("Could not show the toolkit setup window.");
    }
    static void PaintStatus() {
        label.Text = currentStep; detail.Text = currentDetail;
        progress.Visible = !question.Pending;
        progress.Style = currentPercent < 0 ? ProgressBarStyle.Marquee : ProgressBarStyle.Continuous;
        if (currentPercent >= 0) progress.Value = Math.Max(0, Math.Min(100, currentPercent));
    }
    public static void Update(string text, string hint, int percent) {
        if (window != null && window.IsHandleCreated && !window.IsDisposed) {
            OnUi(delegate {
                if (question.Pending || externalWait) return;
                if (percent < 0) stepStart = clock.Elapsed.TotalSeconds;
                currentStep = text; currentDetail = hint; currentPercent = percent; PaintStatus();
            });
        } else { currentStep = text; currentDetail = hint; currentPercent = percent; }
    }
    public static void AskQuestion(string prompt) {
        OnUi(delegate {
            question.Begin(prompt, clock.Elapsed.TotalSeconds);
            currentStep = "Waiting for your answer";
            currentDetail = prompt + "\r\n\r\nChoose Yes or No. Closing this question counts as No.";
            window.ControlBox = true; window.AcceptButton = no; window.CancelButton = no;
            yes.Enabled = no.Enabled = false; yes.Visible = no.Visible = true;
            PaintStatus(); Attention(); no.Focus();
        });
    }
    static void AnswerOnUi(string answer) {
        if (!question.Respond(answer, clock.Elapsed.TotalSeconds)) return;
        yes.Visible = no.Visible = false; window.AcceptButton = window.CancelButton = null;
        window.ControlBox = false; window.TopMost = false; Flash(false);
        currentStep = answer == "closed" ? "Question closed - this counts as No" : "Answer received: " + answer;
        currentDetail = answer == "closed" ? "You closed the question. Setup will stop; no approval was given." : "Continuing setup.";
        PaintStatus();
    }
    // Also used by keyboard/accessibility clients; same guard as button clicks.
    public static void AnswerQuestion(string answer) { OnUi(delegate { AnswerOnUi(answer); }); }
    public static void UserWait(bool active, string instructions) {
        OnUi(delegate {
            if (active && !externalWait) { beforeWaitStep = currentStep; beforeWaitDetail = currentDetail; beforeWaitPercent = currentPercent; }
            externalWait = active; window.ControlBox = false;
            if (active) {
                currentStep = "Waiting for you"; currentDetail = instructions;
                lastExternalAttention = clock.Elapsed.TotalSeconds; PaintStatus(); Attention();
            } else { currentStep = beforeWaitStep; currentDetail = beforeWaitDetail; currentPercent = beforeWaitPercent; window.TopMost = false; Flash(false); PaintStatus(); }
        });
    }
    sealed class Owner : IWin32Window { readonly IntPtr handle; public Owner(IntPtr h) { handle = h; } public IntPtr Handle { get { return handle; } } }
    public static bool Details(string message, string summary, string folder, bool allowWait) {
        bool keepWaiting = false;
        var dialogThread = new Thread(delegate() {
            using (var form = new Form()) {
                form.Text = allowWait ? "Jibo DFU Toolkit - setup stalled" : "Jibo DFU Toolkit - setup stopped";
                form.ClientSize = new Size(680, 440); form.StartPosition = FormStartPosition.CenterScreen;
                form.FormBorderStyle = FormBorderStyle.FixedDialog;
                form.MaximizeBox = false; form.MinimizeBox = false; form.TopMost = true;
                var explanation = new Label();
                explanation.Text = message + "\r\nCopy details to paste in Phoenix Discord #help. Review before sharing.";
                explanation.SetBounds(16, 16, 648, 100); form.Controls.Add(explanation);
                var text = new TextBox(); text.Multiline = true; text.ReadOnly = true; text.ScrollBars = ScrollBars.Both;
                text.WordWrap = false; text.Text = summary; text.SetBounds(16, 120, 648, 256); form.Controls.Add(text);
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
                form.ShowDialog(new Owner(ownerHandle));
            }
        });
        dialogThread.SetApartmentState(ApartmentState.STA); dialogThread.Start();
        dialogThread.Join(); // An explicit user decision, never a command timeout.
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
            $choice = [ToolkitConsoleNative]::MessageBox($owner, "$safe`r`n`r`nYes: Copy details. No: Open log folder. Cancel: Close.", 'Jibo DFU Toolkit - setup stopped', 0x00050213)
            try {
                if ($choice -eq 6) { Set-Clipboard -Value $summary }
                if ($choice -eq 7 -and $folder) { Start-Process -FilePath explorer.exe -ArgumentList ('"' + $folder + '"') | Out-Null }
            } catch { [ToolkitConsoleNative]::MessageBox($owner, 'Could not copy or open the log folder. Review the setup log before sharing it.', 'Jibo DFU Toolkit', 0x00010010) | Out-Null }
            return $false
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
function Invoke-ToolkitElevated([string]$File, [string]$Arguments, [double]$TimeoutSeconds = 180, [switch]$AllowWait) {
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
        if (-not (Wait-ToolkitProcess $process -TimeoutSeconds $TimeoutSeconds -AllowWait:$AllowWait)) {
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
function Wait-ToolkitProcess($Process, [double]$TimeoutSeconds = 120, [switch]$AllowWait) {
    $timer = [Diagnostics.Stopwatch]::StartNew()
    while (-not $Process.WaitForExit(100)) {
        Write-ToolkitInteractionEvents
        if ($script:ToolkitWaitingForUser) { $timer.Restart(); continue }
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
        $stalled = [bool]$_.Exception.Data['ToolkitStall']
        $result = [pscustomobject]@{Output=@(); ExitCode=$(if ($stalled) {124} else {1}); TimedOut=$stalled; StartError=$_.Exception.Message}
    } finally {
        $process.Dispose()
        if ($result) {
            $output = if ($result.ExitCode -ne 0) { ($result.Output -join "`n") } else { '' }
            # Redact BEFORE truncating: otherwise a long secret could lose its key.
            $output = Protect-ToolkitText $output
            # Keep useful diagnostics bounded; never persist argument lists.
            if ($output.Length -gt 8000) { $output = $output.Substring($output.Length - 8000) }
            Write-ToolkitLog 'command-end' @{ step=$commandStep; command=[IO.Path]::GetFileName($File); start_utc=$started.ToString('o'); end_utc=[DateTime]::UtcNow.ToString('o'); duration_seconds=[Math]::Round($timer.Elapsed.TotalSeconds,3); result=$(if ($result.TimedOut) { 'stalled' } elseif ($result.ExitCode -eq 0) { 'ok' } else { 'failed' }); exit_code=$result.ExitCode; error=[string]$result.StartError; output=$output }
        }
    }
    return $result
}
