"""Mac portability checks using descriptors and harmless command stubs."""
import ctypes
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import jibo_dfu as toolkit
import jibo_dfu_bounded as bounded
import jibo_tui


class DescriptorLibusb:
    def __init__(self, records, list_error=None):
        self.records = records
        self.list_error = list_error
        self.calls = []
        self.nodes = [bounded._Device() for _ in records]
        self.pointers = [ctypes.pointer(node) for node in self.nodes]
        self.indexes = {ctypes.addressof(node): index for index, node in enumerate(self.nodes)}
        self.array = (bounded._DeviceP * (len(records) + 1))(*self.pointers)

    def libusb_init(self, _context):
        self.calls.append("init")
        return 0

    def libusb_get_device_list(self, _context, output):
        self.calls.append("list")
        if self.list_error is not None:
            return self.list_error
        ctypes.cast(output, ctypes.POINTER(ctypes.POINTER(bounded._DeviceP)))[0] = self.array
        return len(self.records)

    def record(self, device):
        return self.records[self.indexes[ctypes.addressof(device.contents)]]

    def libusb_get_device_descriptor(self, device, output):
        record = self.record(device)
        descriptor = ctypes.cast(output, ctypes.POINTER(bounded._DeviceDescriptor)).contents
        descriptor.idVendor, descriptor.idProduct = record[:2]
        return 0

    def libusb_get_bus_number(self, device):
        return self.record(device)[2]

    def libusb_get_port_numbers(self, device, output, _length):
        ports = self.record(device)[3]
        for index, number in enumerate(ports):
            output[index] = number
        return len(ports)

    def libusb_free_device_list(self, _devices, unref):
        self.calls.append(("free", unref))

    def libusb_exit(self, _context):
        self.calls.append("exit")

    def libusb_open(self, _device, output):
        self.calls.append("open")
        self.handle = bounded._Handle()
        ctypes.cast(output, ctypes.POINTER(bounded._HandleP))[0] = ctypes.pointer(self.handle)
        return 0

    def libusb_claim_interface(self, _handle, interface):
        self.calls.append(("claim", interface))
        return 0

    def libusb_set_interface_alt_setting(self, _handle, interface, alternate):
        self.calls.append(("alt", interface, alternate))
        return 0

    def libusb_release_interface(self, _handle, interface):
        self.calls.append(("release", interface))

    def libusb_close(self, _handle):
        self.calls.append("close")


