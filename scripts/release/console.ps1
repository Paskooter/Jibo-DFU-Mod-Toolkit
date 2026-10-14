# Windows release console control; native launchers do not load this helper.
function Initialize-ToolkitConsole {
    if ('ToolkitConsoleNative' -as [type]) { return $true }
    if ($env:OS -ne 'Windows_NT') { return $false }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public class ToolkitConsoleNative {
    [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hwnd, int command);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hwnd);
    [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr hwnd, int attribute, out int value, int size);
    [DllImport("user32.dll", CharSet=CharSet.Unicode, EntryPoint="MessageBoxW")]
    public static extern int MessageBox(IntPtr hwnd, string text, string title, uint flags);
}
'@
    return $true
}
function Get-ToolkitConsole {
    if (-not (Initialize-ToolkitConsole)) { return $null }
    $handle = [ToolkitConsoleNative]::GetConsoleWindow()
    if ($handle -eq [IntPtr]::Zero) { return $null }
    # ConPTY's HWND is cloaked, not the displayed Terminal window. Its show/hide
    # compatibility shim supports minimizing, not hiding a Terminal tab/window.
    $cloaked = 0
    $result = [ToolkitConsoleNative]::DwmGetWindowAttribute($handle, 14, [ref]$cloaked, 4)
    return [pscustomobject]@{Handle=$handle; Pseudo=($env:WT_SESSION -or ($result -eq 0 -and $cloaked -ne 0))}
}
function Invoke-ToolkitConsoleLock([scriptblock]$Operation) {
    # The USB monitor and GUI supervisor share one console. Serialize window
    # changes with UAC prompts so readiness cannot hide a prompt in progress.
    $console = Get-ToolkitConsole
    $key = if ($console) { $console.Handle.ToInt64() } else { 0 }
    $mutex = New-Object Threading.Mutex($false, "Local\JiboDfuConsole-$key")
    $owned = $false
    try {
        try { $owned = $mutex.WaitOne() }
        catch [Threading.AbandonedMutexException] { $owned = $true }
        & $Operation
    } finally {
        if ($owned) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}
function Invoke-ToolkitConsolePrompt([scriptblock]$Action) {
    Invoke-ToolkitConsoleLock {
        Set-ToolkitConsole -Visible
        & $Action
    }
}
function Set-ToolkitConsole([switch]$Visible) {
    $console = Get-ToolkitConsole
    if (-not $console) { return }
    Invoke-ToolkitConsoleLock {
        if ($Visible) {
            [ToolkitConsoleNative]::ShowWindow($console.Handle, 9) | Out-Null # SW_RESTORE
        } elseif ($console.Pseudo) {
            [ToolkitConsoleNative]::ShowWindow($console.Handle, 6) | Out-Null # SW_MINIMIZE
        } else {
            # ShowWindow returns the PREVIOUS visibility, not an operation status.
            [ToolkitConsoleNative]::ShowWindow($console.Handle, 0) | Out-Null # SW_HIDE
            if ([ToolkitConsoleNative]::IsWindowVisible($console.Handle)) {
                [ToolkitConsoleNative]::ShowWindow($console.Handle, 6) | Out-Null
            }
        }
    }
}
function Show-ToolkitFailure([string]$Message) {
    try { Set-ToolkitConsole -Visible } catch { Write-Warning 'Could not restore the command window.' }
    Write-Host "Launcher: $Message" -ForegroundColor Red
    # A pseudo HWND cannot prove that the real Terminal restored (or selected
    # this tab). A visible dialog also covers old/unsupported terminal hosts.
    try {
        $console = Get-ToolkitConsole
        if (-not $console -or $console.Pseudo -or -not [ToolkitConsoleNative]::IsWindowVisible($console.Handle) -or [ToolkitConsoleNative]::IsIconic($console.Handle)) {
            if (Initialize-ToolkitConsole) {
                [ToolkitConsoleNative]::MessageBox([IntPtr]::Zero, $Message, 'Jibo DFU Toolkit', 0x00010010) | Out-Null
            }
        }
    } catch { Write-Warning 'Could not show the error dialog. Use -ShowConsole for troubleshooting.' }
}
