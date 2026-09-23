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

    def getch(self):
        return self.keys.pop(0) if self.keys else ord("q")


def fake_api(devices=(), marker=True, alts=None):
    if alts is None:
        alts = ["var", "jibo-dfu-v1"] if marker else ["var"]
    return SimpleNamespace(
        MARKER="jibo-dfu-v1",
        devices=lambda: list(devices),
        tool=lambda _name: "dfu-util",
        dfu_alternatives=lambda _tool, _port: (alts, ""),
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

    def test_live_actions_require_dfu_and_loader_marker(self):
        rcm = jibo_tui.inspect_readiness(fake_api([{"port": "1-1", "state": "rcm"}]))
        items = {item.key: item for item in jibo_tui.build_menu_items(rcm)}
        self.assertTrue(items["enter-dfu"].enabled)
        self.assertFalse(items["backup-var"].enabled)
        self.assertFalse(items["set-mode"].enabled)
        self.assertTrue(items["inspect-backup"].enabled)
        self.assertIn("Enter DFU", items["backup-var"].reason)

        no_marker = jibo_tui.inspect_readiness(
            fake_api([{"port": "1-1", "state": "dfu"}], marker=False))
        items = {item.key: item for item in jibo_tui.build_menu_items(no_marker)}
        self.assertFalse(items["enter-dfu"].enabled)
        self.assertFalse(items["backup-var"].enabled)
        self.assertIn("already in DFU", items["enter-dfu"].reason)

        no_var = jibo_tui.inspect_readiness(fake_api(
            [{"port": "1-1", "state": "dfu"}], alts=["jibo-dfu-v1", "rootfsA"]))
        items = {item.key: item for item in jibo_tui.build_menu_items(no_var)}
        self.assertEqual(no_var.state, "dfu-profile-incomplete")
        self.assertFalse(items["backup-var"].enabled)
        self.assertIn("var", items["backup-var"].reason)

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
        screen = FakeScreen([curses.KEY_DOWN, curses.KEY_ENTER])
        with patch.object(curses, "curs_set", return_value=None):
            app.session(screen)
        self.assertEqual(app.command, "backup-var")

    def test_non_tty_does_not_start_curses(self):
        with patch.object(jibo_tui.sys, "stdin", io.StringIO("")), \
                patch.object(jibo_tui.sys, "stdout", io.StringIO("")), \
                patch.object(curses, "wrapper") as wrapper:
            self.assertIsNone(jibo_tui.run(fake_api()))
        wrapper.assert_not_called()


if __name__ == "__main__":
    unittest.main()
