"""Replay measured Windows child-root normalization; portable, NOT native evidence."""
import os
from pathlib import Path
import re

import pytest

from test_windows_usbipd_bounds import HELPER, PWSH, binary, env_for, execute, q

ROOT = Path(__file__).resolve().parents[1]
HARNESS = Path(os.environ.get('USBIPD_NATIVE_HARNESS', ROOT / 'scripts/release/test-usbipd-native.ps1'))
pytestmark = pytest.mark.skipif(os.name == 'nt' or not PWSH, reason='portable child-root replay')


@pytest.mark.parametrize('remove_programfiles', [False, True], ids=['programfiles-fallback', 'product-removal-control'])
def test_programfiles_phase_survives_child_root_normalization(tmp_path, remove_programfiles):
    target = tmp_path / 'program32' / 'usbipd-win'
    target.mkdir(parents=True)
    path = binary(target).rename(target / 'usbipd.exe')
    source = HELPER.read_text()
    needle = '@($env:ProgramW6432, $env:ProgramFiles)'
    assert needle in source, 'product discovery mutation seam changed'
    mutant = tmp_path / 'without-programfiles.ps1'
    mutant.write_text(source.replace(needle, '@($env:ProgramW6432)'))
    body = r'''
$ast=[Management.Automation.Language.Parser]::ParseFile(HARNESS,[ref]$null,[ref]$null)
$phase=@($ast.EndBlock.Statements | Where-Object {$_ -is [Management.Automation.Language.FunctionDefinitionAst] -and $_.Name -ceq 'Run-BoundsCase'})[0]
$lookup=@($phase.FindAll({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq 'Get-Command'},$true))
if($lookup.Count -ne 1) {throw 'phase lookup seam changed'}
. ([scriptblock]::Create($lookup[0].Extent.Text))
foreach($name in @('Assert-True','Assert-Rejection','Assert-BoundsEvidence')) {
 $f=@($ast.EndBlock.Statements | Where-Object {$_ -is [Management.Automation.Language.FunctionDefinitionAst] -and $_.Name -ceq $name})[0]
 . ([scriptblock]::Create($f.Extent.Text))
}
function Get-ItemProperty {param($LiteralPath,$Name,$ErrorAction) return $null}
$env:USBIPD_BOUNDS_CASE='source-program32'
$env:USBIPD_BOUNDS_DIRECTORY=DIRECTORY
$env:USBIPD_BOUNDS_PROGRAMFILES=PROGRAM
# Native receipt: parent ProgramFiles=program32, child ProgramFiles=ProgramW6432=absent64.
$env:ProgramW6432=ABSENT
$env:ProgramFiles=$env:ProgramW6432
MUTANT
$t=[Diagnostics.Stopwatch]::StartNew()
$r=Resolve-UsbipdExecutable
$elapsed=$t.Elapsed.TotalMilliseconds
$s=$script:UsbipdLastSupervision
$entries=if([IO.File]::Exists($env:BOUNDS_TRACE)) {@([IO.File]::ReadAllLines($env:BOUNDS_TRACE)).Count} else {0}
$value=@{run_id='replay'; id='source-program32'; before_outer_cleanup=$true; helper_sha256=('a'*64); fixture_sha256=('b'*64); elapsed_ms=$elapsed; limit_ms=15300; accepted=[bool]$r; resolved_exact=($r -ceq TARGET); entries=$entries; readers_released=$s.ReadersReleased; containment_stopped=$s.ContainmentStopped; path_unchanged=$true; poison_entered=$false; raw_output_leaked=$false}
$detected=$false
if(REMOVAL) {
 Assert-Rejection {Assert-BoundsEvidence ([pscustomobject]$value) 'source-program32' 'replay'} 'Bounds readiness decision incorrect'
 $detected=$true
} else {Assert-BoundsEvidence ([pscustomobject]$value) 'source-program32' 'replay'}
[pscustomobject]@{resolved=$r; entries=$entries; removal_detected=$detected; readers=$s.ReadersReleased; stopped=$s.ContainmentStopped} | ConvertTo-Json -Compress
'''
    for key, value in {
        'HARNESS': q(HARNESS), 'DIRECTORY': q(tmp_path), 'PROGRAM': q(target.parent),
        'ABSENT': q(tmp_path / 'absent64'), 'TARGET': q(path),
        'MUTANT': '. ' + q(mutant) if remove_programfiles else '',
        'REMOVAL': '$true' if remove_programfiles else '$false',
    }.items():
        body = re.sub(r'\b' + key + r'\b', lambda _: value, body)
    result = execute(tmp_path, body, env_for(tmp_path))
    assert result['readers'] and result['stopped']
    assert result['entries'] == (0 if remove_programfiles else 1)
    assert result['removal_detected'] is remove_programfiles
    assert result['resolved'] == (None if remove_programfiles else str(path))
