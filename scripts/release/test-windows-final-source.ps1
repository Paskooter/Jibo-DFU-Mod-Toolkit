# Isolated CI wrapper only; NOT FOR MERGE. Original native recipes stay unchanged.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$ToolkitRoot,
      [Parameter(Mandatory=$true)][string]$OutputRoot,
      [Parameter(Mandatory=$true)][ValidateSet('source','usb','support')][string]$Group)
$ErrorActionPreference='Stop'
$expected='2afc92a4b9f0bea5a52015f831d96619b3abd6ca'
$wrapper=$env:GITHUB_SHA
$wrapperRoot=Split-Path (Split-Path $PSScriptRoot)
$binary=(Get-Command powershell.exe -CommandType Application -ErrorAction Stop).Source
function Save-Receipt {
    [IO.File]::WriteAllText((Join-Path $OutputRoot ('execution-'+$Group+'.json')),($receipt|ConvertTo-Json -Depth 16),(New-Object Text.UTF8Encoding($false)))
}
function Get-SourceContext {
    $revision=(& git -C $ToolkitRoot rev-parse HEAD|Out-String).Trim()
    if($LASTEXITCODE -ne 0 -or $revision -cne $expected){throw 'Wrong final source checkout'}
    $tree=(& git -C $ToolkitRoot rev-parse 'HEAD^{tree}'|Out-String).Trim()
    if($LASTEXITCODE -ne 0 -or $tree -cne 'c48104a075992cf5aa75826d3fd1cde32cceb4ce'){throw 'Wrong final source tree'}
    # Some existing tests create ignored caches; tracked/index/untracked source must stay clean.
    $status=(& git -C $ToolkitRoot status --porcelain|Out-String).Trim()
    if($LASTEXITCODE -ne 0 -or $status){throw 'Final source checkout is dirty'}
    return @{revision=$revision;tree=$tree;status=$status}
}
if($env:OS -cne 'Windows_NT' -or $PSVersionTable.PSVersion -notmatch '^5\.1\.' -or $PSVersionTable.PSEdition -cne 'Desktop'){throw 'Windows PowerShell 5.1 Desktop required'}
if(Test-Path -LiteralPath (Join-Path $OutputRoot ('execution-'+$Group+'.json'))){throw 'Fresh group receipts required; no reruns'}
$null=New-Item -ItemType Directory -Force -Path $OutputRoot
$ToolkitRoot=[IO.Path]::GetFullPath($ToolkitRoot)
$OutputRoot=[IO.Path]::GetFullPath($OutputRoot)
$actualWrapper=(& git -C $wrapperRoot rev-parse HEAD|Out-String).Trim()
if($LASTEXITCODE -ne 0 -or $actualWrapper -cne $wrapper){throw 'Wrong immutable workflow wrapper'}
$receipt=[ordered]@{kind='full-native-Windows-source-acceptance';source_revision=$expected;wrapper_revision=$wrapper;github_repository=$env:GITHUB_REPOSITORY;github_run_id=$env:GITHUB_RUN_ID;github_run_attempt=$env:GITHUB_RUN_ATTEMPT;github_ref=$env:GITHUB_REF;runner_os=$env:RUNNER_OS;runner_arch=$env:RUNNER_ARCH;ps_version=$PSVersionTable.PSVersion.ToString();ps_edition=$PSVersionTable.PSEdition;wrapper_script_sha256=(Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash.ToLowerInvariant();source_before=(Get-SourceContext);source_after=$null;started_utc=[DateTime]::UtcNow.ToString('o');ended_utc=$null;state='running';stages=@()}
Save-Receipt
# Bind ORIGINAL Run/Validate recipes to the pinned product checkout, not this wrapper.
$env:GITHUB_SHA=$expected
$env:PWSH=$binary
Push-Location $ToolkitRoot
try {
    $stages=@(
        @{id='bootstrap';file=$binary;arguments=@('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File','scripts/release/test-bootstrap.ps1');source='scripts/release/test-bootstrap.ps1'},
        @{id='console';file=$binary;arguments=@('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File','scripts/release/test-console.ps1','-Case','all');source='scripts/release/test-console.ps1'},
        @{id='window-probe';file=$binary;arguments=@('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File','scripts/release/test-window-probe.ps1');source='scripts/release/test-window-probe.ps1'},
        @{id='readiness';file=(Get-Command python -CommandType Application -ErrorAction Stop).Source;arguments=@('-m','pytest','-q','tests/test_windows_readiness.py');source='tests/test_windows_readiness.py'},
        @{id='usbipd-run';file=$binary;arguments=@('-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File','scripts/release/test-usbipd-native.ps1','-Mode','Run','-OutputRoot',(Join-Path $OutputRoot 'usbipd-native-receipts'));source='scripts/release/test-usbipd-native.ps1'},
        @{id='support-run';file=$binary;arguments=@('-NoProfile','-NonInteractive','-WindowStyle','Hidden','-ExecutionPolicy','Bypass','-File','scripts/release/test-support-native.ps1','-Mode','Run','-OutputRoot',(Join-Path $OutputRoot 'support-native-receipts'));source='scripts/release/test-support-native.ps1'}
    )
    $stages=@($stages|Where-Object {if($Group -ceq 'usb'){$_.id -ceq 'usbipd-run'}elseif($Group -ceq 'support'){$_.id -ceq 'support-run'}else{$_.id -notin @('usbipd-run','support-run')}})
    foreach($stage in $stages){
        $entry=[ordered]@{id=$stage.id;binary=$stage.file;arguments=$stage.arguments;source=$stage.source;source_sha256=(Get-FileHash -LiteralPath (Join-Path $ToolkitRoot $stage.source) -Algorithm SHA256).Hash.ToLowerInvariant();started_utc=[DateTime]::UtcNow.ToString('o');ended_utc=$null;exit_code=$null;state='running'}
        $receipt.stages+=@($entry);Save-Receipt
        $out=Join-Path $OutputRoot ($stage.id+'-stdout.txt')
        $err=Join-Path $OutputRoot ($stage.id+'-stderr.txt')
        Write-Output ('BEGIN original native stage '+$stage.id)
        $ErrorActionPreference='Continue'
        $arguments=$stage.arguments
        & $stage.file @arguments 1> $out 2> $err
        $code=$LASTEXITCODE
        $ErrorActionPreference='Stop'
        $entry.exit_code=$code;$entry.ended_utc=[DateTime]::UtcNow.ToString('o')
        $entry.state=if($code -eq 0){'pass'}else{'fail'}
        Save-Receipt
        Get-Content -LiteralPath $out
        Get-Content -LiteralPath $err
        if($code -ne 0){throw ('Original native stage failed: '+$stage.id+' exit='+$code)}
    }
    $receipt.state='pass'
} catch {
    $receipt.state='fail'
    [IO.File]::WriteAllText((Join-Path $OutputRoot ('wrapper-error-'+$Group+'.txt')),$_.Exception.ToString())
    Write-Error $_ -ErrorAction Continue
} finally {
    try {$receipt.source_after=Get-SourceContext} catch {$receipt.state='fail';[IO.File]::WriteAllText((Join-Path $OutputRoot ('source-error-'+$Group+'.txt')),$_.Exception.ToString())}
    $receipt.ended_utc=[DateTime]::UtcNow.ToString('o');Save-Receipt
    Pop-Location
    $env:GITHUB_SHA=$wrapper
}
if($receipt.state -cne 'pass'){exit 1}
exit 0
