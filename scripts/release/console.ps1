# Windowless release support. Source launchers retain their interactive console.
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
    # MB_YESNO | MB_ICONQUESTION | MB_DEFBUTTON2 | MB_SETFOREGROUND.
    return ([ToolkitConsoleNative]::MessageBox([IntPtr]::Zero, $Question, 'Jibo DFU Toolkit', 0x00010124) -eq 6)
}
function Start-ToolkitSetup {
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
    static readonly ManualResetEvent ready = new ManualResetEvent(false);
    public static void Start() {
        if (thread != null) return;
        thread = new Thread(delegate() {
            window = new Form();
            window.Text = "Jibo DFU Toolkit";
            window.ClientSize = new Size(410, 100);
            window.StartPosition = FormStartPosition.CenterScreen;
            window.FormBorderStyle = FormBorderStyle.FixedDialog;
            window.ControlBox = false;
            var label = new Label();
            label.Text = "Setting up Jibo DFU Toolkit\u2026";
            label.AutoSize = true;
            label.Location = new Point(20, 18);
            window.Controls.Add(label);
            var progress = new ProgressBar();
            progress.Style = ProgressBarStyle.Marquee;
            progress.SetBounds(20, 52, 370, 20);
            window.Controls.Add(progress);
            window.Shown += delegate { ready.Set(); };
            Application.Run(window);
        });
        thread.IsBackground = true;
        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        if (!ready.WaitOne(10000)) throw new Exception("Could not show the toolkit setup window.");
    }
    public static void Stop() {
        if (window != null && window.IsHandleCreated && !window.IsDisposed)
            window.BeginInvoke(new Action(delegate { window.Close(); }));
        if (thread != null) thread.Join(10000);
    }
}
'@
    }
    [ToolkitSetupStatus]::Start()
}
function Stop-ToolkitSetup {
    if ('ToolkitSetupStatus' -as [type]) { [ToolkitSetupStatus]::Stop() }
}
function Show-ToolkitFailure([string]$Message) {
    Stop-ToolkitSetup
    Write-Host "Launcher: $Message" -ForegroundColor Red
    if (Initialize-ToolkitConsole) {
        [ToolkitConsoleNative]::MessageBox([IntPtr]::Zero, $Message, 'Jibo DFU Toolkit', 0x00010010) | Out-Null
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
function Invoke-ToolkitNative([string]$File, [string[]]$Arguments) {
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
        $process.WaitForExit()
        # Match the classic "& $FilePath @ArgumentList 2>&1" capture shape: one
        # array item per OUTPUT LINE, not the entire stdout/stderr text as a
        # single string. Every run.ps1/bootstrap.ps1 consumer (Get-Wsl2Distributions'
        # per-item row regex, usbipd state JSON reassembly, wslpath) depends on this.
        $lines = @(ConvertTo-ToolkitNativeLines $stdout.GetAwaiter().GetResult()) +
                 @(ConvertTo-ToolkitNativeLines $stderr.GetAwaiter().GetResult())
        return [pscustomobject]@{Output=$lines; ExitCode=$process.ExitCode; StartError=$null}
    } catch {
        return [pscustomobject]@{Output=@(); ExitCode=1; StartError=$_.Exception.Message}
    } finally { $process.Dispose() }
}
