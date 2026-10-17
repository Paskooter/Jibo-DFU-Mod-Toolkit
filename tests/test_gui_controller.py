"""Headless safety tests: real TUI flows, fake device boundary, no Tk or USB."""
import ast
from dataclasses import asdict
import io
import json
from pathlib import Path
import threading
from unittest.mock import patch

import pytest
import jibo_gui_core as core
import jibo_tui
from gui_fakes import FakeApi, FileLayoutError


def controller(api=None, answers=(), runner=core.sync_runner):
    prompts = []
    remaining = iter(answers)
    def ask(prompt):
        prompts.append(prompt)
        return next(remaining, None)
    c = core.GuiController(api or FakeApi(), prompter=ask, runner=runner)
    c.request_refresh()
    return c, prompts


def start(c, key):
    """Drive moved actions through their real submenu; never add hidden actions."""
    menu = ("more-tools" if key in ("set-mode", "configure-wifi") else
            "backups" if key == "backup-var" else None)
    if menu is None:
        return c.start_action(key)
    original = c._prompter
    first = [True]
    def ask(prompt):
        if first[0]:
            first[0] = False
            return key
        return original(prompt) if original else None
    c._prompter = ask
    return c.start_action(menu)


def outcome(c):
    return [event[1] for event in c.drain() if event[0] == "finished"][-1]


@pytest.mark.parametrize("state", ["none", "multiple", "rcm", "dfu"])
def test_readiness_and_actions_match_text_menu(state):
    api = FakeApi(state)
    c, _ = controller(api)
    expected = jibo_tui.TerminalMenu(api)
    assert asdict(c.snapshot().readiness) == asdict(expected.readiness)
    assert tuple(asdict(i) for i in c.snapshot().items) == tuple(asdict(i) for i in expected.items)
    assert c.snapshot().checked


def test_private_flows_do_not_change_tui_prompt_functions():
    original = tuple(getattr(jibo_tui, name) for name in core.PROMPT_PRIMITIVES)
    c, _ = controller()
    assert c.flows is not jibo_tui
    assert tuple(getattr(jibo_tui, name) for name in core.PROMPT_PRIMITIVES) == original
    tree = ast.parse(Path(jibo_tui.__file__).read_text())
    curses_prompts = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)
                      and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                              and isinstance(call.func.value, ast.Name) and call.func.value.id == "curses"
                              and call.func.attr == "wrapper" for call in ast.walk(node))}
    assert curses_prompts == set(core.PROMPT_PRIMITIVES) | {"run"}


@pytest.mark.parametrize("quick", [True, False])
@pytest.mark.parametrize("approval", [None, "cancel", "CONFIRM", True, "confirm"])
def test_mode_write_requires_explicit_confirm(quick, approval):
    api = FakeApi(quick=quick)
    c, prompts = controller(api, ("developer", approval))
    assert start(c, "set-mode")
    result = outcome(c)
    assert api.writes == (["set_mode_file_live" if quick else "set_mode_live"] if approval == "confirm" else [])
    assert result.status == ("verified" if approval == "confirm" else "cancelled")
    review = prompts[-1]
    assert review.kind == "confirm" and review.initial == "cancel"
    assert review.detail == jibo_tui._confirmation_details(
        {"usb_port": "1-1", "changes": [{"partition": "var", "path": "/jibo/mode.json",
          "description": "Fake file change", "before_size_bytes": 17, "candidate_size_bytes": 26}]}
        if quick else api.plan,
        "Review the file change before changing the robot mode." if quick else
        "Review the var write plan before changing the robot mode.")


@pytest.mark.parametrize("mode", ["developer", "int-developer"])
@pytest.mark.parametrize("boot", ["install", "keep", "remove"])
def test_combined_developer_boot_status_defaults_to_install(mode, boot):
    api = FakeApi()
    c, prompts = controller(api, (mode, "Test network", "open", boot, "confirm"))
    c.start_action("set-mode-wifi")
    assert outcome(c).status == "verified"
    prompt = next(p for p in prompts if p.title == "Boot screen network status")
    assert prompt.initial == "install"
    assert "500 MiB" in prompt.detail and "SHA-256 readback" in prompt.detail
    name, args, kwargs = api.calls[-1]
    assert args == (mode, "Test network", None, True)
    assert kwargs["port"] == "1-1"
    assert name == ("set_mode_wifi_file_live" if boot == "keep" else "set_mode_wifi_live")
    assert kwargs.get("network_status") == (None if boot == "keep" else boot)


