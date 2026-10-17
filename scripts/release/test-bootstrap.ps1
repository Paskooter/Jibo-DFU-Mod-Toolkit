# Parse and exercise the bootstrap without real WSL, USB, installs or UAC.
param([string]$BootstrapPath = (Join-Path $PSScriptRoot 'bootstrap.ps1'), [string]$Scenario = '*')
$ErrorActionPreference = 'Stop'
$bootstrapText = [IO.File]::ReadAllText($BootstrapPath)
if ($bootstrapText -match 'Read-Host') { throw 'Windowless bootstrap must never wait for console input' }
if ($bootstrapText -notmatch 'Invoke-ToolkitElevated' -or [IO.File]::ReadAllText((Join-Path (Split-Path $BootstrapPath) 'console.ps1')) -notmatch '-Verb RunAs -WindowStyle Hidden') { throw 'Elevated setup must use the bounded hidden helper' }
$hostBinary = (Get-Process -Id $PID).Path
$path = $BootstrapPath
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$functions = $ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $true)
foreach ($function in $functions) { Invoke-Expression $function.Extent.Text }
. (Join-Path (Split-Path $BootstrapPath) 'console.ps1')
function Invoke-ToolkitNative { param($File, [string[]]$Arguments); $output = @(& wsl.exe @Arguments); return [pscustomobject]@{Output=$output; ExitCode=$LASTEXITCODE; StartError=$null} }
function Show-ToolkitQuestion { param($Question); return $script:answer -eq 'yes' }
$script:answer = 'n'
function Read-Host { param($Prompt); return $script:answer }
if (Confirm-Setup 'Test') { throw 'Decline was not respected' }
$script:answer = 'yes'
if (-not (Confirm-Setup 'Test')) { throw 'Explicit approval was not accepted' }
$script:received = @()
function wsl.exe { $script:received = $args; $global:LASTEXITCODE=0; return "Ubuntu`0" }
$result = Native -Arguments @('--distribution', 'Ubuntu with spaces', '--exec', 'true')
if ($result -ne 'Ubuntu' -or $script:received.Count -ne 4 -or $script:received[1] -ne 'Ubuntu with spaces') { throw 'Native argument or NUL handling failed' }
function wsl.exe { $global:LASTEXITCODE=1; return 'not installed' }
$failed = $false
try { Native -Arguments @('--version') | Out-Null } catch { $failed = $true }
if (-not $failed) { throw 'Native failure was swallowed' }
# Diagnose old glibc/musl distributions without running a real WSL process.
function wsl.exe { $global:LASTEXITCODE=0; return 'glibc 2.35' }
if (-not (Test-CompatibleDistribution 'fixture')) { throw 'Supported distro refused' }
function wsl.exe { $global:LASTEXITCODE=0; return 'glibc 2.31' }
if (Test-CompatibleDistribution 'fixture') { throw 'Old glibc accepted' }
function wsl.exe { $global:LASTEXITCODE=0; return 'musl' }
if (Test-CompatibleDistribution 'fixture') { throw 'musl distro accepted' }
# Original USB helper is shipped byte-for-byte, rather than a second USB algorithm.
$usbPath = Join-Path (Split-Path (Split-Path $PSScriptRoot)) 'run.ps1'
$usbErrors=$null
[System.Management.Automation.Language.Parser]::ParseFile($usbPath, [ref]$tokens, [ref]$usbErrors) | Out-Null
if ($usbErrors.Count) { throw ($usbErrors | Out-String) }
$usbAst = [System.Management.Automation.Language.Parser]::ParseFile($usbPath, [ref]$tokens, [ref]$usbErrors)
$select = $usbAst.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Select-Distribution'}, $true)[0]
Invoke-Expression $select.Extent.Text
$privateRows = @([pscustomobject]@{Name='Jibo-DFU-Toolkit'; Version=2; IsDefault=$true})
foreach ($requested in @('', 'Jibo-DFU-Toolkit')) {
    $UsbOnly = $false; $refused = $false
    try { Select-Distribution -Distributions $privateRows -Requested $requested | Out-Null }
    catch { if ($_.Exception.Message -notmatch 'Jibo-DFU-Windows-x64.exe') { throw }; $refused = $true }
    if (-not $refused) { throw 'Source build selected the private no-sudo distro' }
}
$UsbOnly = $true
if ((Select-Distribution -Distributions $privateRows -Requested 'Jibo-DFU-Toolkit') -cne 'Jibo-DFU-Toolkit') { throw 'Release USB helper lost private distro access' }
$UsbOnly = $false
Write-Output 'PASS helpers: source private no-sudo guard (automatic/explicit); release USB access preserved'
$questionFixture = Join-Path ([IO.Path]::GetTempPath()) ('dfu-question-log-' + [guid]::NewGuid().ToString('N'))
try {
    Initialize-ToolkitLog -Root $questionFixture -Build ('b' * 40)
    Set-ToolkitStep 'Finding Linux'
    function Show-ToolkitQuestion { param($Question); $script:ToolkitQuestionAnswer='closed'; return $false }
    if (Confirm-Setup 'Set up a private Linux?') { throw 'Closed setup question was accepted' }
    $questions = @(Get-Content $script:ToolkitLogPath | ForEach-Object { $_ | ConvertFrom-Json })
    $asked = @($questions | Where-Object event -eq 'question-start')[-1]
    $answered = @($questions | Where-Object event -eq 'question-end')[-1]
    if ($asked.question -ne 'Set up a private Linux?' -or $answered.answer -ne 'closed' -or -not $asked.utc -or -not $answered.utc -or $answered.duration_seconds -lt 0) { throw 'Question log lacks text, answer or timing' }
    1..12 | ForEach-Object { Write-ToolkitLog 'other' @{message='later event'} }
    if ((Get-ToolkitSupportDetails 'failure') -notmatch 'closed') { throw 'Copy details lost question history' }
    Write-Output 'PASS helpers: question text/closed answer/times/support history'
} finally {
    $script:ToolkitLogPath=$null; $script:ToolkitStepTimer=$null; $script:ToolkitFailedStep=$null
    Remove-Item -Recurse -Force $questionFixture
}


