"""Stateful curses interface for the Jibo DFU toolkit.

This module deliberately receives the command module as an argument. It does
not import jibo_dfu, which keeps the CLI and the terminal interface acyclic.
"""

import curses
from dataclasses import dataclass
import json
from pathlib import Path
import sys
import textwrap


@dataclass(frozen=True)
class Readiness:
    state: str
    devices: tuple
    port: str = ""
    marker_present: bool = False
    alt_names: tuple = ()
    detail: str = ""


@dataclass(frozen=True)
class MenuItem:
    key: str
    label: str
    enabled: bool = True
    reason: str = ""


def inspect_readiness(api):
    """Read USB state and, for DFU, check for the toolkit's loader marker."""
    found = tuple(api.devices())
    if not found:
        return Readiness("none", found)
    if len(found) != 1:
        ports = ", ".join(device.get("port", "?") for device in found)
        return Readiness("multiple", found, detail="Recovery devices found on ports " + ports)

    device = found[0]
    port = device.get("port", "")
    if device.get("state") == "rcm":
        return Readiness("rcm", found, port=port)
    try:
        executable = api.tool("dfu-util")
        names, output = api.dfu_alternatives(executable, port)
    except Exception as exc:
        return Readiness("dfu-error", found, port=port, detail=str(exc))
    names = tuple(names)
    if "Cannot open DFU device" in output or "Permission denied" in output:
        return Readiness("dfu-error", found, port=port,
                         detail="USB access is unavailable. Relaunch the toolkit with sudo.")
    if not names:
        return Readiness("dfu-error", found, port=port,
                         detail="The DFU alternatives could not be read. Check USB access and refresh.")
    if api.MARKER not in names:
        return Readiness("dfu-no-marker", found, port=port, alt_names=names,
                         detail="Jibo recovery loader marker not found")
    if "var" not in names:
        return Readiness("dfu-profile-incomplete", found, port=port,
                         marker_present=True, alt_names=names,
                         detail="The Jibo recovery loader does not expose the var partition")
    return Readiness("dfu-ready", found, port=port, marker_present=True, alt_names=names)


def status_lines(readiness):
    """Return short, distinct RCM-to-DFU status text for the screen."""
    if readiness.state == "none":
        return ("STEP 1 / 3   Connect the robot over USB",
                "No Jibo recovery device is visible. Put the robot into RCM/APX during reset.")
    if readiness.state == "multiple":
        return ("USB STATE   Multiple recovery devices detected",
                (readiness.detail or "Connect one robot at a time.") + ".")
    if readiness.state == "rcm":
        return ("STEP 2 / 3   RCM/APX detected",
                "RCM/APX is the entry state. Choose “Enter DFU from RCM/APX” to load recovery into RAM.")
    if readiness.state == "dfu-ready":
        return ("STEP 3 / 3   DFU active; Jibo recovery loader ready",
                "The robot is ready for the partition and update actions below.")
    if readiness.state == "dfu-error":
        return ("USB STATE   DFU detected; loader status unavailable",
                "DFU is not the same as RCM/APX. " + (readiness.detail or "Check USB access."))
    if readiness.state == "dfu-profile-incomplete":
        return ("USB STATE   DFU active; var partition unavailable",
                "The Jibo recovery loader is active, but it does not expose var. Partition actions are disabled.")
    return ("USB STATE   DFU detected; Jibo recovery loader not found",
            "DFU is not the same as RCM/APX. Restart into RCM/APX, then load the matching recovery.")


