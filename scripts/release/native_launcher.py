"""Frozen Python adapter. The existing toolkit sources remain unmodified."""
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys


def is_pipeline_request(arguments, runtime):
    return bool(arguments and Path(arguments[0]).resolve() ==
                (Path(runtime) / 'jibo_dfu_pipeline.py').resolve())


def configure(runtime):
    # Real .py files are intentional: the GUI clones jibo_tui via get_source().
    sys.path.insert(0, str(runtime))
    tools = runtime / 'tools'
    os.environ['PATH'] = str(tools) + os.pathsep + os.environ.get('PATH', '')
    os.environ['JIBO_DFU_UTIL'] = str(tools / 'dfu-util')
    os.environ['JIBO_SHOFEL2'] = str(tools / 'shofel2_t124')
    os.environ['JIBO_LIBUSB'] = str(runtime / 'lib' / ('libusb-1.0.dylib' if sys.platform == 'darwin' else 'libusb-1.0.so.0'))


def self_check(runtime):
    from manifest import verify_manifest
    receipt = verify_manifest(runtime)
    import ctypes
    ctypes.CDLL(os.environ['JIBO_LIBUSB'])
    import tkinter
    receipt['tcl'] = tkinter.Tcl().eval('info patchlevel')
    tools = runtime / 'tools'
    probes = {'dfu-util': ('--version',), 'shofel2_t124': ('--dfu-stage-capability',),
              'debugfs': ('-V',), 'e2fsck': ('-V',), 'resize2fs': ('-V',),
              'dumpe2fs': ('-V',), 'mke2fs': ('-V',)}
    receipt['tools'] = {}
    for name, args in probes.items():
        result = subprocess.run([str(tools / name), *args], capture_output=True, text=True, timeout=15)
        output = (result.stdout + result.stderr).strip()
        # resize2fs has no version-only success mode; -V prints its version and usage.
        if result.returncode != 0 and not (name == 'resize2fs' and result.returncode == 1 and 'resize2fs' in output):
            raise RuntimeError(name + ' failed: ' + output)
        if name == 'shofel2_t124' and result.stdout.strip() != 'dfu-stage-launch=1':
            raise RuntimeError('ShofEL is missing DFU launch capability')
        receipt['tools'][name] = output
    # Source-based private flow loading must also work inside the frozen bundle.
    import jibo_gui_core
    jibo_gui_core.load_flows()
    print(json.dumps({k: v for k, v in receipt.items() if k != 'files'}, indent=2))


def gui_smoke(runtime):
    # An explicit CI-only, read-only first-window probe; no USB access.
    import jibo_gui
    import jibo_gui_core
    import gui_fakes
    controller = jibo_gui_core.GuiController(gui_fakes.FakeApi('none'))
    root = jibo_gui.create_root()
    errors = []
    root.report_callback_exception = lambda *exc: errors.append(str(exc))
    window = jibo_gui.ToolkitWindow(root, controller)
    for _ in range(20):
        root.update()
    if not root.winfo_viewable() or errors:
        raise RuntimeError('First window did not render: ' + str(errors))
    print(json.dumps({'first_window': True, 'width': root.winfo_width(), 'height': root.winfo_height(),
                      'tk': root.tk.call('info', 'patchlevel'), 'usb_access': False}))
    window.close()
    controller.shutdown()


def main():
    arguments = sys.argv[1:]
    if arguments == ['--version']:
        print('Jibo DFU Mod Toolkit (release bundle)')
        return 0
    runtime = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'runtime'
    configure(runtime)
    if arguments == ['--self-check']:
        self_check(runtime)
        return 0
    if arguments == ['--gui-smoke']:
        gui_smoke(runtime)
        return 0
    if is_pipeline_request(arguments, runtime):
        sys.argv = arguments
        runpy.run_path(str(runtime / 'jibo_dfu_pipeline.py'), run_name='__main__')
        return 0
    sys.argv = [str(runtime / 'jibo_dfu.py'), *(arguments or ['gui'])]
    runpy.run_path(str(runtime / 'jibo_dfu.py'), run_name='__main__')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
