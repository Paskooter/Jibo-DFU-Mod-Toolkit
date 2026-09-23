"""Stateful curses interface for the Jibo DFU toolkit.

This module deliberately receives the command module as an argument. It does
not import jibo_dfu, which keeps the CLI and the terminal interface acyclic.
"""

import curses
from dataclasses import dataclass
import json
from pathlib import Path
import re
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
    shofel_available: bool = False


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
        available = False
        try:
            available = bool(api.shofel_available())
        except Exception:
            pass
        return Readiness("rcm", found, port=port, shofel_available=available)
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

    if readiness.state == "rcm":
        shofel_reason = ("" if readiness.shofel_available else
                          "Install shofel2_t124 and its adjacent emmc_server.bin, or use the CLI --shofel option.")
    elif readiness.state == "multiple":
        shofel_reason = "Connect one robot at a time."
    else:
        shofel_reason = "ShofEL backup requires the robot to be in RCM/APX."

    packages = tuple(update_packages)
    update_required = ("rootfsA", "rootfsB", "services", "emmc-000")
    missing_update = [name for name in update_required if name not in readiness.alt_names]
    has_skills = ("skills" in readiness.alt_names or any(
        re.fullmatch(r"skills-\d{3}", name) for name in readiness.alt_names))
    if not has_skills:
        missing_update.append("skills")
    if not is_dfu:
        update_reason = connection_reason
    elif missing_update:
        if "skills" in missing_update:
            update_reason = (
                "This recovery loader cannot write skills. Load a compatible signed recovery bundle to install updates.")
            other_missing = [name for name in missing_update if name != "skills"]
            if other_missing:
                update_reason += " It also does not expose: " + ", ".join(other_missing) + "."
        else:
            update_reason = "The recovery loader does not expose: " + ", ".join(missing_update) + "."
    elif not packages:
        update_reason = "No packages found in ./updates."
    else:
        update_reason = ""
    items = [
        MenuItem("enter-dfu", "Enter DFU from RCM/APX", is_rcm, enter_reason),
        MenuItem("backup-var-shofel", "Back up var with ShofEL (read-only)",
                 is_rcm and readiness.shofel_available, shofel_reason),
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


def _terminal_available():
    return (getattr(sys.stdin, "isatty", lambda: False)() and
            getattr(sys.stdout, "isatty", lambda: False)())


def _start_screen(screen):
    try:
        screen.keypad(True)
    except Exception:
        pass
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    if hasattr(curses, "use_default_colors"):
        try:
            curses.start_color()
            curses.use_default_colors()
        except curses.error:
            pass


def _screen_heading(screen, title, detail=""):
    screen.erase()
    height, width = screen.getmaxyx()
    bold = getattr(curses, "A_BOLD", 0)
    dim = getattr(curses, "A_DIM", 0)
    _addstr(screen, 0, 0, "JIBO DFU TOOLKIT", bold)
    _addstr(screen, 2, 0, title, bold)
    line = 3
    for paragraph in str(detail or "").splitlines():
        for segment in _wrap(paragraph, max(1, width - 2)):
            if line >= height - 3:
                return line, height, width
            _addstr(screen, line, 0, segment, dim)
            line += 1
    return line, height, width


def _normalise_options(options):
    normalised = []
    for option in options:
        if isinstance(option, MenuItem):
            normalised.append(option)
        elif len(option) == 2:
            key, label = option
            normalised.append(MenuItem(str(key), str(label)))
        elif len(option) == 3:
            key, label, enabled = option
            normalised.append(MenuItem(str(key), str(label), bool(enabled)))
        else:
            key, label, enabled, reason = option
            normalised.append(MenuItem(str(key), str(label), bool(enabled), str(reason)))
    return tuple(normalised)


def _move_menu_selection(options, current, step):
    if not options:
        return 0
    return (current + step) % len(options)


def _select_session(screen, title, options, prompt="", detail="", initial=None,
                    cancel_label="Cancel"):
    _start_screen(screen)
    items = _normalise_options(options)
    selected = 0
    if initial is not None:
        selected = next((i for i, item in enumerate(items) if item.key == initial), 0)
    while True:
        row, height, width = _screen_heading(screen, title, detail)
        row += 1
        dim = getattr(curses, "A_DIM", 0)
        reverse = getattr(curses, "A_REVERSE", 0)
        available = max(1, height - row - 4)
        start = max(0, min(selected - available + 1, len(items) - available))
        for index in range(start, min(len(items), start + available)):
            item = items[index]
            text = ("> " if index == selected else "  ") + item.label
            attr = 0 if item.enabled else dim
            if index == selected:
                attr |= reverse
            _addstr(screen, row + index - start, 0, text, attr)

        hint_y = min(height - 3, row + available)
        if items and not items[selected].enabled:
            hint = items[selected].reason or "This action is unavailable."
        else:
            hint = prompt
        for offset, segment in enumerate(_wrap(hint, max(1, width - 2))):
            _addstr(screen, hint_y + offset, 0, segment, dim)
        _addstr(screen, height - 1, 0,
                "↑/↓ Select   Enter Open   Esc " + str(cancel_label), dim)
        screen.refresh()

        key = screen.getch()
        if key in (27, ord("q"), ord("Q")):
            return None
        if key in (curses.KEY_UP, ord("k")):
            selected = _move_menu_selection(items, selected, -1)
        elif key in (curses.KEY_DOWN, ord("j")):
            selected = _move_menu_selection(items, selected, 1)
        elif key in (curses.KEY_ENTER, 10, 13):
            if not items:
                return None
            if items[selected].enabled:
                return items[selected].key


def select_option(title, options, prompt="", detail="", cancel_label="Cancel", initial=None):
    """Show a selectable curses menu and return its key, or None on cancel.

    ``options`` may contain MenuItem instances or tuples of ``(key, label)``,
    ``(key, label, enabled)``, or ``(key, label, enabled, reason)``.
    Disabled rows remain visible and explain why they cannot be opened.
    """
    if not _terminal_available():
        return None
    return curses.wrapper(lambda screen: _select_session(
        screen, title, options, prompt, detail, initial, cancel_label))


def _read_key(screen):
    get_wch = getattr(screen, "get_wch", None)
    if get_wch is not None:
        try:
            return get_wch()
        except (AttributeError, curses.error):
            pass
    return screen.getch()


def _text_session(screen, title, prompt, default="", password=False, detail=""):
    _start_screen(screen)
    try:
        curses.curs_set(1)
    except curses.error:
        pass
    value = list(str(default))
    while True:
        row, height, width = _screen_heading(screen, title, detail)
        row += 1
        _addstr(screen, row, 0, prompt, getattr(curses, "A_BOLD", 0))
        row += 2
        _addstr(screen, row, 0, "> ")
        visible = "•" * len(value) if password else "".join(value)
        display_width = max(1, width - 4)
        if len(visible) > display_width:
            visible = visible[-display_width:]
        _addstr(screen, row, 2, visible, width=display_width)
        _addstr(screen, row, min(width - 2, 2 + len(visible)), "▏", getattr(curses, "A_REVERSE", 0))
        _addstr(screen, height - 1, 0, "Enter Accept   Esc Cancel   Ctrl-U Clear", getattr(curses, "A_DIM", 0))
        try:
            screen.move(row, min(width - 1, 2 + len(visible)))
        except (AttributeError, curses.error):
            pass
        screen.refresh()

        key = _read_key(screen)
        if isinstance(key, str):
            if key in ("\n", "\r"):
                return "".join(value)
            if key == "\x1b":
                return None
            if key in ("\x7f", "\b"):
                if value:
                    value.pop()
                continue
            if key == "\x15":
                value.clear()
                continue
            if len(key) == 1 and key.isprintable():
                value.append(key)
            continue
        if key in (27,):
            return None
        if key in (curses.KEY_ENTER, 10, 13):
            return "".join(value)
        if key in (curses.KEY_BACKSPACE, 127, 8):
            if value:
                value.pop()
        elif key == 21:
            value.clear()
        elif 32 <= key <= 0x10FFFF and key < curses.KEY_MIN:
            try:
                value.append(chr(key))
            except ValueError:
                pass


def text_input(title, prompt, default="", password=False, detail=""):
    """Collect a line in curses. Password characters are never drawn literally."""
    if not _terminal_available():
        return None
    return curses.wrapper(lambda screen: _text_session(
        screen, title, prompt, default, password, detail))


def confirm_action(title, message, phrase=None):
    """Ask for explicit confirmation with Cancel selected by default.

    The optional phrase is shown as context for older callers. It is not typed;
    automation confirmations remain the responsibility of the command line.
    """
    detail = str(message or "")
    if phrase:
        detail += "\nConfirmation: " + str(phrase)
    choice = select_option(
        title,
        (MenuItem("cancel", "Cancel"), MenuItem("confirm", "Confirm")),
        prompt="Review the details, then select Confirm to continue.",
        detail=detail,
        initial="cancel")
    return choice == "confirm"


def _screen_lines(content):
    if isinstance(content, dict):
        content = json.dumps(content, indent=2, default=str)
    if isinstance(content, str):
        return content.splitlines() or [""]
    return [str(line) for line in content]


def _message_session(screen, title, lines, wait=True):
    _start_screen(screen)
    offset = 0
    while True:
        row, height, width = _screen_heading(screen, title)
        row += 1
        view_height = max(1, height - row - 3)
        wrapped = []
        for line in lines:
            wrapped.extend(_wrap(line, max(1, width - 2)))
        max_offset = max(0, len(wrapped) - view_height)
        offset = min(offset, max_offset)
        for index, line in enumerate(wrapped[offset:offset + view_height]):
            _addstr(screen, row + index, 0, line)
        footer = "↑/↓ Scroll   Enter Back"
        if not wait:
            footer = "Enter Back"
        elif max_offset:
            footer = "↑/↓ Scroll   PgUp/PgDn   Enter Back"
        _addstr(screen, height - 1, 0, footer, getattr(curses, "A_DIM", 0))
        screen.refresh()
        if not wait:
            return
        key = screen.getch()
        if key in (10, 13, curses.KEY_ENTER, 27, ord("q"), ord("Q")):
            return
        if key in (curses.KEY_UP, ord("k")):
            offset = max(0, offset - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            offset = min(max_offset, offset + 1)
        elif key == curses.KEY_PPAGE:
            offset = max(0, offset - view_height)
        elif key == curses.KEY_NPAGE:
            offset = min(max_offset, offset + view_height)
        elif key == curses.KEY_HOME:
            offset = 0
        elif key == curses.KEY_END:
            offset = max_offset


def show_screen(title, lines, wait=True):
    """Show a result, plan, or explanation in the shared curses style."""
    if not _terminal_available():
        return None
    return curses.wrapper(lambda screen: _message_session(
        screen, title, _screen_lines(lines), wait))


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
        "backup-var-shofel": "Reads the GPT and var partition over ShofEL USB. This action does not write eMMC.",
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


def _confirmation_details(plan, introduction):
    if not isinstance(plan, dict):
        return introduction + ("\n" + str(plan) if plan else "")
    lines = [introduction]
    fields = (
        ("operation", "Operation"),
        ("package", "Package"),
        ("version", "Version"),
        ("usb_port", "USB port"),
        ("var_policy", "Var policy"),
        ("partition", "Partition"),
        ("size_bytes", "Size (bytes)"),
        ("before_sha256", "Current var SHA-256"),
        ("candidate_sha256", "Edited var SHA-256"),
        ("baseline_backup", "Rollback backup"),
        ("baseline_sha256", "Backup SHA-256"),
    )
    for key, label in fields:
        if plan.get(key) is not None:
            lines.append("{}: {}".format(label, plan[key]))
    partitions = plan.get("partitions")
    if partitions:
        lines.append("Partitions:")
        for partition in partitions:
            if isinstance(partition, dict):
                name = partition.get("name", "unknown")
                size = partition.get("bytes")
                lines.append("  {}{}".format(name, " ({} bytes)".format(size) if size is not None else ""))
            else:
                lines.append("  " + str(partition))
    if len(lines) == 1:
        lines.append(json.dumps(plan, sort_keys=True, default=str))
    return "\n".join(lines)


def _mode_options():
    return (
        ("normal", "normal — standard use"),
        ("developer", "developer — selected development services"),
        ("int-developer", "int-developer — broader internal development mode"),
        ("oobe", "oobe — setup and onboarding"),
    )


def _select_mode(title="Set robot mode"):
    return select_option(
        title, _mode_options(),
        prompt="Choose the mode to apply.",
        detail="Mode changes are written to the robot only after the change plan is reviewed.")


def _run_enter(api, readiness):
    if readiness.state != "rcm":
        raise RuntimeError("Enter DFU is available only while the robot is in RCM/APX.")
    bundle = Path(api.ROOT) / "bundles" / "default"
    dfu_util = api.tool("dfu-util")
    if not (bundle / "manifest.json").is_file():
        raise RuntimeError("The recovery bundle is missing. Place the matching bundle in bundles/default/.")
    manifest = api.load_bundle(bundle)
    profile = manifest.get("profile", "local recovery bundle")
    if not confirm_action(
            "Enter DFU from RCM/APX",
            "Recovery profile: " + profile +
            "\nLoad the recovery program into RAM and wait for the robot to enter DFU?"):
        return {"status": "cancelled", "message": "Recovery was not loaded."}
    return api.enter(bundle, readiness.port, api.tool("tegrarcm"), dfu_util,
                     allow_unverified_profile=True)


def _run_update(api):
    folder = Path.cwd() / "updates"
    packages = api._update_candidates(folder)
    if not packages:
        return {"status": "cancelled", "message": "No official full-flash packages found in " + str(folder)}
    package_key = select_option(
        "Choose an official update package",
        tuple((str(index), package.name) for index, package in enumerate(packages)),
        prompt="Select a package to continue.",
        detail="Packages are read from " + str(folder) + ".")
    if package_key is None:
        return {"status": "cancelled", "message": "No package selected."}
    policy_key = select_option(
        "Choose var handling",
        (("preserve", "Preserve current var and robot settings"),
         ("fresh", "Use package var for a fresh setup")),
        prompt="Choose how the update handles the var partition.",
        detail=("Preserve keeps the current robot identity, mode, Wi-Fi, and user configuration.\n"
                "Fresh replaces those settings with the package image."))
    if policy_key is None:
        return {"status": "cancelled", "message": "No var policy selected."}
    package = packages[int(package_key)]
    confirmation = lambda plan: confirm_action(
        "Confirm full-flash update",
        _confirmation_details(plan, "Review the package, var policy, and partitions before writing."))
    return api.flash_update(package, preserve_var=policy_key == "preserve",
                            confirmation=confirmation)


def execute_action(api, key, readiness):
    """Run an action selected from the main screen using the shared curses prompts."""
    if key == "enter-dfu":
        return _run_enter(api, readiness)
    if key == "backup-var":
        return api.backup_var()
    if key == "backup-var-shofel":
        return api.backup_var_shofel(port=readiness.port)
    if key == "set-mode":
        mode = _select_mode()
        if mode is None:
            return {"status": "cancelled", "message": "No mode selected."}
        confirmation = lambda plan: confirm_action(
            "Confirm mode change",
            _confirmation_details(plan, "Review the var write plan before changing the robot mode."))
        return api.set_mode_live(mode, confirmation=confirmation)
    if key == "configure-wifi":
        ssid = text_input("Configure Wi-Fi", "Wi-Fi network name (SSID):")
        if ssid is None:
            return {"status": "cancelled", "message": "Wi-Fi setup was cancelled."}
        kind = select_option(
            "Choose network type",
            (("protected", "Protected WPA/WPA2 network"), ("open", "Open network")),
            prompt="Choose the security type for this network.")
        if kind is None:
            return {"status": "cancelled", "message": "No network type selected."}
        open_network = kind == "open"
        password = None
        if not open_network:
            password = text_input("Configure Wi-Fi", "Wi-Fi password:", password=True,
                                  detail="The password is hidden while you type.")
            if password is None:
                return {"status": "cancelled", "message": "Wi-Fi setup was cancelled."}
        confirmation = lambda plan: confirm_action(
            "Confirm Wi-Fi change",
            _confirmation_details(plan, "Review the var write plan before adding this network."))
        try:
            return api.configure_wifi_live(ssid, password, open_network,
                                           confirmation=confirmation)
        finally:
            password = None
    if key == "flash-update":
        return _run_update(api)
    if key == "write-var":
        image = text_input("Write an edited var image", "Path to edited 500 MiB var image:")
        if image is None or not image.strip():
            return {"status": "cancelled", "message": "No image selected."}
        confirmation = lambda plan: confirm_action(
            "Confirm var write",
            _confirmation_details(plan, "Review the var write plan before writing to the robot."))
        return api.write_var(image.strip(), confirmation=confirmation)
    if key == "inspect-backup":
        image = text_input("Inspect a var backup", "Path to var backup image:")
        if image is None or not image.strip():
            return {"status": "cancelled", "message": "No image selected."}
        return api.images.inspect_var(image.strip())
    if key == "edit-backup":
        choice = select_option(
            "Edit a local var backup",
            (("mode", "Change mode"), ("wifi", "Add Wi-Fi network"),
             ("inspect", "Inspect image")),
            prompt="Choose a local image action.",
            detail="These actions create no robot writes.")
        if choice is None:
            return {"status": "cancelled", "message": "No offline action selected."}
        source = text_input("Edit a local var backup", "Path to var backup image:")
        if source is None or not source.strip():
            return {"status": "cancelled", "message": "No image selected."}
        source = source.strip()
        if choice == "inspect":
            return api.images.inspect_var(source)
        if choice == "mode":
            mode = _select_mode("Choose mode for the local image")
            if mode is None:
                return {"status": "cancelled", "message": "No mode selected."}
            return api.images.edit_mode(source, api._default_edit_path(source), mode)

        ssid = text_input("Add Wi-Fi to a local image", "Wi-Fi network name (SSID):")
        if ssid is None:
            return {"status": "cancelled", "message": "Wi-Fi edit was cancelled."}
        kind = select_option(
            "Choose network type",
            (("protected", "Protected WPA/WPA2 network"), ("open", "Open network")),
            prompt="Choose the security type for this network.")
        if kind is None:
            return {"status": "cancelled", "message": "Wi-Fi edit was cancelled."}
        open_network = kind == "open"
        password = None
        if not open_network:
            password = text_input("Add Wi-Fi to a local image", "Wi-Fi password:", password=True,
                                  detail="The password is hidden while you type.")
            if password is None:
                return {"status": "cancelled", "message": "Wi-Fi edit was cancelled."}
        try:
            return api.images.edit_wifi(source, api._default_edit_path(source), ssid,
                                        password, open_network)
        finally:
            password = None
    if key == "verify-bundle":
        path = text_input("Check a local recovery bundle", "Path to recovery bundle directory:")
        if path is None or not path.strip():
            return {"status": "cancelled", "message": "No bundle selected."}
        return api.load_bundle(path.strip())
    raise ValueError("Unknown menu action: " + str(key))


def run(api_module):
    """Run the UI for an interactive terminal; return None when there is none."""
    if not _terminal_available():
        return None

    app = TerminalMenu(api_module)
    while True:
        app.command = None
        curses.wrapper(app.session)
        if app.command in (None, "quit"):
            return 0
        action = app.command
        label = next(item.label for item in app.items if item.key == action)
        try:
            result = execute_action(api_module, action, app.readiness)
            show_screen(label, _screen_lines(result))
        except KeyboardInterrupt:
            show_screen(label, "Cancelled.")
        except Exception as exc:
            show_screen("Action failed", str(exc))
        app.refresh()
