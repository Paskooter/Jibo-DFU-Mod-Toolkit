"""One-file Windows launcher; embeds, but does not rebuild, the Linux payload."""
import hashlib
import json
import re
from pathlib import Path
import subprocess
import sys
import tarfile


def main():
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    revision_file = root / 'revision.txt'
    revision = revision_file.read_text().strip() if revision_file.is_file() else 'unknown'
    print('Jibo DFU Toolkit (build ' + revision[:7] + ')', flush=True)
    archive = root / 'payload.tar.gz'
    expected = (root / 'payload.sha256').read_text().strip()
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise RuntimeError('Embedded Linux payload checksum mismatch')
    args = sys.argv[1:]
    if args == ['--version']:
        print('Jibo DFU Mod Toolkit Windows x64 WSL bootstrapper')
        return 0
    if args == ['--self-check']:
        for helper in ('bootstrap.ps1', 'run.ps1', 'wsl-usb-permissions.sh', 'wsl-user-launch.sh', 'wsl-provision.sh',
                       'LICENSE', 'NOTICE.md', 'THIRD-PARTY-NOTICES.md', 'console.ps1'):
            if not (root / helper).is_file():
                raise RuntimeError('Missing embedded Windows helper: ' + helper)
        with tarfile.open(archive) as source:
            names = source.getnames()
            for required in ('jibo-dfu', 'AppRun', '_internal/runtime/licenses/LICENSE',
                             '_internal/runtime/licenses/THIRD-PARTY-NOTICES.md'):
                if required not in names:
                    raise RuntimeError('Missing embedded file: ' + required)
        print(json.dumps({'payload_sha256': expected, 'revision': (root / 'revision.txt').read_text().strip(),
                          'payload_files': len(names), 'wsl_setup_attempted': False}))
        return 0
    if len(args) == 2 and args[0] == '--extract-payload':
        # Export the source-accompanied archive for inspection/relinking, not extraction
        # of untrusted tar members by the bootstrapper.
        import shutil
        shutil.copyfile(archive, args[1])
        return 0
    options = list(args)
    if '-ShowConsole' in options:
        options.remove('-ShowConsole')
    override = len(options) == 2 and options[0] == '-Distro' and re.fullmatch(r'[A-Za-z0-9._-]{1,256}', options[1])
    if options and not override:
        raise RuntimeError('Usage: Jibo-DFU-Windows-x64.exe [--version|--self-check|--extract-payload FILE] or [-Distro NAME] [-ShowConsole]')
    return subprocess.call(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                            '-File', str(root / 'bootstrap.ps1'), '-PayloadRoot', str(root)] + args)


def run():
    """Keep bootstrapper errors readable when opened by double-click."""
    try:
        return main()
    except Exception as exc:
        print('Launcher: ' + str(exc), flush=True)
        try:
            input('Press Enter to close')
        except EOFError:
            pass
        return 1


if __name__ == '__main__':
    raise SystemExit(run())
