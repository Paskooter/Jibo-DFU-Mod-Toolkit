import io
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import curses

import jibo_tui


class FakeScreen:
    def __init__(self, keys=(), size=(30, 100)):
        self.keys = list(keys)
        self.size = size
        self.lines = []

    def erase(self):
        self.lines.clear()

    def getmaxyx(self):
        return self.size

    def addnstr(self, y, x, text, length, attr=0):
        self.lines.append((y, x, text[:length], attr))

    def refresh(self):
        pass

    def keypad(self, enabled):
        pass

    def move(self, _y, _x):
        pass

    def getch(self):
        return self.keys.pop(0) if self.keys else ord("q")


def fake_api(devices=(), marker=True, alts=None, shofel_dfu=False):
    if alts is None:
        alts = ["var", "jibo-dfu-v1"] if marker else ["var"]
    return SimpleNamespace(
        MARKER="jibo-dfu-v1",
        devices=lambda: list(devices),
        tool=lambda _name: "dfu-util",
        dfu_alternatives=lambda _tool, _port: (alts, ""),
        shofel_dfu_available=lambda: shofel_dfu,
        enter_shofel_dfu=lambda **kwargs: {"action": "enter-dfu-shofel", **kwargs},
        probe_dfu_gpt=lambda **kwargs: {"status": "partition table read and checked", **kwargs},
        _update_candidates=lambda _folder: [],
    )


