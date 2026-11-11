"""Run the installed boot hook with BusyBox ash and a synthetic filesystem."""
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets/boot-network-status"
STOCK_HASH = "66824555951d3be55d74f7d736655f11e945dea887e52f71ff2a5fa2064c14d6"


@unittest.skipUnless(shutil.which("busybox"), "BusyBox is required for ash checks")
class HookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = self.root / "usr/local/bin/jibo-ssm/startup/startup-view.js"
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"synthetic stock view")
        self.var = self.root / "var/jibo/phoenix/network-status"
        self.var.mkdir(parents=True)
        for path in ASSETS.glob("startup-view*.js"):
            (self.var / path.name).write_bytes(path.read_bytes())
        (self.root / "var/log").mkdir()
        (self.root / "proc/self").mkdir(parents=True)
        (self.root / "proc/self/mountinfo").write_text("")
        (self.root / "usr/bin").mkdir(exist_ok=True)
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the OTA guard")
        (self.root / "usr/bin/node").symlink_to(node)
        (self.var / "ota-safe-overlay.js").write_bytes((ASSETS / "ota-safe-overlay.js").read_bytes())
        self.command(self.root / "usr/bin/jibo-getmode", "printf '%s\\n' \"${TEST_MODE:-int-developer}\"\n")
        self.tools = self.root / "bin"
        self.tools.mkdir()
        self.command(self.tools / "mount", "printf '%s\\n' \"$*\" >> \"$TEST_CALLS\"\nexit ${TEST_MOUNT_EXIT:-0}\n")
        real_sha = shutil.which("sha256sum")
        self.command(self.root / "usr/bin/sha256sum",
                     "case \"$1\" in\n*/usr/local/*) printf '%s  %s\\n' \"${TEST_STOCK_HASH:-" +
                     STOCK_HASH + "}\" \"$1\" ;;\n*) exec " + real_sha + " \"$@\" ;;\nesac\n")
        self.env = dict(os.environ, JIBO_NETSTATUS_ROOT=str(self.root),
                        JIBO_NETSTATUS_TEST_NODE_SECONDS="30",
                        PATH=str(self.tools) + os.pathsep + os.environ["PATH"],
                        TEST_CALLS=str(self.root / "mount.calls"))

    def command(self, path, body):
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def run_hook(self, **changes):
        shell = shutil.which("busybox")
        assert shell is not None
        result = subprocess.run([shell, "sh", str(ASSETS / "boot-network-status.sh")],
                                env=dict(self.env, **changes), capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout + result.stderr, "")
        calls = self.root / "mount.calls"
        return calls.read_text().splitlines() if calls.exists() else []

    def test_matching_hash_binds_once_and_logs_no_private_data(self):
        calls = self.run_hook()
        self.assertEqual(calls, ["-n -o bind " + str(self.var / "startup-view.js") + " " + str(self.target)])
        self.assertEqual((self.var / "boot-network-status.log").read_text(),
                         "boot-network-status: bound\n")


    def test_each_pinned_variant_selects_its_own_asset(self):
        for digest, name in (
            (STOCK_HASH, "startup-view.js"),
            ("8f2ba0385c5030d5bf2d86214eb28f272866b72d5ea825cd44650b278e0b1d19", "startup-view-x11-early.js"),
            ("ba8b9c43dd1e1017bface6886ddfe72a9f6e1bbcf2f5710a37460e6082f18848", "startup-view-dom.js"),
            ("f7042f1bcdc0d4120ca315633a447460bbd01b9806281544a49ec28321ce2ff7", "startup-view-dom.js")):
            calls = self.root / "mount.calls"
            if calls.exists():
                calls.unlink()
            self.assertEqual(self.run_hook(TEST_STOCK_HASH=digest),
                             ["-n -o bind " + str(self.var / name) + " " + str(self.target)])

    def test_log_symlink_cannot_modify_an_unrelated_file(self):
        unrelated = self.root / "credentials.json"
        unrelated.write_bytes(b"private synthetic identity")
        (self.var / "boot-network-status.log").symlink_to(unrelated)
        (self.root / "var/log/jibo-boot-network-status.log").symlink_to(unrelated)
        self.run_hook()
        self.assertEqual(unrelated.read_bytes(), b"private synthetic identity")

    def test_log_fifo_cannot_block_boot(self):
        os.mkfifo(self.var / "boot-network-status.log")
        os.mkfifo(self.root / "var/log/jibo-boot-network-status.log")
        self.run_hook()

    def test_missing_owned_log_directory_exits_zero(self):
        for path in self.var.iterdir():
            path.unlink()
        self.var.rmdir()
        self.assertEqual(self.run_hook(TEST_MODE="normal"), [])

    def test_stuck_mode_process_is_bounded_and_cannot_block_boot(self):
        import time
        self.command(self.root / "usr/bin/jibo-getmode", "exec sleep 20\n")
        started = time.monotonic()
        self.assertEqual(self.run_hook(JIBO_NETSTATUS_TEST_NODE_SECONDS=""), [])
        self.assertLess(time.monotonic() - started, 7)

    def test_stuck_native_hash_keeps_its_two_second_bound(self):
        import time
        self.command(self.root / "usr/bin/sha256sum", "exec sleep 20\n")
        started = time.monotonic()
        self.assertEqual(self.run_hook(), [])
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((self.var / "boot-network-status.log").read_text(),
                         "boot-network-status: failed\n")

    def test_stuck_ota_node_is_bounded_and_cannot_mount(self):
        import time
        node = self.root / "usr/bin/node"
        node.unlink()
        self.command(node, "exec sleep 20\n")
        started = time.monotonic()
        self.assertEqual(self.run_hook(JIBO_NETSTATUS_TEST_NODE_SECONDS=""), [])
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual((self.var / "boot-network-status.log").read_text(),
                         "boot-network-status: skipped-ota\n")

    def test_stuck_native_mount_keeps_its_two_second_bound(self):
        import time
        self.command(self.tools / "mount",
                     'printf "%s\\n" "$*" >> "$TEST_CALLS"\nexec sleep 20\n')
        started = time.monotonic()
        self.assertEqual(len(self.run_hook()), 1)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((self.var / "boot-network-status.log").read_text(),
                         "boot-network-status: failed\n")

    def test_failed_mode_command_cannot_apply_its_valid_output(self):
        self.command(self.root / "usr/bin/jibo-getmode", 'printf "int-developer\\n"\nexit 7\n')
        self.assertEqual(self.run_hook(), [])
        self.assertEqual((self.var / "boot-network-status.log").read_text(),
                         "boot-network-status: failed\n")

    def test_synthetic_root_can_shorten_the_node_watchdog(self):
        import time
        self.command(self.root / "usr/bin/jibo-getmode", "exec sleep 20\n")
        started = time.monotonic()
        self.assertEqual(self.run_hook(JIBO_NETSTATUS_TEST_NODE_SECONDS="0.1"), [])
        self.assertLess(time.monotonic() - started, 2)

    def test_slow_node_mode_probe_can_finish_after_two_seconds(self):
        # getmode is Node on the robot; delay in that one process, not a shell
        # child which could outlive a watchdog kill.
        node = shlex.quote(str((self.root / "usr/bin/node").resolve()))
        self.command(self.root / "usr/bin/jibo-getmode", "exec " + node +
                     " -e 'setTimeout(function () { console.log(\"int-developer\"); }, 2200)'\n")
        self.assertEqual(len(self.run_hook(JIBO_NETSTATUS_TEST_NODE_SECONDS="")), 1)
        self.assertEqual((self.var / "boot-network-status.log").read_text(),
                         "boot-network-status: bound\n")

    def test_pending_ota_cannot_acquire_a_services_child_mount(self):
        (self.root / "var/jibo/ota.json").write_text(json.dumps({"idx": 0, "items": [{}]}))
        # The real guard refuses with 0, not safe-to-mount 1 or timeout 124.
        guard = subprocess.run([str(self.root / "usr/bin/node"), str(self.var / "ota-safe-overlay.js"),
                                "check", str(self.root), "startup-view.js"],
                               capture_output=True, text=True, timeout=10)
        self.assertEqual(guard.returncode, 0, guard.stderr)
        self.assertEqual(self.run_hook(), [])
        self.assertIn("skipped-ota", (self.var / "boot-network-status.log").read_text())

    def test_completed_ota_history_does_not_disable_the_feature(self):
        (self.root / "var/jibo/ota.json").write_text(json.dumps({"idx": None, "items": [{}]}))
        self.assertEqual(len(self.run_hook()), 1)

    def test_slow_completed_ota_check_can_finish_after_two_seconds(self):
        node = self.root / "usr/bin/node"
        real_node = shlex.quote(str(node.resolve()))
        node.unlink()
        self.command(node, 'if [ "$2" = check ]; then\n    exec ' + real_node +
                     " -e 'setTimeout(function () { require(process.argv[1]); }, 2200)' \"$@\"\nfi\nexec " +
                     real_node + ' "$@"\n')
        (self.root / "var/jibo/ota.json").write_text(json.dumps({"idx": None, "items": [{}]}))
        self.assertEqual(len(self.run_hook(JIBO_NETSTATUS_TEST_NODE_SECONDS="")), 1)
        self.assertEqual((self.var / "boot-network-status.log").read_text(),
                         "boot-network-status: bound\n")

    def test_malformed_ota_state_is_fail_closed(self):
        (self.root / "var/jibo/ota.json").write_text("invalid JSON")
        self.assertEqual(self.run_hook(), [])

    def test_missing_or_corrupt_release_helper_is_fail_closed(self):
        helper = self.var / "ota-safe-overlay.js"
        helper.unlink()
        self.assertEqual(self.run_hook(), [])
        helper.write_bytes(b"broken helper")
        self.assertEqual(self.run_hook(), [])

    def test_missing_node_is_fail_closed(self):
        (self.root / "usr/bin/node").unlink()
        self.assertEqual(self.run_hook(), [])

    def test_stock_hash_mismatch_is_noop(self):
        self.assertEqual(self.run_hook(TEST_STOCK_HASH="0" * 64), [])

    def test_nondeveloper_modes_are_noop(self):
        for mode in ("normal", "oobe", "service", "int-developer\nnormal", "unknown"):
            with self.subTest(mode=mode):
                self.assertEqual(self.run_hook(TEST_MODE=mode), [])

    def test_developer_mode_is_supported(self):
        self.assertEqual(len(self.run_hook(TEST_MODE="developer")), 1)

    def test_existing_bind_is_noop_on_repeated_execution(self):
        first = self.run_hook()
        (self.root / "proc/self/mountinfo").write_text(
            "42 1 179:5 /jibo/phoenix/network-status/startup-view.js " + str(self.target) +
            " rw - ext4 /dev/mmcblk0p5 rw\n")
        self.assertEqual(self.run_hook(), first)
        self.assertIn("already-mounted", (self.var / "boot-network-status.log").read_text())

    def test_asset_corruption_is_noop(self):
        (self.var / "startup-view.js").write_bytes(b"corrupt asset")
        self.assertEqual(self.run_hook(), [])

    def test_failed_mount_exits_zero(self):
        self.assertEqual(len(self.run_hook(TEST_MOUNT_EXIT="1")), 1)
        self.assertEqual((self.var / "boot-network-status.log").read_text(),
                         "boot-network-status: failed\n")

    def test_missing_tools_and_mountinfo_exit_zero(self):
        for path in (self.root / "usr/bin/jibo-getmode", self.root / "usr/bin/sha256sum",
                     self.root / "proc/self/mountinfo", self.tools / "mount"):
            with self.subTest(path=path):
                content = path.read_bytes()
                mode = path.stat().st_mode
                path.unlink()
                self.assertEqual(self.run_hook(), [])
                path.write_bytes(content)
                path.chmod(mode)

    def test_no_global_log_directory_is_required(self):
        (self.root / "var/log").rmdir()
        self.assertEqual(len(self.run_hook()), 1)


