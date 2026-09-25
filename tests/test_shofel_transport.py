"""Read-only ShofEL transport checks using synthetic GPT and USB output."""
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib
from unittest.mock import patch

import jibo_dfu as toolkit


def make_gpt_prefix(var_size=4096):
    names_and_sizes = (
        ("rootfsA", 1_048_576_000),
        ("rootfsB", 1_048_576_000),
        ("recovery", 52_428_800),
        ("services", 2_097_152_000),
        ("var", var_size),
        ("skills", 10_991_139_328),
    )
    entries = bytearray(128 * 128)
    first_lba = 34
    for index, (name, size) in enumerate(names_and_sizes):
        count = size // 512
        entry = memoryview(entries)[index * 128:(index + 1) * 128]
        entry[:16] = bytes([index + 1]) * 16
        entry[16:32] = bytes([index + 20]) * 16
        struct.pack_into("<QQQ", entry, 32, first_lba, first_lba + count - 1, 0)
        entry[56:128] = name.encode("utf-16le").ljust(72, b"\x00")
        first_lba += count

    data = bytearray(32 * 1024)
    header = memoryview(data)[512:1024]
    header[:8] = b"EFI PART"
    struct.pack_into("<I", header, 8, 0x00010000)
    struct.pack_into("<I", header, 12, 92)
    struct.pack_into("<I", header, 16, 0)
    struct.pack_into("<I", header, 20, 0)
    struct.pack_into("<QQQQ", header, 24, 1, first_lba, 34, first_lba - 1)
    header[56:72] = b"D" * 16
    struct.pack_into("<QIII", header, 72, 2, 128, 128, zlib.crc32(entries) & 0xFFFFFFFF)
    struct.pack_into("<I", header, 16, zlib.crc32(header[:92]) & 0xFFFFFFFF)
    data[1024:1024 + len(entries)] = entries
    return bytes(data)


CHIP_ID = bytes(range(16))
CHIP_ID_OUTPUT = "Chip ID: " + " ".join("0x{:02x}".format(value) for value in CHIP_ID)


class ShofelTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.backups = self.root / "Jibo-Backups"
        self.gpt = make_gpt_prefix()
        self.var = b"V" * 4096
        self.calls = []

    def test_dram_probe_uses_exact_rcm_port_and_excludes_chip_id_from_result(self):
        probe = self.root / "shofel-probe"
        probe.mkdir(exist_ok=True)
        (probe / "dram_probe.bin").write_bytes(b"probe")
        executable = probe / "shofel2_t124"
        executable.write_bytes(b"host")
        output = ("Chip ID: 0x01 0x02\nT124 DRAM/EMC probe (no eMMC access)\n"
                  "  Register preflight: not ready\n"
                  "  DRAM scratch round-trip: skipped; register preflight did not pass\n")
        with patch.object(toolkit, "_shofel_tool", return_value=str(executable)), \
                patch.object(toolkit, "devices", return_value=[{"port": "1-2", "state": "rcm"}]), \
                patch.object(toolkit, "run_with_progress", return_value=output) as run:
            result = toolkit.probe_rcm_dram(port="1-2")
        self.assertEqual(run.call_args.args[0], [str(executable), "--usb-port-path", "1-2", "DRAM_STATUS"])
        self.assertEqual(result["port"], "1-2")
        self.assertNotIn("Chip ID", str(result))
        self.assertIn("Register preflight: not ready", result["details"])

    def test_dram_trace_uses_exact_port_and_only_returns_phases(self):
        directory = self.root / "shofel-trace"
        directory.mkdir()
        (directory / "dram_trace.bin").write_bytes(b"trace")
        executable = directory / "shofel2_t124"
        executable.write_bytes(b"host")
        output = ("Chip ID: 0x01 0x02\n"
                  "DRAM_TRACE phase 0 (payload entry): 0x00000000\n"
                  "DRAM_TRACE phase 15 (complete): 0x00000000\n")
        with patch.object(toolkit, "_shofel_tool", return_value=str(executable)), \
                patch.object(toolkit, "devices", return_value=[{"port": "1-2", "state": "rcm"}]), \
                patch.object(toolkit, "run_with_progress", return_value=output) as run:
            result = toolkit.trace_rcm_dram(port="1-2")
        self.assertEqual(run.call_args.args[0],
                         [str(executable), "--usb-port-path", "1-2", "DRAM_TRACE"])
        self.assertNotIn("Chip ID", str(result))
        self.assertEqual(len(result["phases"]), 2)

    def test_boot0_bct_read_is_bounded_and_does_not_replace_existing_output(self):
        executable = self.root / "shofel2_t124"
        executable.write_bytes(b"host")
        output = self.root / "boot0-prefix.bin"

        def transfer(argv, **_kwargs):
            self.assertEqual(argv, [str(executable), "--usb-port-path", "1-2",
                                    "EMMC_READ_BOOT0_BCT", str(output)])
            output.write_bytes(b"B" * 16_384)
            return "CSD READ_BL_LEN=9 (page size 512 bytes)\nBoot0 prefix read complete"

        with patch.object(toolkit, "_shofel_tool", return_value=str(executable)), \
                patch.object(toolkit, "devices", return_value=[{"port": "1-2", "state": "rcm"}]), \
                patch.object(toolkit, "run_with_progress", side_effect=transfer) as run:
            result = toolkit.read_rcm_boot0_bct(output, port="1-2")
            self.assertEqual(result["size_bytes"], 16_384)
            self.assertEqual(result["read_bl_len_exp"], 9)
            self.assertEqual(result["sha256"], hashlib.sha256(b"B" * 16_384).hexdigest())
            with self.assertRaisesRegex(toolkit.DfuError, "already exists"):
                toolkit.read_rcm_boot0_bct(output, port="1-2")
            self.assertEqual(run.call_count, 1)

    def test_boot0_bct_read_rejects_incomplete_output(self):
        output = self.root / "incomplete-boot0.bin"

        def transfer(_argv, **_kwargs):
            output.write_bytes(b"short")
            return "CSD READ_BL_LEN=9 (page size 512 bytes)"

        with patch.object(toolkit, "_shofel_tool", return_value=str(self.root / "shofel2_t124")), \
                patch.object(toolkit, "devices", return_value=[{"port": "1-2", "state": "rcm"}]), \
                patch.object(toolkit, "run_with_progress", side_effect=transfer):
            with self.assertRaisesRegex(toolkit.DfuError, "wrong size"):
                toolkit.read_rcm_boot0_bct(output, port="1-2")

    def test_boot0_bct_read_requires_page_geometry_report(self):
        output = self.root / "boot0-no-geometry.bin"
        with patch.object(toolkit, "_shofel_tool", return_value=str(self.root / "shofel2_t124")), \
                patch.object(toolkit, "devices", return_value=[{"port": "1-2", "state": "rcm"}]), \
                patch.object(toolkit, "run_with_progress", return_value="Saved 16384 bytes"):
            with self.assertRaisesRegex(toolkit.DfuError, "page geometry"):
                toolkit.read_rcm_boot0_bct(output, port="1-2")

    def test_stage_rcm_dfu_requires_explicit_profile_confirmation_before_usb_access(self):
        with patch.object(toolkit, "_shofel_tool") as locate, \
                patch.object(toolkit, "devices") as connected, \
                patch.object(toolkit, "run_with_progress") as run:
            with self.assertRaisesRegex(toolkit.DfuError, "Confirm the Meerkat Rev02"):
                toolkit.stage_rcm_dfu(port="1-2")
        locate.assert_not_called()
        connected.assert_not_called()
        run.assert_not_called()

    def test_stage_progress_parser_reads_host_byte_count_without_changing_writer_offset(self):
        with tempfile.TemporaryFile() as log:
            log.write(b"Chip ID: [redacted]\rStaged 8192 / 16384 bytes")
            log.flush()
            writer_offset = log.tell()
            self.assertEqual(toolkit._byte_progress_from_log(log), (8192, 16384))
            self.assertEqual(log.tell(), writer_offset)

    def test_stage_rcm_dfu_uses_pinned_default_loader_and_never_launches(self):
        directory = self.root / "shofel-stage"
        directory.mkdir()
        executable = directory / "shofel2_t124"
        executable.write_bytes(b"host")
        (directory / "dfu_stage2.bin").write_bytes(b"stage payload")
        loader = self.root / "signed-loader.bin"
        loader.write_bytes(b"pinned signed loader")
        output = "The SPL was not started; the robot is returning to RCM.\n"
        with patch.object(toolkit, "_shofel_tool", return_value=str(executable)), \
                patch.object(toolkit, "devices", return_value=[{"port": "1-2", "state": "rcm"}]), \
                patch.object(toolkit, "run_with_progress", return_value=output) as run:
            result = toolkit.stage_rcm_dfu(port="1-2", loader=loader,
                                          confirm_meerkat_rev02=True)

        args, kwargs = run.call_args
        self.assertEqual(args[0], [str(executable), "--usb-port-path", "1-2", "DFU_STAGE",
                                   str(loader.resolve()), "--confirm-meerkat-rev02"])
        self.assertNotIn("--launch", args[0])
        self.assertEqual(kwargs["timeout"], 180)
        self.assertEqual(kwargs["cwd"], str(directory))
        self.assertTrue(kwargs["byte_progress"])
        self.assertEqual(result["status"], "loader staged in RAM; not started")
        self.assertEqual(result["port"], "1-2")
        self.assertEqual(result["size_bytes"], len(b"pinned signed loader"))
        self.assertEqual(result["sha256"], hashlib.sha256(b"pinned signed loader").hexdigest())
        self.assertFalse(result["emmc_writes"])
        self.assertFalse(result["started"])

    def test_stage_rcm_dfu_defaults_to_packaged_loader(self):
        directory = self.root / "shofel-stage-default"
        directory.mkdir()
        executable = directory / "shofel2_t124"
        executable.write_bytes(b"host")
        (directory / "dfu_stage2.bin").write_bytes(b"stage payload")
        packaged_loader = toolkit.ROOT / "bundles" / "default" / "loader.bin"
        with patch.object(toolkit, "_shofel_tool", return_value=str(executable)), \
                patch.object(toolkit, "devices", return_value=[{"port": "1-2", "state": "rcm"}]), \
                patch.object(toolkit, "run_with_progress",
                             return_value="The SPL was not started; the robot is returning to RCM.") as run:
            toolkit.stage_rcm_dfu(port="1-2", confirm_meerkat_rev02=True)
        self.assertEqual(run.call_args.args[0][4], str(packaged_loader.resolve()))

    def test_stage_rcm_dfu_rejects_missing_stage_payload_and_non_rcm_device(self):
        directory = self.root / "shofel-stage-incomplete"
        directory.mkdir()
        executable = directory / "shofel2_t124"
        executable.write_bytes(b"host")
        with patch.object(toolkit, "_shofel_tool", return_value=str(executable)), \
                patch.object(toolkit, "devices", return_value=[{"port": "1-2", "state": "rcm"}]), \
                patch.object(toolkit, "run_with_progress") as run:
            with self.assertRaisesRegex(toolkit.DfuError, "dfu_stage2.bin"):
                toolkit.stage_rcm_dfu(port="1-2", confirm_meerkat_rev02=True)
        run.assert_not_called()

        (directory / "dfu_stage2.bin").write_bytes(b"stage payload")
        with patch.object(toolkit, "_shofel_tool", return_value=str(executable)), \
                patch.object(toolkit, "devices", return_value=[{"port": "1-2", "state": "dfu"}]), \
                patch.object(toolkit, "run_with_progress") as run:
            with self.assertRaisesRegex(toolkit.DfuError, "RCM/APX"):
                toolkit.stage_rcm_dfu(port="1-2", confirm_meerkat_rev02=True)
        run.assert_not_called()

    def test_stage_rcm_dfu_requires_host_success_report(self):
        directory = self.root / "shofel-stage-no-confirm"
        directory.mkdir()
        executable = directory / "shofel2_t124"
        executable.write_bytes(b"host")
        (directory / "dfu_stage2.bin").write_bytes(b"stage payload")
        with patch.object(toolkit, "_shofel_tool", return_value=str(executable)), \
                patch.object(toolkit, "devices", return_value=[{"port": "1-2", "state": "rcm"}]), \
                patch.object(toolkit, "run_with_progress", return_value="Transfer completed"):
            with self.assertRaisesRegex(toolkit.DfuError, "did not confirm"):
                toolkit.stage_rcm_dfu(port="1-2", loader=toolkit.ROOT / "bundles/default/loader.bin",
                                      confirm_meerkat_rev02=True)

    def test_shofel_dfu_entry_requires_profile_confirmation_before_usb_access(self):
        with patch.object(toolkit, "_shofel_dfu_tool") as locate, \
                patch.object(toolkit, "devices") as connected:
            with self.assertRaisesRegex(toolkit.DfuError, "Confirm the Meerkat Rev02"):
                toolkit.enter_shofel_dfu(port="1-2")
        locate.assert_not_called()
        connected.assert_not_called()

    def test_shofel_dfu_tool_requires_launch_enabled_host(self):
        directory = self.root / "shofel-launch-gate"
        directory.mkdir()
        executable = directory / "shofel2_t124"
        executable.write_bytes(b"host")
        (directory / "dfu_stage2.bin").write_bytes(b"stage")
        with patch.object(toolkit, "_shofel_tool", return_value=str(executable)), \
                patch.object(toolkit, "run", return_value="dfu-stage-launch=0\n"):
            with self.assertRaisesRegex(toolkit.DfuError, "cannot start"):
                toolkit._shofel_dfu_tool()

    def test_shofel_dfu_entry_launches_then_checks_same_port_marker_and_var(self):
        directory = self.root / "shofel-launch"
        directory.mkdir()
        executable = directory / "shofel2_t124"
        executable.write_bytes(b"host")
        image = self.root / "loader.bin"
        image.write_bytes(b"pinned loader")
        states = [[{"port": "1-2", "state": "rcm"}],
                  [{"port": "1-2", "state": "dfu"}]]
        with patch.object(toolkit, "_shofel_dfu_tool", return_value=str(executable)), \
                patch.object(toolkit, "devices", side_effect=states), \
                patch.object(toolkit, "tool", return_value="dfu-util"), \
                patch.object(toolkit, "run_with_progress",
                             return_value="Starting verified ARM-state SPL at 0x80108000.") as transfer, \
                patch.object(toolkit, "dfu_alternatives",
                             return_value=([toolkit.MARKER, "var"], "")) as alternatives:
            result = toolkit.enter_shofel_dfu(
                port="1-2", loader=image, confirm_meerkat_rev02=True)
        argv = transfer.call_args.args[0]
        self.assertEqual(argv, [str(executable), "--usb-port-path", "1-2", "DFU_STAGE",
                                str(image.resolve()), "--confirm-meerkat-rev02", "--launch"])
        self.assertTrue(transfer.call_args.kwargs["byte_progress"])
        self.assertEqual(transfer.call_args.kwargs["cwd"], str(directory))
        alternatives.assert_called_once_with("dfu-util", "1-2")
        self.assertEqual(result["state"], "dfu")
        self.assertTrue(result["loader_verified"])
        self.assertEqual(result["entry_transport"], "ShofEL")

    def test_shofel_dfu_entry_requires_live_loader_marker_and_var(self):
        with patch.object(toolkit, "_shofel_dfu_tool", return_value="/opt/shofel/shofel2_t124"), \
                patch.object(toolkit, "devices", side_effect=[
                    [{"port": "1-2", "state": "rcm"}], [{"port": "1-2", "state": "dfu"}]]), \
                patch.object(toolkit, "tool", return_value="dfu-util"), \
                patch.object(toolkit, "run_with_progress",
                             return_value="Starting verified ARM-state SPL at 0x80108000."), \
                patch.object(toolkit, "dfu_alternatives", return_value=(["var"], "")):
            with self.assertRaisesRegex(toolkit.DfuError, "missing: jibo-dfu-v1"):
                toolkit.enter_shofel_dfu(confirm_meerkat_rev02=True)

    def test_dfu_gpt_probe_reads_only_32_kib_and_discards_temporary_data(self):
        written = []

        def upload(argv, **_kwargs):
            self.assertEqual(argv[:8], ["dfu-util", "-d", "0955:701a", "--path",
                                        "1-2", "-a", "emmc-000", "-U"])
            self.assertEqual(argv[-2:], ["-Z", "32768"])
            self.assertNotIn("-D", argv)
            destination = Path(argv[8])
            destination.write_bytes(make_gpt_prefix(var_size=toolkit.EXPECTED_VAR_SIZE))
            written.append(destination)
            return "Upload complete"

        with patch.object(toolkit, "_dfu_context",
                          return_value=("1-2", [toolkit.MARKER, "var", "emmc-000"], "device")), \
                patch.object(toolkit, "run_with_progress", side_effect=upload):
            result = toolkit.probe_dfu_gpt(port="1-2", dfu_util="dfu-util")

        self.assertEqual(result["partition_sizes_bytes"]["var"], toolkit.EXPECTED_VAR_SIZE)
        self.assertEqual(len(written), 1)
        self.assertFalse(written[0].exists())
        self.assertTrue(result["temporary_read_removed"])

    def fake_transfer(self, argv, timeout, label, cwd=None, progress_path=None,
                      progress_size=None, *, payload=None, output=CHIP_ID_OUTPUT):
        self.calls.append((argv, timeout, label, cwd))
        self.assertEqual(argv[1:4], ["--usb-port-path", "1-2", "EMMC_READ"])
        self.assertEqual(argv[3], "EMMC_READ")
        start = int(argv[4], 16)
        count = int(argv[5], 16)
        destination = Path(argv[6])
        self.assertEqual(cwd, "/opt/shofel")
        self.assertEqual(Path(progress_path), destination)
        self.assertEqual(progress_size, count * toolkit.EMMC_SECTOR_SIZE)
        self.assertEqual(count * toolkit.EMMC_SECTOR_SIZE,
                         len(self.gpt) if start == 0 else len(self.var))
        destination.write_bytes(self.gpt if start == 0 else (self.var if payload is None else payload))
        return output

    def patches(self, transfer=None, devices=None):
        from contextlib import ExitStack
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(toolkit, "_shofel_tool", return_value="/opt/shofel/shofel2_t124"))
        stack.enter_context(patch.object(toolkit, "devices", return_value=devices or [
            {"port": "1-2", "state": "rcm"}]))
        stack.enter_context(patch.object(toolkit, "BACKUP_ROOT", self.backups))
        stack.enter_context(patch.object(toolkit, "EXPECTED_VAR_SIZE", 4096))
        stack.enter_context(patch.dict(toolkit.updates.KNOWN_CAPACITIES, {"var": 4096}))
        stack.enter_context(patch.object(toolkit, "run_with_progress",
                                         side_effect=transfer or self.fake_transfer))
        return stack

    def test_backup_reads_gpt_then_exact_var_range_and_saves_private_deduplicated_baseline(self):
        with self.patches():
            result = toolkit.backup_var_shofel(port="1-2")

            image = Path(result["image"])
            self.assertEqual(image.read_bytes(), self.var)
            self.assertEqual(image.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.backups.stat().st_mode & 0o777, 0o700)
            self.assertEqual(result["sha256"], hashlib.sha256(self.var).hexdigest())
            manifest = json.loads(Path(result["manifest"]).read_text())
            self.assertEqual(manifest["transport"], "ShofEL2 EMMC_READ (read-only)")
            self.assertEqual(manifest["usb_state"], "RCM/APX (0955:7740)")
            self.assertEqual(manifest["device_tag"],
                             "tegra-chip-id-sha256:" + hashlib.sha256(CHIP_ID).hexdigest())
            self.assertNotIn("0x00 0x01", json.dumps(manifest))
            self.assertEqual([call[0][4:6] for call in self.calls], [["0x0", "0x40"], ["0x7e9022", "0x8"]])
            self.assertTrue(all(call[0][3] == "EMMC_READ" for call in self.calls))

            self.calls.clear()
            reused = toolkit.backup_var_shofel(port="1-2")
            self.assertEqual(reused["status"], "existing verified backup reused")
            self.assertEqual(Path(reused["image"]), image)
            self.assertEqual(len(self.calls), 1, "reuse still validates the live GPT")

    def test_payload_read_error_sentinel_discards_partial_backup(self):
        sentinel = (0xDEAD0005).to_bytes(4, "little") + bytes.fromhex("addeadde") * 1023

        def transfer(argv, timeout, label, cwd=None, **kwargs):
            return self.fake_transfer(argv, timeout, label, cwd, payload=sentinel, **kwargs)

        with self.patches(transfer=transfer):
            with self.assertRaisesRegex(toolkit.DfuError, "eMMC read error"):
                toolkit.backup_var_shofel(port="1-2")
        image_files = list(self.backups.glob("var-backup-*/var.img"))
        self.assertEqual(image_files, [])
        manifest_path = next(self.backups.glob("var-backup-*/backup-manifest.json"))
        record = json.loads(manifest_path.read_text())
        self.assertEqual(record["status"], "failed")
        self.assertNotIn("Chip ID", record["error"])

    def test_short_read_is_rejected_and_partial_file_removed(self):
        def transfer(argv, timeout, label, cwd=None, **kwargs):
            self.calls.append((argv, timeout, label, cwd))
            Path(argv[6]).write_bytes(self.gpt if int(argv[4], 16) == 0 else self.var[:-1])
            return CHIP_ID_OUTPUT

        with self.patches(transfer=transfer):
            with self.assertRaisesRegex(toolkit.DfuError, "expected 4096"):
                toolkit.backup_var_shofel(port="1-2")
        self.assertEqual(list(self.backups.glob("var-backup-*/var.img")), [])

    def test_chip_identity_change_between_gpt_and_var_discards_read(self):
        changed = "Chip ID: " + " ".join("0x{:02x}".format(value) for value in bytes(reversed(range(16))))
        outputs = iter((CHIP_ID_OUTPUT, changed))

        def transfer(argv, timeout, label, cwd=None, **kwargs):
            self.fake_transfer(argv, timeout, label, cwd, **kwargs)
            return next(outputs)

        with self.patches(transfer=transfer):
            with self.assertRaisesRegex(toolkit.DfuError, "device changed"):
                toolkit.backup_var_shofel(port="1-2")
        self.assertEqual(list(self.backups.glob("var-backup-*/var.img")), [])

    def test_existing_output_path_is_never_unlinked_or_overwritten(self):
        existing = self.root / "keep-this-directory"
        existing.mkdir()
        marker = existing / "user-file.bin"
        marker.write_bytes(b"keep me")
        with self.patches():
            with self.assertRaisesRegex(toolkit.DfuError, "already exists"):
                toolkit.backup_var_shofel(port="1-2", out=existing)
        self.assertEqual(marker.read_bytes(), b"keep me")
        self.assertEqual(len(self.calls), 1, "the existing output is rejected before reading var")

    def test_wrong_state_or_multiple_devices_never_runs_shofel(self):
        with self.patches(devices=[{"port": "1-2", "state": "dfu"}]), \
                self.assertRaisesRegex(toolkit.DfuError, "requires.*RCM/APX"):
            toolkit.backup_var_shofel(port="1-2")
        self.assertEqual(self.calls, [])

        with self.patches(devices=[{"port": "1-2", "state": "rcm"},
                                   {"port": "1-3", "state": "rcm"}]), \
                self.assertRaisesRegex(toolkit.DfuError, "Multiple robots"):
            toolkit.backup_var_shofel()
        self.assertEqual(self.calls, [])

    def test_shofel_tool_resolution_requires_adjacent_payload(self):
        folder = self.root / "shofel"
        folder.mkdir()
        executable = folder / "shofel2_t124"
        executable.write_bytes(b"fake executable")
        executable.chmod(0o755)
        payload = folder / "emmc_server.bin"
        payload.write_bytes(b"fake payload")
        intermezzo = folder / "intermezzo.bin"
        intermezzo.write_bytes(b"fake intermezzo")
        self.assertEqual(toolkit._shofel_tool(str(executable)), str(executable.resolve()))

        payload.unlink()
        with self.assertRaisesRegex(toolkit.DfuError, "emmc_server.bin"):
            toolkit._shofel_tool(str(executable))
        payload.write_bytes(b"fake payload")
        intermezzo.unlink()
        with self.assertRaisesRegex(toolkit.DfuError, "intermezzo.bin"):
            toolkit._shofel_tool(str(executable))

    def test_benchmark_reads_eight_mib_and_discards_sample(self):
        captured = []

        def read_sample(executable, port, start, count, destination, timeout, label,
                        include_stats=False):
            self.assertTrue(include_stats)
            captured.append((executable, port, start, count, destination, timeout, label))
            destination.write_bytes(b"x" * (8 * 1024 * 1024))
            return CHIP_ID, 1.0

        with patch.object(toolkit, "_shofel_tool", return_value="/opt/shofel/shofel2_t124"), \
                patch.object(toolkit, "devices", return_value=[{"port": "1-2", "state": "rcm"}]), \
                patch.object(toolkit, "_read_shofel_range", side_effect=read_sample), \
                patch.object(toolkit.time, "monotonic", side_effect=(10.0, 12.0)):
            result = toolkit.benchmark_rcm_read(port="1-2")
        self.assertEqual(captured[0][2:4], (0, 16384))
        self.assertEqual(captured[0][5], 45)
        self.assertFalse(captured[0][4].exists())
        self.assertEqual(result["mib_per_second"], 4.0)
        self.assertEqual(result["transfer_mib_per_second"], 8.0)
        self.assertTrue(result["sample_removed"])

    def test_read_stats_require_matching_byte_count(self):
        destination = self.root / "gpt.img"
        output = CHIP_ID_OUTPUT + "\nREAD_STATS bytes=32768 transfer_seconds=1.250000\n"
        with patch.object(toolkit, "run_with_progress", side_effect=lambda *args, **kwargs:
                          (destination.write_bytes(self.gpt), output)[1]):
            chip_id, seconds = toolkit._read_shofel_range(
                "/opt/shofel/shofel2_t124", "1-2", 0, 64, destination,
                45, "Reading GPT", include_stats=True)
        self.assertEqual(chip_id, CHIP_ID)
        self.assertEqual(seconds, 1.25)

        wrong = CHIP_ID_OUTPUT + "\nREAD_STATS bytes=512 transfer_seconds=1.250000\n"
        with patch.object(toolkit, "run_with_progress", side_effect=lambda *args, **kwargs:
                          (destination.write_bytes(self.gpt), wrong)[1]):
            with self.assertRaisesRegex(toolkit.DfuError, "valid transfer timing"):
                toolkit._read_shofel_range("/opt/shofel/shofel2_t124", "1-2", 0, 64,
                                           destination, 45, "Reading GPT", include_stats=True)
        self.assertFalse(destination.exists())

    def test_eight_bit_benchmark_passes_bus_width_to_shofel(self):
        destination = self.root / "sample.img"
        output = CHIP_ID_OUTPUT + "\nREAD_STATS bytes=32768 transfer_seconds=1.250000\n"
        captured = []

        def transfer(argv, **kwargs):
            captured.append(argv)
            Path(argv[-1]).write_bytes(self.gpt)
            return output

        with patch.object(toolkit, "run_with_progress", side_effect=transfer):
            _, seconds = toolkit._read_shofel_range(
                "/opt/shofel/shofel2_t124", "1-2", 0, 64, destination,
                45, "Reading GPT", include_stats=True, bus_width=8)
        self.assertEqual(captured[0][1:6],
                         ["--usb-port-path", "1-2", "--bus-width", "8", "EMMC_READ"])
        self.assertEqual(seconds, 1.25)

    def test_usb_failure_removes_partial_read_and_requests_rcm_reset(self):
        destination = self.root / "partial.img"

        def fail_transfer(*args, **kwargs):
            destination.write_bytes(b"partial")
            raise toolkit.DfuError("USB receive failed at sector 0")

        with patch.object(toolkit, "run_with_progress", side_effect=fail_transfer):
            with self.assertRaisesRegex(toolkit.DfuError, "Reset the robot into RCM/APX"):
                toolkit._read_shofel_range("/opt/shofel/shofel2_t124", "1-2", 0, 8,
                                           destination, 45, "Reading sample")
        self.assertFalse(destination.exists())

    def test_shofel_errors_redact_the_raw_chip_id(self):
        detail = toolkit._transfer_error_detail("Chip ID: " + " ".join(
            "0x{:02x}".format(value) for value in CHIP_ID) + "\nUSB transfer failed")
        self.assertIn("Chip ID: [redacted]", detail)
        self.assertNotIn("0x00 0x01", detail)


if __name__ == "__main__":
    unittest.main()
