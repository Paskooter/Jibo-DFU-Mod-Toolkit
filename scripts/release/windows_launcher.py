"""One-file Windows launcher; embeds, but does not rebuild, the Linux payload."""
import hashlib
import json
import re
from pathlib import Path
import subprocess
import sys
import tarfile


def show_console():
    """Troubleshooting only: attach to the caller or allocate a real console."""
    if sys.platform != "win32":
        return
    import ctypes
    kernel = ctypes.windll.kernel32
    if not kernel.AttachConsole(-1):
        kernel.AllocConsole()
    if sys.stdout is None:
        sys.stdout = open("CONOUT$", "w", buffering=1)
    if sys.stderr is None:
        sys.stderr = open("CONOUT$", "w", buffering=1)
    if sys.stdin is None:
        sys.stdin = open("CONIN$", "r")


def show_error(message):
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, "Jibo DFU Toolkit", 0x00010010)
    else:
        print("Launcher: " + message, file=sys.stderr)


def main():
    args = sys.argv[1:]
    if len(args) == 3 and args[0] in ("--version", "--self-check") and args[1] == "--output":
        from contextlib import redirect_stdout, redirect_stderr
        with open(args[2], "w", encoding="utf-8") as output, redirect_stdout(output), redirect_stderr(output):
            try:
                return _main(args[:1])
            except Exception as exc:
                print('Launcher: ' + str(exc), flush=True)
                return 1
    return _main(args)


def _main(args):
    if '-ShowConsole' in args:
        show_console()
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    revision_file = root / 'revision.txt'
    revision = revision_file.read_text().strip() if revision_file.is_file() else 'unknown'
    print('Jibo DFU Toolkit (build ' + revision[:7] + ')', flush=True)
    archive = root / 'payload.tar.gz'
    expected = (root / 'payload.sha256').read_text().strip()
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise RuntimeError('Embedded Linux payload checksum mismatch')
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
    visible = '-ShowConsole' in args
    window = [] if visible else ['-WindowStyle', 'Hidden']
    return subprocess.call(['powershell.exe', '-NoProfile', '-NonInteractive', *window,
                            '-ExecutionPolicy', 'Bypass', '-File', str(root / 'bootstrap.ps1'),
                            '-PayloadRoot', str(root)] + args,
                           creationflags=0 if visible else 0x08000000)


def run():
    """Keep errors visible without an invisible console input wait."""
    try:
        return main()
    except Exception as exc:
        show_error(str(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(run())
