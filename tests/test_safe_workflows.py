import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import jibo_dfu as j
import jibo_images as images


class SafeWorkflowTests(unittest.TestCase):
    def test_guided_menu_requires_terminal_instead_of_numbered_fallback(self):
        display = io.StringIO()
        with patch("jibo_tui.run", return_value=None), patch.object(j.sys, "stderr", display):
            self.assertEqual(j._launch_menu(), 2)
        self.assertIn("interactive terminal", display.getvalue())

    def test_guided_write_confirmation_accepts_callback_and_cancellation(self):
        plan = {"partition": "var", "operation": "set mode to developer"}
        received = []
        self.assertTrue(j._confirm_write(lambda value: received.append(value) or True, plan))
        self.assertEqual(received, [plan])
        self.assertFalse(j._confirm_write(lambda _value: False, plan))

    def test_partition_read_shows_progress_and_completion(self):
        progress = io.StringIO()
        with patch.object(j.sys, "stderr", progress):
            output = j.run_with_progress(
                [sys.executable, "-c", "print('transfer output')"],
                timeout=5, label="Reading sample partition")
        self.assertIn("transfer output", output)
        self.assertIn("Reading sample partition...", progress.getvalue())
        self.assertIn("Read sample partition in", progress.getvalue())

    def test_dfu_download_shows_byte_progress_in_terminal(self):
        class TerminalOutput(io.StringIO):
            def isatty(self):
                return True

        progress = TerminalOutput()
        command = ("import sys,time; "
                   "sys.stderr.write('Download [==========]  50%     52428800 bytes\\r'); "
                   "sys.stderr.flush(); time.sleep(0.4)")
        with patch.object(j.sys, "stderr", progress):
            j.run_with_progress([sys.executable, "-c", command], timeout=5,
                                label="Writing sample partition", download_size=104_857_600)
        self.assertIn("50.0% | 50.0/100.0 MiB", progress.getvalue())

    def test_progress_tracks_private_temporary_output(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "var.img"
            temporary = Path(directory) / "var.img.tmp.ABC123"
            temporary.write_bytes(b"partial")
            self.assertEqual(j._transfer_output_size(target), 7)
            target.write_bytes(b"complete")
            self.assertEqual(j._transfer_output_size(target), 8)

    def test_failed_partition_read_removes_partial_dump(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "current-var.img"

            def failed_transfer(argv, timeout, label, **kwargs):
                self.assertEqual(kwargs["progress_path"], destination)
                self.assertEqual(kwargs["progress_size"], j.EXPECTED_VAR_SIZE)
                Path(argv[-1]).write_bytes(b"partial dump")
                raise j.DfuError("simulated transfer failure")

            with patch.object(j, "run_with_progress", side_effect=failed_transfer):
                with self.assertRaisesRegex(j.DfuError, "simulated transfer failure"):
                    j._upload_var("dfu-util", "1-1", destination)
            self.assertFalse(destination.exists())

    def test_failed_partition_read_removes_partial_dump(self):
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp) / "current-var.img"

            def failed_transfer(argv, timeout, label, **kwargs):
                self.assertEqual(kwargs["progress_path"], destination)
                self.assertEqual(kwargs["progress_size"], j.EXPECTED_VAR_SIZE)
                Path(argv[-1]).write_bytes(b"partial dump")
                raise j.DfuError("simulated transfer failure")

            with patch.object(j, "run_with_progress", side_effect=failed_transfer):
                with self.assertRaisesRegex(j.DfuError, "simulated transfer failure"):
                    j._upload_var("dfu-util", "1-1", destination)
            self.assertFalse(destination.exists())

    def test_ext4_failure_includes_read_only_check_report(self):
        failure = SimpleNamespace(returncode=4, stdout="Free blocks count wrong (12, counted=11).\n",
                                  stderr="")
        with patch.object(images.shutil, "which", return_value="/sbin/e2fsck"), \
                patch.object(images.subprocess, "run", return_value=failure):
            with self.assertRaisesRegex(images.ImageError, "exit code 4") as caught:
                images._check_ext4("not-opened-by-the-mock.img")
        self.assertIn("Free blocks count wrong", str(caught.exception))

    def test_pending_journal_is_replayed_only_on_working_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "original.img"
            edited = Path(temp) / "working.img"
            source.write_bytes(b"original capture")
            repaired = SimpleNamespace(returncode=1, stdout="journal replayed", stderr="")
            with patch.object(images.shutil, "which", return_value="/sbin/e2fsck"), \
                    patch.object(images.subprocess, "run", return_value=repaired) as fsck, \
                    patch.object(images, "_check_ext4"):
                _, copied, journal_replayed = images._copy_for_edit(source, edited)
            self.assertTrue(journal_replayed)
            self.assertEqual(copied.read_bytes(), b"original capture")
            self.assertEqual(source.read_bytes(), b"original capture")
            self.assertEqual(fsck.call_args.args[0][-1], str(edited))
            self.assertIn("-p", fsck.call_args.args[0])

    def test_ext4_mode_restoration_keeps_regular_file_type_bits(self):
        stat = "Inode: 21 Type: regular Mode: 0600 Flags: 0x0\nUser: 0 Group: 0\n"
        with patch.object(images, "_run_debugfs", return_value=stat):
            metadata = images._file_metadata("image.img", "/jibo/mode.json")
        self.assertEqual(metadata["mode"], "0100600")

    def test_existing_device_backup_is_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            backup_root = root / "Jibo-Backups"
            backup_dir = backup_root / "var-backup-original"
            backup_dir.mkdir(parents=True)
            original = b"saved-baseline"
            original_hash = hashlib.sha256(original).hexdigest()
            image_path = backup_dir / "var.img"
            image_path.write_bytes(original)
            tag = "serial-sha256:device-one"
            (backup_dir / "backup-manifest.json").write_text(json.dumps({
                "schema": 1, "kind": "jibo-var-backup", "image": "var.img",
                "size_bytes": len(original), "sha256": original_hash,
                "device_tag": tag, "created_utc": "2026-09-22T00:00:00Z"}))
            operation_dir = root / "set-mode"
            operation_dir.mkdir()
            current = b"current-state"
            current_hash = hashlib.sha256(current).hexdigest()

            def upload(_tool, _port, destination):
                Path(destination).write_bytes(current)
                return current_hash

            with patch.object(j, "EXPECTED_VAR_SIZE", len(original)), \
                    patch.object(j, "BACKUP_ROOT", backup_root), \
                    patch.object(j, "_upload_var", side_effect=upload):
                state = j._prepare_current_and_baseline("dfu-util", "1-1", tag, operation_dir)

            self.assertEqual(state["baseline"], image_path)
            self.assertEqual(state["baseline_sha256"], original_hash)
            self.assertFalse(state["baseline_matches_current"])
            self.assertEqual(len(list(backup_root.glob("var-backup-*"))), 1)

    def test_prewrite_failure_keeps_baseline_and_only_a_small_record(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            operation_dir = root / "set-mode"
            operation_dir.mkdir()
            baseline = root / "baseline.img"
            baseline.write_bytes(b"the one saved baseline")
            current = operation_dir / "current-var.img"

            def prepare(_tool, _port, _tag, _directory):
                current.write_bytes(b"temporary current dump")
                return {"before": current, "before_sha256": "current-hash",
                        "baseline": baseline, "baseline_sha256": "baseline-hash"}

            with patch.object(j, "_dfu_context", return_value=("1-1", ["var"], "device")), \
                    patch.object(j, "_new_operation_dir", return_value=operation_dir), \
                patch.object(j, "_prepare_current_and_baseline", side_effect=prepare), \
                    patch.object(j.images, "edit_mode", side_effect=images.ImageError("bad ext4 image")), \
                    patch.object(j, "_write_candidate") as write:
                with self.assertRaisesRegex(j.DfuError, "No partition write was attempted") as caught:
                    j.set_mode_live("developer", dfu_util="mock-dfu-util")

            write.assert_not_called()
            self.assertTrue(baseline.is_file())
            self.assertFalse(current.exists())
            self.assertIn(str(baseline), str(caught.exception))
            manifest = json.loads((operation_dir / "write-manifest.json").read_text())
            self.assertEqual(manifest["status"], "failed before write")
            self.assertEqual({item.name for item in operation_dir.iterdir()}, {"write-manifest.json"})


if __name__ == "__main__":
    unittest.main()
