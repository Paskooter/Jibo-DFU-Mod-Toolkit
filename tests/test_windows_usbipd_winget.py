"""The usbipd-win install through winget must never wait on a hidden console.

The bootstrap launches winget elevated with its console window hidden. WinGet's
own prompts (winget-cli src/AppInstallerCLICore/Workflows/PromptFlow.cpp,
HandleSourceAgreementsForOneSource and PackageAgreementsPrompt) read that
console, so nobody can answer them. These checks need no Windows or winget.
"""
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / 'scripts/release/bootstrap.ps1'
CONSOLE = ROOT / 'scripts/release/console.ps1'
PWSH = os.environ.get('PWSH') or shutil.which('pwsh') or shutil.which('powershell.exe')


def winget_arguments():
    match = re.search(r"Invoke-ToolkitElevated -File \$winget\.Source -Arguments '([^']+)'(.*)$", BOOTSTRAP.read_text(), re.M)
    assert match, 'usbipd-win winget call not found'
    return match.group(1).split(), match.group(2)


def winget_would_prompt(words, source_accepted_before=False):
    """Model of winget-cli PromptFlow.cpp (IsInteractivityAllowed and both agreement prompts).

    Returns the first prompt winget would show on its console, or None. On a PC where
    winget was never used, the source agreements were never accepted.
    """
    interactive_allowed = '--disable-interactivity' not in words
    if not source_accepted_before and '--accept-source-agreements' not in words and interactive_allowed:
        return 'source-agreements'
    # Package agreements: accepted by flag; otherwise prompted when allowed, or the
    # install ends with APPINSTALLER_CLI_ERROR_PACKAGE_AGREEMENTS_NOT_ACCEPTED (no hang).
    if '--accept-package-agreements' not in words and interactive_allowed:
        return 'package-agreements'
    return None


def test_old_arguments_prompt_in_the_hidden_console_on_a_fresh_pc():
    old = 'install --interactive --exact --id dorssel.usbipd-win'.split()
    assert winget_would_prompt(old) == 'source-agreements'


def test_usbipd_install_cannot_prompt_in_winget_console():
    words, rest = winget_arguments()
    assert winget_would_prompt(words) is None
    # Each flag alone is enough for its own prompt; keep both plus the switch.
    for flag in ('--accept-source-agreements', '--accept-package-agreements', '--disable-interactivity'):
        assert flag in words
    # Only the community source: the msstore source has its own agreements and
    # cannot provide usbipd-win. --interactive keeps the installer's own window.
    assert words[words.index('--source') + 1] == 'winget'
    assert words[:5] == ['install', '--interactive', '--exact', '--id', 'dorssel.usbipd-win']
    for forbidden in ('--silent', '-h', '--force', '--ignore-security-hash', '--scope'):
        assert forbidden not in words
    assert '-InstallerWaitSeconds $script:ToolkitUsbipdInstallerWaitSeconds' in rest
    assert '$script:ToolkitUsbipdInstallerWaitSeconds = 180' in CONSOLE.read_text()


def test_installer_wait_never_stops_the_installer_without_a_choice():
    text = CONSOLE.read_text()
    body = text.split('function Wait-ToolkitProcess', 1)[1].split('\nfunction ', 1)[0]
    user = body.split('if ($script:ToolkitWaitingForUser) {', 1)[1].split('if (Test-ToolkitStall', 1)[0]
    assert 'Show-ToolkitSupportDialog $UserWaitMessage -AllowWait' in user
    assert 'Stop-ToolkitProcess' not in user and 'Kill' not in user
    # Old behavior is kept when no limit is given (other admin prompts, UAC).
    assert '$UserWaitSeconds -le 0' in user


@pytest.mark.parametrize('scenario', ['usb-winget-hidden-prompt', 'usb-installer-never-finishes'])
def test_bootstrap_ends_a_blocked_winget_visibly(tmp_path, scenario):
    if not PWSH:
        pytest.skip('requires PowerShell (set PWSH for portable installs)')
    result = subprocess.run([PWSH, '-NoProfile', '-File', str(ROOT / 'scripts/release/test-bootstrap.ps1'), '-Scenario', scenario],
                            capture_output=True, text=True, timeout=150, env={**os.environ, 'TMPDIR': str(tmp_path)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'PASS scenario: ' + scenario in result.stdout
