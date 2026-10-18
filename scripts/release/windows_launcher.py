"""One-file Windows launcher; embeds, but does not rebuild, the Linux payload."""
import hashlib
import json
import os
import platform
import re
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import uuid

_setup_log_path: Path | None = None


def protect_text(text):
    text = re.sub(r'(?im)^.*(?:password|passwd|token|authorization|ssid|wi-?fi|psk|secret)[\x22\x27]?\s*[:=].*$',
                  '[private data removed]', str(text))
    text = re.sub(r'https?://[^\s"<>]+', '[URL removed]', text, flags=re.I)
    for name in ('USERPROFILE', 'LOCALAPPDATA', 'TEMP', 'HOME'):
        value = os.environ.get(name)
        if value and len(value) > 3:
            text = re.sub(re.escape(value), '[user folder]', text, flags=re.I)
    text = re.sub(r'[A-Z]:\\Users\\[^\\\s]+', '[user folder]', text, flags=re.I)
    return re.sub(r'/(?:home|Users)/[^/\s]+', '[user folder]', text)


def write_setup_log(path, event, message='', **fields):
    record = {'utc': datetime.now(timezone.utc).isoformat(), 'event': event,
              'step': 'Starting Windows launcher', 'message': protect_text(message)}
    record.update({key: protect_text(value) for key, value in fields.items()})
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(record) + '\n')


def initialize_setup_log(root=None, build='unknown'):
    name = 'setup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ-') + uuid.uuid4().hex[:8] + '.log'
    for base in (root or os.environ.get('LOCALAPPDATA'), tempfile.gettempdir()):
        if not base:
            continue
        try:
            folder = Path(base) / 'Jibo-DFU-Toolkit' / 'logs'
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / name
            write_setup_log(path, 'launcher-start', build=build, windows=platform.platform(), wsl='not checked')
            return path
        except OSError:
            continue
    raise RuntimeError('Cannot create a setup log in local app data or the temporary folder. Check free space and folder permissions; setup has not started.')


def support_details(path, message):
    lines = path.read_text(encoding='utf-8').splitlines()
    header = json.loads(lines[0])
    return protect_text('Jibo DFU Toolkit setup support\nBuild: ' + header['build'] +
                        '\nWindows: ' + header['windows'] + '\nWSL: not checked\nStep: Starting Windows launcher\nError: ' +
                        message[:1200] + '\nLast log lines:\n' + '\n'.join(line[:500] for line in lines[-10:]))


def copy_details(text):
    """Win32 clipboard fallback even when PowerShell cannot start."""
    import ctypes
    from ctypes import wintypes
    user, kernel = ctypes.windll.user32, ctypes.windll.kernel32
    kernel.GlobalAlloc.restype = ctypes.c_void_p
    kernel.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel.GlobalLock.restype = ctypes.c_void_p
    kernel.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel.GlobalFree.argtypes = [ctypes.c_void_p]
    user.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    user.SetClipboardData.restype = ctypes.c_void_p
    data = (text + '\0').encode('utf-16-le')
    handle = kernel.GlobalAlloc(0x0002, len(data))
    if not handle:
        raise OSError('Clipboard allocation failed')
    owned = True
    try:
        pointer = kernel.GlobalLock(handle)
        if not pointer:
            raise OSError('Clipboard lock failed')
        ctypes.memmove(pointer, data, len(data))
        kernel.GlobalUnlock(handle)
        if not user.OpenClipboard(None):
            raise OSError('Clipboard busy')
        try:
            if not user.EmptyClipboard() or not user.SetClipboardData(13, handle):
                raise OSError('Clipboard copy failed')
            owned = False
        finally:
            user.CloseClipboard()
    finally:
        if owned:
            kernel.GlobalFree(handle)


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


