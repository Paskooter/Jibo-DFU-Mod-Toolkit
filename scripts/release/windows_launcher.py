"""One-file Windows launcher; embeds, but does not rebuild, the Linux payload."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile


def main():
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
    archive = root / 'payload.tar.gz'
    expected = (root / 'payload.sha256').read_text().strip()
    if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
        raise RuntimeError('Embedded Linux payload checksum mismatch')
    args = sys.argv[1:]
    if args == ['--version']:
        print('Jibo DFU Mod Toolkit Windows x64 WSL bootstrapper')
        return 0
    if args == ['--self-check']:
        with tarfile.open(archive) as source:
            names = source.getnames()
            for required in ('jibo-dfu', 'AppRun', '_internal/runtime/licenses/THIRD-PARTY-NOTICES.md'):
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
    if args:
        raise RuntimeError('Usage: Jibo-DFU-Windows-x64.exe [--version|--self-check|--extract-payload FILE]')
    return subprocess.call(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                            '-File', str(root / 'bootstrap.ps1'), '-PayloadRoot', str(root)])


if __name__ == '__main__':
    raise SystemExit(main())
