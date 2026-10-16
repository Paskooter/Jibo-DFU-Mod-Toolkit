"""Setup window presentation: plain-language catalogue and preview tooling contracts.

The setup window shows plain wording; logs and Copy details keep the exact technical
strings. These checks keep the two in step without Windows, WSL, USB or a display.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
PWSH = os.environ.get('PWSH') or shutil.which('pwsh') or shutil.which('powershell.exe')
CONSOLE = ROOT / 'scripts/release/console.ps1'
BOOTSTRAP = ROOT / 'scripts/release/bootstrap.ps1'


def ps_strings(pattern, *paths):
    found = []
    for path in paths:
        for match in re.finditer(pattern, path.read_text()):
            value = match.group(1) if match.group(1) is not None else match.group(2)
            found.append(value.replace("''", "'"))
    return sorted(set(found))


def catalogue(tmp_path, steps, questions, waits, failures):
    if not PWSH:
        pytest.skip('requires PowerShell (set PWSH for portable installs)')
    request = tmp_path / 'request.json'
    request.write_text(json.dumps({'steps': steps, 'questions': questions, 'waits': waits, 'failures': failures}))
    script = tmp_path / 'catalogue.ps1'
    script.write_text("$ErrorActionPreference='Stop'\n. '" + str(CONSOLE) + "'\n" + r"""
Add-Type -TypeDefinition $script:ToolkitQuestionModelSource
$request = Get-Content -Raw '""" + str(request) + r"""' | ConvertFrom-Json
$result = [ordered]@{ steps=@{}; questions=@{}; waits=@{}; failures=@{} }
foreach ($s in $request.steps) { $t = [ToolkitSetupText]::Step($s, ''); $result.steps[$s] = if ($t) { @{title=$t.Title; description=$t.Description; phase=$t.Phase} } else { $null } }
foreach ($q in $request.questions) { $t = [ToolkitSetupText]::Question($q); $result.questions[$q] = @{id=$t.Id; title=$t.Title; yes=$t.YesLabel; no=$t.NoLabel; what=$t.WhatHappens; long=$t.HowLong} }
foreach ($w in $request.waits) { $t = [ToolkitSetupText]::Wait($w); $result.waits[$w] = @{kind=$t.Kind; title=$t.Title; steps=@($t.Steps); hint=$t.Hint} }
foreach ($f in $request.failures) { $t = [ToolkitSetupText]::Failure($f.message, [bool]$f.wait); $result.failures[$f.message] = @{tone=$t.Tone; headline=$t.Headline; step=$t.Step; what=$t.What; try=$t.Try} }
$result | ConvertTo-Json -Depth 6
""")
    result = subprocess.run([PWSH, '-NoProfile', '-File', str(script)], capture_output=True, text=True,
                            timeout=60, env={**os.environ, 'TMPDIR': str(tmp_path)})
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


STEP_CALL = r"Set-ToolkitStep (?:'((?:[^']|'')*)'|\"([^\"$]*)\")"
QUESTION_CALL = r"Confirm-Setup (?:'((?:[^']|'')*)'|\"([^\"$]*)\")"
WAIT_CALL = r"Set-ToolkitUserWait \$true (?:'((?:[^']|'')*)'|\"([^\"$]*)\")"


def test_every_logged_setup_string_has_plain_window_wording(tmp_path):
    steps = ps_strings(STEP_CALL, BOOTSTRAP, CONSOLE)
    steps += ['Starting setup', 'Checking Windows and WSL', 'Cleaning up USB support']
    questions = ps_strings(QUESTION_CALL, BOOTSTRAP)
    waits = ps_strings(WAIT_CALL, BOOTSTRAP, CONSOLE)
    assert len(steps) >= 12 and len(questions) == 3 and len(waits) == 5
    data = catalogue(tmp_path, sorted(set(steps)), questions, waits, [])
    for step, text in data['steps'].items():
        assert text, 'no plain wording for logged step: ' + step
        assert 0 <= text['phase'] < 5
    for prompt, text in data['questions'].items():
        assert text['id'], 'no plain question for: ' + prompt
        # The safe answer is always the plainly labelled one.
        assert text['no'] == 'Not now' and text['yes'] not in ('Yes', 'No', 'Not now')
        assert text['what'] and text['long']
    for instructions, text in data['waits'].items():
        assert text['kind'] in ('admin', 'installer', 'restart'), 'no plain wait wording for: ' + instructions
        assert text['steps'] and text['hint']
        assert 'taskbar' in text['hint']
    shown = [t['title'] + ' ' + t['description'] for t in data['steps'].values()]
    shown += [t['title'] for t in data['questions'].values()]
    shown += [t['title'] + ' ' + ' '.join(t['steps']) for t in data['waits'].values()]
    for text in shown:
        assert not re.search(r'\b(?:WSLg|distro|distribution|EXE|stalled|UAC|administrator)\b', text), text


@pytest.mark.parametrize('message,wait,tone,headline', [
    ('Downloading Ubuntu stalled: no bytes arrived for 120 seconds. Check your internet connection, then reopen the toolkit.', True, 'stall', 'The download stopped'),
    ('Unpacking the toolkit stalled: no completion after 300 seconds. Close other WSL work, run wsl --shutdown in PowerShell, then reopen. Restart Windows if it happens again.', True, 'stall', 'Setup is taking longer than expected'),
    ('Opening the toolkit stalled: its window did not appear within 120 seconds. Close other WSL work, run wsl --shutdown, then reopen.', True, 'stall', 'The toolkit window did not appear'),
    ('Step: Checking the toolkit\r\nWSL failed: exit 7\r\nTry: Close other WSL work, run wsl --shutdown in PowerShell, then reopen. Restart Windows if it happens again.', False, 'error', 'Setup stopped'),
    ('You closed the question. This counts as No; no approval was given.\r\nStep: Finding Linux\r\nDistribution setup declined. No download or import was attempted.\r\nTry: Close other WSL work, run wsl --shutdown in PowerShell, then reopen. Restart Windows if it happens again.', False, 'info', 'Setup stopped'),
    ('Step: Installing Windows components\r\nWSL setup requested. Reboot if Windows requests it, then run this download again.\r\nTry: Check for an installer or Windows administrator approval prompt. Restart Windows if requested.', False, 'info', 'Restart Windows to finish setup'),
    ('Step: Checking the Ubuntu image\r\nUbuntu image checksum mismatch. Nothing was registered.\r\nTry: Close other WSL work, run wsl --shutdown in PowerShell, then reopen. Restart Windows if it happens again.', False, 'error', 'The download was damaged'),
])
def test_failure_wording_states_what_happened_and_what_to_try(tmp_path, message, wait, tone, headline):
    text = catalogue(tmp_path, [], [], [], [{'message': message, 'wait': wait}])['failures'][message]
    assert text['tone'] == tone and text['headline'] == headline
    assert text['what'] and text['try']
    if wait:
        assert 'Keep waiting' in text['try'] and 'Stop setup' in text['try']
    if message.startswith('You closed'):
        assert 'closed the question' in text['what'] and 'Not now' in text['what']
    for line in (text['headline'], text['what'], text['try']):
        assert 'wsl --shutdown' not in line and not re.search(r'\bstalled\b', line), line