def build_menu_items(readiness, update_packages=()):
    """Build actions with disabled reasons instead of hiding their requirements."""
    is_rcm = readiness.state == "rcm"
    is_dfu = readiness.state == "dfu-ready"
    if readiness.state == "none":
        connection_reason = "Connect the robot and enter RCM/APX first."
        dfu_reason = connection_reason
    elif readiness.state == "multiple":
        connection_reason = "Connect one robot at a time."
        dfu_reason = connection_reason
    elif readiness.state == "rcm":
        connection_reason = "Enter DFU from RCM/APX first."
        dfu_reason = connection_reason
    elif readiness.state == "dfu-error":
        connection_reason = "DFU was detected, but its Jibo loader could not be checked."
        dfu_reason = "Restart into RCM/APX before entering DFU again."
    elif readiness.state == "dfu-profile-incomplete":
        connection_reason = readiness.detail or "The Jibo recovery loader does not expose var."
        dfu_reason = "The robot is already in DFU."
    elif readiness.state == "dfu-no-marker":
        connection_reason = "The Jibo recovery loader marker is missing."
        dfu_reason = "The robot is already in DFU; enter RCM/APX before loading recovery again."
    else:
        connection_reason = ""
        dfu_reason = "The robot is already in DFU."

    if is_rcm:
        enter_reason = ""
    elif readiness.state in ("dfu-ready", "dfu-error", "dfu-no-marker", "dfu-profile-incomplete"):
        enter_reason = "The robot is already in DFU."
    else:
        enter_reason = "Connect the robot and enter RCM/APX first."

    packages = tuple(update_packages)
    update_required = ("rootfsA", "rootfsB", "services", "skills", "emmc-000")
    missing_update = [name for name in update_required if name not in readiness.alt_names]
    if not is_dfu:
        update_reason = connection_reason
    elif missing_update:
        update_reason = "The recovery loader does not expose: " + ", ".join(missing_update) + "."
    elif not packages:
        update_reason = "No packages found in ./updates."
    else:
        update_reason = ""
    items = [
        MenuItem("enter-dfu", "Enter DFU from RCM/APX", is_rcm, enter_reason),
        MenuItem("backup-var", "Back up var", is_dfu, connection_reason),
        MenuItem("set-mode", "Set robot mode", is_dfu, connection_reason),
        MenuItem("configure-wifi", "Configure Wi-Fi", is_dfu, connection_reason),
        MenuItem("flash-update", "Install an official update package",
                 is_dfu and not missing_update and bool(packages), update_reason),
        MenuItem("write-var", "Write an edited var image", is_dfu, connection_reason),
        MenuItem("inspect-backup", "Inspect a local var backup"),
        MenuItem("edit-backup", "Edit a local var backup"),
        MenuItem("verify-bundle", "Check a local recovery bundle"),
    ]
    return tuple(items)


def _addstr(screen, y, x, value, attr=0, width=None):
    try:
        height, columns = screen.getmaxyx()
        if y < 0 or y >= height or x >= columns:
            return
        available = max(0, columns - x - 1)
        if width is not None:
            available = min(available, width)
        screen.addnstr(y, x, str(value), available, attr)
    except curses.error:
        pass


def _wrap(value, width):
    return textwrap.wrap(str(value), max(1, width), break_long_words=True,
                         break_on_hyphens=False) or [""]


