"""Headless controller for the graphical interface.

The GUI has no device or flow logic of its own. It loads a *private* instance
of ``jibo_tui`` -- the same ``execute_action()`` flows, menu model, review and
confirmation texts, and result summaries that the text menu uses -- and
replaces only that instance's curses prompt primitives with ones that ask the
graphical view. Every robot operation still runs through the ``jibo_dfu``
functions those flows call, with the same confirmation callbacks.

The real ``jibo_tui`` module is never modified, so the text menu behaves
exactly as before. Nothing in this module imports tkinter; the controller is
tested headless against fakes of the device layer.

Threading: the view lives on the Tk main thread. One action at a time runs on
a worker thread; its prompts are handed to the view through an event queue and
the worker blocks until the user answers. USB probes and actions share one
lock, so a status probe never overlaps a transfer.
"""

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import queue
import re
import sys
import tempfile
import threading
import types


# Every jibo_tui function that opens curses is replaced in the private flow
# module. tests/test_gui_controller.py checks this list against jibo_tui's
# source so a new curses prompt cannot silently take over the terminal.
PROMPT_PRIMITIVES = ("select_option", "select_multiple_options", "text_input", "show_screen",
                     "run_with_progress")
CONFIRM_KEYS = ("cancel", "confirm")
BUSY_REASON = "Another operation is running. Wait for it to finish."
PHOENIX_GUIDE_URL = "https://jibo.io/guide"
FLOW_STEPS = ("Connect", "RCM/APX", "DFU", "Choose an action")
_STEP_FOR_STATE = {"none": 0, "multiple": 0, "rcm": 1, "dfu-error": 2,
                   "dfu-no-marker": 2, "dfu-profile-incomplete": 2, "dfu-ready": 3}
_WARNING_STATES = frozenset(("multiple", "dfu-error", "dfu-no-marker", "dfu-profile-incomplete"))
_PERCENT = re.compile(r"(\d{1,3}(?:\.\d+)?)%")


def load_flows():
    """Return a private copy of jibo_tui whose prompts can be rebound safely."""
    spec = importlib.util.find_spec("jibo_tui")
    get_source = getattr(getattr(spec, "loader", None), "get_source", None)
    source = get_source("jibo_tui") if get_source is not None else None
    if source is None:
        raise ImportError("jibo_tui.py is required by the graphical interface.")
    origin = str(getattr(spec, "origin", None) or "jibo_tui.py")
    module = types.ModuleType("_jibo_gui_flows")
    module.__file__ = origin
    # dataclasses look up their defining module while the class is created.
    sys.modules[module.__name__] = module
    exec(compile(source, origin, "exec"), module.__dict__)
    return module


class Prompt:
    """One question from a running flow, answered on the Tk thread."""

    def __init__(self, kind, title, detail="", prompt="", items=(), initial=None,
                 default="", password=False, cancel_label="Cancel", lines=()):
        self.kind = kind            # select, confirm, multi, text, or message
        self.title = str(title)
        self.detail = str(detail or "")
        self.prompt = str(prompt or "")
        self.items = tuple(items)
        self.initial = initial
        self.default = str(default or "")
        self.password = bool(password)
        self.cancel_label = str(cancel_label or "Cancel")
        self.lines = tuple(lines)
        self._answered = threading.Event()
        self._value = None

    def resolve(self, value):
        if not self._answered.is_set():
            self._value = value
            self._answered.set()

    @property
    def answered(self):
        return self._answered.is_set()

    def wait(self):
        self._answered.wait()
        # Hand the answer over once; a Wi-Fi password must not linger here.
        value, self._value = self._value, None
        return value

    def __repr__(self):
        # Never include an answer; it may be a Wi-Fi password.
        return "Prompt({!r}, {!r})".format(self.kind, self.title)