@pytest.mark.parametrize("mode", ["normal", "oobe"])
def test_non_developer_has_no_boot_status_prompt(mode):
    c, prompts = controller(answers=(mode, "Test network", "open", "confirm"))
    c.start_action("set-mode-wifi")
    assert outcome(c).status == "verified"
    assert not any(p.title == "Boot screen network status" for p in prompts)


@pytest.mark.parametrize("mode", ["developer", "int-developer", "normal", "oobe"])
@pytest.mark.parametrize("boot", ["install", "keep", "remove"])
def test_phoenix_card_only_follows_successful_combined_int_developer_setup(mode, boot):
    answers = [mode, "Test network", "open"]
    if mode in ("developer", "int-developer"):
        answers.append(boot)
    c, _ = controller(answers=(*answers, "confirm"))
    c.start_action("set-mode-wifi")
    result = outcome(c)
    assert result.status == "verified" and result.mode == mode
    assert c.should_show_phoenix(result) == (mode == "int-developer")


@pytest.mark.parametrize("boot", ["install", "keep", "remove"])
def test_phoenix_card_never_follows_successful_developer_mode_setup(boot):
    # The jibo.io command needs SSH, which the developer-mode firewall rejects.
    api = FakeApi()
    c, _ = controller(api, ("developer", "Test network", "open", boot, "confirm"))
    c.start_action("set-mode-wifi")
    result = outcome(c)
    assert api.writes and result.kind == "result" and result.action == "set-mode-wifi"
    assert result.status == "verified" and result.mode == "developer"
    assert not c.should_show_phoenix(result)


@pytest.mark.parametrize("status", ["verified", "already current"])
@pytest.mark.parametrize("mode,shown", [
    ("int-developer", True),
    ("developer", False),
    ("normal", False),
    ("oobe", False),
    ("", False),
    ("INT-DEVELOPER", False),
    ("int-developer ", False),
])
def test_phoenix_card_requires_exact_int_developer_mode(status, mode, shown):
    c = core.GuiController(FakeApi())
    assert c.should_show_phoenix(core.Outcome(
        "result", "set-mode-wifi", "Setup", (), status, mode)) is shown


def test_phoenix_dismissal_is_remembered_locally_without_touching_robot(tmp_path):
    prefs = tmp_path / "preferences.json"
    result = core.Outcome("result", "set-mode-wifi", "Setup", (), "verified", "int-developer")
    c = core.GuiController(FakeApi(), preferences_path=prefs)
    assert c.should_show_phoenix(result)
    assert not prefs.exists()  # no first-launch write
    c.dismiss_phoenix()
    assert not c.should_show_phoenix(result)
    assert json.loads(prefs.read_text()) == {"phoenix_dismissed": True}
    fresh = core.GuiController(FakeApi(), preferences_path=prefs)
    assert not fresh.should_show_phoenix(result)
    assert not c.api.calls and not fresh.api.calls


def test_phoenix_browser_opener_is_explicit_injectable_and_remembers_engagement(tmp_path):
    opened = []
    prefs = tmp_path / "preferences.json"
    def open_browser(url):
        opened.append(url)
        return True
    c = core.GuiController(FakeApi(), preferences_path=prefs, browser_opener=open_browser)
    result = core.Outcome("result", "set-mode-wifi", "Setup", (), "verified", "int-developer")
    assert c.should_show_phoenix(result) and not opened
    assert c.open_phoenix_guide()
    assert opened == ["https://jibo.io/guide"]
    assert not c.should_show_phoenix(result)
    assert c.open_phoenix_guide()  # explicit entry still works after dismissal
    assert opened == ["https://jibo.io/guide"] * 2
    assert not c.api.calls


@pytest.mark.parametrize("status", ["failed", "readback mismatch", "cancelled", "complete", ""])
def test_phoenix_card_rejects_unverified_results(status):
    c, _ = controller()
    assert not c.should_show_phoenix(core.Outcome(
        "result", "set-mode-wifi", "Setup", (), status, "int-developer"))


