"""Local-only release controls with sockets closed and synthetic USB."""
from contextlib import ExitStack, contextmanager, redirect_stdout
import io
import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import jibo_calibration_client as wire
import jibo_calibration_flow as flow
import jibo_dfu as dfu
import jibo_gui_core as gui_core
import jibo_tui as tui
from gui_fakes import FakeApi
from test_calibration_flow import DummyClient


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.api = FakeApi(quick=False)
        self.api.BACKUP_ROOT = self.root / "saved"
        self.client = DummyClient()
        self.api.calibration_client_factory = lambda: self.client
        self.identity = {"cid_serial": "0123456789abcdef0123456789abcdef", "usb_port": "1-1"}
        self.api.calibration_identity = lambda port=None: dict(self.identity)
        self.backup = {"status": "backup complete", "image": "synthetic.img", "sha256": "a" * 64}
        self.prompts = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.sockets = self.stack.enter_context(patch.object(socket, "socket", side_effect=AssertionError("socket opened")))
        self.connections = self.stack.enter_context(patch.object(socket, "create_connection", side_effect=AssertionError("network attempted")))
        self.dns = self.stack.enter_context(patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS attempted")))

    def choose(self, title, options, **kwargs):
        items = tuple(options) if options and hasattr(options[0], "key") else tui._normalise_options(options)
        self.prompts.append((title, items))
        keys = [i.key for i in items]
        if "keep-calibration" in keys:
            return getattr(self, "policy", "keep-calibration")
        if "confirm" in keys:
            return "confirm"
        if title == "More tools":
            return None
        return keys[0] if keys else None

    def run_surface(self, surface, action):
        if surface == "tui":
            with patch.object(tui, "select_option", side_effect=self.choose), redirect_stdout(io.StringIO()):
                result = tui.execute_action(self.api, action, tui.inspect_readiness(self.api))
                return tui._result_summary(action, result)
        def ask(prompt):
            return self.choose(prompt.title, prompt.items)
        controller = gui_core.GuiController(self.api, prompter=ask, runner=gui_core.sync_runner,
                                             preferences_path=self.root / "gui-prefs.json")
        controller.request_refresh()
        controller.drain()
        self.assertTrue(controller.start_action(action))
        result = [event[1] for event in controller.drain() if event[0] == "finished"][-1]
        self.last_outcome = result
        return result.lines

    def assert_local_only(self):
        self.sockets.assert_not_called()
        self.connections.assert_not_called()
        self.dns.assert_not_called()
        self.assertEqual(self.client.requests, [])
        self.assertFalse((self.api.BACKUP_ROOT / "calibration").exists())
        self.assertFalse(any(title == flow.CONSENT_TITLE or title == "Look up this Jibo's calibration"
                             for title, _ in self.prompts))

    def test_backup_has_no_consent_result_or_shared_state(self):
        self.api.backup_var = lambda **kw: dfu.backup_var(dfu_util="synthetic-usb", **kw)
        for surface in ("tui", "gui"):
            with self.subTest(surface=surface), patch.object(dfu, "_backup_var_without_event", return_value=self.backup), \
                    patch.object(dfu, "calibration_identity", return_value=self.identity):
                lines = self.run_surface(surface, "backup-var")
                self.assert_local_only()
                self.assertFalse(any("Calibration sharing" in line or "Removal receipt" in line for line in lines))
                self.assertIn("Var backup is ready.", lines)

    def test_flow_entry_points_do_not_prompt_request_or_write_preferences(self):
        session = flow.SharingFlow(self.api, self.choose)
        self.assertIsNone(session.lookup_for_fresh_setup(self.identity))
        self.assertEqual(self.prompts, [])
        for name in ("settings", "remove"):
            with self.subTest(entry=name), self.assertRaises(wire.Unavailable):
                getattr(session, name)(self.identity)
        session.save_preference("a" * 64, choice="share")
        self.assert_local_only()

    def test_client_constructor_stops_before_optional_dependencies(self):
        with patch.object(wire, "_crypto") as crypto, patch.object(wire, "load_core") as core:
            with self.assertRaises(wire.Unavailable):
                wire.CalibrationClient(self.root / "calibration")
            crypto.assert_not_called()
            core.assert_not_called()
        self.assert_local_only()

    def test_existing_client_cannot_request_submit_lookup_remove_or_create_receipt(self):
        from test_calibration_flow import core
        with patch.object(wire, "CALIBRATION_SHARING_AVAILABLE", True, create=True), \
                patch.object(wire, "_crypto", return_value=(None, None, None)):
            client = wire.CalibrationClient(self.root / "calibration", core=core)
        key = core.VECTOR["robot_key"]
        candidate = core.VECTOR["candidate_sha256"]
        for name, args in (("request", ("GET", "/v3/robots/" + key)), ("submit", (key, core.FILES)),
                           ("lookup", (key,)), ("remove", (self.root / "receipt.json",)),
                           ("candidate_status", (key, candidate)), ("_receipt", (key, candidate))):
            with self.subTest(entry=name), self.assertRaises(wire.Unavailable):
                getattr(client, name)(*args)
        self.assert_local_only()
        self.assertFalse((self.root / "calibration").exists())

    def test_more_tools_enumerates_only_local_actions_in_both_surfaces(self):
        for surface in ("tui", "gui"):
            with self.subTest(surface=surface):
                self.prompts.clear()
                self.run_surface(surface, "more-tools")
                self.assertEqual(len(self.prompts), 1)
                title, options = self.prompts[0]
                self.assertEqual(title, "More tools")
                self.assertEqual([item.key for item in options], ["probe-dfu-gpt", "backup-partitions",
                    "restore-partitions", "write-var", "remove-boot-network-status", "inspect-backup", "edit-backup"])
                self.assert_local_only()

    def test_hidden_actions_refuse_before_hardware_or_prompts(self):
        readiness = tui.inspect_readiness(self.api)
        for key in ("calibration-sharing", "calibration-lookup", "calibration-remove"):
            with self.subTest(key=key), patch.object(self.api, "calibration_identity") as identity:
                with self.assertRaises(wire.Unavailable):
                    tui.execute_action(self.api, key, readiness)
                identity.assert_not_called()
        self.assertEqual(self.prompts, [])
        self.assert_local_only()

    @contextmanager
    def flash_fixture(self, cameras=True):
        from calibration_support import make_image, FILES, PATHS, IDENTITY
        from test_updates import compact_set
        directory = self.root / ("flash-" + str(len(list(self.root.glob("flash-*")))))
        directory.mkdir()
        entries = {"/jibo/identity.json": IDENTITY}
        if cameras:
            entries.update({PATHS[k]: FILES[k] for k in ("left", "right", "inter_camera", "imu", "audio")})
        source = make_image(directory, "backup.img", entries)
        stock = make_image(directory, "stock.img", {**{PATHS[k]: FILES[k] for k in ("left", "right", "inter_camera")},
                                                   "/jibo/mode.json": b'{"mode":"oobe"}'})
        tag = "serial-sha256:" + "a" * 64
        digest = dfu._sha256_file(source)
        manifest = directory / "backup-manifest.json"
        manifest.write_text(json.dumps({"kind": "jibo-var-backup", "device_tag": tag, "sha256": digest}))
        self.backup = {"image": str(source), "sha256": digest, "manifest": str(manifest), "status": "backup complete"}
        package = SimpleNamespace(source=directory / "package", version="synthetic", phoenix=None)
        capacities = {name: stock.stat().st_size for name in dfu.UPDATE_ORDER}
        self.writes = []
        self.record_path = directory / "operation" / "update-manifest.json"
        self.api.packages = [package.source]
        self.api.backup_var = lambda **kw: dfu.backup_var(dfu_util="synthetic-usb", **kw)
        self.api.flash_update = lambda path, **kw: dfu.flash_update(path, dfu_util="synthetic-usb", out=directory / "operation", **kw)
        def transfer(argv, **kwargs):
            self.writes.append(argv[argv.index("-a") + 1])
        with ExitStack() as stack:
            for owner, name, kwargs in (
                (dfu, "EXPECTED_VAR_SIZE", {"new": source.stat().st_size}),
                (dfu, "BACKUP_ROOT", {"new": self.api.BACKUP_ROOT}),
                (dfu, "calibration_identity", {"return_value": dict(self.identity)}),
                (dfu, "devices", {"return_value": self.api.devices()}),
                (dfu, "_dfu_context", {"return_value": ("1-1", list(capacities) + [dfu.MARKER], tag, "")}),
                (dfu, "_read_gpt_capacities", {"return_value": capacities}),
                (dfu.updates, "validate_package", {"return_value": package}),
                (dfu.updates, "prepare_compact_images", {"return_value": compact_set({n: stock for n in dfu.UPDATE_ORDER})}),
                (dfu, "_package_content_sha256", {"return_value": "b" * 64}),
                (dfu, "_backup_var_without_event", {"return_value": self.backup}),
                (dfu, "run_with_progress", {"side_effect": transfer}),
                (dfu, "run", {"return_value": "SYNTHETIC RESET"})):
                stack.enter_context(patch.object(owner, name, **kwargs))
            yield
        self.assertEqual(dfu._sha256_file(source), digest)

    def test_no_local_calibration_fails_closed_with_one_message_and_record(self):
        expected = "This Jibo's calibration was not found in a local backup. Nothing was written to the robot."
        for surface in ("tui", "gui"):
            with self.subTest(surface=surface), self.flash_fixture(cameras=False):
                if surface == "tui":
                    with self.assertRaises(dfu.DfuError) as failure:
                        self.run_surface(surface, "flash-update")
                    text = str(failure.exception)
                else:
                    lines = self.run_surface(surface, "flash-update")
                    self.assertEqual(self.last_outcome.kind, "error")
                    text = "\n".join(lines)
                self.assertEqual(text, expected + "\nUpdate record: " + str(self.record_path))
                record = json.loads(self.record_path.read_text())
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["error"], expected)
                self.assertEqual(record["writes"], [])
                self.assertEqual(record["backups"]["var"], self.backup)
                self.assertEqual(self.writes, [])
                self.assert_local_only()

    def test_client_error_during_preparation_marks_failed_and_shows_record(self):
        for error in (wire.ClientError("Calibration check failed."), wire.HttpError(404), wire.Unavailable("Calibration check unavailable.")):
            with self.subTest(error=type(error).__name__), self.flash_fixture(cameras=False):
                class Session:
                    seen = True
                    def lookup_for_fresh_setup(self, identity):
                        raise error
                failure = None
                with patch.object(wire, "CALIBRATION_SHARING_AVAILABLE", True, create=True), flow.operation(Session()), redirect_stdout(io.StringIO()):
                    try:
                        dfu.flash_update("package", port="1-1", dfu_util="synthetic-usb",
                                         out=self.record_path.parent, confirmation="FLASH UPDATE")
                    except (dfu.DfuError, wire.ClientError) as exc:
                        failure = exc
                record = json.loads(self.record_path.read_text())
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["error"], str(error))
                self.assertIsInstance(failure, dfu.DfuError)
                self.assertEqual(str(failure), str(error) + "\nUpdate record: " + str(self.record_path))
                self.assertEqual(record["writes"], [])
                self.assertEqual(self.writes, [])
                self.assert_local_only()

    def test_keep_and_unsafe_flash_with_local_backup_do_not_share(self):
        for surface in ("tui", "gui"):
            for policy in ("keep-calibration", "without-calibration"):
                with self.subTest(surface=surface, policy=policy), self.flash_fixture():
                    self.policy = policy
                    lines = self.run_surface(surface, "flash-update")
                    self.assertEqual(self.writes, list(dfu.UPDATE_ORDER))
                    self.assertFalse(any("Calibration sharing" in line or "Removal receipt" in line for line in lines))
                    self.assert_local_only()

    def test_old_sharing_result_fields_are_not_rendered(self):
        result = dict(self.backup, calibration_sharing="Submitted for review",
                      calibration_sharing_detail="Removal receipt: synthetic receipt")
        for surface in (tui, gui_core.load_flows()):
            with self.subTest(surface=surface.__name__):
                lines = surface._result_summary("backup-var", result)
                self.assertFalse(any("Calibration sharing" in line or "Removal receipt" in line for line in lines))
        self.assert_local_only()

    def test_packaged_release_switch_works_without_optional_dependencies(self):
        import subprocess
        import sys
        import zipfile
        import scripts.check_package as check_package
        from test_package import PackageTests
        self.assertIn("jibo_calibration_client.py", check_package.SOURCES)
        fixture = PackageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.run_package()
        extracted = self.root / "packaged"
        with zipfile.ZipFile(fixture.output) as archive:
            self.assertEqual(archive.read("jibo_calibration_client.py"), Path(wire.__file__).read_bytes())
            archive.extractall(extracted)
        code = (
            "import socket; from unittest.mock import patch; import jibo_dfu, jibo_tui, jibo_calibration_client as c; "
            "assert c.CALIBRATION_SHARING_AVAILABLE is False; "
            "c._crypto=lambda: (_ for _ in ()).throw(AssertionError('optional dependency used')); "
            "c.load_core=lambda: (_ for _ in ()).throw(AssertionError('optional core used')); "
            "socket.socket=lambda *a,**kw: (_ for _ in ()).throw(AssertionError('socket opened')); "
            "socket.create_connection=socket.socket; socket.getaddrinfo=socket.socket; "
            "\ntry: c.CalibrationClient('unused')\nexcept c.Unavailable: print('local-only package verified')\n"
            "else: raise AssertionError('client available')\n")
        result = subprocess.run([sys.executable, "-S", "-c", code], cwd=extracted,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "local-only package verified")
        self.assert_local_only()

    def test_public_docs_describe_only_local_calibration(self):
        project = Path(__file__).resolve().parents[1]
        self.assertFalse((project / "docs" / "calibration-sharing.md").exists())
        for path in [project / "README.md", *sorted((project / "docs").rglob("*.md"))]:
            with self.subTest(document=path.relative_to(project).as_posix()):
                text = path.read_text().lower()
                for phrase in ("calibration-sharing", "calibration sharing", "free database",
                               "calibration database", "network lookup", "reviewed source"):
                    self.assertNotIn(phrase, text)
        self.assertIn("Calibration backups and restores stay on this computer.",
                      (project / "docs" / "rtm-images-and-calibration.md").read_text())