class TerminalMenu:
    def __init__(self, api):
        self.api = api
        self.readiness = Readiness("none", ())
        self.items = ()
        self.selected = 0
        self.command = None
        self.note = ""
        self.refresh()

    def refresh(self):
        self.readiness = inspect_readiness(self.api)
        folder = Path.cwd() / "updates"
        try:
            packages = self.api._update_candidates(folder)
        except Exception:
            packages = ()
        self.items = build_menu_items(self.readiness, packages)
        self.selected = min(self.selected, len(self.items) - 1) if self.items else 0

    def render(self, screen):
        screen.erase()
        height, width = screen.getmaxyx()
        normal = 0
        dim = getattr(curses, "A_DIM", 0)
        bold = getattr(curses, "A_BOLD", 0)
        reverse = getattr(curses, "A_REVERSE", 0)
        _addstr(screen, 0, 0, "JIBO DFU TOOLKIT", bold)
        _addstr(screen, 1, 0, "USB flow:  Connect  →  RCM/APX  →  DFU  →  Choose an action")
        first, second = status_lines(self.readiness)
        _addstr(screen, 3, 0, first, bold)
        line = 4
        for segment in _wrap(second, max(1, width - 2)):
            _addstr(screen, line, 0, segment)
            line += 1
        if self.readiness.port:
            _addstr(screen, line, 0, "USB port " + self.readiness.port)
            line += 1
        line += 1
        _addstr(screen, line, 0, "ACTIONS   (↑/↓ select, Enter open, r refresh, q quit)", bold)
        first_item_line = line + 1
        for index, item in enumerate(self.items):
            y = first_item_line + index
            marker = "> " if index == self.selected else "  "
            label = marker + item.label
            attr = normal if item.enabled else dim
            if index == self.selected:
                attr |= reverse
            _addstr(screen, y, 0, label, attr)
            if not item.enabled:
                status_col = min(width - 1, max(32, len(label) + 2))
                if status_col < width - 1:
                    _addstr(screen, y, status_col, "unavailable", dim,
                            width=max(0, width - status_col - 1))

        hint_y = min(height - 2, first_item_line + len(self.items) + 1)
        if self.selected < len(self.items):
            selected_item = self.items[self.selected]
            explanation = selected_item.reason if not selected_item.enabled else _action_hint(selected_item.key)
            for offset, segment in enumerate(_wrap(explanation, max(1, width - 2))):
                _addstr(screen, hint_y + offset, 0, segment, dim)
        if self.note:
            _addstr(screen, max(0, height - 1), 0, self.note, bold)
        else:
            _addstr(screen, max(0, height - 1), 0, "q Quit   r Refresh", dim)
        screen.refresh()

    def session(self, screen):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        try:
            screen.keypad(True)
        except Exception:
            pass
        if hasattr(curses, "use_default_colors"):
            try:
                curses.start_color()
                curses.use_default_colors()
            except curses.error:
                pass
        while True:
            self.render(screen)
            key = screen.getch()
            if key in (ord("q"), ord("Q"), 27):
                self.command = "quit"
                return
            if key in (ord("r"), ord("R")):
                self.note = "Refreshing USB state..."
                self.render(screen)
                self.refresh()
                self.note = "USB state refreshed."
            elif key in (curses.KEY_UP, ord("k")):
                self.selected = (self.selected - 1) % len(self.items)
                self.note = ""
            elif key in (curses.KEY_DOWN, ord("j")):
                self.selected = (self.selected + 1) % len(self.items)
                self.note = ""
            elif key in (curses.KEY_ENTER, 10, 13):
                if not self.items:
                    continue
                item = self.items[self.selected]
                if not item.enabled:
                    self.note = item.reason
                    continue
                self.command = item.key
                return


def _action_hint(key):
    hints = {
        "enter-dfu": "Loads the recovery program into RAM while the robot is in RCM/APX.",
        "backup-var": "Read the robot's var partition and save one reusable local rollback image.",
        "set-mode": "Choose a mode; review the proposed change and confirm before writing.",
        "configure-wifi": "Enter a network; review the proposed change and confirm before writing.",
        "flash-update": "Choose a package and var policy; review the flash plan before confirming.",
        "write-var": "Select an edited image; review and confirm before writing.",
        "inspect-backup": "Read mode and Wi-Fi presence from a local image without showing credentials.",
        "edit-backup": "Create an edited copy of a local image. The source image is kept unchanged.",
        "verify-bundle": "Check local recovery bundle files and their manifest.",
    }
    return hints.get(key, "")


def _print_result(result):
    if result is None:
        return
    if isinstance(result, str):
        print(result)
    else:
        print(json.dumps(result, indent=2, default=str))


def _select_mode():
    print("  1 normal  2 developer  3 int-developer  4 oobe")
    return {"1": "normal", "2": "developer", "3": "int-developer", "4": "oobe"}.get(
        input("Choose a mode: ").strip())


