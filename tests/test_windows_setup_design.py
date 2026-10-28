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
    # Successful usbipd install now re-probes and continues in this process;
    # the old unconditional reopen/PATH-refresh wait is intentionally gone.
    assert len(steps) >= 12 and len(questions) == 3 and len(waits) == 3
    assert not any('then reopen the EXE' in wait for wait in waits)
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
    ('Windows display is not ready (WARN:COPY MODE).', False, 'error', 'Windows could not display the toolkit'),
    ('Step: Fixing a Windows display problem\r\nWindows display is not ready (WARN:COPY MODE), and the automatic fix didn\'t work. The toolkit window was not opened. Save your other Linux work, run wsl --shutdown in PowerShell (this closes all Linux apps), then open the toolkit again. Restart Windows if it still happens.\r\nTry: Save your other Linux work, run wsl --shutdown in PowerShell (this closes all Linux apps), then open the toolkit again. Restart Windows if it still happens.', False, 'error', 'Windows could not display the toolkit'),
    ('Windows toolkit window could not be verified within 120 seconds.', False, 'error', 'The toolkit window could not open'),
    ('Downloading Ubuntu stalled: no bytes arrived for 120 seconds. Check your internet connection, then reopen the toolkit.', True, 'stall', 'The download seems stuck'),
    ('Unpacking the toolkit stalled: no completion after 300 seconds. Close other WSL work, run wsl --shutdown in PowerShell, then reopen. Restart Windows if it happens again.', True, 'stall', 'Setup is taking longer than expected'),
    ('Opening the toolkit stalled: its window did not appear within 120 seconds. Close other WSL work, run wsl --shutdown, then reopen.', True, 'stall', 'The toolkit window did not appear'),
    ('Step: Checking the toolkit\r\nWSL failed: exit 7\r\nTry: Close other WSL work, run wsl --shutdown in PowerShell, then reopen. Restart Windows if it happens again.', False, 'error', 'Setup stopped'),
    ('You closed the question. This counts as No; no approval was given.\r\nStep: Finding Linux\r\nDistribution setup declined. No download or import was attempted.\r\nTry: Close other WSL work, run wsl --shutdown in PowerShell, then reopen. Restart Windows if it happens again.', False, 'info', 'Setup stopped'),
    ('Step: Installing Windows components\r\nWSL setup requested. Reboot if Windows requests it, then run this download again.\r\nTry: Check for an installer or Windows administrator approval prompt. Restart Windows if requested.', False, 'info', 'Open the toolkit again to finish setup'),
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
        if message.startswith(('Windows display is not ready', 'Step: Fixing a Windows display problem')):
            continue  # Copy mode's way out IS wsl --shutdown; see the dedicated test below.
        assert 'wsl --shutdown' not in line and not re.search(r'\bstalled\b', line), line


DISPLAY_FIX_STEP = ('Fixing a Windows display problem', 'This is a known Windows issue. It takes a few seconds.')
DISPLAY_TRY = ('Save your other Linux work, run wsl --shutdown in PowerShell (this closes all Linux apps), '
               'then open the toolkit again. Restart Windows if it still happens.')


def test_copy_mode_fix_and_refusal_wording(tmp_path):
    """User-facing strings for the automatic copy-mode fix, pinned on purpose."""
    failed = 'Windows display is not ready (WARN:COPY MODE), and the automatic fix didn\'t work. The toolkit window was not opened. ' + DISPLAY_TRY
    unfixed = 'Windows display is not ready (WARN:COPY MODE). The toolkit window was not opened. ' + DISPLAY_TRY
    full = 'Step: Fixing a Windows display problem\r\n' + failed + '\r\nTry: ' + DISPLAY_TRY
    data = catalogue(tmp_path, ['Fixing a Windows display problem'], [], [],
                     [{'message': full, 'wait': False}, {'message': unfixed, 'wait': False}])
    step = data['steps']['Fixing a Windows display problem']
    assert (step['title'], step['description']) == DISPLAY_FIX_STEP and step['phase'] == 4
    after = data['failures'][full]
    assert after['headline'] == 'Windows could not display the toolkit'
    assert after['what'] == 'Windows reported WARN:COPY MODE, and the automatic fix didn\u2019t work. The toolkit window was not opened.'
    assert after['try'] == DISPLAY_TRY
    before = data['failures'][unfixed]
    assert before['what'] == 'Windows reported WARN:COPY MODE. The toolkit window was not opened.' and before['try'] == DISPLAY_TRY
    shown = ' '.join([step['title'], step['description'], after['headline'], after['what'], after['try'], before['what']])
    for word in ('weston', 'compositor', 'virtiofs', 'shared memory', 'gfxredir', 'system distro'):
        assert word not in shown.lower(), word
    # Restarting Windows is the last resort now, not the first step.
    assert not after['try'].startswith('Restart') and after['try'].index('wsl --shutdown') < after['try'].index('Restart Windows')
    source = BOOTSTRAP.read_text()
    assert source.count("didn''t work. The toolkit window was not opened. " + DISPLAY_TRY) == 1
    assert 'Save and close your other Linux work, restart Windows' not in source