def install_prompts(flows, ask, run_work=None):
    """Route the private flow module's curses prompts to ``ask(Prompt)``.

    Answers are validated against the offered choices, so the view can only
    return what the text menu could have returned. ``confirm_action`` itself is
    left untouched: it builds the same review text and Cancel/Confirm choice,
    with Cancel preselected, through ``select_option``. ``run_work`` replaces
    the terminal progress screen for network work (Phoenix image list and
    downloads); it runs on the flow's worker thread, never on Tk's.
    """
    def select_option(title, options, prompt="", detail="", cancel_label="Cancel", initial=None):
        items = flows._normalise_options(options)
        kind = "confirm" if tuple(item.key for item in items) == CONFIRM_KEYS else "select"
        try:
            from jibo_calibration_flow import CONSENT_TITLE
        except ImportError:
            CONSENT_TITLE = None  # An older offline package still supports ordinary prompts.
        if title == CONSENT_TITLE and tuple(item.key for item in items) == ("share", "local"):
            kind = "calibration-consent"
        answer = ask(Prompt(kind, title, detail=detail, prompt=prompt, items=items,
                            initial=initial, cancel_label=cancel_label))
        enabled = {item.key for item in items if item.enabled}
        return answer if isinstance(answer, str) and answer in enabled else None

    def select_multiple_options(title, options, initial=()):
        items = flows._normalise_options(options)
        answer = ask(Prompt("multi", title, items=items, initial=tuple(initial)))
        if answer is None or isinstance(answer, str):
            return None
        chosen = set(answer)
        selected = tuple(item.key for item in items if item.key in chosen)
        # The text menu cannot continue with an empty selection either.
        return selected or None

    def text_input(title, prompt, default="", password=False, detail=""):
        answer = ask(Prompt("text", title, detail=detail, prompt=prompt,
                            default=default, password=password))
        return answer if isinstance(answer, str) else None

    def show_screen(title, lines, wait=True):
        ask(Prompt("message", title, lines=tuple(flows._screen_lines(lines))))
        return None

    def run_with_progress(title, work, cancel, detail=""):
        if run_work is not None:
            return run_work(title, work, cancel, detail)
        try:
            return work(lambda _progress: None, cancel), None
        except Exception as exc:
            return None, exc

    flows.select_option = select_option
    flows.select_multiple_options = select_multiple_options
    flows.text_input = text_input
    flows.show_screen = show_screen
    flows.run_with_progress = run_with_progress
    # Safety net: any curses prompt added later and not routed above returns
    # None (cancel) instead of taking over the launcher's terminal.
    flows._terminal_available = lambda: False
    return flows


class OutputCollector:
    """Turn backend stdout/stderr, including carriage-return progress, into lines."""

    def __init__(self, limit=400):
        self.lines = deque(maxlen=limit)
        self.current = ""

    def reset(self):
        self.lines.clear()
        self.current = ""

    def feed(self, text):
        for part in re.split(r"(\r\n|\r|\n)", str(text)):
            if part in ("\n", "\r\n"):
                line = self.current.rstrip()
                self.current = ""
                if line.strip():
                    self.lines.append(line)
            elif part == "\r":
                self.current = ""
            elif part:
                self.current += part

    @property
    def status(self):
        return self.current.strip()

    @property
    def percent(self):
        match = _PERCENT.search(self.status)
        if not match:
            return None
        return max(0.0, min(100.0, float(match.group(1))))

    @property
    def step(self):
        return self.lines[-1] if self.lines else ""


@dataclass(frozen=True)
class OutputState:
    step: str
    status: str
    percent: object
    lines: tuple


@dataclass(frozen=True)
class Outcome:
    kind: str          # result, error, or note
    action: str
    title: str
    lines: tuple
    status: str = ""   # the backend result's status, when it reported one
    mode: str = ""     # only the result's reported mode, never inferred from a prompt


@dataclass(frozen=True)
class Snapshot:
    readiness: object
    items: tuple
    busy: bool
    checked: bool
    active_label: str
    note: str
    step_label: str
    headline: str
    detail: str
    port: str
    step_index: int
    warning: bool


def thread_runner(target, kind):
    """Run actions on non-daemon threads so an exit waits for a transfer to end."""
    thread = threading.Thread(target=target, name="jibo-gui-" + kind, daemon=(kind != "action"))
    thread.start()
    return thread


def sync_runner(target, _kind):
    target()