def create_error_owner():
    """A visible native owner when PowerShell/WinForms cannot start."""
    import ctypes
    from ctypes import wintypes
    user = ctypes.windll.user32
    user.CreateWindowExW.restype = ctypes.c_void_p
    user.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    user.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user.DestroyWindow.argtypes = [ctypes.c_void_p]
    owner = user.CreateWindowExW(0x00040000, 'STATIC', ERROR_TITLE,
                                0x00C80000, 200, 200, 640, 180, None, None, None, None)
    if not owner:
        raise OSError('Could not create the Windows setup error window')
    user.ShowWindow(owner, 5)
    return owner


ERROR_TITLE = 'Jibo DFU Toolkit \u2013 setup stopped'


def error_dialog_text(safe, log_path=None):
    """Plain wording for the early fallback box. Its buttons are Windows' own Yes/No/Cancel."""
    if not log_path:
        return 'Setup stopped.\n\n' + safe
    return ('Setup stopped.\n\n' + safe +
            '\n\nTo get help, copy the details, press Open a support ticket in the #help channel on the Phoenix Discord, and paste them into your ticket.'
            '\n\n    Yes  \u2013  Copy details\n    No  \u2013  Open log folder\n    Cancel  \u2013  Close'
            # Folder and file on separate lines: a long path has no place to wrap.
            '\n\nSetup log folder:\n' + str(Path(log_path).parent) + '\nFile: ' + Path(log_path).name)


def show_error(message):
    if sys.platform == "win32":
        import ctypes
        safe = protect_text(message)
        owner = create_error_owner()
        ctypes.windll.user32.MessageBoxW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        try:
            if _setup_log_path:
                details = support_details(_setup_log_path, safe)
                while True:
                    choice = ctypes.windll.user32.MessageBoxW(
                        owner, error_dialog_text(safe, _setup_log_path), ERROR_TITLE, 0x00010213)  # no MB_TOPMOST
                    try:
                        if choice == 6:
                            copy_details(details)
                            return
                        if choice == 7:
                            os.startfile(str(_setup_log_path.parent))
                            continue
                    except OSError:
                        ctypes.windll.user32.MessageBoxW(owner, 'Could not copy the details or open the folder. The setup log is at the path shown in the previous message.', 'Jibo DFU Toolkit', 0x00010010)
                        continue
                    return
            ctypes.windll.user32.MessageBoxW(owner, error_dialog_text(safe), "Jibo DFU Toolkit", 0x00010010)
        finally:
            ctypes.windll.user32.DestroyWindow(owner)
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
                       'LICENSE', 'NOTICE.md', 'THIRD-PARTY-NOTICES.md', 'console.ps1', 'usbipd.ps1'):
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
    environment = dict(os.environ)
    if _setup_log_path:
        environment['JIBO_DFU_SETUP_LOG'] = str(_setup_log_path)
    code = subprocess.call(['powershell.exe', '-NoProfile', '-NonInteractive', *window,
                            '-ExecutionPolicy', 'Bypass', '-File', str(root / 'bootstrap.ps1'),
                            '-PayloadRoot', str(root)] + args,
                           creationflags=0 if visible else 0x08000000, env=environment)
    if _setup_log_path:
        records = [json.loads(line) for line in _setup_log_path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
        if not any(row.get('event') == 'setup-end' for row in records):
            raise RuntimeError('Windows setup could not start or ended without a result (PowerShell exit ' + str(code) + '). Restart Windows and reopen the toolkit; use Copy details if it happens again.')
    return code


def run():
    """Keep errors visible and logged, even before PowerShell can start."""
    global _setup_log_path
    try:
        # Diagnostic --output modes must remain non-modal for release CI.
        diagnostic = sys.argv[1:2] in (['--version'], ['--self-check'], ['--extract-payload'])
        if sys.platform == 'win32' and not diagnostic:
            root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
            try:
                build = (root / 'revision.txt').read_text().strip()
            except OSError:
                build = 'unknown'
            _setup_log_path = initialize_setup_log(build=build)
        return main()
    except Exception as exc:
        if _setup_log_path:
            try:
                write_setup_log(_setup_log_path, 'launcher-failed', str(exc))
            except OSError:
                pass  # Preserve the visible error if the disk filled after startup.
        show_error(str(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(run())