@pytest.mark.parametrize('message,headline', [
    ('usbipd-win setup returned -1978335189 (0x8A15002B). WinGet found no applicable update, but no usable usbipd executable was found.', 'USB support is not ready'),
    ('usbipd-win setup returned 0 (0x00000000). The installer finished, but no usable usbipd executable was found.', 'USB support is not ready'),
    ('usbipd-win installer exited with code 3010 (0x00000BC2). Restart Windows as requested, then reopen the toolkit.', 'Restart Windows to finish USB setup'),
    ('usbipd-win installer exited with code 1603 (0x00000643). Check the usbipd-win installer and Windows administrator approval; no USB handoff was attempted.', 'USB support setup stopped'),
])
def test_usbipd_failure_is_specific_without_generic_reboot_advice(tmp_path, message, headline):
    full = 'Step: Installing Windows components\r\n' + message + '\r\nTry: Check for an installer or Windows administrator approval prompt. Restart Windows if requested.'
    text = catalogue(tmp_path, [], [], [], [{'message': full, 'wait': False}])['failures'][full]
    assert text['headline'] == headline
    assert 'usbipd' in text['what'].lower()
    if '3010' in message:
        assert 'Restart Windows' in text['try']
    else:
        assert 'restart' not in text['try'].lower() and 'reinstall' not in text['try'].lower()
    if '0x8A15002B' in message:
        assert '-1978335189' in text['what'] and '0x8A15002B' in text['what']
        assert 'usbipd --version' in text['try']


@pytest.mark.parametrize('wait,headline', [
    (True, 'USB support is taking a long time to install'),
    (False, 'USB support was not installed'),
])
def test_unfinished_usbipd_installer_gives_the_manual_route(tmp_path, wait, headline):
    message = ('usbipd-win installer did not finish within 180 seconds after Windows approval. If its installer window is open, '
               'finish it. Otherwise install usbipd-win yourself: download the x64 .msi from github.com/dorssel/usbipd-win/releases, '
               'run it, restart Windows if it asks, then reopen the toolkit.')
    if not wait:
        message = 'Step: Installing Windows components\r\n' + message + '\r\nTry: Check for an installer or Windows administrator approval prompt. Restart Windows if requested.'
    text = catalogue(tmp_path, [], [], [], [{'message': message, 'wait': wait}])['failures'][message]
    assert text['headline'] == headline and '3 minutes' in text['what']
    assert 'https://github.com/dorssel/usbipd-win/releases' in text['try'] and 'x64.msi' in text['try']
    assert ('Keep waiting' in text['try'] and 'Stop setup' in text['try']) == wait
    for line in (text['headline'], text['what'], text['try']):
        assert not re.search(r'\b(?:stalled|winget|administrator|UAC)\b', line), line