class TuiReadinessTests(unittest.TestCase):
    def test_rcm_and_dfu_are_distinct_states(self):
        rcm = jibo_tui.inspect_readiness(fake_api([{"port": "1-1", "state": "rcm"}]))
        dfu = jibo_tui.inspect_readiness(fake_api([{"port": "1-1", "state": "dfu"}]))

        self.assertEqual(rcm.state, "rcm")
        self.assertEqual(dfu.state, "dfu-ready")
        self.assertIn("RCM/APX", jibo_tui.status_lines(rcm)[0])
        self.assertIn("DFU active", jibo_tui.status_lines(dfu)[0])

    def test_shofel_entry_is_primary_and_requires_launch_capable_tool_in_rcm(self):
        available = jibo_tui.inspect_readiness(fake_api(
            [{"port": "1-2", "state": "rcm"}], shofel_dfu=True))
        items = jibo_tui.build_menu_items(available)
        self.assertEqual(items[0].key, "enter-dfu-shofel")
        self.assertEqual(items[0].label, "Enter DFU with ShofEL (RAM loader)")
        self.assertTrue(items[0].enabled)
        self.assertFalse(hasattr(available, "shofel_available"))
        self.assertFalse({"enter-dfu-signed", "backup-var-shofel", "verify-bundle"} &
                         {item.key for item in items})
        online_rcm_actions = {"enter-dfu-shofel", "probe-dfu-gpt", "backup-var",
                              "set-mode", "configure-wifi", "flash-update", "write-var"}
        self.assertEqual({item.key for item in items if item.key in online_rcm_actions and item.enabled},
                         {"enter-dfu-shofel"})

        unavailable = jibo_tui.inspect_readiness(fake_api(
            [{"port": "1-2", "state": "rcm"}], shofel_dfu=False))
        item = jibo_tui.build_menu_items(unavailable)[0]
        self.assertFalse(item.enabled)
        self.assertIn("launch-enabled ShofEL", item.reason)

        already_dfu = jibo_tui.inspect_readiness(fake_api(
            [{"port": "1-2", "state": "dfu"}], shofel_dfu=True))
        item = {entry.key: entry for entry in
                jibo_tui.build_menu_items(already_dfu)}["enter-dfu-shofel"]
        self.assertFalse(item.enabled)
        self.assertIn("already in DFU", item.reason)

    def test_live_actions_require_dfu_and_loader_marker(self):
        rcm = jibo_tui.inspect_readiness(fake_api([{"port": "1-1", "state": "rcm"}]))
        items = {item.key: item for item in jibo_tui.build_menu_items(rcm)}
        self.assertFalse(items["enter-dfu-shofel"].enabled)
        self.assertNotIn("enter-dfu-signed", items)
        self.assertNotIn("backup-var-shofel", items)
        self.assertNotIn("verify-bundle", items)
        self.assertFalse(items["probe-dfu-gpt"].enabled)
        self.assertFalse(items["backup-var"].enabled)
        self.assertFalse(items["set-mode"].enabled)
        self.assertTrue(items["inspect-backup"].enabled)
        self.assertIn("Enter DFU", items["backup-var"].reason)

        no_marker = jibo_tui.inspect_readiness(
            fake_api([{"port": "1-1", "state": "dfu"}], marker=False))
        items = {item.key: item for item in jibo_tui.build_menu_items(no_marker)}
        self.assertFalse(items["enter-dfu-shofel"].enabled)
        self.assertFalse(items["probe-dfu-gpt"].enabled)
        self.assertFalse(items["backup-var"].enabled)

        no_var = jibo_tui.inspect_readiness(fake_api(
            [{"port": "1-1", "state": "dfu"}], alts=["jibo-dfu-v1", "rootfsA"]))
        items = {item.key: item for item in jibo_tui.build_menu_items(no_var)}
        self.assertEqual(no_var.state, "dfu-profile-incomplete")
        self.assertFalse(items["probe-dfu-gpt"].enabled)
        self.assertFalse(items["backup-var"].enabled)
        self.assertIn("var", items["backup-var"].reason)

    def test_dfu_gpt_probe_is_enabled_only_with_ready_loader(self):
        ready = jibo_tui.inspect_readiness(fake_api(
            [{"port": "1-3", "state": "dfu"}]))
        item = {entry.key: entry for entry in
                jibo_tui.build_menu_items(ready)}["probe-dfu-gpt"]
        self.assertTrue(item.enabled)

        rcm = jibo_tui.inspect_readiness(fake_api(
            [{"port": "1-3", "state": "rcm"}]))
        item = {entry.key: entry for entry in
                jibo_tui.build_menu_items(rcm)}["probe-dfu-gpt"]
        self.assertFalse(item.enabled)
        self.assertIn("Enter DFU", item.reason)

        no_marker = jibo_tui.inspect_readiness(fake_api(
            [{"port": "1-3", "state": "dfu"}], marker=False))
        item = {entry.key: entry for entry in
                jibo_tui.build_menu_items(no_marker)}["probe-dfu-gpt"]
        self.assertFalse(item.enabled)

    def test_dfu_usb_permission_error_is_not_reported_as_missing_loader(self):
        api = fake_api([{"port": "1-1", "state": "dfu"}])
        api.dfu_alternatives = lambda _tool, _port: ([], "dfu-util: Cannot open DFU device 0955:701a")
        readiness = jibo_tui.inspect_readiness(api)
        self.assertEqual(readiness.state, "dfu-error")
        self.assertIn("sudo", readiness.detail)

    def test_update_requires_a_package_in_addition_to_dfu(self):
        ready = jibo_tui.Readiness("dfu-ready", ({"port": "1-1", "state": "dfu"},),
                                   port="1-1", marker_present=True,
                                   alt_names=("var", "jibo-dfu-v1", "rootfsA", "rootfsB",
                                              "services", "skills", "emmc-000"))
        items = {item.key: item for item in jibo_tui.build_menu_items(ready)}
        self.assertFalse(items["flash-update"].enabled)
        self.assertIn("No packages", items["flash-update"].reason)
        items = {item.key: item for item in
                 jibo_tui.build_menu_items(ready, ["update.tar.bz2"])}
        self.assertTrue(items["flash-update"].enabled)

        incomplete = jibo_tui.Readiness("dfu-ready", ready.devices, port="1-1",
                                        marker_present=True, alt_names=("var", "jibo-dfu-v1"))
        item = {item.key: item for item in
                jibo_tui.build_menu_items(incomplete, ["update.tar.bz2"])}["flash-update"]
        self.assertFalse(item.enabled)
        self.assertIn("rootfsA", item.reason)
        self.assertIn("emmc-000", item.reason)

    def test_update_reason_identifies_missing_skills_and_accepts_chunk_alternatives(self):
        base = ("var", "jibo-dfu-v1", "rootfsA", "rootfsB", "services", "emmc-000")
        missing = jibo_tui.Readiness("dfu-ready", (), port="1-1", marker_present=True,
                                     alt_names=base)
        item = {entry.key: entry for entry in
                jibo_tui.build_menu_items(missing, ["update.tar.bz2"])}["flash-update"]
        self.assertFalse(item.enabled)
        self.assertIn("cannot write skills", item.reason)
        self.assertIn("compatible DFU loader", item.reason)

        chunked = jibo_tui.Readiness("dfu-ready", (), port="1-1", marker_present=True,
                                     alt_names=base + ("skills-000", "skills-001"))
        item = {entry.key: entry for entry in
                jibo_tui.build_menu_items(chunked, ["update.tar.bz2"])}["flash-update"]
        self.assertTrue(item.enabled)

    def test_enter_does_not_activate_disabled_item(self):
        app = jibo_tui.TerminalMenu(fake_api())
        screen = FakeScreen([curses.KEY_ENTER, ord("q")])
        with patch.object(curses, "curs_set", return_value=None):
            app.session(screen)
        self.assertEqual(app.command, "quit")
        self.assertEqual(app.note, app.items[0].reason)

    def test_enabled_item_is_returned_for_dispatch(self):
        api = fake_api([{"port": "1-1", "state": "dfu"}])
        app = jibo_tui.TerminalMenu(api)
        screen = FakeScreen([curses.KEY_DOWN, curses.KEY_DOWN,
                             curses.KEY_ENTER])
        with patch.object(curses, "curs_set", return_value=None):
            app.session(screen)
        self.assertEqual(app.command, "backup-var")

    def test_dfu_gpt_probe_passes_selected_port_without_confirmation(self):
        api = fake_api([{"port": "1-3", "state": "dfu"}])
        ready = jibo_tui.inspect_readiness(api)
        with patch.object(jibo_tui, "confirm_action") as confirm:
            result = jibo_tui.execute_action(api, "probe-dfu-gpt", ready)
        self.assertEqual(result, {
            "status": "partition table read and checked",
            "port": "1-3",
        })
        confirm.assert_not_called()

    def test_dfu_gpt_probe_dispatch_refuses_non_dfu_state(self):
        api = fake_api([{"port": "1-3", "state": "rcm"}])
        ready = jibo_tui.inspect_readiness(api)
        with self.assertRaisesRegex(RuntimeError, "only while the robot is in DFU"):
            jibo_tui.execute_action(api, "probe-dfu-gpt", ready)

    def test_shofel_dfu_action_confirms_profile_then_uses_selected_port(self):
        api = fake_api([{"port": "1-2", "state": "rcm"}], shofel_dfu=True)
        ready = jibo_tui.inspect_readiness(api)
        with patch.object(jibo_tui, "confirm_action", return_value=True) as confirm:
            result = jibo_tui.execute_action(api, "enter-dfu-shofel", ready)
        self.assertEqual(result, {
            "action": "enter-dfu-shofel",
            "port": "1-2",
            "confirm_meerkat_rev02": True,
        })
        confirm.assert_called_once()
        self.assertEqual(confirm.call_args.args[0], "Enter DFU with ShofEL")
        self.assertIn("Meerkat Rev02 SDRAM profile", confirm.call_args.args[1])
        self.assertIn("USB port 1-2", confirm.call_args.args[1])
        self.assertIn("No partition write", confirm.call_args.args[1])

    def test_shofel_dfu_action_cancel_does_not_start_loader(self):
        api = fake_api([{"port": "1-2", "state": "rcm"}], shofel_dfu=True)
        ready = jibo_tui.inspect_readiness(api)
        with patch.object(jibo_tui, "confirm_action", return_value=False):
            result = jibo_tui.execute_action(api, "enter-dfu-shofel", ready)
        self.assertEqual(result["status"], "cancelled")

    def test_non_tty_does_not_start_curses(self):
        with patch.object(jibo_tui.sys, "stdin", io.StringIO("")), \
                patch.object(jibo_tui.sys, "stdout", io.StringIO("")), \
                patch.object(curses, "wrapper") as wrapper:
            self.assertIsNone(jibo_tui.run(fake_api()))
        wrapper.assert_not_called()