def test_phoenix_card_uses_full_var_new_mode_result():
    api = FakeApi()
    original = api.set_mode_wifi_live
    def full_var(*args, **kwargs):
        result = original(*args, **kwargs)
        result["new_mode"] = result.pop("mode")
        return result
    api.set_mode_wifi_live = full_var
    c, _ = controller(api, ("int-developer", "Test network", "open", "install", "confirm"))
    c.start_action("set-mode-wifi")
    assert c.should_show_phoenix(outcome(c))


def test_phoenix_card_never_follows_failed_combined_write():
    api = FakeApi()
    api.fail = RuntimeError("Readback mismatch")
    c, _ = controller(api, ("int-developer", "Test network", "open", "install", "confirm"))
    c.start_action("set-mode-wifi")
    result = outcome(c)
    assert result.kind == "error" and not c.should_show_phoenix(result)


@pytest.mark.parametrize("action,answers", [
    ("set-mode", ("int-developer", "confirm")),
    ("configure-wifi", ("Test network", "open", "confirm")),
])
def test_phoenix_card_does_not_follow_separate_edits(action, answers):
    c, _ = controller(answers=answers)
    start(c, action)
    assert not c.should_show_phoenix(outcome(c))


@pytest.mark.parametrize("saved", ["bad json", "null", "[]", '{"phoenix_dismissed": "true"}'])
def test_bad_phoenix_preferences_do_not_break_offline_controller(tmp_path, saved):
    prefs = tmp_path / "prefs.json"
    prefs.write_text(saved)
    c = core.GuiController(FakeApi(), preferences_path=prefs)
    assert c.should_show_phoenix(core.Outcome(
        "result", "set-mode-wifi", "Setup", (), "verified", "int-developer"))


def test_phoenix_preference_write_failure_still_dismisses_for_session(tmp_path):
    parent = tmp_path / "not-a-directory"
    parent.write_text("existing file")
    c = core.GuiController(FakeApi(), preferences_path=parent / "prefs.json")
    c.dismiss_phoenix()
    assert not c.should_show_phoenix(core.Outcome(
        "result", "set-mode-wifi", "Setup", (), "verified", "int-developer"))
    assert "could not be saved" in c.snapshot().note


