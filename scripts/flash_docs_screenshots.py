#!/usr/bin/env python3
"""Capture the two flash-choice screens with neutral fixtures and no USB.

Requires Tk, Pillow and ImageMagick. Run under a desktop or xvfb-run.
"""
import argparse
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

from scripts.docs_screenshots import DocsApi, clean_png
import jibo_flash_policy as policy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=ROOT / 'docs/images/gui')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    import jibo_gui as gui
    import jibo_gui_core as core
    api = DocsApi()
    errors = []
    with tempfile.TemporaryDirectory(prefix='flash-docs-') as temp:
        controller = core.GuiController(api, preferences_path=Path(temp) / 'preferences.json')
        root = gui.create_root()
        root.report_callback_exception = lambda *exc: errors.append(str(exc))
        window = gui.ToolkitWindow(root, controller)
        root.geometry('1080x720')

        def pump(predicate, timeout=5):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                root.update()
                assert not errors, errors
                if predicate():
                    return
                time.sleep(0.01)
            raise AssertionError('Documentation capture timed out: ' + window.page_name)

        def texts(widget):
            if not widget.winfo_viewable():
                return ''
            values = [str(widget.cget('text'))] if 'text' in widget.keys() else []
            if widget.winfo_class() == 'Text':
                values.append(widget.get('1.0', 'end-1c'))
            values.extend(texts(child) for child in widget.winfo_children())
            return '\n'.join(values)

        def capture(name):
            root.update()
            visible = texts(root)
            assert not re.search(r'\bfake\b|\btest(?:ing)?\b|/tmp', visible, re.I), visible
            path = args.out / (name + '.png')
            subprocess.run(['import', '-window', str(root.winfo_id()), str(path)], check=True, timeout=10)
            clean_png(path)
            print(path)

        try:
            pump(lambda: not controller.busy)
            root.focus_force()
            window.open_action('flash-update')
            pump(lambda: window.prompt is not None)
            window.controls['primary'].invoke()
            pump(lambda: window.prompt is not None and window.prompt.title == 'Choose calibration handling')
            assert window.controls['selected']() == policy.KEEP
            assert tuple(window.controls['options']) == policy.POLICIES
            capture('flash-calibration')
            window.controls['choose'](policy.WITHOUT)
            window.controls['primary'].invoke()
            pump(lambda: window.prompt is not None and window.prompt.kind == 'confirm')
            assert window.prompt is not None
            assert window.prompt.title == policy.UNSAFE_TITLE
            assert window.prompt.initial == 'cancel'
            pump(lambda: not window.controls['primary'].instate(['disabled']))
            assert root.focus_get() == window.controls['cancel']
            capture('flash-without-calibration-confirm')
            window.controls['cancel'].invoke()
            pump(lambda: not controller.busy)
            assert not api.calls and not api.writes
            print('Verified: two choices, keep preselected, unsafe Cancel-default, zero backend writes/calls.')
        finally:
            controller.shutdown()
            pump(lambda: not controller.busy)
            window.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