class GuiController:
    """State, single-operation guard, and flow driver for one robot session."""

    def __init__(self, api, prompter=None, runner=thread_runner, flows=None,
                 preferences_path=None, browser_opener=None):
        self.api = api
        self._browser_opener = browser_opener
        # Embedders/tests may leave persistence off. The real GUI passes a
        # local path; this never stores a robot identity or network settings.
        self._preferences_path = Path(preferences_path) if preferences_path is not None else None
        self._phoenix_dismissed = False
        if self._preferences_path is not None:
            try:
                saved = json.loads(self._preferences_path.read_text(encoding="utf-8"))
                self._phoenix_dismissed = isinstance(saved, dict) and saved.get("phoenix_dismissed") is True
            except (OSError, ValueError):
                pass  # absent/corrupt preferences must not block an offline tool
        self.flows = install_prompts(flows or load_flows(), self._ask, self._run_work)
        self._prompter = prompter
        self._runner = runner
        self._lock = threading.RLock()
        self._usb_lock = threading.Lock()
        self._events = queue.Queue()
        self._output = OutputCollector()
        self._output_flagged = False
        self._busy = False
        self._probing = False
        self._closing = False
        self._pending = None
        self._work_cancel = None   # CancelToken of running network work, else None
        self._work_token = None
        self._generation = 0
        self._active_label = ""
        self._note = ""
        # The first USB probe runs in the background so the window can appear
        # immediately; until then no robot action is offered.
        self.menu = None
        self._checked = False
        self._probe_error = ""
        self._readiness = self.flows.Readiness("none", ())
        self._items = self.flows.build_menu_items(self._readiness)

    # -- state -------------------------------------------------------------
    @property
    def busy(self):
        with self._lock:
            return self._busy

    @property
    def capturing(self):
        return self.busy

    @property
    def pending_prompt(self):
        with self._lock:
            return self._pending

    @property
    def download_active(self):
        """True while a Phoenix list check or download runs; it never uses USB."""
        with self._lock:
            return self._work_cancel is not None

    def cancel_download(self):
        """Ask running network work to stop; a partial download is kept for resume."""
        with self._lock:
            token = self._work_cancel
            if token is not None:
                self._note = "Canceling the download..."
        if token is None:
            return False
        token.cancel()
        self._emit(("state",))
        return True

    def _run_work(self, title, work, cancel, detail=""):
        """Run network work on this action thread; marshal progress to the view.

        Progress callbacks arrive on the download thread. They only feed the
        output collector and queue an event; the Tk thread renders it. Callbacks
        that arrive after the work ended or the window closed are dropped.
        """
        token = object()
        with self._lock:
            if self._closing:
                cancel.cancel()
            self._work_cancel, self._work_token = cancel, token
        self.write_output("\n" + str(title) + "\n")
        self._emit(("download", True))

        def report(progress):
            with self._lock:
                live = self._work_token is token and not self._closing
            if live:
                self.write_output("\r" + self.flows.format_progress(progress))

        try:
            return work(report, cancel), None
        except Exception as exc:
            return None, exc
        finally:
            with self._lock:
                if self._work_token is token:
                    self._work_cancel = self._work_token = None
            self._emit(("download", False))

    def snapshot(self):
        with self._lock:
            readiness, items, busy = self._readiness, self._items, self._busy
            note, label, checked = self._note, self._active_label, self._checked
            probe_error = self._probe_error
        first, second = self.flows.status_lines(readiness)
        step_label, _, headline = first.partition("   ")
        if not checked:
            step_label, headline = "USB", "Checking the USB connection..."
            second = "Looking for a Jibo in RCM/APX or DFU mode."
            if probe_error:
                step_label, headline = "USB", "USB check failed"
                second = probe_error + " Refresh USB to retry; previous device state is not trusted."
        if busy or not checked:
            reason = (BUSY_REASON if busy else
                      "USB state could not be checked. Refresh USB to retry." if probe_error else
                      "Checking the USB connection...")
            items = tuple(self.flows.MenuItem(item.key, item.label, False, reason)
                          for item in items)
        return Snapshot(readiness=readiness, items=items, busy=busy, checked=checked,
                        active_label=label, note=note, step_label=step_label.strip(),
                        headline=(headline or first).strip(), detail=second,
                        port=readiness.port if checked else "", step_index=_STEP_FOR_STATE.get(readiness.state, 0),
                        warning=bool(probe_error) or readiness.state in _WARNING_STATES)

    def hint(self, item):
        """The same explanation line the text menu shows under the selection."""
        return item.reason if not item.enabled else self.flows._action_hint(item.key)

    def set_note(self, text):
        with self._lock:
            self._note = str(text or "")
        self._emit(("state",))

    def should_show_phoenix(self, outcome):
        """Offer an optional next step only after successful int-developer setup."""
        return bool(not self._phoenix_dismissed and outcome is not None and outcome.kind == "result" and
                    outcome.action == "set-mode-wifi" and
                    outcome.status in ("verified", "already current") and
                    outcome.mode == "int-developer")

    def dismiss_phoenix(self):
        """Suppress future automatic cards, even if local persistence fails."""
        self._phoenix_dismissed = True
        path = self._preferences_path
        if path is None:
            return
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                             dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"phoenix_dismissed": True}, stream)
                stream.write("\n")
            # Match the toolkit's invoking-user ownership under sudo.
            chown = getattr(self.api, "_chown_to_invoking_user", lambda _path: None)
            chown(path.parent)
            chown(temporary)
            os.replace(temporary, path)
        except OSError:
            self.set_note("Phoenix guidance hidden for this session; the local preference could not be saved.")
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass  # a read-only preference directory must not block the GUI

    def open_phoenix_guide(self):
        """Open only on an explicit click; never fetch or test site availability."""
        try:
            opener = self._browser_opener
            if opener is None:
                import webbrowser
                opener = webbrowser.open
            opened = bool(opener(PHOENIX_GUIDE_URL))
        except Exception:
            opened = False
        if opened:
            self.dismiss_phoenix()
        else:
            self.set_note("Could not open a browser. You can open " + PHOENIX_GUIDE_URL + " yourself.")
        return opened

    # -- events ------------------------------------------------------------
    def _emit(self, event):
        self._events.put(event)

    def drain(self):
        events = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    # -- output capture ----------------------------------------------------
    def write_output(self, text):
        with self._lock:
            self._output.feed(text)
            flagged, self._output_flagged = self._output_flagged, True
        if not flagged:
            self._emit(("output",))

    def output_state(self):
        with self._lock:
            self._output_flagged = False
            return OutputState(self._output.step, self._output.status,
                               self._output.percent, tuple(self._output.lines))

    # -- USB refresh -------------------------------------------------------
    def _refresh_menu(self):
        """Create or refresh the text menu's model; return it and whether it changed."""
        if self.menu is None:
            self.menu = self.flows.TerminalMenu(self.api)
            return self.menu, True
        return self.menu, self.menu.refresh()

    def request_refresh(self, manual=False):
        """Probe USB in the background unless an operation owns the robot."""
        with self._lock:
            if self._busy or self._probing or self._closing:
                return False
            self._probing = True
            generation = self._generation
            if manual:
                self._note = "Refreshing USB state..."
        if manual:
            self._emit(("state",))
        try:
            self._runner(lambda: self._probe(generation, manual), "probe")
        except Exception:
            with self._lock:
                self._probing = False
            raise
        return True

    def _probe(self, generation, manual):
        result = None
        error = None
        try:
            with self._usb_lock:
                with self._lock:
                    stale = self._busy or generation != self._generation
                if not stale:
                    first = self.menu is None
                    menu, changed = self._refresh_menu()
                    result = (changed and not first, menu.readiness, menu.items)
        except Exception as exc:
            error = exc
        with self._lock:
            self._probing = False
            if self._busy or generation != self._generation:
                return
            self._checked = error is None
            self._probe_error = str(error) if error is not None else ""
            if error is not None:
                self._note = "USB check failed: " + str(error)
            elif result is not None:
                changed, self._readiness, self._items = result
                if manual:
                    self._note = "USB state refreshed."
                elif changed:
                    self._note = "USB state changed. Available actions updated."
        self._emit(("state",))

    # -- actions -----------------------------------------------------------
    def start_action(self, key):
        """Start one action; refuse while another is running."""
        with self._lock:
            if self._busy or self._closing:
                return False
            item = next((entry for entry in self._items if entry.key == key), None)
            if not self._checked or item is None or not item.enabled:
                if not self._checked:
                    self._note = "Checking the USB connection..."
                elif item is None:
                    self._note = "The selected action is no longer available."
                else:
                    self._note = item.reason
                busy = False
            else:
                busy = True
                self._busy = True
                self._generation += 1
                self._active_label = item.label
                self._note = ""
                self._output.reset()
                self._output_flagged = False
        if not busy:
            self._emit(("state",))
            return False
        self._emit(("started", key))
        try:
            self._runner(lambda: self._run_action(key), "action")
        except Exception:
            with self._lock:
                self._busy = False
                self._active_label = ""
            self._emit(("state",))
            raise
        return True

    def _run_action(self, key):
        outcome = None
        try:
            with self._usb_lock:
                outcome = self._dispatch(key)
                try:
                    menu, _ = self._refresh_menu()
                except Exception:
                    menu = self.menu
                if menu is not None:
                    with self._lock:
                        self._readiness, self._items = menu.readiness, menu.items
        except Exception as exc:  # pragma: no cover - defensive
            outcome = Outcome("error", key, "Action failed", (str(exc),))
        finally:
            with self._lock:
                self._busy = False
                self._pending = None
                self._active_label = ""
                self._generation += 1
                if outcome is not None and outcome.kind == "note":
                    self._note = outcome.lines[0] if outcome.lines else ""
            self._emit(("finished", outcome))
            self._emit(("state",))

    def _dispatch(self, key):
        """Mirror jibo_tui.run(): recheck USB, run the flow, summarise the result."""
        # Recheck immediately before dispatch so an unplugged robot cannot
        # open an action enabled by an earlier USB snapshot.
        menu, _ = self._refresh_menu()
        item = next((entry for entry in menu.items if entry.key == key), None)
        if item is None:
            return Outcome("note", key, "", ("The selected action is no longer available.",))
        if not item.enabled:
            return Outcome("note", key, "", (item.reason,))
        readiness = menu.readiness
        try:
            result = self.flows.execute_action(self.api, key, readiness)
        except KeyboardInterrupt:
            return Outcome("result", key, item.label, ("Cancelled.",), "cancelled")
        except Exception as exc:
            try:
                current = tuple(self.api.devices())
            except Exception:
                current = None
            if current == () and readiness.devices:
                return Outcome("error", key, "Robot disconnected",
                               ("The robot is no longer visible over USB.", str(exc)))
            return Outcome("error", key, "Action failed", (str(exc),))
        if key == "enter-dfu-shofel" and isinstance(result, dict) and result.get("state") == "dfu":
            # The text menu returns straight to the refreshed main screen here.
            return Outcome("note", key, item.label,
                           ("The RAM loader started and the robot is in DFU.",), "dfu")
        status = str(result.get("status", "complete")) if isinstance(result, dict) else ""
        return Outcome("result", key, item.label,
                       tuple(str(line) for line in self.flows._result_summary(key, result)),
                       status, str(result.get("mode") or result.get("new_mode") or "")
                       if isinstance(result, dict) else "")

    # -- prompts -----------------------------------------------------------
    def _ask(self, prompt):
        if self._prompter is not None:
            return self._prompter(prompt)
        with self._lock:
            if self._closing:
                return None
            self._pending = prompt
        self._emit(("prompt", prompt))
        try:
            return prompt.wait()
        finally:
            with self._lock:
                if self._pending is prompt:
                    self._pending = None

    def shutdown_if_safe(self):
        """Atomically refuse window closure once a prompt has approved a transfer.

        Network work (image list or download) never touches USB, so closing
        cancels it instead of refusing; the partial download stays for resume.
        """
        with self._lock:
            pending = self._pending
            work = self._work_cancel
            if self._busy and work is None and (pending is None or pending.answered):
                return False
            self._closing = True
            if work is not None:
                work.cancel()
            if pending is not None:
                pending.resolve(None)
            return True

    def shutdown(self):
        """Cancel any open prompt or download; later prompts cancel immediately."""
        with self._lock:
            self._closing = True
            pending = self._pending
            work = self._work_cancel
        if work is not None:
            work.cancel()
        if pending is not None:
            pending.resolve(None)


