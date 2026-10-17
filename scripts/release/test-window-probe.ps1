# Real Windows window probe; portable execution checks compilation only.
param([string]$ConsolePath=(Join-Path $PSScriptRoot 'console.ps1'))
$ErrorActionPreference='Stop'
. $ConsolePath
# Real launcher deadline with only WSL/process/window boundaries modeled. The
# short fixture budget is explicit; the product's two-minute limit is unchanged.
& {
    $tokens=$null; $errors=$null
    $bootstrap=Join-Path (Split-Path $ConsolePath) 'bootstrap.ps1'
    $ast=[Management.Automation.Language.Parser]::ParseFile($bootstrap,[ref]$tokens,[ref]$errors)
    if ($errors.Count) { throw 'Bootstrap does not parse' }
    foreach ($f in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]},$true)) { Invoke-Expression $f.Extent.Text }
    function Native { param($Arguments); return $Arguments[-1].Replace('\','/') }
    function Invoke-ToolkitNative { return [pscustomobject]@{ExitCode=0;Output=@('shared')} }
    function Write-ToolkitLog { param($Event,$Data) }
    function Complete-ToolkitStep { param($Result,$ExitCode,$ErrorText) }
    function Get-ToolkitGuiWindowState { return 'missing' }
    function Start-ToolkitProcess {
        param($FilePath,$ArgumentList)
        $ready=[regex]::Match($ArgumentList, '"([^"]+/ready)"$').Groups[1].Value
        $p=[pscustomobject]@{Handle=1;ExitCode=0;HasExited=$false;Ready=$ready;CloseObserved=$false}
        $p | Add-Member ScriptMethod WaitForExit {
            param($ms)
            if ($ms -eq 100) { Start-Sleep -Milliseconds 100; [IO.File]::WriteAllText($this.Ready, 'ready'); return $false }
            if ($ms -eq 5000) {
                if (Test-Path ($this.Ready + '.visible')) { throw 'Missing native window enabled operations' }
                if (-not (Test-Path ($this.Ready + '.close'))) { throw 'Timeout did not request safe close' }
                $this.CloseObserved=$true; $this.HasExited=$true
            }
            return $this.HasExited
        }
        $script:timeoutProcess=$p
        return $p
    }
    $timer=[Diagnostics.Stopwatch]::StartNew(); $refused=$false
    try { Invoke-WslGui -Name Ubuntu -User jibo -Target /fixture -Session /fixture/session -ReadyTimeoutSeconds 0.15 }
    catch {
        if ($_.Exception.Message -notmatch '^Windows toolkit window could not be verified within' -or -not $_.Exception.Data['ToolkitStall']) { throw }
        $refused=$true
    }
    if (-not $refused -or -not $script:timeoutProcess.CloseObserved -or $timer.Elapsed.TotalSeconds -gt 3) { throw 'Readiness deadline/idle cleanup failed' }
    'PASS launcher native-window deadline and idle-close request'
}
if (-not $script:ToolkitWindowProbeSource) { throw 'Missing Windows startup probe' }
Add-Type -TypeDefinition $script:ToolkitWindowProbeSource
if ($env:OS -eq 'Windows_NT') {
    Add-Type -AssemblyName System.Windows.Forms
    # The nonresponding fixture runs in its own process, like the real WSLg
    # window, and blocks its UI thread in kernel32 Sleep. A managed WaitOne on
    # an STA thread still dispatches sent messages (WM_NULL, WM_GETTEXT), so an
    # in-process "blocked" window answers the probe and proves nothing.
    Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class ToolkitWindowFixtureNative {
    [StructLayout(LayoutKind.Sequential)] public struct Rect { public int Left, Top, Right, Bottom; }
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] public static extern bool IsHungAppWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr h, out Rect rect);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder text, int n);
    public static string Title(IntPtr h) { var t = new StringBuilder(2048); GetWindowText(h, t, t.Capacity); return t.ToString(); }
    public static bool Eligible(IntPtr h) {
        Rect r;
        return IsWindow(h) && IsWindowVisible(h) && !IsIconic(h) && GetClientRect(h, out r) && r.Right > r.Left && r.Bottom > r.Top;
    }
}
'@
    $fixtureChild=@'
