"""Private stdin framing controls; not native Windows acceptance."""
import base64
import os
from pathlib import Path
import re
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


def _gate_receiver(script):
    """Return the runner's verbatim gate receiver statements."""
    text = (ROOT / 'scripts/release' / script).read_text()
    if script == 'test-support-native.ps1':
        region = text.split("$template=@'\n", 1)[1].split("\n'@", 1)[0]
    else:
        region = text.split('function Run-Phase {', 1)[1].split('\nfunction ', 1)[0]
        region = region.split("if ($PhaseName -like 'bounds-*') {", 1)[1].split('\n    }', 1)[0]
    check = {'test-usbipd-native.ps1': r"Assert-True \(\[Console\]::In\.ReadLine\(\)[^\n]*?'Bounds outer Job gate missing'",
             'test-support-native.ps1': r"if \(\[Console\]::In\.ReadLine\(\)[^\n]*?\{ exit 121 \}"}[script]
    statement = re.search(check, region)
    assert statement is not None, 'Gate receiver absent'
    before = region[:statement.start()]
    return [line.strip() for line in before.splitlines()
            if re.match(r'^\s*\[Console\]::InputEncoding\s*=', line)] + [statement.group()]


@pytest.mark.parametrize('script,payload', [
    ('test-usbipd-native.ps1', 'outer-job:71d1cbc6eba6455f8c57dda4e6defab7'),
    ('test-support-native.ps1', '0009f524f44542e3b1c44272ccb334d7'),
])
def test_native_gate_receiver_declares_utf8_under_oem_console_code_page(tmp_path, script, payload):
    """Replay the gate bytes measured on the Windows PowerShell 5.1 runner.

    The Framework sender's StandardInput is UTF-8 with a BOM (EF BB BF) and
    CRLF. The windowless receiver's Console.InputEncoding was IBM437, which
    decodes the BOM as U+2229 U+2557 U+2510, so TrimStart(U+FEFF) cannot
    remove it. The receiver must declare its UTF-8 protocol encoding.
    """
    if not PWSH:
        pytest.skip('PowerShell required for code-page gate control')
    receiver = _gate_receiver(script)
    wire = b'\xef\xbb\xbf' + payload.encode('ascii') + b'\r\n'
    nonce = payload.split(':', 1)[-1]
    prelude = ("$ErrorActionPreference='Stop'\n"
               "function Assert-True($Value,[string]$Message) { if (-not $Value) { throw $Message } }\n"
               "$RunId='" + nonce + "'; $r=[pscustomobject]@{nonce='" + nonce + "'}\n"
               "# The measured receiver condition: OEM console input code page.\n"
               "[Console]::InputEncoding=[Text.Encoding]::GetEncoding(437)\n")

    def run(lines, data):
        driver = tmp_path / 'receiver.ps1'
        driver.write_text(prelude + '\n'.join(lines) + "\n[Console]::Out.WriteLine('GATE-ACCEPTED')\nexit 0\n")
        return subprocess.run([PWSH, '-NoProfile', '-NonInteractive', '-File', str(driver)],
                              input=data, capture_output=True, timeout=20)

    accepted = run(receiver, wire)
    assert accepted.returncode == 0 and b'GATE-ACCEPTED' in accepted.stdout, accepted.stdout + accepted.stderr
    # Condition control: without the declared encoding, the same bytes are rejected.
    legacy = [line for line in receiver if not line.startswith('[Console]::InputEncoding')]
    assert legacy != receiver, 'Receiver does not declare its protocol encoding'
    rejected = run(legacy, wire)
    assert rejected.returncode != 0 and b'GATE-ACCEPTED' not in rejected.stdout, 'OEM decoding condition not reproduced'
    # Exact-nonce checking is retained after the transport fix.
    wrong = run(receiver, b'\xef\xbb\xbf' + payload[:-1].encode('ascii') + b'X\r\n')
    assert wrong.returncode != 0 and b'GATE-ACCEPTED' not in wrong.stdout, 'Wrong nonce accepted'


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