class OutputRouter:
    """stdout/stderr stand-in: operation output goes to the GUI, the rest passes through.

    It reports a terminal while an operation runs so the backend emits its
    live carriage-return progress, which the GUI turns into a progress bar.
    """

    def __init__(self, original, controller):
        self._original = original
        self._controller = controller

    def write(self, text):
        if self._controller.capturing:
            self._controller.write_output(text)
            return len(text)
        if self._original is None:
            return len(text)
        return self._original.write(text)

    def flush(self):
        if self._original is not None:
            try:
                self._original.flush()
            except (OSError, ValueError):
                pass

    def isatty(self):
        if self._controller.capturing:
            return True
        return bool(self._original is not None and self._original.isatty())

    def __getattr__(self, name):
        return getattr(self._original, name)


@contextmanager
def capture_output(controller):
    original = sys.stdout, sys.stderr
    sys.stdout = OutputRouter(original[0], controller)
    sys.stderr = OutputRouter(original[1], controller)
    try:
        yield
    finally:
        sys.stdout, sys.stderr = original


TEXT_MENU_HINT = ("The text menu has the same actions: run ./run.sh (or .\\run.ps1 on "
                  "Windows) without --gui.")


def unavailable_message(problem, detail="", platform=None, environ=None, version=None):
    """Explain why the window cannot open and how to fix it, pointing to the text menu."""
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    version = sys.version_info if version is None else version
    lines = ["Jibo DFU Toolkit: the graphical window could not open" +
             (": " + str(detail).strip() if str(detail).strip() else ".")]
    if problem == "tkinter":
        if platform == "darwin":
            lines.append("Install Tk support for this Python: brew install python-tk@{0}.{1} "
                         "(Homebrew) or sudo port install py{0}{1}-tkinter (MacPorts)."
                         .format(version[0], version[1]))
        elif platform.startswith("win"):
            lines.append("Reinstall Python from python.org with the 'tcl/tk and IDLE' option.")
        else:
            lines.append("Install Tk support for Python: sudo apt install python3-tk "
                         "(Ubuntu, Debian, WSL), sudo dnf install python3-tkinter (Fedora), "
                         "or sudo pacman -S tk (Arch). ./run.sh --gui installs it when it can.")
    else:
        if environ.get("WSL_DISTRO_NAME") or environ.get("WSL_INTEROP"):
            lines.append("Windows shows Linux windows through WSLg (Windows 11, or Windows 10 "
                         "with WSL from the Microsoft Store). Run 'wsl --update' and then "
                         "'wsl --shutdown' in PowerShell, and start the toolkit again.")
        elif platform == "darwin":
            lines.append("Start the toolkit from a logged-in macOS desktop session.")
        else:
            lines.append("No graphical display was found. Start the toolkit from a desktop "
                         "session (DISPLAY must be set; sudo must keep DISPLAY and XAUTHORITY).")
    lines.append(TEXT_MENU_HINT)
    return "\n".join(lines)


def _terminal_available():
    return (getattr(sys.stdin, "isatty", lambda: False)() and
            getattr(sys.stdout, "isatty", lambda: False)())


def _is_tk_import_error(error):
    name = str(getattr(error, "name", "") or "")
    return name in ("tkinter", "_tkinter") or "tkinter" in str(error) or "libtk" in str(error)


def launch(api, fallback=None, stream=None):
    """Open the window; if Tk or a display is missing, explain and fall back.

    ``fallback`` (the text menu) runs only when a terminal is attached, so a
    missing GUI never leaves the user without the toolkit's actions.
    """
    stream = stream or sys.stderr
    try:
        import jibo_gui
    except ImportError as exc:
        if not _is_tk_import_error(exc):
            raise
        message = unavailable_message("tkinter", "this Python has no working tkinter module")
        return _fall_back(message, fallback, stream)
    try:
        return jibo_gui.main(api)
    except jibo_gui.DisplayUnavailable as exc:
        return _fall_back(unavailable_message("display", exc), fallback, stream)


def _fall_back(message, fallback, stream):
    print(message, file=stream, flush=True)
    if fallback is not None and _terminal_available():
        print("Opening the text menu instead.", file=stream, flush=True)
        return fallback()
    return 2