@unittest.skipUnless(shutil.which("busybox"), "BusyBox is required for ash checks")
class BoundedTests(unittest.TestCase):
    def run_bounded(self, command, prelude="", seconds="0.1"):
        # Execute the production function verbatim without running apply_status.
        source = (ASSETS / "boot-network-status.sh").read_text().split("\napply_status()", 1)[0]
        shell = shutil.which("busybox")
        assert shell is not None
        return subprocess.run([shell, "sh", "-c", source + "\n" + prelude +
                               "\nbounded " + shlex.quote(seconds) + " " + command + "\nexit $?\n"],
                              env=dict(os.environ, JIBO_NETSTATUS_ROOT=""),
                              capture_output=True, text=True, timeout=5)

    def test_watchdog_deadline_has_a_distinct_timeout_status(self):
        result = self.run_bounded("sh -c 'exec sleep 20'")
        self.assertEqual(result.returncode, 124, result.stderr)

    def test_parent_cleanup_cannot_erase_the_watchdog_timeout_receipt(self):
        # Hold the watchdog after its real kill so cleanup TERM deterministically
        # races with receipt publication. This used to leak signal status 137.
        result = self.run_bounded("sh -c 'exec sleep 20'", """
kill() {
    if [ "$1" = -KILL ]; then
        command kill "$@"
        status=$?
        sleep 0.1
        return "$status"
    fi
    command kill "$@"
}
""")
        self.assertEqual(result.returncode, 124, result.stderr)

    def test_completed_status_is_preserved_without_waiting_for_deadline(self):
        for status in (0, 1, 7, 124, 137):
            with self.subTest(status=status):
                # The subprocess's five-second outer limit is shorter than the
                # watchdog: preserving a result must not wait for its deadline.
                result = self.run_bounded("sh -c 'exit " + str(status) + "'", seconds="20")
                self.assertEqual(result.returncode, status, result.stderr)

    def test_unrelated_sigkill_is_not_a_watchdog_timeout(self):
        result = self.run_bounded("sh -c 'kill -KILL $$'", seconds="20")
        self.assertEqual(result.returncode, 137, result.stderr)

    def test_missing_command_keeps_its_failure_status(self):
        result = self.run_bounded("jibo-synthetic-nonexistent-command", seconds="20")
        self.assertEqual(result.returncode, 127, result.stderr)


@unittest.skipUnless(shutil.which("busybox"), "BusyBox is required for ash checks")
class BudgetTests(unittest.TestCase):
    def test_test_budget_override_requires_a_synthetic_root(self):
        source = (ASSETS / "boot-network-status.sh").read_text().split("\napply_status()", 1)[0]

        def budget(root, override):
            shell = shutil.which("busybox")
            assert shell is not None
            result = subprocess.run([shell, "sh", "-c",
                                     source + '\nprintf "%s" "$node_seconds"\n'],
                                    env=dict(os.environ, JIBO_NETSTATUS_ROOT=root,
                                             JIBO_NETSTATUS_TEST_NODE_SECONDS=override),
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        default = budget("", "")
        self.assertEqual(default, "5")
        self.assertEqual(budget("", "9"), default)
        self.assertEqual(budget("/synthetic", "9"), "9")


if __name__ == "__main__":
    unittest.main()