def test_phoenix_failed_atomic_write_and_cleanup_never_block_dismissal(tmp_path, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise OSError("Read-only preferences directory")
    c = core.GuiController(FakeApi(), preferences_path=tmp_path / "prefs.json")
    monkeypatch.setattr(core.os, "replace", unavailable)
    monkeypatch.setattr(Path, "unlink", unavailable)
    c.dismiss_phoenix()
    assert not c.should_show_phoenix(core.Outcome(
        "result", "set-mode-wifi", "Setup", (), "verified", "int-developer"))
    assert "could not be saved" in c.snapshot().note


def test_phoenix_default_opener_does_nothing_until_explicit_open():
    with patch("webbrowser.open", return_value=True) as opener:
        c, _ = controller(answers=("int-developer", "Test network", "open", "install", "confirm"))
        c.start_action("set-mode-wifi")
        assert c.should_show_phoenix(outcome(c))
        assert not opener.called
        assert c.open_phoenix_guide()
        opener.assert_called_once_with("https://jibo.io/guide")


@pytest.mark.parametrize("raises", [False, True])
def test_phoenix_browser_failure_is_nonblocking_and_keeps_guidance(raises):
    def opener(_url):
        if raises:
            raise OSError("No desktop browser")
        return False
    c = core.GuiController(FakeApi(), browser_opener=opener)
    assert not c.open_phoenix_guide()
    assert "https://jibo.io/guide" in c.snapshot().note
    assert c.should_show_phoenix(core.Outcome(
        "result", "set-mode-wifi", "Setup", (), "verified", "int-developer"))
    assert not c.api.calls


@pytest.mark.parametrize("action,answers", [
    ("configure-wifi", ("Test network", "protected", "testpass1", "different", "cancel")),
    ("set-mode-wifi", ("developer", "Test network", "protected", "testpass1", "different", "cancel")),
])
def test_mismatched_password_cancel_never_calls_device_edit(action, answers):
    api = FakeApi()
    c, prompts = controller(api, answers)
    start(c, action)
    assert outcome(c).status == "cancelled"
    assert not api.calls and not api.writes
    assert len([p for p in prompts if p.password]) == 2
    assert "testpass1" not in repr(prompts)


@pytest.mark.parametrize("mode", ["developer", "int-developer"])
@pytest.mark.parametrize("stop", range(7))
def test_combined_flow_cancellation_at_every_prompt_never_writes(stop, mode):
    api = FakeApi()
    answers = [mode, "Test network", "protected", "testpass1", "testpass1", "install", "confirm"]
    answers[stop] = None
    c, prompts = controller(api, answers[:stop + 1])
    c.start_action("set-mode-wifi")
    result = outcome(c)
    assert result.status == "cancelled" and not api.writes
    assert not c.should_show_phoenix(result)
    assert len(prompts) == stop + 1


def test_protected_wifi_requires_two_matching_entries_and_review():
    api = FakeApi()
    c, prompts = controller(api, ("Test network", "protected", "testpass1", "testpass1", "confirm"))
    start(c, "configure-wifi")
    assert outcome(c).status == "verified"
    assert len([p for p in prompts if p.password]) == 2
    assert "testpass1" not in repr(prompts) + repr(api.calls) + repr(c.output_state())


@pytest.mark.parametrize("status", [3, 4])
def test_full_var_fallback_is_an_explicit_choice_then_separate_confirmation(status):
    api = FakeApi()
    def quick(*args, **kwargs):
        raise FileLayoutError(status)
    api.set_mode_file_live = quick
    c, prompts = controller(api, ("developer", "cancel"))
    start(c, "set-mode")
    assert outcome(c).status == "cancelled" and not api.writes and not api.calls
    assert prompts[-1].initial == "cancel"
    assert "500 MiB" in prompts[-1].detail
    c, prompts = controller(api, ("developer", "full", "confirm"))
    start(c, "set-mode")
    assert outcome(c).status == "verified"
    assert api.writes == ["set_mode_live"]
    assert prompts[-1].kind == "confirm" and prompts[-1].initial == "cancel"


@pytest.mark.parametrize("policy", ["keep-calibration", "without-calibration"])
@pytest.mark.parametrize("approve", ["cancel", "confirm"])
def test_flash_policies_keep_original_backend_confirmation(policy, approve):
    api = FakeApi()
    c, prompts = controller(api, ("0", policy, *(("confirm",) if policy == "without-calibration" else ()), approve))
    c.start_action("flash-update")
    assert outcome(c).status == ("verified" if approve == "confirm" else "cancelled")
    assert api.writes == (["flash_update"] if approve == "confirm" else [])
    assert api.calls[-1][2]["var_policy"] == policy
    assert api.calls[-1][2]["unsafe_without_calibration"] == (policy == "without-calibration")
    assert "var only" in prompts[-1].detail
    assert "rootfsA" in prompts[-1].detail and "skills" in prompts[-1].detail


@pytest.mark.parametrize("approve", ["cancel", "confirm"])
def test_remove_boot_status_uses_reviewed_full_var_operation(approve):
    api = FakeApi()
    c, prompts = controller(api, ("remove-boot-network-status", approve))
    c.start_action("more-tools")
    assert outcome(c).status == ("verified" if approve == "confirm" else "cancelled")
    assert api.writes == (["boot_network_status_live"] if approve == "confirm" else [])
    assert api.calls[-1][1] == ("remove",)
    assert prompts[-1].title == "Confirm boot-screen status removal"
    assert "Remove only the diagnostic var files and loopback hook." in prompts[-1].detail


@pytest.mark.parametrize("approve", ["cancel", "confirm"])
def test_restore_var_selection_and_confirmation(tmp_path, approve):
    source = tmp_path / "backup-set.json"
    source.write_text(json.dumps({"partitions": [{"name": "var", "size_bytes": 524288000},
                                               {"name": "services", "size_bytes": 524288000}]}))
    api = FakeApi()
    api.saved_backups = lambda: []
    api.describe_backup_set = lambda p: dict(
        manifest=str(p), state="complete", restorable=True,
        partitions=[dict(name="var", size_bytes=524288000, present=True),
                    dict(name="services", size_bytes=524288000, present=True)])
    c, prompts = controller(api, ("restore-backup", "other-folder", str(source), ("var",), approve))
    c.start_action("backups")
    assert outcome(c).status == ("verified" if approve == "confirm" else "cancelled")
    assert api.calls[-1][1] == (source, ("var",))
    assert api.writes == (["restore_partitions"] if approve == "confirm" else [])
    assert prompts[-1].initial == "cancel"
    assert "var (524288000 bytes)" in prompts[-1].detail
    assert "services" not in prompts[-1].detail


def test_backup_and_more_tools_use_same_device_or_image_functions():
    api = FakeApi()
    c, _ = controller(api)
    start(c, "backup-var")
    assert outcome(c).lines[0] == "Var backup is ready."
    assert api.calls[-1] == ("backup_var", (), {"port": "1-1"})
    for answers, name in [(("probe-dfu-gpt",), "probe_dfu_gpt"),
                           (("inspect-backup", "/tmp/fake.img"), "inspect_var"),
                           (("edit-backup", "mode", "/tmp/fake.img", "developer"), "edit_mode")]:
        c, _ = controller(api, answers)
        c.start_action("more-tools")
        assert outcome(c).kind == "result"
        assert api.calls[-1][0] == name


@pytest.mark.parametrize("approve", [None, "confirm"])
def test_write_var_passes_path_and_confirmation(approve):
    api = FakeApi()
    c, prompts = controller(api, ("write-var", "/tmp/edited.img", approve))
    c.start_action("more-tools")
    assert outcome(c).status == ("verified" if approve == "confirm" else "cancelled")
    assert api.calls[-1][1] == ("/tmp/edited.img",)
    assert prompts[-1].title == "Confirm var write"


def test_rcm_entry_requires_review_and_refreshes_to_dfu():
    api = FakeApi("rcm")
    c, prompts = controller(api, ("confirm",))
    c.start_action("enter-dfu-shofel")
    assert outcome(c).kind == "note"
    assert c.snapshot().readiness.state == "dfu-ready"
    assert "Slot 3 has not been tested" in prompts[0].detail
    assert "No partition write" in prompts[0].detail


def test_disconnected_between_home_and_dispatch_never_starts_action():
    api = FakeApi()
    c, _ = controller(api)
    api.state = "none"
    c.start_action("set-mode-wifi")
    assert outcome(c).kind == "note"
    assert not api.calls and not api.writes


def test_duplicate_start_and_refresh_refused_until_action_finishes():
    jobs = []
    c, _ = controller(runner=lambda target, kind: jobs.append((target, kind)))
    jobs.pop(0)[0]()
    assert start(c, "backup-var")
    assert not start(c, "backup-var")
    assert not c.request_refresh()
    assert c.busy and all(not i.enabled for i in c.snapshot().items)
    jobs.pop(0)[0]()
    assert not c.busy and len(c.api.calls) == 1


def test_usb_probe_and_action_are_serialized_and_stale_probe_cannot_enable_actions():
    api = FakeApi()
    c, _ = controller(api)
    entered, release = threading.Event(), threading.Event()
    original = api.devices
    def blocking_probe():
        entered.set()
        assert release.wait(3)
        return original()
    api.devices = blocking_probe
    c._runner = core.thread_runner
    assert c.request_refresh()
    assert entered.wait(2)
    assert start(c, "backup-var")
    assert c.busy and not c.request_refresh()
    assert not api.calls  # action is waiting for the USB probe lock
    release.set()
    # Poll the completion queue rather than relying on a blind sleep.
    deadline = __import__("time").monotonic() + 3
    while __import__("time").monotonic() < deadline:
        event = c._events.get(timeout=3)
        if event[0] == "finished":
            assert event[1].kind == "result"
            break
    else:
        pytest.fail("action did not finish")
    assert not c.busy and len(api.calls) == 1


def test_safe_shutdown_refuses_already_approved_prompt():
    c, _ = controller()
    c._busy = True
    c._pending = core.Prompt("confirm", "Confirm write")
    c._pending.resolve("confirm")
    assert not c.shutdown_if_safe() and not c._closing
    c._pending = core.Prompt("text", "Wi-Fi")
    assert c.shutdown_if_safe() and c._pending.answered and c._closing


def test_shutdown_releases_pending_prompt_without_writing():
    c = core.GuiController(FakeApi())
    # Do the initial probe synchronously, then use a real action thread.
    c._runner = core.sync_runner
    c.request_refresh()
    jobs = []
    c._runner = lambda target, kind: jobs.append(target)
    assert c.start_action("set-mode-wifi")
    worker = threading.Thread(target=jobs.pop())
    worker.start()
    event = c._events.get(timeout=2)
    while event[0] != "prompt":
        event = c._events.get(timeout=2)
    c.shutdown()
    worker.join(timeout=2)
    assert not worker.is_alive() and not c.api.writes
    assert not start(c, "backup-var")


def test_output_router_captures_carriage_progress_and_restores_streams():
    c, _ = controller()
    c._busy = True
    original = core.sys.stdout, core.sys.stderr
    with core.capture_output(c):
        assert core.sys.stderr.isatty()
        core.sys.stderr.write("Reading var\n\r[##########----------] 50.0% | 250/500 MiB")
    assert (core.sys.stdout, core.sys.stderr) == original
    state = c.output_state()
    assert state.percent == 50 and state.step == "Reading var"
    assert "250/500 MiB" in state.status


def test_unavailable_gui_explains_tk_and_wslg_and_text_menu():
    assert "python3-tk" in core.unavailable_message("tkinter", platform="linux")
    assert "python-tk@3.12" in core.unavailable_message("tkinter", platform="darwin", version=(3, 12))
    text = core.unavailable_message("display", platform="linux", environ={"WSL_DISTRO_NAME": "Ubuntu"})
    assert "WSLg" in text and "wsl --update" in text and "without --gui" in text


def test_launch_missing_tk_is_actionable_without_calling_device_api():
    import builtins
    original_import = builtins.__import__
    def missing_tk(name, *args, **kwargs):
        if name == "jibo_gui":
            raise ModuleNotFoundError("No module named _tkinter", name="_tkinter")
        return original_import(name, *args, **kwargs)
    api = FakeApi()
    stream = io.StringIO()
    with patch("builtins.__import__", side_effect=missing_tk), \
            patch.object(core, "_terminal_available", return_value=False):
        assert core.launch(api, stream=stream) == 2
    assert "python3-tk" in stream.getvalue() and "text menu" in stream.getvalue()
    assert not api.calls


def test_unrelated_import_error_is_not_hidden_as_missing_tk():
    import builtins
    original_import = builtins.__import__
    def broken_code(name, *args, **kwargs):
        if name == "jibo_gui":
            raise ImportError("Unrelated programming error", name="missing_project_module")
        return original_import(name, *args, **kwargs)
    with patch("builtins.__import__", side_effect=broken_code):
        with pytest.raises(ImportError, match="programming error"):
            core.launch(FakeApi())


def test_missing_tk_fallback_only_runs_with_terminal():
    stream = io.StringIO()
    with patch.object(core, "_terminal_available", return_value=False):
        assert core._fall_back("Tk unavailable", lambda: pytest.fail("not a terminal"), stream) == 2
    with patch.object(core, "_terminal_available", return_value=True):
        assert core._fall_back("Tk unavailable", lambda: 17, stream) == 17


def test_failed_usb_refresh_disables_previously_ready_actions():
    api = FakeApi()
    c, _ = controller(api)
    api.devices = lambda: (_ for _ in ()).throw(RuntimeError("Fake USB enumeration failed"))
    c.request_refresh()
    snap = c.snapshot()
    assert not snap.checked
    assert snap.headline == "USB check failed"
    assert "Fake USB enumeration failed" in snap.detail
    assert not any(item.enabled for item in snap.items)
    assert not start(c, "set-mode") and not api.calls
    api.devices = lambda: [{"port": "1-1", "state": "dfu"}]
    c.request_refresh()
    assert c.snapshot().checked and any(i.enabled for i in c.snapshot().items)


def test_failed_device_action_is_not_reported_as_success():
    api = FakeApi()
    api.fail = RuntimeError("Fake readback mismatch: leave robot in DFU")
    c, _ = controller(api, ("developer", "confirm"))
    start(c, "set-mode")
    result = outcome(c)
    assert result.kind == "error" and "readback mismatch" in result.lines[0]
    assert not c.busy