param([string]$Title,[string]$Mode,[string]$Ready)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms
# Signal and block in one native call path so no script code (and no pumping
# managed wait) runs between publishing the handle and blocking the UI thread.
Add-Type -TypeDefinition 'using System.IO; using System.Runtime.InteropServices; public static class ToolkitFixtureBlock { [DllImport("kernel32.dll")] static extern void Sleep(uint milliseconds); public static void SignalAndBlock(string ready, string text) { File.WriteAllText(ready + ".tmp", text); File.Move(ready + ".tmp", ready); Sleep(60000); } }'
$form=New-Object Windows.Forms.Form
$form.Text=$Title; $form.Show(); [Windows.Forms.Application]::DoEvents()
$handle=[string]$form.Handle.ToInt64()
if ($Mode -eq 'block') { [ToolkitFixtureBlock]::SignalAndBlock($Ready, $handle) }
else { [IO.File]::WriteAllText("$Ready.tmp", $handle); [IO.File]::Move("$Ready.tmp", $Ready); [Windows.Forms.Application]::Run($form) }
'@
    $fixtureRoot=Join-Path ([IO.Path]::GetTempPath()) ('toolkit-window-fixture-' + [guid]::NewGuid().ToString('N'))
    $null=New-Item -ItemType Directory -Path $fixtureRoot
    $fixtureScript=Join-Path $fixtureRoot 'window.ps1'
    [IO.File]::WriteAllText($fixtureScript, $fixtureChild)
    function Invoke-WindowFixtureProcess([string]$Title, [string]$Mode, [scriptblock]$Check) {
        $ready=Join-Path $fixtureRoot "$Mode.ready"
        $psi=New-Object Diagnostics.ProcessStartInfo
        $psi.FileName=[Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
        $psi.Arguments=('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -Title "{1}" -Mode {2} -Ready "{3}"' -f $fixtureScript,$Title,$Mode,$ready)
        $psi.UseShellExecute=$false
        $child=[Diagnostics.Process]::Start($psi)
        try {
            $wait=[Diagnostics.Stopwatch]::StartNew()
            while (-not (Test-Path -LiteralPath $ready)) {
                if ($child.HasExited) { throw "Fixture window process exited early ($($child.ExitCode))" }
                if ($wait.Elapsed.TotalSeconds -gt 60) { throw 'Fixture window process did not start' }
                Start-Sleep -Milliseconds 20
            }
            $handle=[IntPtr][long][IO.File]::ReadAllText($ready)
            & $Check $handle
        } finally {
            if (-not $child.HasExited) { $child.Kill() }
            if (-not $child.WaitForExit(5000)) { throw 'Fixture window process survived' }
        }
    }
    $form=New-Object Windows.Forms.Form
    $token='fixture-' + [guid]::NewGuid().ToString('N')
    try {
        $form.Text="Jibo DFU Toolkit (build aaaaaaa) [startup $token] (Jibo-DFU-Toolkit)"
        $null=$form.Handle
        if ([ToolkitWindowProbe]::State($token) -ne 'missing') { throw 'Hidden window admitted' }
        $form.Show(); [Windows.Forms.Application]::DoEvents()
        if ([ToolkitWindowProbe]::State($token) -ne 'visible') { throw 'Visible window refused' }
        if ([ToolkitWindowProbe]::State('another-invocation') -ne 'missing') { throw 'Unrelated window admitted' }
        $form.Text='[WARN:COPY MODE] ' + $form.Text
        [Windows.Forms.Application]::DoEvents()
        if ([ToolkitWindowProbe]::State($token) -ne 'copy') { throw 'Copy-mode window admitted' }
        $form.Text=$form.Text.Replace('[WARN:COPY MODE] ', '')
        $form.WindowState='Minimized'; [Windows.Forms.Application]::DoEvents()
        if ([ToolkitWindowProbe]::State($token) -eq 'visible') { throw 'Minimized window admitted' }
    } finally { $form.Dispose() }
    try {
        $title="Jibo DFU Toolkit (build aaaaaaa) [startup $token] (Ubuntu)"
        # Positive control through the same separate-process harness.
        Invoke-WindowFixtureProcess $title 'pump' {
            param($handle)
            $state=[ToolkitWindowProbe]::State($token)
            if ($state -ne 'visible') { throw "Responsive separate-process window refused ($state)" }
        }
        Invoke-WindowFixtureProcess $title 'block' {
            param($handle)
            $timer=[Diagnostics.Stopwatch]::StartNew()
            $state=[ToolkitWindowProbe]::State($token)
            $elapsed=$timer.ElapsedMilliseconds
            if ($state -eq 'visible') { throw "Nonresponding window admitted after $elapsed ms" }
            if ($timer.Elapsed.TotalSeconds -gt 1.5) { throw "Hung window probe was not bounded ($elapsed ms)" }
            # Refused for the intended reason only: the window was a visible,
            # sized, non-minimized match that failed the WM_NULL check.
            if ($state -ne 'missing') { throw "Nonresponding window gave unexpected state $state" }
            if ([ToolkitWindowFixtureNative]::Title($handle) -ne $title) { throw 'Nonresponding fixture title did not match' }
            if (-not [ToolkitWindowFixtureNative]::Eligible($handle)) { throw 'Nonresponding fixture was not a visible, sized window' }
            if ($elapsed -lt 150 -and -not [ToolkitWindowFixtureNative]::IsHungAppWindow($handle)) { throw "Nonresponding window refused without a responsiveness timeout ($elapsed ms)" }
        }
    } finally { Remove-Item -LiteralPath $fixtureRoot -Recurse -Force -ErrorAction SilentlyContinue }
    'PASS native Windows display probe (visible, hidden, unrelated, copy, minimized, nonresponding)'
} else { 'PASS display probe compiled (Win32 not executed)' }
