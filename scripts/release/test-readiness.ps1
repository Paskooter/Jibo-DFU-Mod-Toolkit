# Synthetic readiness checks only: no real WSL, installs, admin or devices.
param([string]$BootstrapPath = (Join-Path $PSScriptRoot 'bootstrap.ps1'), [string]$Case = 'native-streams')
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($BootstrapPath, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($function in $ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst]}, $true)) {
    Invoke-Expression $function.Extent.Text
}
. (Join-Path (Split-Path $BootstrapPath) 'console.ps1')
if ($Case -like 'wsl-advice-*') {
    if ($Case -eq 'wsl-advice-update-choice') {
        $script:AdviceFixtureText = 'Run wsl.exe --install. Not a command: wsl.exe --update-preview'
        function Invoke-ToolkitNative {
            param($File, $Arguments, $TimeoutSeconds, [switch]$AllowWait, [switch]$WatchWslSetup)
            return [pscustomobject]@{Output=@($script:AdviceFixtureText); ExitCode=$null; WslSetupRequired=$true; StartError=$null}
        }
        $failure = $null
        try { Native -Arguments @('--version') -WatchWslSetup | Out-Null } catch { $failure = $_.Exception }
        if (-not $failure.Data['ToolkitWslSetupRequired'] -or $failure.Data['ToolkitWslUpdateRequired']) { throw 'A non-command suffix changed installation advice into an update' }
        $script:AdviceFixtureText = 'Run wsl.exe --update. Not a command: wsl.exe --install-preview'
        try { Native -Arguments @('--version') -WatchWslSetup | Out-Null } catch { $failure = $_.Exception }
        if (-not $failure.Data['ToolkitWslUpdateRequired']) { throw 'A non-command suffix changed update advice into an installation' }
    } else {
        $advice = switch ($Case) {
            'wsl-advice-install-suffix' { 'wsl.exe --install-preview' }
            'wsl-advice-update-suffix' { 'wsl.exe --update-preview' }
            'wsl-advice-install-assignment' { 'wsl.exe --install=unrecognized' }
            default { throw 'Unknown advice fixture' }
        }
        if (Test-ToolkitWslSetupOutput $advice) { throw 'A non-command was mistaken for exact WSL setup advice' }
        foreach ($valid in @("Run 'wsl.exe --install'.", 'Run wsl.exe --update.', 'wsl.exe --install --no-distribution')) {
            if (-not (Test-ToolkitWslSetupOutput $valid)) { throw 'Exact install/update advice was lost' }
        }
    }
    Write-Output "PASS readiness: $Case"
    exit 0
}
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('dfu-readiness-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $fixture | Out-Null
$oldSystemRoot = $env:SystemRoot
$oldMode = $env:TOOLKIT_READINESS_FIXTURE
$oldTrace = $env:TOOLKIT_READINESS_TRACE
try {
    Initialize-ToolkitLog -Root $fixture -Build ('e' * 40) -ExistingPath ''
    Set-ToolkitStep 'Starting Linux'
    $env:SystemRoot = Join-Path $fixture 'host'
    # Use the exact production Native path even on Linux, where backslashes are
    # literal filename characters to ProcessStartInfo (not PowerShell paths).
    $fake = "$env:SystemRoot\System32\wsl.exe"
    if ($Case -eq 'post-import-readiness') {
        # Only this new-import rollback fixture replaces Native; all ordinary
        # readiness cases below execute the real capture against an owned child.
        Import-Module Microsoft.PowerShell.Utility
        $oldLocal = $env:LOCALAPPDATA
        try {
            $env:LOCALAPPDATA = Join-Path $fixture 'local'
            [IO.File]::WriteAllText((Join-Path $fixture 'wsl-provision.sh'), 'Synthetic, never executed')
            $script:ImportFixtureCalls = @()
            function Native {
                param([string[]]$Arguments, $TimeoutSeconds)
                $script:ImportFixtureCalls += ,$Arguments
                if ($Arguments -contains 'getconf') { return 'glibc 2.35' }
                if ($Arguments -contains 'uname') { return 'x86_64' }
                if ($Arguments -contains 'id') { if ($Arguments[-1] -eq '-un') { return 'jibo' }; return '0' }
                if ($Arguments -contains 'wslpath') { return '/synthetic/provision.sh' }
                return ''
            }
            function Receive-ToolkitDownload { param($Uri,$OutFile,$ExpectedBytes); [IO.File]::WriteAllText($OutFile, 'Synthetic image') }
            function Get-Item { param($LiteralPath); return [pscustomobject]@{Length=360684292} }
            function Get-FileHash { param($LiteralPath,$Algorithm); return [pscustomobject]@{Hash='4499c4fe257f2fc83145b429ce211a0a43fd590e70d6261ede616210947d9f8f'} }
            $failure = $null
            try { Install-PrivateDistribution -Root $fixture | Out-Null } catch { $failure = $_.Exception }
            if (-not $failure -or $failure.Data['ToolkitReadinessCheck'] -cne 'default-uid' -or $failure.Message -notmatch 'id -u') { throw 'New-import failure lost its exact readiness check' }
            $imports = @($script:ImportFixtureCalls | Where-Object { $_[0] -eq '--import' })
            $rollbacks = @($script:ImportFixtureCalls | Where-Object { $_[0] -eq '--unregister' })
            if ($imports.Count -ne 1 -or $rollbacks.Count -ne 1 -or ($rollbacks[0] -join '|') -cne '--unregister|Jibo-DFU-Toolkit') { throw 'Owned new-import rollback boundary changed' }
            Write-Output "PASS readiness: $Case"
            exit 0
        } finally { $env:LOCALAPPDATA = $oldLocal }
    }
    $source = @'
using System;
using System.IO;
public class ReadinessFixture {
    public static void Main(string[] args) {
        string mode = Environment.GetEnvironmentVariable("TOOLKIT_READINESS_FIXTURE");
        File.AppendAllText(Environment.GetEnvironmentVariable("TOOLKIT_READINESS_TRACE"), String.Join("|", args) + "\n");
        if (mode == "native-probe-streams" && args.Length == 1 && args[0] == "--version") {
            Console.WriteLine("WSLg version: 1.0.73"); Console.Error.WriteLine("wsl: harmless synthetic warning"); return;
        }
        if ((args.Length != 5 && args.Length != 6) || args[0] != "--distribution" || args[2] != "--exec") Environment.Exit(91);
        if (mode == "stdout-warning") Console.WriteLine("wsl: harmless synthetic warning");
        if (mode == "glibc-command" && args[3] == "getconf") {
            Console.Error.WriteLine("Synthetic getconf failure\npassword=synthetic-private-value\nhttps://invalid.example/synthetic-private-url");
            Environment.Exit(17);
        }
        if (mode == "hypervisor" && args[3] == "getconf") {
            Console.Error.WriteLine("Synthetic Wsl/Service/CreateInstance/CreateVm/HCS_E_HYPERV_NOT_INSTALLED");
            Environment.Exit(17);
        }
        if (args[3] == "getconf" && args[4] == "GNU_LIBC_VERSION") {
            if (mode == "native-timeout") { Console.Error.WriteLine("Synthetic command waiting"); Console.Error.Flush(); System.Threading.Thread.Sleep(60000); }
            string libc = "glibc 2.35";
            if (mode == "glibc-parse") libc = "musl synthetic invalid data";
            if (mode == "glibc-old") libc = "glibc 2.31";
            if (mode == "glibc-multiline") libc = "glibc\n2.35";
            if (mode == "glibc-overflow") libc = "glibc 99999999999999999999.35";
            if (mode == "glibc-conflict") libc = "glibc 2.31\nglibc 2.35";
            if (mode == "glibc-junk") libc = "arbitrary invalid stdout\nglibc 2.35";
            if (mode == "glibc-stderr-data") { libc = "musl"; Console.Error.WriteLine("glibc 2.35"); }
            if (mode == "ready24") libc = "glibc 2.39";
            Console.WriteLine(libc);
        }
        else if (args[3] == "uname" && args[4] == "-m") Console.WriteLine(mode == "architecture" ? "aarch64" : mode == "architecture-conflict" ? "aarch64\nx86_64" : "x86_64");
        else if (args[3] == "id" && args[4] == "-u") Console.WriteLine(mode == "uid" ? "0" : mode == "uid-conflict" ? "0\n1000" : "1000");
        else if (args[3] == "test" && args.Length == 6 && args[4] == "-d" && args[5] == "/mnt/wslg") {
            if (mode == "wslg") { Console.Error.WriteLine("Synthetic /mnt/wslg directory missing"); Environment.Exit(1); }
            if (mode == "wslg-output") Console.WriteLine("arbitrary invalid stdout");
        }
        else if (args[3] == "cat" && args[4] == "/etc/os-release") {
            string release = "ID=ubuntu\nVERSION_ID=\"22.04\"";
            if (mode == "ubuntu-id") release = "ID=debian\nVERSION_ID=22.04";
            if (mode == "ubuntu-version") release = "ID=ubuntu\nVERSION_ID=20.04";
            if (mode == "ubuntu-missing-id") release = "VERSION_ID=22.04";
            if (mode == "ubuntu-missing-version") release = "ID=ubuntu";
            if (mode == "ubuntu-malformed-version") release = "ID=ubuntu\nVERSION_ID=not-a-version";
            if (mode == "ubuntu-overflow-version") release = "ID=ubuntu\nVERSION_ID=99999999999999999999.04";
            if (mode == "release-conflict-id") release = "ID=debian\n" + release;
            if (mode == "release-conflict-version") release = "VERSION_ID=20.04\n" + release;
            if (mode == "release-malformed") release = "arbitrary invalid stdout\n" + release;
            if (mode == "release-quote") release = "ID=\"ubuntu'\nVERSION_ID=22.04";
            if (mode == "release-duplicate") release = "ID=ubuntu\n" + release;
            if (mode == "release-warning-junk") release = "wsl: harmless synthetic warning\narbitrary invalid stdout\n" + release;
            if (mode == "ready24") release = "# Synthetic complete release metadata\nNAME=\"Ubuntu\"\nID=ubuntu\nID_LIKE=debian\nPRETTY_NAME=\"Ubuntu 24.04 LTS\"\nVERSION_ID=\"24.04\"\nHOME_URL=\"https://invalid.example/\"";
            if (mode == "explicit-custom") release = "ID=debian\nVERSION_ID=12";
            Console.WriteLine(release);
        }
        else Environment.Exit(92);
        if (mode == "stderr-warning") Console.Error.WriteLine("wsl: harmless synthetic warning");
    }
}
'@
    if ($env:OS -eq 'Windows_NT') {
        New-Item -ItemType Directory (Join-Path $env:SystemRoot 'System32') | Out-Null
        Add-Type -TypeDefinition $source -OutputAssembly $fake -OutputType ConsoleApplication
    } else {
        $cs = Join-Path $fixture 'fixture.cs'
        $exe = Join-Path $fixture 'fixture.exe'
        [IO.File]::WriteAllText($cs, $source)
        & mcs ('-out:' + $exe) $cs
        if ($LASTEXITCODE -ne 0) { throw 'Synthetic native fixture compilation failed' }
        [IO.File]::WriteAllText($fake, ("#!/bin/sh`nexec mono '" + $exe.Replace("'", "'\''") + "' " + '"$@"' + "`n"), (New-Object Text.UTF8Encoding($false)))
        & chmod +x $fake
        if ($LASTEXITCODE -ne 0) { throw 'Synthetic native fixture was not executable' }
    }
    $env:TOOLKIT_READINESS_TRACE = Join-Path $fixture 'commands.txt'
    $env:TOOLKIT_READINESS_FIXTURE = if ($Case -eq 'native-streams') { 'stderr-warning' } elseif ($Case -eq 'native-data-warning') { 'stdout-warning' } elseif ($Case -eq 'uid-requested') { 'uid' } elseif ($Case -eq 'automatic-declined-check') { 'glibc-old' } else { $Case }
    function Assert-ReadinessRefusal([string]$Check, [string]$Reason, [scriptblock]$Action, $ExitCode = 0) {
        $failure = $null
        try { & $Action | Out-Null } catch { $failure = $_.Exception }
        if (-not $failure) { throw 'Incompatible Linux was admitted' }
        if ($failure.Message -notmatch [regex]::Escape($Check) -or $failure.Message -notmatch [regex]::Escape($Reason)) { throw ('Exact failed readiness check was lost: ' + $failure.Message) }
        if ($failure.Message -match '--unregister|reinstall|remove it') { throw 'Existing private Linux received destructive recovery advice' }
        $events = @(Get-Content -LiteralPath $script:ToolkitLogPath | ForEach-Object { $_ | ConvertFrom-Json })
        $record = @($events | Where-Object { $_.event -eq 'linux-readiness' -and $_.result -eq 'failed' })
        if ($record.Count -ne 1 -or $record[0].check -cne $Check -or $record[0].reason -cne $Reason -or $record[0].exit_code -ne $ExitCode) { throw 'Structured log lost the failed check/reason/native status' }
        $details = Get-ToolkitSupportDetails $failure.Message
        if ($details -notmatch [regex]::Escape($Check) -or $details -notmatch [regex]::Escape($Reason)) { throw 'Copy support details lost the failed readiness check' }
        Add-Type -TypeDefinition $script:ToolkitQuestionModelSource
        $shown = [ToolkitSetupText]::Failure($failure.Message, $false)
        if ($shown.What -notmatch [regex]::Escape($Check) -or $shown.Try -match '--unregister') { throw 'Visible support wording hid the failed readiness check or advised deleting Linux' }
        if (Test-Path -LiteralPath $env:TOOLKIT_READINESS_TRACE) {
            $trace = [IO.File]::ReadAllText($env:TOOLKIT_READINESS_TRACE)
            if ($trace -match '--unregister|--import|--install|--update|--terminate|--shutdown') { throw 'Readiness changed a distribution or the host' }
        }
        return $failure
    }
    $privateRows = @([pscustomobject]@{Name='Jibo-DFU-Toolkit'; Version=2; Default=$true})
    if ($Case -eq 'native-streams') {
        $result = Invoke-ToolkitNative -File $fake -Arguments @('--distribution','Jibo-DFU-Toolkit','--exec','getconf','GNU_LIBC_VERSION')
        if ($result.ExitCode -ne 0) { throw ('Fixture failed: ' + $result.StartError) }
        if (($result.Stdout -join "`n") -cne 'glibc 2.35') { throw 'Data stdout was not captured separately from stderr' }
        if (($result.Stderr -join "`n") -cne 'wsl: harmless synthetic warning') { throw 'Diagnostic stderr was not retained separately' }
        if (($result.Output -join "`n") -cne "glibc 2.35`nwsl: harmless synthetic warning") { throw 'Combined Output compatibility changed' }
    } elseif ($Case -eq 'native-data-warning') {
        $text = Native -Arguments @('--distribution','Jibo-DFU-Toolkit','--exec','getconf','GNU_LIBC_VERSION')
        if ($text -cne 'glibc 2.35') { throw 'Native data still contains the WSL stdout diagnostic envelope' }
    } elseif ($Case -in @('stderr-warning','stdout-warning','ready24')) {
        $runtime = Get-DistributionRuntime 'Jibo-DFU-Toolkit' -RequireUbuntu
        $version = if ($Case -eq 'ready24') { [version]'24.04' } else { [version]'22.04' }
        if (-not $runtime -or $runtime.Name -cne 'Jibo-DFU-Toolkit' -or $runtime.OsVersion -ne $version) { throw 'Valid Linux data or an unambiguous harmless warning prevented readiness' }
        if ([IO.File]::ReadAllLines($env:TOOLKIT_READINESS_TRACE).Count -ne 5) { throw 'Readiness did not exercise all five real native commands' }
    } elseif ($Case -in @('glibc-parse','glibc-multiline','glibc-overflow','glibc-conflict','glibc-junk','glibc-stderr-data')) {
        Assert-ReadinessRefusal 'glibc' 'parse' { Select-ToolkitDistribution -Rows $privateRows -Root $fixture } | Out-Null
    } elseif ($Case -eq 'glibc-old') {
        $failure = Assert-ReadinessRefusal 'glibc' 'version' { Select-ToolkitDistribution -Rows $privateRows -Root $fixture }
        if ($failure.Message -notmatch '2.31' -or $failure.Message -notmatch '2.35') { throw 'Observed and required glibc versions were not retained' }
    } elseif ($Case -eq 'glibc-command') {
        $failure = Assert-ReadinessRefusal 'glibc' 'command' { Select-ToolkitDistribution -Rows $privateRows -Root $fixture } 17
        if ($failure.Data['ToolkitExitCode'] -ne 17 -or $failure.Data['ToolkitStall'] -or $failure.Message -notmatch 'Synthetic getconf failure') { throw 'Native exit/diagnostic was lost' }
        if (($failure.Message + [IO.File]::ReadAllText($script:ToolkitLogPath)) -match 'synthetic-private-value|synthetic-private-url') { throw 'Unsafe native text leaked into readiness diagnostics' }
    } elseif ($Case -eq 'hypervisor') {
        $failure = Assert-ReadinessRefusal 'glibc' 'command' { Select-ToolkitDistribution -Rows $privateRows -Root $fixture } 17
        foreach ($term in @('HCS_E_HYPERV_NOT_INSTALLED', 'Virtual Machine Platform', 'firmware virtualization', 'hypervisor configuration', 'does not identify which prerequisite', 'Restart Windows', 'reopen')) {
            if ($failure.Message -notmatch [regex]::Escape($term)) { throw ('WSL prerequisite guidance lost: ' + $term) }
        }
    } elseif ($Case -eq 'native-start') {
        $env:SystemRoot = Join-Path $fixture 'does-not-exist'
        $failure = Assert-ReadinessRefusal 'glibc' 'command' { Select-ToolkitDistribution -Rows $privateRows -Root $fixture } 1
        $record = @(Get-Content $script:ToolkitLogPath | ForEach-Object { $_ | ConvertFrom-Json } | Where-Object event -eq 'linux-readiness')[-1]
        if (-not $failure.Data['ToolkitStartError'] -or -not $record.start_error -or $record.timed_out) { throw 'Actual native start failure diagnostic was lost' }
        if (Test-Path -LiteralPath $env:TOOLKIT_READINESS_TRACE) { throw 'Missing executable unexpectedly ran a command' }
    } elseif ($Case -in @('architecture','architecture-conflict','uid','uid-conflict','uid-requested','wslg','ubuntu-id','ubuntu-version')) {
        $expectedCheck = switch -Wildcard ($Case) { 'uid*' {'default-uid'}; 'architecture*' {'architecture'}; default {$Case} }
        $reason = if ($Case -eq 'wslg') { 'command' } else { 'value' }
        $exitCode = if ($Case -eq 'wslg') { 1 } else { 0 }
        $requested = if ($Case -eq 'uid-requested') { 'Jibo-DFU-Toolkit' } else { '' }
        $failure = Assert-ReadinessRefusal $expectedCheck $reason { Select-ToolkitDistribution -Rows $privateRows -Requested $requested -Root $fixture } $exitCode
        $command = switch -Wildcard ($Case) { 'architecture*' {'uname -m'}; 'uid*' {'id -u'}; 'wslg' {'/mnt/wslg'}; default {'/etc/os-release'} }
        if ($failure.Message -notmatch [regex]::Escape($command)) { throw 'Failure lost the exact command or path checked' }
        if ($Case -eq 'architecture' -and $failure.Message -notmatch 'aarch64') { throw 'Failure hid the observed architecture' }
        if ($Case -eq 'uid' -and $failure.Message -notmatch 'Observed stdout: "0"') { throw 'Failure hid the observed root UID' }
    } elseif ($Case -eq 'invalid-name') {
        $invalidRows = @([pscustomobject]@{Name='synthetic unsafe name'; Version=2; Default=$true})
        Assert-ReadinessRefusal 'distribution-name' 'value' { Select-ToolkitDistribution -Rows $invalidRows -Requested 'synthetic unsafe name' -Root $fixture } $null | Out-Null
        if (Test-Path -LiteralPath $env:TOOLKIT_READINESS_TRACE) { throw 'Unsafe name reached a native probe' }
    } elseif ($Case -in @('wsl1-private','wsl1-requested','missing-requested','duplicate-requested','mixed-requested')) {
        $privateRows[0].Version = if ($Case -in @('duplicate-requested','mixed-requested')) { 2 } else { 1 }
        $rows = if ($Case -eq 'missing-requested') { @() } elseif ($Case -eq 'duplicate-requested') { @($privateRows[0],$privateRows[0]) } elseif ($Case -eq 'mixed-requested') { @($privateRows[0],[pscustomobject]@{Name='Jibo-DFU-Toolkit'; Version=1; Default=$false}) } else { $privateRows }
        $requested = if ($Case -eq 'wsl1-private') { '' } else { 'Jibo-DFU-Toolkit' }
        Assert-ReadinessRefusal 'wsl2' 'registration' { Select-ToolkitDistribution -Rows $rows -Requested $requested -Root $fixture } $null | Out-Null
        if (Test-Path -LiteralPath $env:TOOLKIT_READINESS_TRACE) { throw 'Unregistered, ambiguous or WSL 1 Linux was probed' }
    } elseif ($Case -like 'release-*') {
        Assert-ReadinessRefusal 'os-release' 'parse' { Select-ToolkitDistribution -Rows $privateRows -Root $fixture } | Out-Null
        if ([IO.File]::ReadAllLines($env:TOOLKIT_READINESS_TRACE).Count -ne 5) { throw 'Malformed release fixture did not reach the real cat check' }
    } elseif ($Case -in @('ubuntu-missing-id','ubuntu-missing-version','ubuntu-malformed-version','ubuntu-overflow-version','wslg-output')) {
        $check = if ($Case -eq 'ubuntu-missing-id') {'ubuntu-id'} elseif ($Case -eq 'wslg-output') {'wslg'} else {'ubuntu-version'}
        Assert-ReadinessRefusal $check 'parse' { Select-ToolkitDistribution -Rows $privateRows -Root $fixture } | Out-Null
    } elseif ($Case -eq 'native-timeout') {
        # Narrow only the clock policy; use the real native wait, pipe capture,
        # child termination, checker, selection, log and support path.
        function Get-ToolkitStepPolicy { param($Name); return [pscustomobject]@{Seconds=0.5; Hint='Synthetic bounded wait'; AllowWait=$false} }
        if ($env:OS -eq 'Windows_NT') {
            $script:ReadinessStop = (Get-Item Function:Stop-ToolkitProcess).ScriptBlock
            $script:ReadinessHostRoot = $oldSystemRoot
            function Stop-ToolkitProcess($Process) {
                $root = $env:SystemRoot
                try { $env:SystemRoot = $script:ReadinessHostRoot; & $script:ReadinessStop $Process }
                finally { $env:SystemRoot = $root }
            }
        }
        $failure = Assert-ReadinessRefusal 'glibc' 'command' { Select-ToolkitDistribution -Rows $privateRows -Root $fixture } 124
        $record = @(Get-Content $script:ToolkitLogPath | ForEach-Object { $_ | ConvertFrom-Json } | Where-Object event -eq 'linux-readiness')[-1]
        if (-not $record.timed_out -or -not $failure.Data['ToolkitStall'] -or -not $record.start_error) { throw 'Real timeout was swallowed or lost its diagnostics' }
        $commands = @(Get-Content $script:ToolkitLogPath | ForEach-Object { $_ | ConvertFrom-Json } | Where-Object event -eq 'command-end')
        if ($commands.Count -ne 1 -or $commands[0].exit_code -ne 124 -or $commands[0].result -ne 'stalled' -or -not $commands[0].start_utc -or -not $commands[0].end_utc) { throw 'Native timeout receipt was not retained' }
    } elseif ($Case -eq 'native-probe-streams') {
        $probe = Join-Path $fixture 'wsl.exe'
        [IO.File]::Copy($fake, $probe)
        if ($env:OS -ne 'Windows_NT') { & chmod +x $probe }
        $result = Invoke-ToolkitNative -File $probe -Arguments @('--version') -WatchWslSetup
        if ($result.ExitCode -ne 0 -or $result.WslSetupRequired -or $result.TimedOut -or ($result.Stdout -join "`n") -cne 'WSLg version: 1.0.73' -or ($result.Stderr -join "`n") -cne 'wsl: harmless synthetic warning' -or @($result.Output).Count -ne 2) { throw 'Streaming probe lost separate or combined output/status' }
    } elseif ($Case -eq 'explicit-custom') {
        $rows = @([pscustomobject]@{Name='Synthetic-Linux'; Version=2; Default=$true})
        if ((Select-ToolkitDistribution -Rows $rows -Requested 'Synthetic-Linux' -Root $fixture) -cne 'Synthetic-Linux') { throw 'Explicit compatible non-Ubuntu Linux was refused' }
    } elseif ($Case -eq 'automatic-allowlist') {
        $rows = @('docker-desktop','Synthetic-Linux','Ubuntu-custom','ubuntu-22.04') | ForEach-Object { [pscustomobject]@{Name=$_; Version=2; Default=$true} }
        function Confirm-Setup { param($Question); return $false }
        $failure = $null
        try { Select-ToolkitDistribution -Rows $rows -Root $fixture | Out-Null } catch { $failure = $_.Exception }
        if (-not $failure -or $failure.Message -notmatch 'setup declined' -or (Test-Path -LiteralPath $env:TOOLKIT_READINESS_TRACE)) { throw 'Automatic selection probed or used a non-allowlisted distro' }
    } elseif ($Case -eq 'automatic-declined-check') {
        $rows = @([pscustomobject]@{Name='Ubuntu-22.04'; Version=2; Default=$true})
        function Confirm-Setup { param($Question); return $false }
        $failure = Assert-ReadinessRefusal 'glibc' 'version' { Select-ToolkitDistribution -Rows $rows -Root $fixture }
        if ($failure.Message -notmatch 'Distribution setup declined') { throw 'Setup consent outcome was lost while retaining the failed check' }
    } elseif ($Case -in @('wsl1-ubuntu','duplicate-ubuntu','mixed-ubuntu')) {
        $rows = @([pscustomobject]@{Name='Ubuntu-22.04'; Version=1; Default=$true})
        if ($Case -ne 'wsl1-ubuntu') {
            $rows[0].Version = 2
            $rows += [pscustomobject]@{Name='Ubuntu-22.04'; Version=$(if ($Case -eq 'mixed-ubuntu') {1} else {2}); Default=$false}
        }
        function Confirm-Setup { param($Question); return $false }
        Assert-ReadinessRefusal 'wsl2' 'registration' { Select-ToolkitDistribution -Rows $rows -Root $fixture } $null | Out-Null
        if (Test-Path -LiteralPath $env:TOOLKIT_READINESS_TRACE) { throw 'Automatic selection probed WSL 1 or ambiguous registrations' }
    } else { throw ('Unknown readiness case: ' + $Case) }
    Write-Output "PASS readiness: $Case"
} finally {
    $env:SystemRoot = $oldSystemRoot
    $env:TOOLKIT_READINESS_FIXTURE = $oldMode
    $env:TOOLKIT_READINESS_TRACE = $oldTrace
    $script:ToolkitLogPath = $null
    Remove-Item -LiteralPath $fixture -Recurse -Force
}