class MacUsbTests(unittest.TestCase):
    def test_descriptor_enumeration_uses_dfu_util_topology_without_opening_devices(self):
        lib = DescriptorLibusb([
            (0x0955, 0x701A, 20, (3, 2)),
            (0x1234, 0x701A, 1, (1,)),
            (0x0955, 0x7740, 0, (2,)),
            (0x0955, 0x9999, 1, (4,)),
        ])
        with patch.object(bounded.sys, "platform", "darwin"):
            self.assertEqual(bounded.list_recovery_devices(libusb=lib), [
                {"port": "0-2", "state": "rcm"},
                {"port": "20-3.2", "state": "dfu"},
            ])
        self.assertEqual(lib.calls, ["init", "list", ("free", 1), "exit"])

    def test_empty_or_failed_enumeration_releases_the_context(self):
        lib = DescriptorLibusb([])
        self.assertEqual(bounded.list_recovery_devices(libusb=lib), [])
        self.assertEqual(lib.calls[-2:], [("free", 1), "exit"])
        lib = DescriptorLibusb([], list_error=-1)
        with self.assertRaises(bounded.BoundedDfuError):
            bounded.list_recovery_devices(libusb=lib)
        self.assertEqual(lib.calls[-1], "exit")

    def test_missing_topology_is_an_error_instead_of_hiding_a_robot(self):
        lib = DescriptorLibusb([(0x0955, 0x701A, 20, ())])
        with self.assertRaisesRegex(bounded.BoundedDfuError, "no usable port path"):
            bounded.list_recovery_devices(libusb=lib)
        self.assertEqual(lib.calls[-2:], [("free", 1), "exit"])

    def test_mac_device_discovery_surfaces_libusb_errors(self):
        with patch.object(toolkit.sys, "platform", "darwin"), \
                patch.object(bounded, "list_recovery_devices", side_effect=bounded.BoundedDfuError("missing libusb")):
            with self.assertRaisesRegex(toolkit.DfuError, "missing libusb"):
                toolkit.devices()

    def test_mac_marker_read_skips_sysfs_and_claims_matching_dfu_device(self):
        lib = DescriptorLibusb([(0x0955, 0x701A, 0, (3, 1))])
        marker = b"m" * bounded.MARKER_BYTES
        with patch.object(bounded.sys, "platform", "darwin"), \
                patch.object(bounded, "_verify_sysfs_device") as sysfs, \
                patch.object(bounded, "_interface_alt", return_value=(2, 4)), \
                patch.object(bounded, "_dfu_transfer_size", return_value=4096), \
                patch.object(bounded, "_upload_bounded", return_value=marker) as upload:
            self.assertEqual(bounded.read_dfu_alt_prefix("0-3.1", libusb=lib), marker)
        sysfs.assert_not_called()
        upload.assert_called_once()
        self.assertEqual(lib.calls, ["init", "list", "open", ("claim", 2), ("alt", 2, 4),
                                     ("release", 2), "close", ("free", 1), "exit"])

    def test_mac_marker_read_rejects_wrong_identity_or_port_before_open(self):
        for record in ((0x0955, 0x7740, 0, (3,)), (0x1234, 0x701A, 0, (3,)),
                       (0x0955, 0x701A, 0, (4,))):
            lib = DescriptorLibusb([record])
            with self.subTest(record=record), patch.object(bounded.sys, "platform", "darwin"):
                with self.assertRaisesRegex(bounded.BoundedDfuError, "No Jibo DFU device"):
                    bounded.read_dfu_alt_prefix("0-3", libusb=lib)
            self.assertNotIn("open", lib.calls)
            self.assertEqual(lib.calls[-2:], [("free", 1), "exit"])

    def test_mac_port_zero_is_valid_but_linux_port_zero_is_rejected(self):
        with patch.object(bounded.sys, "platform", "darwin"):
            self.assertEqual(bounded._parse_port("0-3.1"), (0, (3, 1)))
        with patch.object(bounded.sys, "platform", "linux"):
            with self.assertRaises(bounded.BoundedDfuError):
                bounded._parse_port("0-3.1")

    def test_mac_transfer_falls_back_to_dfu_util_even_if_usbfs_directory_exists(self):
        with patch.object(toolkit.sys, "platform", "darwin"), \
                patch.object(Path, "is_dir", return_value=True), \
                patch.dict(os.environ, {"JIBO_DFU_TRANSFER": "pipelined"}):
            argv = toolkit._partition_transfer_argv("/native/dfu-util", "0-3", "var", "-U",
                                                   Path("/tmp/var.img"), 524288000)
        self.assertEqual(argv[0], "/native/dfu-util")
        self.assertEqual(argv[-2:], ["-Z", "524288000"])

    def test_mac_resolves_native_dfu_util_and_honors_explicit_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tools").mkdir()
            bundled = root / "tools/dfu-util"
            bundled.write_bytes(b"ELF placeholder")
            with patch.object(toolkit.sys, "platform", "darwin"), \
                    patch.object(toolkit, "ROOT", root), \
                    patch.dict(os.environ, {"JIBO_DFU_UTIL": ""}), \
                    patch.object(toolkit.shutil, "which", return_value="/native/dfu-util"):
                self.assertEqual(toolkit.tool("dfu-util"), "/native/dfu-util")
                self.assertEqual(toolkit.tool("dfu-util", str(bundled)), str(bundled))
                with patch.dict(os.environ, {"JIBO_DFU_UTIL": str(bundled)}):
                    self.assertEqual(toolkit.tool("dfu-util"), str(bundled))

    def test_mac_shofel_tool_rejects_bundled_linux_elf_and_honors_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tools").mkdir()
            (root / "tools/shofel2_t124").write_bytes(b"ELF placeholder")
            native = root / "native"
            native.mkdir()
            host = native / "shofel2_t124"
            host.write_bytes(b"native host")
            host.chmod(0o755)
            (native / "intermezzo.bin").write_bytes(b"intermezzo")
            (native / "dfu_stage2.bin").write_bytes(b"stage payload")
            with patch.object(toolkit.sys, "platform", "darwin"), \
                    patch.object(toolkit, "ROOT", root), \
                    patch.dict(os.environ, {"JIBO_SHOFEL2": str(host)}), \
                    patch.object(toolkit, "run", return_value="dfu-stage-launch=1\n") as capability:
                self.assertEqual(toolkit._shofel_dfu_tool(), str(host.resolve()))
            capability.assert_called_once_with(
                [str(host.resolve()), "--dfu-stage-capability"], timeout=5)
            with patch.dict(os.environ, {"JIBO_SHOFEL2": ""}), \
                    patch.object(toolkit.shutil, "which", return_value=None):
                with self.assertRaisesRegex(toolkit.DfuError, "Missing shofel2_t124"):
                    toolkit._shofel_dfu_tool()

    def test_mac_entry_availability_follows_the_native_helper(self):
        with tempfile.TemporaryDirectory() as directory:
            loader = Path(directory) / "loader.bin"
            loader.write_bytes(b"pinned loader")
            with patch.object(toolkit.sys, "platform", "darwin"), \
                    patch.object(toolkit, "DEFAULT_LOADER", loader), \
                    patch.object(toolkit, "_shofel_dfu_tool",
                                 return_value="/native/shofel2_t124"):
                self.assertTrue(toolkit.shofel_dfu_available())
            with patch.object(toolkit, "DEFAULT_LOADER", loader), \
                    patch.object(toolkit, "_shofel_dfu_tool",
                                 side_effect=toolkit.DfuError("Missing shofel2_t124")):
                self.assertFalse(toolkit.shofel_dfu_available())
            with patch.object(toolkit, "DEFAULT_LOADER",
                              Path(directory) / "missing-loader.bin"), \
                    patch.object(toolkit, "_shofel_dfu_tool",
                                 return_value="/native/shofel2_t124"):
                self.assertFalse(toolkit.shofel_dfu_available())

    def test_mac_entry_requires_profile_confirmation_before_usb_access(self):
        with patch.object(toolkit.sys, "platform", "darwin"), \
                patch.object(toolkit, "_shofel_dfu_tool") as locate, \
                patch.object(toolkit, "devices") as connected:
            with self.assertRaisesRegex(toolkit.DfuError, "Confirm the Meerkat Rev02"):
                toolkit.enter_shofel_dfu(port="0-3")
        locate.assert_not_called()
        connected.assert_not_called()

    def test_mac_shofel_entry_launches_and_confirms_dfu_on_libusb_port(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            launch = root / "shofel-launch"
            launch.mkdir()
            executable = launch / "shofel2_t124"
            executable.write_bytes(b"native host")
            image = root / "loader.bin"
            image.write_bytes(b"pinned loader")
            states = [[{"port": "0-3.2", "state": "rcm"}],
                      [{"port": "0-3.2", "state": "dfu"}]]
            with patch.object(toolkit.sys, "platform", "darwin"), \
                    patch.object(toolkit, "_shofel_dfu_tool", return_value=str(executable)), \
                    patch.object(toolkit, "devices", side_effect=states), \
                    patch.object(toolkit, "tool", return_value="dfu-util"), \
                    patch.object(toolkit, "run_with_progress",
                                 return_value="Starting verified ARM-state SPL at 0x80108000.") as transfer, \
                    patch.object(toolkit, "dfu_alternatives",
                                 return_value=([toolkit.MARKER, "var"], "")):
                result = toolkit.enter_shofel_dfu(
                    port="0-3.2", loader=image, confirm_meerkat_rev02=True)
            argv = transfer.call_args.args[0]
            self.assertEqual(argv, [str(executable), "--usb-port-path", "0-3.2", "DFU_STAGE",
                                    str(image.resolve()), "--confirm-meerkat-rev02", "--launch"])
            self.assertTrue(transfer.call_args.kwargs["byte_progress"])
            self.assertEqual(result["state"], "dfu")
            self.assertTrue(result["loader_verified"])
            self.assertEqual(result["entry_transport"], "ShofEL")

    def test_mac_rcm_menu_offers_native_shofel_entry(self):
        ready = jibo_tui.Readiness(
            "rcm", ({"port": "0-3", "state": "rcm"},), port="0-3",
            shofel_dfu_available=True)
        with patch.object(toolkit.sys, "platform", "darwin"):
            items = {item.key: item for item in jibo_tui.build_menu_items(ready)}
            status = jibo_tui.status_lines(ready)
        self.assertTrue(items["enter-dfu-shofel"].enabled)
        self.assertEqual(items["enter-dfu-shofel"].reason, "")
        self.assertIn("STEP 2 / 3", status[0])
        self.assertIn("ShofEL", status[1])

        missing = jibo_tui.Readiness("rcm", ({"port": "0-3", "state": "rcm"},), port="0-3")
        items = {item.key: item for item in jibo_tui.build_menu_items(missing)}
        self.assertFalse(items["enter-dfu-shofel"].enabled)
        self.assertIn("Run the launcher to build it", items["enter-dfu-shofel"].reason)


class MacLibraryTests(unittest.TestCase):
    def test_homebrew_and_macports_library_fallbacks(self):
        for prefix in ("/opt/homebrew", "/usr/local", "/opt/local"):
            wanted = prefix + ("/lib/libusb-1.0.dylib" if prefix == "/opt/local" else
                               "/opt/libusb/lib/libusb-1.0.dylib")
            library = MagicMock()

            def load(name):
                if name != wanted:
                    raise OSError("not found")
                return library

            with self.subTest(prefix=prefix), patch.object(bounded.sys, "platform", "darwin"), \
                    patch.dict(os.environ, {"JIBO_LIBUSB": ""}), \
                    patch.object(bounded.ctypes.util, "find_library", return_value=None), \
                    patch.object(bounded.ctypes, "CDLL", side_effect=load):
                self.assertIs(bounded._load_libusb(), library)

    def test_explicit_library_failure_does_not_silently_select_another_library(self):
        with patch.dict(os.environ, {"JIBO_LIBUSB": "/custom/libusb.dylib"}), \
                patch.object(bounded.ctypes, "CDLL", side_effect=OSError("wrong architecture")) as load:
            with self.assertRaisesRegex(bounded.BoundedDfuError, "wrong architecture"):
                bounded._load_libusb()
        load.assert_called_once_with("/custom/libusb.dylib")


class MacLauncherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="jibo-mac-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo with spaces"
        self.repo.mkdir()
        source = Path(__file__).resolve().parents[1]
        for name in ("run.sh", "run-macos.sh"):
            shutil.copy2(source / name, self.repo / name)
        (self.repo / "jibo_dfu.py").write_text("# launcher fixture\n")
        self.prefix = self.root / "native tools"
        self.bin = self.prefix / "bin"
        self.bin.mkdir(parents=True)
        self.trace = self.root / "trace"
        self.env = os.environ.copy()
        for name in ("JIBO_PYTHON", "JIBO_LIBUSB", "JIBO_DFU_UTIL",
                     "JIBO_SHOFEL2", "JIBO_PAYLOADS_FROM", "JIBO_SHOFEL_SRC"):
            self.env.pop(name, None)
        self.entry = self.root / "native-entry" / "shofel2_t124"
        self.env.update(PATH=str(self.bin), JIBO_MAC_PREFIX=str(self.prefix),
                        JIBO_TEST_TRACE=str(self.trace), JIBO_TEST_ARCH="x86_64",
                        JIBO_SHOFEL2=str(self.entry))
        self.stub("dirname", '#!/bin/sh\nexec /usr/bin/dirname "$@"\n')
        self.stub("uname", '#!/bin/sh\nif [ "$1" = -s ]; then echo Darwin; else echo "$JIBO_TEST_ARCH"; fi\n')
        self.stub("sysctl", '#!/bin/sh\necho "${JIBO_TEST_TRANSLATED:-0}"\n')
        self.stub("dfu-util", "#!/bin/sh\nexit 0\n")
        self.stub("make", "#!/bin/sh\nexit 0\n")
        self.stub("cc", "#!/bin/sh\nexit 0\n")
        self.stub("python3", '''#!/bin/sh
if [ "$1" = -c ]; then exit "${JIBO_TEST_PREFLIGHT_FAILURE:-0}"; fi
printf '%s\\n' "$0" "$JIBO_DFU_UTIL" "${JIBO_LIBUSB:-}" "${JIBO_SHOFEL2:-}" "$@" >> "$JIBO_TEST_TRACE"
''')

    def stub(self, name, content):
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o755)

    def launch(self, *args):
        return subprocess.run([shutil.which("bash") or "/bin/bash", str(self.repo / "run.sh"), *args],
                              env=self.env, capture_output=True, text=True, timeout=10)

    def test_both_architectures_route_to_mac_source_and_preserve_arguments(self):
        for machine in ("x86_64", "arm64"):
            self.env["JIBO_TEST_ARCH"] = machine
            self.trace.unlink(missing_ok=True)
            result = self.launch("inspect-var", "/tmp/backup with spaces.img")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("RCM-to-DFU entry is available", result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(self.trace.read_text().splitlines(), [
                str(self.bin / "python3"), str(self.bin / "dfu-util"), "", str(self.entry),
                str(self.repo / "jibo_dfu.py"), "inspect-var", "/tmp/backup with spaces.img"])

    def test_explicit_helper_skips_the_entry_build(self):
        self.entry.parent.mkdir(parents=True)
        self.entry.write_text("#!/bin/sh\nexit 0\n")
        self.entry.chmod(0o755)
        result = self.launch("detect")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any("build_file_level_entry" in line
                             for line in self.trace.read_text().splitlines()))

    def test_missing_helper_builds_the_native_entry_pair_then_exports_it(self):
        saved_helper = self.env.pop("JIBO_SHOFEL2")
        self.addCleanup(self.env.update, {"JIBO_SHOFEL2": saved_helper})
        shofel_src = self.repo / ".build" / "ShofEL2-for-T124"
        shofel_src.mkdir(parents=True)
        try:
            result = self.launch("detect")
        finally:
            self.env["JIBO_SHOFEL2"] = saved_helper
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.trace.read_text().splitlines()
        build_index = lines.index(str(self.repo / "scripts" / "build_file_level_entry.py"))
        self.assertEqual(lines[build_index + 1:build_index + 3],
                         ["--source", str(shofel_src)])
        helper = str(self.repo / ".build" / "file-level-entry" / "shofel2_t124")
        exec_index = lines.index(str(self.repo / "jibo_dfu.py"))
        self.assertLess(build_index, exec_index)
        self.assertEqual(lines[exec_index - 1], helper)

    def test_macports_versioned_python_is_preferred_to_system_python(self):
        (self.bin / "python3").rename(self.bin / "python3.12")
        system_bin = self.root / "system"
        system_bin.mkdir()
        system_python = system_bin / "python3"
        system_python.write_text("#!/bin/sh\nexit 72\n")
        system_python.chmod(0o755)
        self.env["PATH"] += os.pathsep + str(system_bin)
        result = self.launch("detect")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.trace.read_text().splitlines()[0], str(self.bin / "python3.12"))

    def test_rosetta_and_dependency_failure_stop_before_opening_the_app(self):
        self.env["JIBO_TEST_TRANSLATED"] = "1"
        result = self.launch("detect")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Rosetta disabled", result.stderr)
        self.assertFalse(self.trace.exists())
        self.env["JIBO_TEST_TRANSLATED"] = "0"
        self.env["JIBO_TEST_PREFLIGHT_FAILURE"] = "1"
        result = self.launch("detect")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Dependency checks failed", result.stderr)
        self.assertFalse(self.trace.exists())

    def test_native_library_is_passed_from_the_selected_prefix(self):
        for relative in ("opt/libusb/lib/libusb-1.0.dylib", "lib/libusb-1.0.dylib"):
            library = self.prefix / relative
            library.parent.mkdir(parents=True, exist_ok=True)
            library.write_bytes(b"fixture")
            self.trace.unlink(missing_ok=True)
            result = self.launch("detect")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.trace.read_text().splitlines()[2], str(library))
            library.unlink()


if __name__ == "__main__":
    unittest.main()