def test_early_fallback_box_explains_its_windows_buttons(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location('design_launcher', ROOT / 'scripts/release/windows_launcher.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    log = tmp_path / 'setup.log'
    text = module.error_dialog_text('Embedded Linux payload checksum mismatch', log)
    # MessageBox buttons stay Yes/No/Cancel, so each one is spelled out.
    for line in ('Yes  \u2013  Copy details', 'No  \u2013  Open log folder', 'Cancel  \u2013  Close', '#help', 'Phoenix Discord',
                 'Open a support ticket', 'into your ticket'):
        assert line in text
    assert text.index('checksum mismatch') < text.index('Yes  ')
    assert str(log.parent) in text and text.rstrip().endswith('File: ' + log.name)
    assert 'Copy details' not in module.error_dialog_text('Usage: ...')
    assert module.ERROR_TITLE == 'Jibo DFU Toolkit \u2013 setup stopped'


def test_setup_window_fallback_box_explains_its_windows_buttons(tmp_path):
    if not PWSH:
        pytest.skip('requires PowerShell (set PWSH for portable installs)')
    script = tmp_path / 'fallback.ps1'
    script.write_text("$ErrorActionPreference='Stop'\n. '" + str(CONSOLE) + "'\n"
                      "[Console]::OutputEncoding = [Text.Encoding]::UTF8\n"
                      "Write-Output (Get-ToolkitFallbackTitle)\nWrite-Output (Get-ToolkitFallbackText 'Synthetic failure')\n")
    result = subprocess.run([PWSH, '-NoProfile', '-File', str(script)], capture_output=True, text=True,
                            encoding='utf-8', timeout=30, env={**os.environ, 'TMPDIR': str(tmp_path)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[0] == 'Jibo DFU Toolkit \u2013 setup stopped'
    for line in ('Synthetic failure', 'Yes  \u2013  Copy details', 'No  \u2013  Open log folder', 'Cancel  \u2013  Close', '#help',
                 'Open a support ticket', 'into your ticket'):
        assert line in result.stdout


def test_setup_scripts_stay_ascii_for_windows_powershell():
    # Windows PowerShell 5.1 reads BOM-less scripts as ANSI; visible typography uses escapes.
    for name in ('console.ps1', 'bootstrap.ps1'):
        data = (ROOT / 'scripts/release' / name).read_bytes()
        assert all(byte < 128 for byte in data), name


def test_setup_preview_workflow_is_manual_only_and_publishes_nothing():
    text = (ROOT / '.github/workflows/setup-previews.yml').read_text()
    trigger = text.split('\non:', 1)[1].split('\npermissions:', 1)[0]
    events = [line.strip() for line in trigger.splitlines()
              if line.strip() and not line.lstrip().startswith('#')]
    assert events == ['workflow_dispatch:']
    assert 'contents: read' in text and 'write' not in text
    assert 'runs-on: windows-2022' in text
    assert 'scripts/release/render-setup-previews.ps1' in text and 'shell: powershell' in text
    assert 'actions/upload-artifact@v4' in text
    for forbidden in ('build.py', 'pyinstaller', 'gh release', 'git tag', 'git push', 'softprops', '--draft', 'secrets.'):
        assert forbidden not in text.lower(), forbidden


@pytest.mark.parametrize('document', ['README.md', 'docs/release.md'])
def test_setup_docs_quote_embedded_questions_and_support_actions(tmp_path, document):
    questions = ps_strings(QUESTION_CALL, BOOTSTRAP)
    data = catalogue(tmp_path, [], questions, [], [])
    linux = next(q for q in data['questions'].values() if q['id'] == 'linux')
    text = (ROOT / document).read_text()
    assert linux['title'] in text
    for label in (linux['yes'], linux['no'], 'Keep waiting', 'Stop setup',
                  'Copy details', 'Open log folder', 'Waiting for you'):
        assert '**' + label + '**' in text
    for obsolete in ('**Yes / No**', 'Setup consent uses Yes/No dialogs', '[y/N]'):
        assert obsolete not in text


def test_release_docs_direct_source_builds_away_from_private_linux():
    text = (ROOT / 'docs/release.md').read_text()
    source = text.split('Source `run.ps1`/`run-gui.cmd`', 1)[1].split('\n\n', 1)[0]
    assert 'refuse' in source and '`Jibo-DFU-Toolkit`' in source
    assert 'no Linux password or sudo access' in source
    assert 'sudo-enabled user' in source and 'Jibo-DFU-Windows-x64.exe' in source


def test_preview_renderer_uses_the_shipped_setup_window_source():
    script = (ROOT / 'scripts/release/render-setup-previews.ps1').read_text()
    assert ". (Join-Path $PSScriptRoot 'console.ps1')" in script
    assert '$script:ToolkitSetupUiSource + $script:ToolkitQuestionModelSource + $script:ToolkitPreviewSource' in script
    # Synthetic data only, and never setup, WSL, installers or USB.
    for forbidden in ('wsl.exe', 'usbipd.exe', 'winget', 'Invoke-ToolkitNative', 'Invoke-ToolkitElevated', 'Start-Process',
                      'bootstrap.ps1', 'Set-Clipboard', 'Show-ToolkitQuestion', 'Set-ToolkitUserWait'):
        assert forbidden not in script, forbidden
    assert all(byte < 128 for byte in (ROOT / 'scripts/release/render-setup-previews.ps1').read_bytes())
    # The release EXE does not carry the preview tool.
    assert 'render-setup-previews' not in (ROOT / 'scripts/release/build.py').read_text()


def test_help_text_points_to_a_support_ticket_not_a_post_in_help():
    # #help is read-only: it only has an "Open a support ticket" button that opens a
    # private ticket channel. Nothing may tell users to post or paste in #help itself.
    sources = {name: (ROOT / name).read_text(encoding='utf-8') for name in (
        'scripts/release/console.ps1', 'scripts/release/windows_launcher.py', 'README.md', 'docs/release.md')}
    for name, text in sources.items():
        flat = ' '.join(text.split())
        assert not re.search(r'(?:paste|post|ask)[^.]{0,40}\bin (?:the )?\*{0,2}#help', flat, re.I), name
        assert not re.search(r'sharing them in #help', flat), name
    console = sources['scripts/release/console.ps1']
    help_line = re.search(r'public const string HelpLine = "([^"]+)"', console).group(1)
    copied = re.search(r'public const string CopiedLine = "([^"]+)"', console).group(1)
    assert 'Open a support ticket' in help_line and '#help' in help_line and 'your ticket' in help_line
    assert 'support ticket' in copied and '#help' not in copied