# Exercise the entire missing-WSLg refusal path in a child process. A mock admin
# function leaves a sentinel if any installation is attempted after declining.
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('dfu-bootstrap-test-' + [guid]::NewGuid())
New-Item -ItemType Directory $fixture | Out-Null
try {
    Copy-Item $BootstrapPath (Join-Path $fixture 'bootstrap.ps1')
    $path = Join-Path $fixture 'bootstrap.ps1'
    $mockHelper = [IO.File]::ReadAllText((Join-Path (Split-Path $BootstrapPath) 'console.ps1')) + @'

function Invoke-ToolkitNative { param($File, [string[]]$Arguments); $output = @(& wsl.exe @Arguments); return [pscustomobject]@{Output=$output; ExitCode=$LASTEXITCODE; StartError=$null} }
function Show-ToolkitQuestion { param($Question); return $false }
function Start-ToolkitSetup {}
function Stop-ToolkitSetup {}
function Show-ToolkitSupportDialog { param($Message, [switch]$AllowWait); return $false }
'@
    [IO.File]::WriteAllText((Join-Path $fixture 'console.ps1'), $mockHelper)
    [IO.File]::WriteAllText((Join-Path $fixture 'payload.tar.gz'), 'not read on refusal')
    [IO.File]::WriteAllText((Join-Path $fixture 'run.ps1'), 'not run on refusal')
    Copy-Item (Join-Path (Split-Path (Split-Path (Split-Path $BootstrapPath))) 'usbipd.ps1') $fixture
    [IO.File]::WriteAllText((Join-Path $fixture 'revision.txt'), ('a' * 40))
    $child = Join-Path $fixture 'refusal.ps1'
    $childText = @'
param($Bootstrap, $Payload, $Case)
# Native dialogs are an external boundary; console.ps1 is exercised separately.
Add-Type -TypeDefinition 'using System; public class ToolkitConsoleNative { public static IntPtr GetConsoleWindow() { return IntPtr.Zero; } public static bool ShowWindow(IntPtr h, int c) { return false; } public static bool IsWindowVisible(IntPtr h) { return true; } public static bool IsIconic(IntPtr h) { return false; } public static int DwmGetWindowAttribute(IntPtr h, int a, out int v, int s) { v=0; return 0; } public static int MessageBox(IntPtr h, string t, string c, uint f) { return 1; } }'
$global:BootstrapTestCase = $Case
$global:BootstrapTestSentinel = Join-Path $Payload 'unexpected-admin'
function wsl.exe {
    if ($global:BootstrapTestCase -eq 'no-distro' -and $args[0] -eq '--version') {
        $global:LASTEXITCODE=0; return 'WSLg 1.0'
    }
    $global:LASTEXITCODE=1; return 'No installed distributions or GUI support'
}
function Read-Host { param($Prompt); return 'n' }
function Start-Process { [IO.File]::WriteAllText($global:BootstrapTestSentinel, 'failed'); throw 'Unexpected admin action' }
$parameters = @{PayloadRoot=$Payload}
if ($Case -like 'explicit-*') { $parameters['Distro'] = if ($Case -eq 'explicit-missing') { 'NotInstalled' } else { 'NVIDIA-Workbench' } }
& $Bootstrap @parameters
exit $LASTEXITCODE
'@
    [IO.File]::WriteAllText($child, $childText)
    foreach ($case in @('no-gui', 'no-distro')) {
        $messages = @(& $hostBinary -NoProfile -File $child -Bootstrap $path -Payload $fixture -Case $case 2>&1)
        $expected = if ($case -eq 'no-distro') { 'Distribution setup declined' } else { 'No setup changes were made' }
        if ($LASTEXITCODE -ne 1 -or (Test-Path (Join-Path $fixture 'unexpected-admin')) -or (($messages -join ' ') -notmatch $expected)) {
            throw ('Full bootstrap refusal path failed (' + $case + '): ' + ($messages -join ' '))
        }
    }
} finally { Remove-Item -Recurse -Force $fixture }
# Real native-process argv roundtrip, no admin/WSL. A raw CLR argv probe is
# deliberate: Windows PowerShell 5.1 -File is itself an argument transformer,
# so it is not an independent oracle for the argv wsl.exe will receive.
$argvFixture = Join-Path ([IO.Path]::GetTempPath()) ('dfu-argv-' + [guid]::NewGuid())
New-Item -ItemType Directory $argvFixture | Out-Null
try {
    $argvChild = Join-Path $argvFixture 'argv.exe'
    $argvOutput = Join-Path $argvFixture 'argv.txt'
    $source = @'
using System;
using System.Text;
public class NativeArgvProbe {
    public static void Main(string[] args) {
        Console.WriteLine(args.Length);
        foreach (string value in args) {
            Console.WriteLine("ARG:" + Convert.ToBase64String(Encoding.UTF8.GetBytes(value)));
        }
    }
}
'@
    if ($env:OS -eq 'Windows_NT') {
        Add-Type -TypeDefinition $source -Language CSharp -OutputAssembly $argvChild -OutputType ConsoleApplication
        $argvHost = $argvChild
        $prefix = @()
    } else {
        # Local Linux verification uses the already installed Mono compiler/runtime.
        $sourcePath = Join-Path $argvFixture 'argv.cs'
        [IO.File]::WriteAllText($sourcePath, $source)
        & mcs ('-out:' + $argvChild) $sourcePath
        if ($LASTEXITCODE -ne 0) { throw 'Native argv probe compilation failed' }
        $argvHost = (Get-Command mono -ErrorAction Stop).Source
        $prefix = @($argvChild)
    }
    $values = @('Ubuntu with spaces', '/path with spaces/helper.sh', 'embedded"quote', 'C:\trailing\', '')
    $argumentLine = ConvertTo-NativeArgumentLine ($prefix + $values)
    $process = Start-Process -FilePath $argvHost -ArgumentList $argumentLine -RedirectStandardOutput $argvOutput -Wait -PassThru
    $lines = [IO.File]::ReadAllLines($argvOutput)
    if ($process.ExitCode -ne 0 -or $lines.Count -ne ($values.Count + 1) -or $lines[0] -ne [string]$values.Count) {
        throw ('Native argv roundtrip count failed: ' + ($lines -join '|'))
    }
    for ($i=0; $i -lt $values.Count; $i++) {
        if (-not $lines[$i+1].StartsWith('ARG:')) { throw 'Native argv probe output is incomplete' }
        $actual = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($lines[$i+1].Substring(4)))
        if ($actual -cne $values[$i]) { throw ('Native argv roundtrip failed at index ' + $i) }
    }
} finally { Remove-Item -Recurse -Force $argvFixture }
# wsl.exe option parsing, mirrored from microsoft/WSL src/windows/common/WslClient.cpp
# (WslMain + helpers::ParseArgument): options are split at the first space/tab with
# NO quote handling until --exec/-e; only the remainder goes to CommandLineToArgvW.
# A token that is not a known option (e.g. a quoted "--distribution") ends option
# parsing and the whole remaining line runs through the default distro's shell.
function Split-WslOptions([string]$Line) {
    $options = @{}
    $rest = $Line
    while ($true) {
        $rest = $rest.TrimStart(" `t")
        $index = $rest.IndexOfAny([char[]]@(' ', "`t"))
        $token = if ($index -ge 0) { $rest.Substring(0, $index) } else { $rest }
        if (-not $token) { return [pscustomobject]@{Options=$options; Exec=$false; Rest=''} }
        if ($token -cin @('--distribution', '-d', '--user', '-u')) {
            $rest = $rest.Substring($token.Length).TrimStart(" `t")
            $index = $rest.IndexOfAny([char[]]@(' ', "`t"))
            $value = if ($index -ge 0) { $rest.Substring(0, $index) } else { $rest }
            $options[$token.TrimStart('-').Substring(0, 1)] = $value
            $rest = $rest.Substring($value.Length)
            continue
        }
        if ($token -cin @('--exec', '-e')) {
            return [pscustomobject]@{Options=$options; Exec=$true; Rest=$rest.Substring($token.Length).TrimStart(" `t")}
        }
        return [pscustomobject]@{Options=$options; Exec=$false; Rest=$rest}
    }
}
$parsed = Split-WslOptions '"--distribution" "Ubuntu-22.04" "--user" "root" "--exec" "bash"'
if ($parsed.Exec -or $parsed.Options.Count) { throw 'wsl.exe option emulator accepted quoted options' }
$parsed = Split-WslOptions '--distribution Ubuntu-22.04 --user root --exec "bash" "/a b"'
if (-not $parsed.Exec -or $parsed.Options['d'] -cne 'Ubuntu-22.04' -or $parsed.Options['u'] -cne 'root' -or $parsed.Rest -cne '"bash" "/a b"') { throw 'wsl.exe option emulator rejected bare options' }
foreach ($name in @('Ubuntu-22.04', 'NVIDIA-Workbench', 'docker-desktop', 'Debian.12_x')) {
    if (-not (Test-WslDistributionName $name)) { throw ('Valid WSL distribution name refused: ' + $name) }
}
foreach ($name in @('Ubuntu with spaces', 'a"b', "a`tb", '', ('x' * 257))) {
    if (Test-WslDistributionName $name) { throw ('Invalid WSL distribution name accepted: ' + $name) }
}
# Windows privileges: root inside the user's own distro needs neither UAC nor a Linux
# password; the GUI uses an explicit non-root user.
function Start-ToolkitSetup {}
function Stop-ToolkitSetup {}
function Show-ToolkitSupportDialog { param($Message, [switch]$AllowWait); return $false }
function Start-ToolkitProcess { param($FilePath, $ArgumentList); return (Start-Process -FilePath $FilePath -ArgumentList $ArgumentList -WindowStyle Hidden -PassThru) }
$script:calls = @()
function Native { param([string[]]$Arguments); $script:calls += ,$Arguments; return '' }
function Invoke-ApprovedAdmin { param($File, $Arguments); $script:calls += ,@('UAC', $File, $Arguments) }
function Start-Process {
    param($FilePath, $ArgumentList, [switch]$NoNewWindow, [switch]$PassThru, $WindowStyle)
    $script:calls += ,@('WATCH', $FilePath, $ArgumentList)
    return [pscustomobject]@{ HasExited=$false; Id=4242 }
}
$helper = '/mnt/c/path with spaces/usb.sh'
$watch = Start-WslUsbAccess -Name 'Ubuntu-22.04' -User 'fixture' -Uid '1000' -Gid '1001' -Helper $helper -Session '/home/fixture/.usb-session'
if (@($script:calls | Where-Object { $_[0] -eq 'UAC' }).Count) { throw 'WSL root USB setup requested a Windows administrator prompt' }
if ($script:calls.Count -ne 3) { throw ('Unexpected USB setup calls: ' + ($script:calls | ConvertTo-Json -Compress)) }
if (($script:calls[0] -join '|') -cne ('--distribution|Ubuntu-22.04|--user|root|--exec|bash|' + $helper + '|1000|1001|once')) { throw 'Initial root USB grant did not use the user''s own WSL session' }
if (($script:calls[1] -join '|') -cne '--distribution|Ubuntu-22.04|--user|fixture|--exec|touch|--|/home/fixture/.usb-session') { throw 'Session marker was not created by the normal user' }
if ($script:calls[2][0] -ne 'WATCH') { throw 'Root USB watcher was not started' }
$parsed = Split-WslOptions ([string]$script:calls[2][2])
if (-not $parsed.Exec -or $parsed.Options['d'] -cne 'Ubuntu-22.04' -or $parsed.Options['u'] -cne 'root') {
    throw ('wsl.exe would not parse the watcher options (it would run them through the shell): ' + $script:calls[2][2])
}
if ($parsed.Rest -cne (ConvertTo-NativeArgumentLine @('bash', $helper, '1000', '1001', '/home/fixture/.usb-session'))) { throw 'Watcher command arguments were not preserved' }
$script:calls = @()
function Native { param([string[]]$Arguments); $script:calls += ,$Arguments; if ($Arguments -contains 'once') { throw 'WSL failed: synthetic root grant failure' }; return '' }
$failed = $false
try { Start-WslUsbAccess -Name 'Ubuntu-22.04' -User 'fixture' -Uid '1000' -Gid '1001' -Helper '/usb.sh' -Session '/session' | Out-Null } catch { $failed = $true }
if (-not $failed -or $script:calls.Count -ne 1) { throw 'Root USB grant failure did not stop before session/watcher setup' }
$script:calls = @()
$failed = $false
try { Start-WslUsbAccess -Name 'Ubuntu with spaces' -User 'fixture' -Uid '1000' -Gid '1001' -Helper '/usb.sh' -Session '/session' | Out-Null } catch { $failed = $true }
if (-not $failed -or $script:calls.Count) { throw 'Invalid distribution name reached wsl.exe' }
$script:calls = @()
function Native { param([string[]]$Arguments); return $Arguments[-1] }
function Start-Process {
    param($FilePath, $ArgumentList, [switch]$NoNewWindow, [switch]$PassThru, $WindowStyle)
    $script:calls += ,@($FilePath, $ArgumentList)
    $process = [pscustomobject]@{ExitCode=41}
    $process | Add-Member ScriptMethod WaitForExit { param($Timeout); return $true }
    return $process
}
$code = Invoke-WslGui -Name 'Ubuntu-22.04' -User 'fixture' -Target '/home/fixture/path with spaces' -Session '/session'
if ($ErrorActionPreference -ne 'Stop') { throw 'GUI native handling leaked preference changes' }
$parsed = Split-WslOptions ([string]$script:calls[0][1])
if ($code -ne 41 -or -not $parsed.Exec -or $parsed.Options['d'] -ne 'Ubuntu-22.04' -or $parsed.Options['u'] -ne 'fixture' -or $parsed.Rest -notlike '"bash" "/home/fixture/path with spaces/wsl-user-launch.sh" "/home/fixture/path with spaces/jibo-dfu" "/session" *') { throw 'GUI identity, direct binary dispatch or exit propagation failed' }
# Actual top-level bootstrap, with traces at every external boundary.
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('dfu-bootstrap-flow-' + [guid]::NewGuid())
New-Item -ItemType Directory $fixture | Out-Null
$passed = 0; $failedCases = @()
try {
    Copy-Item $BootstrapPath (Join-Path $fixture 'bootstrap.ps1')
    $path = Join-Path $fixture 'bootstrap.ps1'
    $mockHelper = [IO.File]::ReadAllText((Join-Path (Split-Path $BootstrapPath) 'console.ps1')) + @'

function Start-ToolkitProcess { param($FilePath, $ArgumentList); return (Start-Process -FilePath $FilePath -ArgumentList $ArgumentList -WindowStyle Hidden -PassThru) }
function Invoke-ToolkitNative {
    param($File, [string[]]$Arguments)
    $output = if ($File -eq 'powershell.exe') { @(& powershell.exe @Arguments) } else { @(& wsl.exe @Arguments) }
    return [pscustomobject]@{Output=$output; ExitCode=$LASTEXITCODE; StartError=$null}
}
function Show-ToolkitQuestion { param($Question); return ((Read-Host $Question) -match '^(?i:y|yes)$') }
function Receive-ToolkitDownload { param($Uri, $OutFile, $ExpectedBytes); Record 'download' @($Uri, $OutFile); [IO.File]::WriteAllText($OutFile, 'fixture') }
function Start-ToolkitSetup {}
function Stop-ToolkitSetup {}
function Show-ToolkitSupportDialog { param($Message, [switch]$AllowWait); return $false }
'@
    [IO.File]::WriteAllText((Join-Path $fixture 'console.ps1'), $mockHelper)
    foreach ($file in @('payload.tar.gz', 'run.ps1', 'wsl-usb-permissions.sh', 'wsl-user-launch.sh', 'wsl-provision.sh')) {
        [IO.File]::WriteAllText((Join-Path $fixture $file), 'synthetic; never executed')
    }
    [IO.File]::WriteAllText((Join-Path $fixture 'revision.txt'), ('a' * 40))
    $child = Join-Path $fixture 'flow.ps1'
    $usbHelper = Join-Path (Split-Path (Split-Path (Split-Path $BootstrapPath))) 'usbipd.ps1'
    if (Test-Path $usbHelper) { Copy-Item $usbHelper $fixture }
    $programFiles = Join-Path $fixture 'Program Files'
    $usbFolder = Join-Path $programFiles 'usbipd-win'
    New-Item -ItemType Directory -Path $usbFolder -Force | Out-Null
    $fixtureUsb = Join-Path $usbFolder 'usbipd.exe'
    if ($env:OS -eq 'Windows_NT') {
        Add-Type -TypeDefinition 'public class FixtureUsb { public static void Main(string[] a) { if (a.Length != 1 || a[0] != "--version") System.Environment.Exit(91); if (System.Environment.GetEnvironmentVariable("USBIPD_FIXTURE_MODE") == "broken") { System.Console.WriteLine("token=fixture-secret"); System.Environment.Exit(17); } System.Console.WriteLine("5.3.0"); } }' -OutputAssembly $fixtureUsb -OutputType ConsoleApplication
    } else {
        [IO.File]::WriteAllText($fixtureUsb, "#!/bin/sh`n[ `"`$1`" = --version ] && [ `$# = 1 ] || exit 91`nif [ `"`$USBIPD_FIXTURE_MODE`" = broken ]; then printf 'token=fixture-secret\n'; exit 17; fi`nprintf '5.3.0\n'`n")
        & chmod +x $fixtureUsb
    }
    $childText = @'
param($Bootstrap, $Payload, $Case)
# Native dialogs are an external boundary; console.ps1 is exercised separately.
Add-Type -TypeDefinition 'using System; public class ToolkitConsoleNative { public static IntPtr GetConsoleWindow() { return IntPtr.Zero; } public static bool ShowWindow(IntPtr h, int c) { return false; } public static bool IsWindowVisible(IntPtr h) { return true; } public static bool IsIconic(IntPtr h) { return false; } public static int DwmGetWindowAttribute(IntPtr h, int a, out int v, int s) { v=0; return 0; } public static int MessageBox(IntPtr h, string t, string c, uint f) { return 1; } }'
# Windows PowerShell 5.1 exports Get-FileHash as a FUNCTION. Load Utility
# before defining mocks: its lazy import (first ConvertTo-Json trace) would
# otherwise replace the mock with the real hash function. PS7 uses a cmdlet.
Import-Module Microsoft.PowerShell.Utility
$global:UacCase = $Case
$global:UacTrace = Join-Path $Payload ($Case + '.jsonl')
$env:LOCALAPPDATA = Join-Path $Payload $Case
$env:ProgramFiles = Join-Path $Payload 'Program Files'
$env:ProgramW6432 = $env:ProgramFiles
$global:FixtureUsb = Join-Path $env:ProgramFiles 'usbipd-win/usbipd.exe'
$global:UsbInstallCase = $Case -like 'usb-*' -and $Case -notin @('usb-declined','usb-installed-stale-path','usb-installed-current-path')
if ($global:UsbInstallCase) { $env:ProgramFiles = Join-Path $Payload $Case; $env:ProgramW6432 = $env:ProgramFiles }
function Get-ItemProperty { param($LiteralPath,$Name,$ErrorAction); return $null } # No real registry inventory.
function Record($Kind, $Values) { [IO.File]::AppendAllText($global:UacTrace, (([pscustomobject]@{Kind=$Kind; Values=@($Values)} | ConvertTo-Json -Compress) + "`n")) }
function wsl.exe {
    Record 'wsl' $args
    $global:LASTEXITCODE = 0
    if ($args[0] -eq '--version') { return 'WSLg 1.0' }
    if ($args[0] -eq '--list') {
        $header = 'NAME STATE VERSION'
        switch ($global:UacCase) {
            'no-distro-decline' { return $header }
            'tool-set-decline' { return "$header`n* docker-desktop Stopped 2`n NVIDIA-Workbench Stopped 2`n rancher-desktop Stopped 2`n podman-machine-default Stopped 2" }
            'old-ubuntu-decline' { return "$header`n* Ubuntu-20.04 Stopped 2" }
            'nonstandard-decline' { return "$header`n* My-Ubuntu Stopped 2`n Debian Stopped 2`n Kali Stopped 2" }
            'ubuntu24' { return "$header`n* docker-desktop Stopped 2`n Ubuntu-24.04 Stopped 2" }
            'private' { return "$header`n* Jibo-DFU-Toolkit Stopped 2" }
            'private-priority' { return "$header`n* Ubuntu-24.04 Stopped 2`n Jibo-DFU-Toolkit Stopped 2" }
            'default-preferred' { return "$header`n* Ubuntu-22.04 Stopped 2`n Ubuntu-24.04 Stopped 2" }
            'newest' { return "$header`n* docker-desktop Stopped 2`n Ubuntu-22.04 Stopped 2`n Ubuntu-24.04 Stopped 2" }
            'wsl1-decline' { return "$header`n* Ubuntu-22.04 Stopped 1" }
            'unhealthy-private' { return "$header`n* Jibo-DFU-Toolkit Stopped 2" }
            { $_ -like 'usb-*' -or $_ -in @('wrong-os-decline','old-version-decline','arm64-decline','musl-decline','root-user','no-wslg-decline','grant-failed','watcher-failed','success') } { return "$header`n* Ubuntu-22.04 Stopped 2" }
            default { return "$header`n* NVIDIA-Workbench Stopped 2" }
        }
    }
    if ($args[0] -eq '--import' -and $global:UacCase -eq 'import-failed') { $global:LASTEXITCODE = 1; return 'Synthetic import failure' }
    if ($args -contains 'wsl-provision.sh' -or ($args -join ' ') -match '/wsl-provision.sh') {
        if ($global:UacCase -eq 'provision-failed') { $global:LASTEXITCODE = 1; return 'Synthetic provisioning failure' }
    }
    if ($args -contains 'getconf') { if ($args -contains 'Ubuntu-20.04' -or $global:UacCase -eq 'explicit-incompatible') { return 'glibc 2.31' }; if ($global:UacCase -eq 'musl-decline') { return 'musl' }; return 'glibc 2.35' }
    if ($args -contains 'uname') { if ($global:UacCase -eq 'arm64-decline') { return 'aarch64' }; return 'x86_64' }
    if ($args -contains 'id') {
        if ($args[-1] -eq '-u') { if ($global:UacCase -in @('root-user','unhealthy-private')) { return '0' }; return '1000' }
        if ($args[-1] -eq '-g') { return '1001' }
        if ($args -contains 'Jibo-DFU-Toolkit') { if ($global:UacCase -eq 'user-verify-failed') { return 'root' }; return 'jibo' }; return 'fixture'
    }
    if ($args -contains 'test' -and $global:UacCase -eq 'no-wslg-decline') { $global:LASTEXITCODE=1; return 'Missing WSLg' }
    if ($args -contains 'cat') {
        if ($global:UacCase -eq 'wrong-os-decline') { return "ID=debian`nVERSION_ID=24.04" }
        if ($global:UacCase -eq 'old-version-decline') { return "ID=ubuntu`nVERSION_ID=20.04" }
        if ($args -contains 'Ubuntu-24.04') { return "ID=ubuntu`nVERSION_ID=24.04" }
        return "ID=ubuntu`nVERSION_ID=`"22.04`""
    }
    if ($args -contains 'printenv') { return '/home/fixture/path with spaces' }
    if ($args -contains 'wslpath') { return ('/mnt/c/fixture/' + (Split-Path $args[-1] -Leaf)) }
    if ($args -contains 'once' -and $global:UacCase -eq 'grant-failed') { $global:LASTEXITCODE=1; return 'Synthetic grant failure' }
    if ($args -contains 'display-mode') { return 'shared' }
    if ($args -contains 'bash' -and ($args -join ' ') -match '/wsl-user-launch.sh|/AppRun') { $global:LASTEXITCODE = 41 }
}
function powershell.exe { Record 'usb' $args; $global:LASTEXITCODE = if ($global:UacCase -eq 'usb-declined') { 3 } else { 0 } }
function Get-Command {
    param($Name, $ErrorAction, $CommandType)
    if ($Name -like 'usbipd*' -and ($global:UacCase -eq 'usb-installed-stale-path' -or $global:UsbInstallCase)) { return $null }
    if ($Name -eq 'winget.exe') { if ($global:UacCase -eq 'usb-winget-missing') { return $null }; return [pscustomobject]@{Source='winget-fixture'; CommandType='Application'} }
    return [pscustomobject]@{Source=$global:FixtureUsb; CommandType='Application'}
}
function Read-Host { param($Prompt); Record 'prompt' @($Prompt); if ($Prompt -match 'Install usbipd-win' -and $global:UsbInstallCase -and $global:UacCase -ne 'usb-install-refused') { return 'yes' }; if ($Prompt -match 'Set up a private Linux' -and $global:UacCase -in @('create-success','hash-mismatch','size-mismatch','import-failed','provision-failed','user-verify-failed')) { return 'yes' }; return 'n' }
function Receive-ToolkitDownload { param($Uri, $OutFile, $ExpectedBytes); Record 'download' @($Uri, $OutFile); [IO.File]::WriteAllText($OutFile, 'fixture') }
function Get-Item { param($LiteralPath); return [pscustomobject]@{Length=$(if ($global:UacCase -eq 'size-mismatch') { 12 } else { 360684292 })} }
function Get-FileHash { param($LiteralPath, $Algorithm); Record 'hash' @($Algorithm); return [pscustomobject]@{Hash=$(if ($global:UacCase -eq 'hash-mismatch') { '0' * 64 } else { '4499c4fe257f2fc83145b429ce211a0a43fd590e70d6261ede616210947d9f8f' })} }
function Start-Process {
    param($FilePath, $ArgumentList, $Verb, [switch]$Wait, [switch]$PassThru, [switch]$NoNewWindow, $WindowStyle)
    Record 'start' @($FilePath, $ArgumentList, $Verb)
    if ($FilePath -eq 'winget-fixture') {
        if ($Verb -ne 'RunAs' -or $ArgumentList -cne 'install --interactive --exact --id dorssel.usbipd-win') { throw 'Installer consent/arguments changed' }
        if ($global:UacCase -eq 'usb-uac-cancelled') { throw 'Synthetic UAC cancellation' }
        if ($global:UacCase -in @('usb-fresh-ready','usb-noop-ready','usb-noop-broken','usb-reboot-ready','usb-failure-ready')) {
            $directory = Join-Path $env:ProgramFiles 'usbipd-win'
            New-Item -ItemType Directory -Path $directory -Force | Out-Null
            Copy-Item $global:FixtureUsb (Join-Path $directory 'usbipd.exe')
            if ($global:UacCase -eq 'usb-noop-broken') { $env:USBIPD_FIXTURE_MODE = 'broken' }
        }
        $exit = switch ($global:UacCase) { 'usb-fresh-ready' {0}; 'usb-fresh-missing' {0}; 'usb-reboot-ready' {3010}; 'usb-winget-reboot' {-1978334967}; 'usb-failure-ready' {1603}; default {-1978335189} }
        $process = [pscustomobject]@{ExitCode=$exit; Handle=1; HasExited=$true}
        $process | Add-Member ScriptMethod WaitForExit { param($Timeout); return $true }
        return $process
    }
    $isGui = $ArgumentList -match '^--distribution ([A-Za-z0-9._-]+) --user ([^\s"]+) --exec "bash" "([^"]+/wsl-user-launch.sh)" "([^"]+/jibo-dfu)" "([^"]+)" "([^"]+)"$'
    if ($isGui) { Record 'wsl' @('--distribution', $Matches[1], '--user', $Matches[2], '--exec', 'bash', $Matches[3], $Matches[4], $Matches[5], $Matches[6]) }
    $process = [pscustomobject]@{Id=4242; HasExited=($isGui -or ($global:UacCase -eq 'watcher-failed' -and $FilePath -match 'wsl.exe$')); ExitCode=$(if ($isGui) { 41 } else { 0 }); Handle=1}
    $process | Add-Member ScriptMethod WaitForExit { param($Timeout); if ($Timeout) { return $this.HasExited }; $this.HasExited=$true }
    return $process
}
function Stop-Process { param($Id); Record 'stop' @($Id) }
$parameters = @{PayloadRoot=$Payload}
if ($Case -like 'explicit-*') { $parameters['Distro'] = if ($Case -eq 'explicit-missing') { 'NotInstalled' } else { 'NVIDIA-Workbench' } }
& $Bootstrap @parameters
exit $LASTEXITCODE
'@
    [IO.File]::WriteAllText($child, $childText)
    foreach ($case in @('nvidia-decline','tool-set-decline','old-ubuntu-decline','nonstandard-decline','no-distro-decline','ubuntu24','private','private-priority','default-preferred','newest','wrong-os-decline','old-version-decline','wsl1-decline','arm64-decline','musl-decline','root-user','no-wslg-decline','explicit-good','explicit-incompatible','explicit-missing','hash-mismatch','size-mismatch','create-success','import-failed','provision-failed','user-verify-failed','unhealthy-private','success','grant-failed','usb-declined','watcher-failed','usb-installed-stale-path','usb-installed-current-path','usb-noop-ready','usb-noop-missing','usb-noop-broken','usb-fresh-ready','usb-fresh-missing','usb-reboot-ready','usb-winget-reboot','usb-failure-ready','usb-uac-cancelled','usb-install-refused','usb-winget-missing') | Where-Object { $_ -like $Scenario }) {
        try {
            $messages = @(& $hostBinary -NoProfile -File $child -Bootstrap $path -Payload $fixture -Case $case 2>&1)
            $code = $LASTEXITCODE
            $text = $messages -join "`n"
            $events = @(Get-Content (Join-Path $fixture ($case + '.jsonl')) | ForEach-Object { $_ | ConvertFrom-Json })
            $prompts = @($events | Where-Object { $_.Kind -eq 'prompt' -and $_.Values[0] -notmatch 'Press Enter' })
            $gui = @($events | Where-Object { $_.Kind -eq 'wsl' -and ($_.Values -join ' ') -match '/wsl-user-launch.sh|/AppRun' -and $_.Values -contains 'bash' -and $_.Values -notcontains 'display-mode' })
            $downloads = @($events | Where-Object { $_.Kind -eq 'download' })
            $imports = @($events | Where-Object { $_.Kind -eq 'wsl' -and $_.Values -contains '--import' })
            $admin = @($events | Where-Object { $_.Kind -eq 'start' -and $_.Values[-1] -eq 'RunAs' })
            $usbInstallCase = $case -like 'usb-*' -and $case -notin @('usb-declined','usb-installed-stale-path','usb-installed-current-path')
            if ($admin.Count -and -not $usbInstallCase) { throw 'distro setup requested UAC' }
            if ($usbInstallCase) {
                $expectedAdmin = if ($case -in @('usb-install-refused','usb-winget-missing')) {0} else {1}
                if ($admin.Count -ne $expectedAdmin -or $prompts.Count -ne 1 -or $prompts[0].Values[0] -notmatch 'Install usbipd-win') { throw 'USB install did not ask/install exactly once' }
                if ($case -notin @('usb-noop-ready','usb-fresh-ready') -and (@($events | Where-Object Kind -eq 'usb').Count -or @($events | Where-Object { $_.Kind -eq 'wsl' -and $_.Values -contains 'once' }).Count)) { throw 'Unready USB prerequisite reached handoff/grant' }
                if ($case -in @('usb-noop-missing','usb-noop-broken') -and $text -notmatch 'usbipd-win.*-1978335189.*0x8A15002B') { throw ('Missing actionable no-op status: ' + $text) }
                if ($text -match 'fixture-secret') { throw 'Probe output leaked' }
            }
            if ($case -notlike 'explicit-*') {
                if ($text -match 'NVIDIA-Workbench|docker-desktop|rancher-desktop|podman-machine-default|My-Ubuntu|Debian|Kali') { throw 'tool/custom distro was listed' }
                if (@($events | Where-Object { $_.Kind -eq 'wsl' -and ($_.Values -join ' ') -match 'NVIDIA-Workbench|docker-desktop|rancher-desktop|podman-machine-default|My-Ubuntu|Debian|Kali' }).Count) { throw 'tool/custom distro was probed/used' }
            }
            $declines = @('nvidia-decline','tool-set-decline','old-ubuntu-decline','nonstandard-decline','no-distro-decline','wrong-os-decline','old-version-decline','wsl1-decline','arm64-decline','musl-decline','root-user','no-wslg-decline')
            $chosen = switch ($case) {
                'ubuntu24' { 'Ubuntu-24.04' }; 'private' { 'Jibo-DFU-Toolkit' }; 'private-priority' { 'Jibo-DFU-Toolkit' }
                'default-preferred' { 'Ubuntu-22.04' }; 'newest' { 'Ubuntu-24.04' }; 'explicit-good' { 'NVIDIA-Workbench' }
                'create-success' { 'Jibo-DFU-Toolkit' }; 'success' { 'Ubuntu-22.04' }; 'usb-installed-stale-path' { 'Ubuntu-22.04' }
                'usb-installed-current-path' { 'Ubuntu-22.04' }
                { $_ -in @('usb-noop-ready','usb-fresh-ready') } { 'Ubuntu-22.04' }
            }
            if ($case -in $declines) {
                if ($code -ne 1 -or $prompts.Count -ne 1 -or $prompts[0].Values[0] -notmatch 'Set up a private Linux') { throw 'private creation was not offered' }
                if ($downloads.Count -or $imports.Count -or $gui.Count -or @($events | Where-Object { $_.Kind -eq 'start' }).Count) { throw 'decline caused setup side effects' }
            } elseif ($chosen) {
                if (@($events | Where-Object { $_.Kind -eq 'wsl' -and $_.Values -contains 'display-mode' }).Count -ne 1) { throw 'Display preflight was not run exactly once' }
                if ($code -ne 41 -or $gui.Count -ne 1 -or $gui[0].Values[1] -cne $chosen -or $text -notmatch ('Using Linux: ' + [regex]::Escape($chosen))) { throw ('wrong GUI choice/exit: ' + $text) }
                $user = if ($chosen -eq 'Jibo-DFU-Toolkit') { 'jibo' } else { 'fixture' }
                if (($gui[0].Values -join '|') -notmatch ('\|--user\|' + $user + '\|') -or ($gui[0].Values -join ' ') -match '/AppRun') { throw 'GUI was not direct normal-user launch' }
                if ($case -ne 'create-success' -and -not $usbInstallCase -and ($prompts.Count -or $downloads.Count -or $imports.Count)) { throw 'existing matching distro prompted or installed' }
                if ($case -in @('success','usb-installed-stale-path','usb-installed-current-path','usb-noop-ready','usb-fresh-ready')) {
                    $resolved = if ($usbInstallCase) { Join-Path (Join-Path (Join-Path $fixture $case) 'usbipd-win') 'usbipd.exe' } else { $fixtureUsb }
                    $handoff = @($events | Where-Object Kind -eq 'usb')
                    if ($handoff.Count -ne 1 -or $handoff[0].Values -notcontains '-UsbipdPath') { throw 'Resolved usbipd path lost before USB-only child' }
                    $index = [Array]::IndexOf([object[]]$handoff[0].Values, '-UsbipdPath')
                    if ($handoff[0].Values[$index+1] -cne $resolved) { throw 'USB-only child received another executable' }
                    $monitors = @($events | Where-Object { $_.Kind -eq 'start' -and [string]$_.Values[1] -match '-MonitorUsb' })
                    if ($monitors.Count -ne 1 -or [string]$monitors[0].Values[1] -notlike ('*-UsbipdPath "' + $resolved + '"*')) { throw 'Resolved usbipd path lost before monitor child' }
                }
                $grant = @($events | Where-Object { $_.Kind -eq 'wsl' -and $_.Values -contains 'once' })
                if ($grant.Count -ne 1 -or $grant[0].Values[3] -ne 'root') { throw 'password-free root USB grant did not run once' }
                $watch = @($events | Where-Object { $_.Kind -eq 'start' -and [string]$_.Values[1] -match '--user root' })
                if ($watch.Count -ne 1 -or [string]$watch[0].Values[1] -notmatch ('^--distribution ' + [regex]::Escape($chosen) + ' --user root --exec "bash"')) { throw 'watcher argv unsafe' }
                if (-not @($events | Where-Object { $_.Kind -eq 'wsl' -and $_.Values -contains 'rm' }).Count) { throw 'USB marker was not cleaned' }
            } else {
                if ($code -ne 1 -or $gui.Count) { throw ('failed to refuse: ' + $text) }
            }
            if ($case -in @('create-success','hash-mismatch','size-mismatch','import-failed','provision-failed','user-verify-failed')) {
                if ($prompts.Count -ne 1 -or $downloads.Count -ne 1) { throw ('creation did not ask/download once: ' + $text) }
                if ($downloads[0].Values[0] -cne 'https://releases.ubuntu.com/jammy/ubuntu-22.04.5-wsl-amd64.wsl') { throw 'image URL not pinned' }
                if ($case -ne 'size-mismatch' -and @($events | Where-Object { $_.Kind -eq 'hash' }).Count -ne 1) { throw 'hash mock was not used; real boundary escaped' }
                if (Test-Path $downloads[0].Values[1]) { throw 'download survived setup' }
                if ($case -in @('hash-mismatch','size-mismatch')) {
                    if ($imports.Count -or $text -notmatch 'checksum|size') { throw 'bad image was registered or not diagnosed' }
                } else {
                    if ($imports.Count -ne 1 -or $imports[0].Values[1] -cne 'Jibo-DFU-Toolkit' -or ($imports[0].Values[-2..-1] -join '|') -ne '--version|2') { throw 'wrong import name/version' }
                    if ($imports[0].Values[2] -cne (Join-Path (Join-Path (Join-Path $fixture $case) 'Jibo-DFU-Toolkit') 'wsl')) { throw 'wrong per-user install location' }
                    if ($case -eq 'create-success') {
                        $provision = @($events | Where-Object { $_.Kind -eq 'wsl' -and $_.Values -contains 'bash' -and ($_.Values -join ' ') -match '/wsl-provision.sh' })
                        if ($provision.Count -ne 1 -or $provision[0].Values[3] -ne 'root') { throw 'provisioning not root/noninteractive' }
                        if (-not @($events | Where-Object { $_.Kind -eq 'wsl' -and $_.Values[0] -eq '--terminate' }).Count) { throw 'default user not applied by termination' }
                    } elseif ($case -ne 'import-failed') {
                        if (@($events | Where-Object { $_.Kind -eq 'wsl' -and $_.Values[0] -eq '--unregister' }).Count -ne 1) { throw 'failed new import not rolled back' }
                    }
                }
            }
            if ($case -eq 'unhealthy-private' -and ($downloads.Count -or $imports.Count -or $prompts.Count)) { throw 'existing unhealthy private distro was changed' }
            if ($case -eq 'grant-failed' -and @($events | Where-Object { $_.Kind -eq 'start' }).Count) { throw 'watcher started after grant failure' }
            $passed++; Write-Output "PASS scenario: $case"
        } catch { $failedCases += $case; Write-Output "FAIL scenario: $case - $($_.Exception.Message)" }
    }
} finally { Remove-Item -Recurse -Force $fixture }
Write-Output "Bootstrap scenarios: $passed passed; $($failedCases.Count) failed. Helpers/parser/native argv passed."
if ($failedCases.Count) { exit 1 }