class CursesPromptTests(unittest.TestCase):
    def run_prompt(self, function, screen):
        with patch.object(jibo_tui, "_terminal_available", return_value=True), \
                patch.object(curses, "wrapper", side_effect=lambda callback: callback(screen)):
            return function()

    def test_select_option_returns_enabled_choice_and_explains_disabled_rows(self):
        blocked_screen = FakeScreen([curses.KEY_DOWN, curses.KEY_ENTER, 27])
        blocked = self.run_prompt(
            lambda: jibo_tui.select_option(
                "Choose action",
                (("ready", "Ready action"),
                 jibo_tui.MenuItem("blocked", "Blocked action", False,
                                   "Enter DFU first."))),
            blocked_screen)
        self.assertIsNone(blocked)
        self.assertTrue(any("Enter DFU first." in text for _, _, text, _ in blocked_screen.lines))

        screen = FakeScreen([curses.KEY_DOWN, curses.KEY_ENTER,
                             curses.KEY_DOWN, curses.KEY_ENTER])
        selected = self.run_prompt(
            lambda: jibo_tui.select_option(
                "Choose action",
                (("ready", "Ready action"),
                 jibo_tui.MenuItem("blocked", "Blocked action", False,
                                   "Enter DFU first."))),
            screen)
        self.assertEqual(selected, "ready")

    def test_text_input_supports_editing_and_escape_cancels(self):
        screen = FakeScreen([ord("a"), ord("b"), curses.KEY_BACKSPACE,
                             ord("c"), curses.KEY_ENTER])
        value = self.run_prompt(
            lambda: jibo_tui.text_input("Image path", "Path:", default=""), screen)
        self.assertEqual(value, "ac")

        cancel_screen = FakeScreen([27])
        cancelled = self.run_prompt(
            lambda: jibo_tui.text_input("Image path", "Path:"), cancel_screen)
        self.assertIsNone(cancelled)

    def test_password_prompt_never_draws_secret_text(self):
        secret = "robotpass"
        screen = FakeScreen([*(ord(char) for char in secret), curses.KEY_ENTER])
        value = self.run_prompt(
            lambda: jibo_tui.text_input("Wi-Fi", "Password:", password=True), screen)
        self.assertEqual(value, secret)
        drawn = "".join(text for _, _, text, _ in screen.lines)
        self.assertNotIn(secret, drawn)
        self.assertIn("•", drawn)

    def test_confirmation_defaults_to_cancel_and_requires_explicit_selection(self):
        cancel_screen = FakeScreen([curses.KEY_ENTER])
        accepted = self.run_prompt(
            lambda: jibo_tui.confirm_action("Confirm write", "Write var to port 1-1?"),
            cancel_screen)
        self.assertFalse(accepted)

        confirm_screen = FakeScreen([curses.KEY_DOWN, curses.KEY_ENTER])
        accepted = self.run_prompt(
            lambda: jibo_tui.confirm_action("Confirm write", "Write var to port 1-1?"),
            confirm_screen)
        self.assertTrue(accepted)
        drawn = "".join(text for _, _, text, _ in confirm_screen.lines)
        self.assertIn("Write var to port 1-1?", drawn)

    def test_result_screen_shows_content_in_curses(self):
        screen = FakeScreen([curses.KEY_ENTER])
        result = self.run_prompt(
            lambda: jibo_tui.show_screen("Result", {"status": "complete"}), screen)
        self.assertIsNone(result)
        drawn = "".join(text for _, _, text, _ in screen.lines)
        self.assertIn('"status": "complete"', drawn)

    def test_confirmation_summary_keeps_update_and_var_plan_details_visible(self):
        update = jibo_tui._confirmation_details({
            "package": "jibo-pvt-flash-build.tar.bz2",
            "version": "5.4.2",
            "usb_port": "1-1",
            "var_policy": "preserve current configuration",
            "partitions": [{"name": "rootfsA", "bytes": 100},
                           {"name": "skills", "bytes": 200}],
        }, "Review the update plan.")
        self.assertIn("jibo-pvt-flash-build.tar.bz2", update)
        self.assertIn("preserve current configuration", update)
        self.assertIn("rootfsA (100 bytes)", update)
        self.assertIn("skills (200 bytes)", update)

        var = jibo_tui._confirmation_details({
            "operation": "set mode to developer",
            "usb_port": "1-1",
            "before_sha256": "before-hash",
            "candidate_sha256": "candidate-hash",
            "baseline_backup": "/backups/var.img",
        }, "Review the var plan.")
        self.assertIn("Current var SHA-256: before-hash", var)
        self.assertIn("Edited var SHA-256: candidate-hash", var)
        self.assertIn("Rollback backup: /backups/var.img", var)

    def test_cancelled_wifi_ssid_does_not_request_network_type(self):
        api = SimpleNamespace()
        with patch.object(jibo_tui, "text_input", return_value=None), \
                patch.object(jibo_tui, "select_option") as select:
            result = jibo_tui.execute_action(
                api, "configure-wifi", jibo_tui.Readiness("dfu-ready", ()))
        self.assertEqual(result["status"], "cancelled")
        self.assertIn("Wi-Fi setup", result["message"])
        select.assert_not_called()

    def test_mode_action_supplies_curses_confirmation_callback_to_backend(self):
        calls = {}
        api = SimpleNamespace(
            set_mode_live=lambda mode, confirmation=None: calls.update(
                mode=mode, confirmation=confirmation))
        with patch.object(jibo_tui, "_select_mode", return_value="developer"), \
                patch.object(jibo_tui, "confirm_action", return_value=True) as confirm:
            jibo_tui.execute_action(api, "set-mode", jibo_tui.Readiness("dfu-ready", ()))
            self.assertEqual(calls["mode"], "developer")
            self.assertTrue(calls["confirmation"]({"partition": "var", "usb_port": "1-1"}))
            confirmation_text = confirm.call_args.args[1]
        self.assertIn("Partition: var", confirmation_text)
        self.assertIn("USB port: 1-1", confirmation_text)


if __name__ == "__main__":
    unittest.main()
