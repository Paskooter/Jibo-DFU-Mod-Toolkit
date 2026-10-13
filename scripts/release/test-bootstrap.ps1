# Parse and exercise the bootstrap without real WSL, USB, installs or UAC.
$ErrorActionPreference = 'Stop'
$hostBinary = (Get-Process -Id $PID).Path
$path = Join-Path $PSScriptRoot 'bootstrap.ps1'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$functions = $ast.FindAll({param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $true)
foreach ($function in $functions) { Invoke-Expression $function.Extent.Text }
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
# Exercise the entire missing-WSLg refusal path in a child process. A mock admin
# function leaves a sentinel if any installation is attempted after declining.
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('dfu-bootstrap-test-' + [guid]::NewGuid())
New-Item -ItemType Directory $fixture | Out-Null
try {
    [IO.File]::WriteAllText((Join-Path $fixture 'payload.tar.gz'), 'not read on refusal')
    [IO.File]::WriteAllText((Join-Path $fixture 'run.ps1'), 'not run on refusal')
    $child = Join-Path $fixture 'refusal.ps1'
    $childText = @'
param($Bootstrap, $Payload, $Case)
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
& $Bootstrap -PayloadRoot $Payload
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
$script:calls = @()
function Native { param([string[]]$Arguments); $script:calls += ,$Arguments; return '' }
function Invoke-ApprovedAdmin { param($File, $Arguments); $script:calls += ,@('UAC', $File, $Arguments) }
function Start-Process {
    param($FilePath, $ArgumentList, [switch]$NoNewWindow, [switch]$PassThru)
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
function wsl.exe { $script:calls += ,$args; $global:LASTEXITCODE=41; Write-Error 'Synthetic GUI stderr'; return 'Synthetic GUI stdout' }
$code = Invoke-WslGui -Name 'Ubuntu with spaces' -User 'fixture' -Target '/home/fixture/path with spaces' -Session '/session'
if ($ErrorActionPreference -ne 'Stop') { throw 'GUI native stderr handling leaked preference changes' }
if ($code -ne 41 -or ($script:calls[0] -join '|') -ne '--distribution|Ubuntu with spaces|--user|fixture|--exec|bash|/home/fixture/path with spaces/wsl-user-launch.sh|/home/fixture/path with spaces/jibo-dfu|/session') { throw 'GUI identity, direct binary dispatch or exit propagation failed' }
# Exercise the actual top-level bootstrap, not just helpers. Every external
# command is a synthetic boundary, with an invocation trace and controlled exits.
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('dfu-bootstrap-flow-' + [guid]::NewGuid())
New-Item -ItemType Directory $fixture | Out-Null
try {
    foreach ($file in @('payload.tar.gz', 'run.ps1', 'wsl-usb-permissions.sh', 'wsl-user-launch.sh')) {
        [IO.File]::WriteAllText((Join-Path $fixture $file), 'synthetic; never executed')
    }
    [IO.File]::WriteAllText((Join-Path $fixture 'revision.txt'), ('a' * 40))
    $child = Join-Path $fixture 'flow.ps1'
    $childText = @'
param($Bootstrap, $Payload, $Case)
$global:UacCase = $Case
$global:UacTrace = Join-Path $Payload ($Case + '.jsonl')
function Record($Kind, $Values) { [IO.File]::AppendAllText($global:UacTrace, (([pscustomobject]@{Kind=$Kind; Values=@($Values)} | ConvertTo-Json -Compress) + "`n")) }
function wsl.exe {
    Record 'wsl' $args
    $global:LASTEXITCODE = 0
    if ($args[0] -eq '--version') { return 'WSLg 1.0' }
    if ($args[0] -eq '--list') {
        if ($global:UacCase -like 'old-default*') { return "  NAME                STATE     VERSION`n* Ubuntu-20.04        Stopped   2`n  NVIDIA-Workbench    Stopped   2" }
        return "  NAME            STATE     VERSION`n* Ubuntu-22.04    Running   2"
    }
    if ($args -contains 'getconf') { if ($args -contains 'Ubuntu-20.04') { return 'glibc 2.31' }; return 'glibc 2.35' }
    if ($args -contains 'uname') { return 'x86_64' }
    if ($args -contains 'id') {
        if ($args[-1] -eq '-u') { if ($global:UacCase -eq 'root-user') { return '0' }; return '1000' }
        if ($args[-1] -eq '-g') { return '1001' }
        return 'fixture'
    }
    if ($args -contains 'printenv') { return '/home/fixture/path with spaces' }
    if ($args -contains 'wslpath') { return ('/mnt/c/fixture/' + (Split-Path $args[-1] -Leaf)) }
    if ($args -contains 'once' -and $global:UacCase -eq 'grant-failed') { $global:LASTEXITCODE = 1; return 'Could not grant Jibo USB access' }
    if ($args -contains 'bash' -and ($args -join ' ') -match '/wsl-user-launch.sh|/AppRun') { $global:LASTEXITCODE = 41 }
}
function powershell.exe {
    Record 'usb' $args
    $global:LASTEXITCODE = if ($global:UacCase -eq 'usb-declined') { 3 } else { 0 }
}
function Get-Command { param($Name, $ErrorAction); return [pscustomobject]@{Source='fixture-usbipd'} }
function Read-Host {
    param($Prompt)
    Record 'prompt' @($Prompt)
    if ($global:UacCase -eq 'old-default-install') { return '2' }
    if ($global:UacCase -eq 'old-default-other') { return '1' }
    return 'n'
}
function Start-Process {
    param($FilePath, $ArgumentList, $Verb, [switch]$Wait, [switch]$PassThru, [switch]$NoNewWindow)
    Record 'start' @($FilePath, $ArgumentList, $Verb)
    if ($Verb -eq 'RunAs') {
        if ($global:UacCase -eq 'uac-declined') { throw 'Synthetic UAC cancellation' }
        $code = if ($global:UacCase -eq 'admin-failed') { 5 } else { 0 }
        return [pscustomobject]@{ExitCode=$code}
    }
    $process = [pscustomobject]@{Id=4242; HasExited=($global:UacCase -eq 'watcher-failed' -and $FilePath -match 'wsl.exe$' -and $ArgumentList -match '--user"?\s+"?root'); ExitCode=0; Handle=1}
    $process | Add-Member ScriptMethod WaitForExit { param($Timeout); if ($Timeout) { return $this.HasExited }; $this.HasExited=$true }
    return $process
}
function Stop-Process { param($Id); Record 'stop' @($Id) }
& $Bootstrap -PayloadRoot $Payload
exit $LASTEXITCODE
'@
    [IO.File]::WriteAllText($child, $childText)
    foreach ($case in @('success', 'old-default-other', 'old-default-install', 'root-user', 'grant-failed', 'usb-declined', 'watcher-failed')) {
        $messages = @(& $hostBinary -NoProfile -File $child -Bootstrap $path -Payload $fixture -Case $case 2>&1)
        $code = $LASTEXITCODE
        $events = @(Get-Content (Join-Path $fixture ($case + '.jsonl')) | ForEach-Object { $_ | ConvertFrom-Json })
        $guiCalls = @($events | Where-Object { $_.Kind -eq 'wsl' -and $_.Values -contains 'bash' -and ($_.Values -join ' ') -match '/wsl-user-launch.sh|/AppRun' })
        $admin = @($events | Where-Object { $_.Kind -eq 'start' -and $_.Values[-1] -eq 'RunAs' })
        if ($admin.Count) { throw ('Bootstrap requested a Windows administrator prompt itself (' + $case + '); only usbipd bind may: ' + ($admin | ConvertTo-Json -Compress -Depth 5)) }
        if ($case -in @('success', 'old-default-other')) {
            if ($code -ne 41 -or $guiCalls.Count -ne 1) { throw ('Full bootstrap GUI exit failed (' + $case + '): ' + ($messages -join ' ') + ' TRACE: ' + ($events | ConvertTo-Json -Compress -Depth 5)) }
            $gui = $guiCalls[0].Values
            if (($gui -join '|') -notmatch '\|--user\|fixture\|' -or ($gui -join ' ') -match '/AppRun') { throw 'Full EXE route still requests sudo/root GUI' }
            $distro = if ($case -eq 'success') { 'Ubuntu-22.04' } else { 'NVIDIA-Workbench' }
            if (($gui -join '|') -notmatch ('^--distribution\|' + [regex]::Escape($distro) + '\|')) { throw ('GUI ran in the wrong distribution: ' + ($gui -join '|')) }
            $grant = @($events | Where-Object { $_.Kind -eq 'wsl' -and ($_.Values -join '|') -match ('^--distribution\|' + [regex]::Escape($distro) + '\|--user\|root\|--exec\|bash\|.*\|1000\|1001\|once$') })
            if ($grant.Count -ne 1) { throw ('Root USB grant did not run once in the user''s WSL session: ' + ($events | ConvertTo-Json -Compress -Depth 5)) }
            $watch = @($events | Where-Object { $_.Kind -eq 'start' -and [string]$_.Values[1] -match '--user"?\s+"?root' })
            if ($watch.Count -ne 1 -or ([string]$watch[0].Values[1]) -notmatch ('^--distribution ' + [regex]::Escape($distro) + ' --user root --exec "bash" ')) { throw ('Root watcher options would not be parsed by wsl.exe: ' + ($watch | ConvertTo-Json -Compress -Depth 5)) }
            if (-not @($events | Where-Object { $_.Kind -eq 'wsl' -and $_.Values -contains 'rm' }).Count) { throw 'Full bootstrap did not clean the permission session' }
            if ($case -eq 'old-default-other') {
                $prompt = @($events | Where-Object { $_.Kind -eq 'prompt' })
                if ($prompt.Count -ne 1 -or ($messages -join ' ') -notmatch 'Install Ubuntu 22.04') { throw 'Incompatible-default prompt did not offer a fresh Ubuntu install' }
            }
        } elseif ($case -eq 'old-default-install') {
            $install = @($events | Where-Object { $_.Kind -eq 'start' -and [string]$_.Values[1] -eq '--install --distribution Ubuntu-22.04' -and -not $_.Values[2] })
            if ($code -ne 41 -or $install.Count -ne 1 -or $guiCalls.Count -ne 1) { throw ('Install choice did not install Ubuntu as this user and continue: ' + ($messages -join ' ') + ' TRACE: ' + ($events | ConvertTo-Json -Compress -Depth 5)) }
            if (($guiCalls[0].Values -join '|') -notmatch '^--distribution\|Ubuntu-22.04\|') { throw 'GUI did not continue in the newly installed Ubuntu-22.04' }
        } else {
            if ($code -ne 1 -or $guiCalls.Count) { throw ('Full bootstrap failed to refuse ' + $case + ': ' + ($messages -join ' ')) }
            if ($case -eq 'root-user' -and @($events | Where-Object { $_.Kind -eq 'start' -or $_.Values -contains 'tar' }).Count) { throw 'Root default user was rejected too late' }
            if ($case -eq 'grant-failed' -and @($events | Where-Object { $_.Kind -eq 'start' -and [string]$_.Values[1] -match '--user"?\s+"?root' }).Count) { throw 'Root watcher started after a failed USB grant' }
        }
    }
} finally { Remove-Item -Recurse -Force $fixture }
Write-Output 'PASS: PowerShell parse/helpers, wsl.exe option-parser model, distro-name rule, password-free root USB grant/watcher, plus 9 full bootstrap scenarios (2 setup refusals, GUI exit, incompatible default + other distro, incompatible default + fresh Ubuntu install, root-user refusal, USB grant failure, USB refusal, watcher failure); all WSL/USB/admin boundaries mocked.'
