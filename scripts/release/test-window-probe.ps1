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
    Add-Type -ReferencedAssemblies System.Windows.Forms,System.Drawing -TypeDefinition @'
using System;
using System.Threading;
using System.Windows.Forms;
public static class ToolkitHungWindowFixture {
    static readonly ManualResetEvent ready = new ManualResetEvent(false);
    static readonly ManualResetEvent release = new ManualResetEvent(false);
    static Thread thread;
    public static void Start(string title) {
        thread = new Thread(delegate() {
            using (var form = new Form()) {
                form.Text = title; form.Show(); Application.DoEvents();
                ready.Set(); release.WaitOne();
            }
        });
        thread.IsBackground = true; thread.SetApartmentState(ApartmentState.STA); thread.Start();
        if (!ready.WaitOne(5000)) throw new Exception("Fixture window did not start");
    }
    public static void Stop() { release.Set(); if (!thread.Join(5000)) throw new Exception("Fixture thread survived"); }
}
'@
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
        [ToolkitHungWindowFixture]::Start("Jibo DFU Toolkit (build aaaaaaa) [startup $token] (Ubuntu)")
        $timer=[Diagnostics.Stopwatch]::StartNew()
        if ([ToolkitWindowProbe]::State($token) -eq 'visible') { throw 'Nonresponding window admitted' }
        if ($timer.Elapsed.TotalSeconds -gt 1.5) { throw 'Hung window probe was not bounded' }
    } finally { [ToolkitHungWindowFixture]::Stop() }
    'PASS native Windows display probe (visible, hidden, unrelated, copy, minimized, nonresponding)'
} else { 'PASS display probe compiled (Win32 not executed)' }
