#!/usr/bin/env python3
"""Real-window GUI smoke + screenshot evidence. All device calls use FakeApi.

Run under Xvfb: python3 scripts/gui_smoke.py --out gui-shots
Requires Tk and ImageMagick's `import`. Never opens jibo_dfu's device layer.
`--runtime-root` exercises GUI modules extracted from a test package.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("gui-shots"))
    parser.add_argument("--runtime-root", type=Path, default=ROOT)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "tests"))
    sys.path.insert(0, str(args.runtime_root.resolve()))
    from gui_fakes import FakeApi, FileLayoutError
    import jibo_gui as gui
    import jibo_gui_core as core

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    api = FakeApi("none")
    c = core.GuiController(api)
    root = gui.create_root()
    callback_errors = []
    root.report_callback_exception = lambda *exc: callback_errors.append("".join(traceback.format_exception(*exc)))
    w = gui.ToolkitWindow(root, c)
    shots = []
    checks = []

    def check(condition, name):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    def pump_until(predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            root.update()
            if callback_errors:
                raise AssertionError(callback_errors)
            if predicate():
                return
            time.sleep(0.01)
        raise AssertionError("GUI state timed out: " + w.page_name)

    def shot(name, popup=None):
        root.update()
        check(not callback_errors, "callbacks clean: " + name)
        check(root.winfo_viewable(), "window visible: " + name)
        path = out / (name + ".png")
        check(name not in {s["name"] for s in shots}, "unique screen: " + name)
        window_id = root.winfo_id() if popup is None else root.tk.call("winfo", "id", popup)
        subprocess.run(["import", "-window", str(window_id), str(path)], check=True, timeout=10)
        shots.append({"name": name, "popup": popup, "path": str(path), "page": w.page_name,
                      "title": str(root.tk.call("wm", "title", popup)) if popup else
                               w.prompt.title if w.prompt else "",
                      "width": int(root.tk.call("winfo", "width", popup)) if popup else root.winfo_width(),
                      "height": int(root.tk.call("winfo", "height", popup)) if popup else root.winfo_height(),
                      "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})

    def prompt_screen(name, answer):
        pump_until(lambda: w.prompt is not None)
        p = w.prompt
        if p.kind == "confirm":
            check(w.controls["review"].get() == p.detail, "exact review: " + name)
            check(p.initial == "cancel", "cancel default: " + name)
            check(root.focus_get() is w.controls["cancel"], "cancel focused: " + name)
            w.controls["primary"].invoke()
            check(not p.answered, "early confirm refused: " + name)
            shot(name)
            if answer == "confirm":
                pump_until(lambda: not w.controls["primary"].instate(["disabled"]))
                if name == "18-combined-full-var-review":
                    shot(name + "-armed")
                w.controls["primary"].invoke()
            else:
                w.controls["cancel"].invoke()
        elif p.kind == "text":
            w.controls["value"].set(answer or "")
            if p.password:
                check(bool(w.controls["entry"].cget("show")), "masked password: " + name)
            shot(name)
            variable = w.controls["value"]
            w.controls["primary" if answer is not None else "cancel"].invoke()
            check(variable.get() == "", "entry cleared: " + name)
        elif p.kind == "multi":
            w.controls["all"](False)
            for selected in answer:
                w.controls["toggle"](selected)
            shot(name)
            w.controls["primary"].invoke()
        elif "boot_enabled" in w.controls:
            check(w.controls["boot_enabled"].get(), "developer checkbox defaults ON")
            if answer == "keep":
                w.controls["checkbox"].invoke()
            shot(name)
            w.controls["remove" if answer == "remove" else "primary"].invoke()
        else:
            if answer is not None:
                w.controls["choose"](answer)
            shot(name)
            w.controls["primary" if answer is not None else "cancel"].invoke()
        pump_until(lambda: w.prompt is not p)

    def dismiss_popup(name):
        # Drive actual Tk dialogs through their nested event loop, never mocks.
        def capture_and_cancel():
            popups = [str(child) for child in root.tk.call("winfo", "children", ".")
                      if bool(int(root.tk.call("winfo", "toplevel", child) == str(child)))
                      and bool(int(root.tk.call("winfo", "viewable", child)))]
            check(len(popups) == 1, "one native dialog: " + name)
            popup = popups[0]
            shot(name, popup=popup)
            root.tk.call("event", "generate", popup, "<Escape>")
        root.after(300, capture_and_cancel)

    def flow(key, entries, result_name):
        w.show_home()
        root.update()
        menu = "more-tools" if key in ("set-mode", "configure-wifi") else key
        w.action_rows[menu].activate()
        check(c.busy, "action busy: " + result_name)
        check(not c.start_action(key), "duplicate refused: " + result_name)
        check(not c.request_refresh(), "probe refused while busy: " + result_name)
        if menu != key:
            prompt_screen(result_name + "-submenu", key)
        for name, answer in entries:
            prompt_screen(name, answer)
        pump_until(lambda: not c.busy and w.page_name in ("result", "home"))
        shot(result_name)
        if w.page_name == "result":
            w.controls["primary"].invoke()
        root.update()

    try:
        with core.capture_output(c):
            shot("00-checking-usb")
            pump_until(lambda: c.snapshot().checked)
            w.render_state()
            shot("01-no-device")
            for state, name in (("multiple", "02-multiple-devices"), ("rcm", "03-rcm-apx")):
                api.state = state
                c.request_refresh()
                pump_until(lambda: c.snapshot().readiness.state == state)
                w.render_state()
                shot(name)
            flow("enter-dfu-shofel", [("04-rcm-profile-review", "confirm")], "05-dfu-ready")
            flow("set-mode", [("06-mode-select", "developer"), ("07-mode-review", "confirm")], "08-mode-success")
            flow("set-mode", [("09-mode-cancel-select", "normal"), ("10-mode-cancel-review", None)], "11-mode-cancelled")
            flow("set-mode-wifi", [("12-combined-mode", "int-developer"),
                 ("13-wifi-ssid", "Example network"), ("14-wifi-security", "protected"),
                 ("15-wifi-password", "examplepass1"), ("16-wifi-password-repeat", "examplepass1"),
                 ("17-boot-status-on", "install"), ("18-combined-full-var-review", "confirm")], "19-combined-success")
            flow("set-mode-wifi", [("20-keep-mode", "developer"), ("21-keep-ssid", "Example network"),
                 ("22-open-network", "open"), ("23-boot-status-off-keep", "keep"),
                 ("24-combined-fast-review", "confirm")], "25-combined-fast-success")
            flow("configure-wifi", [("26-wifi-only-ssid", "Example network"),
                 ("27-wifi-only-security", "protected"), ("28-mismatch-first", "examplepass1"),
                 ("29-mismatch-repeat", "differentpass1"), ("30-password-mismatch", "cancel")], "31-wifi-cancelled")
            flow("configure-wifi", [("32-open-wifi-ssid", "Open network"), ("33-open-wifi-type", "open"),
                 ("34-wifi-review", "confirm")], "35-wifi-success")
            # Progress is generated by a real worker held behind a test-only gate.
            gate = threading.Event()
            original_backup = api.backup_var
            def progress_backup(**kwargs):
                print("Reading the 500 MiB var partition from USB (FAKE DEVICE)", flush=True)
                sys.stderr.write("\r[##########----------] 50.0% | 250.0/500.0 MiB | 10.00 MiB/s | 25s")
                sys.stderr.flush()
                gate.wait(5)
                return original_backup(**kwargs)
            api.backup_var = progress_backup
            w.open_action("backups")
            prompt_screen("36a-backups-menu", "backup-var")
            pump_until(lambda: float(w.progress_bar.cget("value")) == 50)
            shot("36-backup-progress")
            check(c.busy and not c.start_action("backup-var"), "transfer duplicate refused")
            gate.set()
            pump_until(lambda: not c.busy and w.page_name == "result")
            shot("37-backup-success")
            w.controls["log_toggle"].invoke()
            shot("38-backup-operation-log")
            w.show_home()
            api.backup_var = original_backup
            flow("backups", [("39-more-tools", "backup-choose"),
                 ("40-backup-var-checklist", ("var",)),
                 ("40a-backup-review", "start")], "41-backup-set-success")
            with tempfile.TemporaryDirectory(prefix="gui-smoke-") as directory:
                manifest = Path(directory) / "backup-set.json"
                manifest.write_text(json.dumps({"partitions": [{"name": "var", "size_bytes": 524288000},
                                                              {"name": "services", "size_bytes": 524288000}]}))
                flow("backups", [("42-restore-tool", "restore-backup"),
                     ("42a-other-folder", "other-folder"),
                     ("43-restore-path", str(manifest)), ("44-restore-var-checklist", ("var",)),
                     ("45-restore-review", "confirm")], "46-restore-success")
            flow("flash-update", [("47-update-package", "0"), ("48-update-calibration-policy", "keep-calibration"),
                 ("49-update-review", "confirm")], "50-update-success")
            flow("flash-update", [("51-update-fresh-package", "0"), ("52-update-calibration-policy", "keep-calibration"),
                 ("53-update-fresh-review-cancel", None)], "54-update-cancelled")
            flow("flash-update", [("55-update-factory-package", "0"), ("56-update-unsafe-policy", "without-calibration"),
                 ("57-update-unsafe-confirmation", "confirm"), ("57b-update-write-review", "confirm")], "58-update-factory-success")
            flow("more-tools", [("59-remove-tool", "remove-boot-network-status"),
                 ("60-remove-boot-review", "confirm")], "61-remove-success")
            flow("more-tools", [("62-write-var-tool", "write-var"), ("63-write-var-path", "/tmp/edited-var.img"),
                 ("64-write-var-review", "confirm")], "65-write-var-success")
            flow("more-tools", [("66-inspect-tool", "inspect-backup"), ("67-inspect-local-path", "/tmp/var.img")], "68-inspect-result")
            flow("more-tools", [("69-edit-tool", "edit-backup"), ("70-local-edit-kind", "mode"),
                 ("71-local-edit-path", "/tmp/var.img"), ("72-local-edit-mode", "developer")], "73-local-edit-result")
            flow("more-tools", [("73a-local-wifi-tool", "edit-backup"),
                 ("73b-local-wifi-kind", "wifi"), ("73c-local-wifi-path", "/tmp/var.img"),
                 ("73d-local-wifi-ssid", "Example network"), ("73e-local-wifi-security", "protected"),
                 ("73f-local-wifi-password", "examplepass1"), ("73g-local-wifi-repeat", "examplepass1")],
                 "73h-local-wifi-result")
            flow("more-tools", [("74-gpt-tool", "probe-dfu-gpt")], "75-gpt-result")
            old_quick = api.set_mode_file_live
            api.set_mode_file_live = lambda *a, **k: (_ for _ in ()).throw(FileLayoutError(4))
            flow("set-mode", [("76-fallback-mode", "developer"), ("77-dirty-journal-fallback", "full"),
                 ("78-full-var-mode-review", "confirm")], "79-fallback-success")
            api.set_mode_file_live = old_quick
            api.fail = RuntimeError("FAKE readback mismatch. The robot remains in DFU; no additional write was attempted.")
            flow("set-mode", [("80-error-mode", "developer")], "81-readback-error")
            api.fail = None
            # All loader failure states are real model states generated by the fake boundary.
            api.alts = ["var"]
            c.request_refresh()
            pump_until(lambda: c.snapshot().readiness.state == "dfu-no-marker")
            w.render_state()
            shot("82-missing-loader-marker")
            api.alts = [api.MARKER]
            c.request_refresh()
            pump_until(lambda: c.snapshot().readiness.state == "dfu-profile-incomplete")
            w.render_state()
            shot("83-incomplete-loader")
            api.dfu_alternatives = lambda *a: ([], "Cannot open DFU device")
            c.request_refresh()
            pump_until(lambda: c.snapshot().readiness.state == "dfu-error")
            w.render_state()
            shot("84-usb-access-error")
            api.devices = lambda: (_ for _ in ()).throw(RuntimeError("FAKE USB enumeration failed"))
            c.request_refresh()
            pump_until(lambda: c.snapshot().headline == "USB check failed")
            w.render_state()
            shot("84a-usb-enumeration-error")
            check(not any(i.enabled for i in c.snapshot().items), "failed USB probe disables stale actions")
            # Supplemental screen type and small-window layout coverage.
            w.show_prompt(core.Prompt("message", "No device changes", lines=("A local image inspection is read-only.",)))
            shot("85-message-screen")
            w.controls["primary"].invoke()
            root.geometry("920x640")
            w.show_prompt(core.Prompt("confirm", "Confirm full-flash update",
                          detail="Review this exact plan.\n" * 80, initial="cancel",
                          items=c.flows._normalise_options((("cancel", "Cancel"), ("confirm", "Confirm")))))
            shot("86-small-window-scrollable-review")
            review = w.controls["review"]
            check(review.bar.winfo_ismapped(), "long review has scrollbar")
            pump_until(lambda: not w.controls["primary"].instate(["disabled"]))
            shot("86-small-window-review-armed")
            w.controls["cancel"].invoke()
            w.show_home()
            with tempfile.TemporaryDirectory(prefix="gui-picker-") as directory:
                api.BACKUP_ROOT = Path(directory)
                (Path(directory) / "fake-var.img").write_bytes(b"fake image-picker fixture")
                (Path(directory) / "backup-set.json").write_text("{}")
                w.show_prompt(core.Prompt("text", "Write an edited var image", prompt="Path to edited 500 MiB var image:"))
                value = w.controls["value"]
                dismiss_popup("87-image-file-picker")
                w._browse(value)
                check(value.get() == "", "file-picker cancel preserves empty input")
                dismiss_popup("88-backup-manifest-file-picker")
                w._browse(value, json_file=True)
                check(value.get() == "", "manifest-picker cancel preserves empty input")
                dismiss_popup("89-backup-folder-picker")
                w._browse(value, folder=True)
                check(value.get() == "", "folder-picker cancel preserves empty input")
                w.answer(None)
            c._busy = True
            dismiss_popup("90-active-operation-close-warning")
            w.close()
            check(not w.closed, "native warning prevents active-transfer close")
            c._pending = core.Prompt("confirm", "Confirm var write")
            dismiss_popup("91-cancel-and-quit-default-no")
            w.close()
            check(not w.closed and not c._pending.answered, "native quit question defaults no")
            c._busy = False
            c._pending = None
            check(not callback_errors, "zero Tk callback errors")
            w.close()
    finally:
        c.shutdown()
        if not w.closed:
            w.close()

    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())
    receipt = {"revision": revision, "dirty": dirty, "runtime_root": str(args.runtime_root.resolve()),
               "gui_source": gui.__file__, "controller_source": core.__file__,
               "flow_source": c.flows.__file__,
               "fake_device": True, "screenshots": shots, "screenshot_count": len(shots),
               "checks": checks, "check_count": len(checks), "tk_callback_errors": callback_errors,
               "fake_write_count": len(api.writes), "fake_call_count": len(api.calls)}
    (out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps({key: value for key, value in receipt.items() if key not in ("screenshots", "checks")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
