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

        def read_sample(executable, port, start, count, destination, timeout, label):
            captured.append((executable, port, start, count, destination, timeout, label))
            destination.write_bytes(b"x" * (8 * 1024 * 1024))

        with patch.object(toolkit, "_shofel_tool", return_value="/opt/shofel/shofel2_t124"), \
                patch.object(toolkit, "devices", return_value=[{"port": "1-2", "state": "rcm"}]), \
                patch.object(toolkit, "_read_shofel_range", side_effect=read_sample), \
                patch.object(toolkit.time, "monotonic", side_effect=(10.0, 12.0)):
            result = toolkit.benchmark_rcm_read(port="1-2")
        self.assertEqual(captured[0][2:4], (0, 16384))
        self.assertFalse(captured[0][4].exists())
        self.assertEqual(result["mib_per_second"], 4.0)
        self.assertTrue(result["sample_removed"])

    def test_shofel_errors_redact_the_raw_chip_id(self):
        detail = toolkit._transfer_error_detail("Chip ID: " + " ".join(
            "0x{:02x}".format(value) for value in CHIP_ID) + "\nUSB transfer failed")
        self.assertIn("Chip ID: [redacted]", detail)
        self.assertNotIn("0x00 0x01", detail)


if __name__ == "__main__":
    unittest.main()
