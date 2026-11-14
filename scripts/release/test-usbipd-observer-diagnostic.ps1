# Private, unexecuted native follow-up recipe. Reuses existing reviewed functions.
# Diagnostic subset only: NEVER a complete native-usbipd qualification receipt.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ToolkitRoot,
    [Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{40}$')][string]$ExpectedRevision,
    [Parameter(Mandatory=$true)][string]$OutputRoot
)
$ErrorActionPreference='Stop'
Import-Module Microsoft.PowerShell.Utility
Import-Module Microsoft.PowerShell.Management
try {
    $ToolkitRoot=[IO.Path]::GetFullPath($ToolkitRoot)
    $HarnessRoot=$ToolkitRoot
    $SelfPath=Join-Path $ToolkitRoot 'scripts/release/test-usbipd-native.ps1'
    $HostBinary=(Get-Process -Id $PID).Path
    $tokens=$null; $errors=$null
    $ast=[Management.Automation.Language.Parser]::ParseFile($SelfPath,[ref]$tokens,[ref]$errors)
    if($errors.Count -ne 0) {throw 'Native harness does not parse'}
    foreach($f in @($ast.EndBlock.Statements | Where-Object {$_ -is [Management.Automation.Language.FunctionDefinitionAst]})) {
        . ([scriptblock]::Create($f.Extent.Text))
    }
    # Refuse Core/Mono before creating output or running any fixture/compiler.
    $runtime=Runtime-Identity
    Assert-WindowsIdentity $runtime
    $revision=(& git -C $ToolkitRoot rev-parse HEAD | Out-String).Trim()
    Assert-True ($LASTEXITCODE -eq 0 -and $revision -ceq $ExpectedRevision) 'Wrong diagnostic source revision'
    $dirty=(& git -C $ToolkitRoot status --porcelain --untracked-files=no | Out-String).Trim()
    Assert-True ($LASTEXITCODE -eq 0 -and -not $dirty) 'Diagnostic checkout has tracked modifications'
    $sources=Check-Sources
    Assert-True (Test-Path -LiteralPath (Join-Path $env:SystemRoot 'System32/taskkill.exe') -PathType Leaf) 'Native tree termination prerequisite unavailable'
    Assert-True (-not (Test-Path -LiteralPath $OutputRoot)) 'Refusing to overwrite earlier diagnostic output'
    $OutputRoot=[IO.Path]::GetFullPath($OutputRoot)
    $null=New-Item -ItemType Directory -Path $OutputRoot
    $RunId=[guid]::NewGuid().ToString('N')
    $ScratchRoot=Join-Path ([IO.Path]::GetTempPath()) ('usbipd native '+$RunId)
    $null=New-Item -ItemType Directory -Path $ScratchRoot
    Save-Json (Join-Path $ScratchRoot 'owner.json') @{run_id=$RunId; harness_sha256=(Hash-File $SelfPath)}
    $names=@('compile','bounds-healthy','bounds-pipe-preflight','bounds-hang','bounds-pipe','bounds-blocked-output','bounds-shared-remaining','bounds-shared-cutoff')
    $specs=@(Get-Specs | Where-Object {$_.Id -in $names})
    Assert-True (($specs.Id -join '|') -ceq ($names -join '|')) 'Unexpected narrow phase order'
    $result=[ordered]@{kind='diagnostic-subset-NOT-native-acceptance'; native_qualification=$false; run_id=$RunId; revision=$revision; runtime=$runtime; source_sha256=$sources; harness_sha256=(Hash-File $SelfPath); phases=@(); state='running'}
    $summary=Join-Path $OutputRoot 'observer-diagnostic.json'
    try {
        foreach($spec in $specs) {
            $dir=Join-Path $OutputRoot $spec.Id
            $null=New-Item -ItemType Directory -Path $dir
            # Original budgets, pre-gated outer Jobs, termination and zero-active
            # requirements. No PID journal ever grants signalling authority.
            $null=Invoke-OwnedPhase $spec.Id $spec.Seconds $dir
            $state='pass'
            try {Assert-PhaseEvidence $spec (Read-Json (Join-Path $dir 'phase.json')) $dir $RunId}
            catch {$state='fail'; [IO.File]::WriteAllText((Join-Path $dir 'assertion.txt'),$_.Exception.Message)}
            $files=[ordered]@{}
            foreach($file in [IO.Directory]::GetFiles($dir)) {$files[[IO.Path]::GetFileName($file)]=Hash-File $file}
            $result.phases+=@([pscustomobject]@{id=$spec.Id; state=$state; files=$files})
            Save-Json $summary $result
        }
        $result.state='complete'; Save-Json $summary $result
        $after=Check-Sources
        foreach($name in $sources.Keys) {Assert-True ($after[$name] -ceq $sources[$name]) 'Source changed during diagnostic'}
        Assert-True (@($result.phases | Where-Object {$_.state -cne 'pass'}).Count -eq 0) 'Narrow native diagnostic has failed phases'
        foreach($spec in $specs) {Assert-PhaseEvidence $spec (Read-Json (Join-Path (Join-Path $OutputRoot $spec.Id) 'phase.json')) (Join-Path $OutputRoot $spec.Id) $RunId}
        Write-Output 'PASS 8 narrow native diagnostic phases; NOT complete native Windows qualification'
    } finally {
        # Scratch is newly created by this invocation, not supplied by a journal.
        if(Test-Path -LiteralPath $ScratchRoot) {Remove-Item -LiteralPath $ScratchRoot -Recurse -Force}
    }
    exit 0
} catch {Write-Error $_ -ErrorAction Continue; exit 1}
