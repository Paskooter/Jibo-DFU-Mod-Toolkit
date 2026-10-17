"""Private stdin framing controls; not native Windows acceptance."""
import base64
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
PWSH = os.environ.get('PWSH') or shutil.which('pwsh')


@pytest.mark.parametrize('script,function,variable,expected', [
    ('test-usbipd-native.ps1', 'Invoke-OwnedPhase', 'process', b'outer-job:fixture\n'),
    ('test-support-native.ps1', 'Invoke-SupportChild', 'child', b'fixture\n'),
])
def test_native_gate_accepts_framework_preamble_but_preserves_nonce(tmp_path, script, function, variable, expected):
    """Exercise verbatim sender and receiver with a Framework-style writer.

    Process.StandardInput uses Console.InputEncoding and AutoFlush on Framework.
    Enabling AutoFlush emits a UTF-8 BOM even before a raw BaseStream write.
    Console.In preserves that marker; the receiver must handle the framing.
    """
    if not PWSH:
        pytest.skip('PowerShell required for byte-level gate control')
    source = ROOT / 'scripts/release' / script
    driver = tmp_path / 'gate.ps1'
    driver.write_text(r'''
param($Source,$Function,$Variable)
$ErrorActionPreference='Stop'
$text=[IO.File]::ReadAllText($Source)
$ast=[Management.Automation.Language.Parser]::ParseFile($Source,[ref]$null,[ref]$null)
$f=@($ast.FindAll({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -ceq $Function},$true))[0]
# Only the sender statements, verbatim; no process/job/fixture is executed.
$sender=@($f.Extent.Text -split '\r?\n' | Where-Object {
    $_ -match '^\s*\$gateBytes\s*=' -or
    $_ -match '^\s*\$(process|child)\.StandardInput\.(WriteLine|BaseStream\.(Write|Flush))\('
}) -join "`n"
if (-not $sender) { throw 'Gate sender statements absent' }
$receiver=[regex]::Match($text,'\[Console\]::In\.ReadLine\(\)(?:\.TrimStart\(\[char\]0xFEFF\))?').Value
if (-not $receiver) { throw 'Gate receiver expression absent' }
$stream=New-Object IO.MemoryStream
$writer=New-Object IO.StreamWriter -ArgumentList @($stream,(New-Object Text.UTF8Encoding($true)),1024,$true)
$writer.AutoFlush=$true
$RunId='fixture'; $Nonce='fixture'
Set-Variable -Name $Variable -Value ([pscustomobject]@{StandardInput=$writer})
& ([scriptblock]::Create($sender))
$wire=[Text.Encoding]::UTF8.GetString($stream.ToArray())
if ([int][char]$wire[0] -ne 0xFEFF) { throw 'Framework preamble control not entered' }
[Console]::SetIn((New-Object IO.StringReader($wire)))
$received=& ([scriptblock]::Create($receiver))
[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($received))
# Transport handling must not normalize malformed nonce data.
[Console]::SetIn((New-Object IO.StringReader(([char]0xFEFF + ' wrong-nonce' + "`n"))))
if ((& ([scriptblock]::Create($receiver))) -cne ' wrong-nonce') { throw 'Malformed nonce was normalized' }
$writer.Dispose(); $stream.Dispose()
''')
    result = subprocess.run([PWSH, '-NoProfile', '-File', str(driver), str(source), function, variable],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    actual = base64.b64decode(result.stdout.strip())
    assert actual == expected.rstrip(b'\n'), f'Encoding marker corrupted the exact gate nonce: {actual!r}'


@pytest.mark.parametrize('prefix', ['', '\ufeff'])
def test_usbipd_worker_json_line_handles_only_transport_bom(tmp_path, prefix):
    if not PWSH:
        pytest.skip('PowerShell required for JSON framing control')
    text = (ROOT / 'usbipd.ps1').read_text()
    import re
    match = re.search(r'\[Console\]::In\.ReadLine\(\)(?:\.TrimStart\(\[char\]0xFEFF\))?', text)
    assert match is not None, 'Worker request reader absent'
    receiver = match.group()
    request = '{"PreferredPath":"","ProbeSeconds":5,"Functions":{},"Variables":{}}'
    driver = tmp_path / 'json-line.ps1'
    wire = base64.b64encode((prefix + request + '\n').encode()).decode()
    driver.write_text("$wire=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('" + wire + "'))\n" +
                      "[Console]::SetIn((New-Object IO.StringReader($wire)))\n" +
                      "$line=" + receiver + "\n[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($line))\n")
    result = subprocess.run([PWSH, '-NoProfile', '-File', str(driver)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert base64.b64decode(result.stdout.strip()).decode() == request