def _run_enter(api, readiness):
    if readiness.state != "rcm":
        raise RuntimeError("Enter DFU is available only while the robot is in RCM/APX.")
    bundle = Path(api.ROOT) / "bundles" / "default"
    dfu_util = api.tool("dfu-util")
    if not (bundle / "manifest.json").is_file():
        raise RuntimeError("The recovery bundle is missing. Place the matching bundle in bundles/default/.")
    manifest = api.load_bundle(bundle)
    print("Recovery profile: " + manifest.get("profile", "local recovery bundle") + ".")
    if not api._ask_confirmation("Load recovery into RAM and wait for DFU?", "ENTER RCM"):
        return {"status": "cancelled", "message": "Recovery was not loaded."}
    return api.enter(bundle, readiness.port, api.tool("tegrarcm"), dfu_util,
                     allow_unverified_profile=True)


def _run_update(api):
    folder = Path.cwd() / "updates"
    packages = api._update_candidates(folder)
    if not packages:
        return {"status": "cancelled", "message": "No official full-flash packages found in " + str(folder)}
    print("Official full-flash packages:")
    for index, package in enumerate(packages, 1):
        print("  {}  {}".format(index, package.name))
    selection = input("Choose a package number (or Enter to cancel): ").strip()
    if not selection.isdigit() or not 1 <= int(selection) <= len(packages):
        return {"status": "cancelled", "message": "No package selected."}
    print("  1  Preserve current var: keep identity, mode, Wi-Fi, and user configuration")
    print("  2  Fresh var: use the package image for setup")
    policy = input("Choose var handling: ").strip()
    if policy not in ("1", "2"):
        return {"status": "cancelled", "message": "No var policy selected."}
    return api.flash_update(packages[int(selection) - 1], preserve_var=policy == "1")


def execute_action(api, key, readiness):
    """Run one selected action outside curses so prompts and progress stay clear."""
    if key == "enter-dfu":
        return _run_enter(api, readiness)
    if key == "backup-var":
        return api.backup_var()
    if key == "set-mode":
        mode = _select_mode()
        return {"status": "cancelled", "message": "No mode selected."} if mode is None else api.set_mode_live(mode)
    if key == "configure-wifi":
        ssid = input("Wi-Fi network name (SSID): ")
        kind = input("Network type: [1] protected WPA/WPA2  [2] open: ").strip()
        if kind not in ("1", "2"):
            return {"status": "cancelled", "message": "No network type selected."}
        open_network = kind == "2"
        password = api._read_password_interactively(open_network)
        try:
            return api.configure_wifi_live(ssid, password, open_network)
        finally:
            password = None
    if key == "flash-update":
        return _run_update(api)
    if key == "write-var":
        image = input("Path to edited 500 MiB var image: ").strip()
        if not image:
            return {"status": "cancelled", "message": "No image selected."}
        return api.write_var(image)
    if key == "inspect-backup":
        return api._interactive_inspect()
    if key == "edit-backup":
        print("  1  Change mode  2  Add Wi-Fi network  3  Inspect image")
        choice = input("Choose an offline image action: ").strip()
        if choice == "1":
            return api._interactive_edit_mode()
        if choice == "2":
            return api._interactive_edit_wifi()
        if choice == "3":
            return api._interactive_inspect()
        return {"status": "cancelled", "message": "No offline action selected."}
    if key == "verify-bundle":
        path = input("Path to recovery bundle directory: ").strip()
        if not path:
            return {"status": "cancelled", "message": "No bundle selected."}
        return api.load_bundle(path)
    raise ValueError("Unknown menu action: " + str(key))


def run(api_module):
    """Run the UI for an interactive terminal; return None when there is none."""
    if not (getattr(sys.stdin, "isatty", lambda: False)() and
            getattr(sys.stdout, "isatty", lambda: False)()):
        return None

    app = TerminalMenu(api_module)
    while True:
        app.command = None
        curses.wrapper(app.session)
        if app.command in (None, "quit"):
            return 0
        action = app.command
        print("\n" + next(item.label for item in app.items if item.key == action))
        try:
            _print_result(execute_action(api_module, action, app.readiness))
        except KeyboardInterrupt:
            print("\nCancelled.")
        except Exception as exc:
            print("\n" + str(exc), file=sys.stderr)
        try:
            input("\nPress Enter to return to the menu...")
        except EOFError:
            return 0
        app.refresh()
