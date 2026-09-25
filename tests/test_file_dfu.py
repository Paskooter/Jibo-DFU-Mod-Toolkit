"""Host-only tests for the experimental generic file mailbox client."""
import hashlib
import io
import struct
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import jibo_dfu as j


class FileDfuTests(unittest.TestCase):
    def test_path_validation_confines_names_to_simple_absolute_paths(self):
        self.assertEqual(j._validate_file_path("/etc/wpa_supplicant.conf"),
                         b"/etc/wpa_supplicant.conf")
        self.assertEqual(j._validate_file_path("/data/a + β"), "/data/a + β".encode("utf-8"))
        for bad in ("relative", "/", "/etc/../passwd", "/etc//shadow", "/etc/a\n"):
            with self.subTest(path=bad), self.assertRaises(j.DfuError):
                j._validate_file_path(bad)

    def test_request_and_response_bind_to_nonce_and_content_hash(self):
        request_id = bytes(range(16))
        precondition = b"p" * j.FILE_WRITE_PRECONDITION.size
        request, returned_id = j._file_request("/jibo/mode.json", j.FILE_RPC_WRITE,
                                              b"{}", request_id, precondition)
        self.assertEqual(returned_id, request_id)
        header = j.FILE_RPC_HEADER.unpack_from(request)
        self.assertEqual(header[:5], (j.FILE_RPC_MAGIC, j.FILE_RPC_WRITE,
                                      len(b"/jibo/mode.json"), 2, request_id))
        self.assertEqual(request[len(j.FILE_RPC_HEADER.pack(*header)):][-2:], b"{}")
        data = b"response"
        response = (j.FILE_RPC_RESPONSE_HEADER.pack(
            j.FILE_RPC_RESPONSE_MAGIC, request_id, 0, len(data), hashlib.sha256(data).digest()) + data)
        self.assertEqual(j._decode_file_response(response, request_id), data)
        with self.assertRaisesRegex(j.DfuError, "does not match"):
            j._decode_file_response(response, b"x" * 16)
        with self.assertRaisesRegex(j.DfuError, "SHA-256"):
            j._decode_file_response(response[:-1] + b"!", request_id)

    def test_write_request_requires_compare_and_write_precondition(self):
        with self.assertRaisesRegex(j.DfuError, "precondition"):
            j._file_request("/jibo/mode.json", j.FILE_RPC_WRITE, b"{}")
        self.assertNotEqual(j.FILE_WRITE_PRECONDITION.size, 0)

    def test_request_transfer_does_not_pass_upload_size_with_download(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(j, "run_with_progress") as run:
                j._file_request_transfer("dfu-util", "1-2", "var", b"req", directory, "test")
            argv = run.call_args.args[0]
            self.assertIn("-D", argv)
            self.assertNotIn("-Z", argv)
            self.assertFalse((Path(directory) / "file-request.bin").exists())

    def test_stat_preflight_requires_regular_file_and_one_small_extent(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(j, "_file_request_transfer"), \
                patch.object(j, "_upload_file_response") as response:
            base = (7, 3, 4096, 0, 0, 0o100600, 1, 1, b"u" * 16)
            response.return_value = j.FILE_STAT_STRUCT.pack(*base)
            self.assertEqual(j._stat_partition_file_rpc(
                "dfu-util", "1-2", "var", "/jibo/mode.json", directory)["allocated_bytes"], 4096)
            for changes, message in (((5, 3, 4096, 0, 0, 0o040755, 1, 1, b"u" * 16),
                                      "regular file"),
                                     ((5, 3, 8192, 0, 0, 0o100600, 1, 1, b"u" * 16),
                                      "regular file"),
                                     ((5, 3, 4096, 0, 0, 0o100600, 1, 2, b"u" * 16),
                                      "one allocated extent")):
                response.return_value = j.FILE_STAT_STRUCT.pack(*changes)
                with self.subTest(changes=changes), self.assertRaisesRegex(j.DfuError, message):
                    j._stat_partition_file_rpc(
                        "dfu-util", "1-2", "var", "/jibo/mode.json", directory)

    def test_bundled_loader_is_capability_gated_before_file_transfer(self):
        with patch.object(j, "_dfu_context",
                          return_value=("1-2", [j.MARKER, "var"], "unknown", "")):
            with self.assertRaisesRegex(j.DfuError, "pinned loader cannot perform file-level"):
                j._file_loader_context("1-2", "dfu-util", ("var",), True)

    def test_unknown_identity_cannot_start_a_file_write(self):
        names = [j.MARKER, "jibo-file-v1", "jibo-file-var-in", "jibo-file-var-out"]
        unknown = "serial-sha256:" + hashlib.sha256(b"UNKNOWN").hexdigest()
        with patch.object(j, "_dfu_context", return_value=("1-2", names, unknown, "")):
            with self.assertRaisesRegex(j.DfuError, "stable eMMC identity"):
                j._file_loader_context("1-2", "dfu-util", ("var",), True)

    def _transaction_patches(self, operation_dir, events, *, confirm=True):
        metadata = {"inode": 12, "size_bytes": 3, "allocated_bytes": 4096,
                    "uid": 0, "gid": 0, "mode": 0o100600, "nlink": 1,
                    "extent_count": 1, "ext4_uuid": "ab" * 16}
        stat_calls = [0]
        def stat(*_args):
            stat_calls[0] += 1
            events.append("stat" if stat_calls[0] == 1 else "post-stat")
            result = metadata.copy()
            if stat_calls[0] > 1:
                result["size_bytes"] = 4
            return result
        def read(*_args):
            events.append("read")
            return b"old"
        def backup(*args):
            events.append("backup:" + args[3])
            return {"image": "/backup/var.img", "sha256": "baseline",
                    "manifest": "/backup/manifest.json", "status": "backup complete"}
        def send(*_args):
            events.append("write")
        def response(*_args):
            events.append("ack")
            return b""
        return (
            patch.object(j, "_file_loader_context", return_value=(
                "1-2", [j.MARKER, "jibo-file-var-in", "jibo-file-var-out", "var"],
                "serial-sha256:robot", "")),
            patch.object(j, "_read_gpt_layout", return_value={
                "var": {"first_lba": 100, "last_lba": 107, "size_bytes": 4096}}),
            patch.object(j, "_stat_partition_file_rpc", side_effect=stat),
            patch.object(j, "_read_partition_file_rpc", side_effect=read),
            patch.object(j, "_partition_file_backup", side_effect=backup),
            patch.object(j, "_file_request_transfer", side_effect=send),
            patch.object(j, "_upload_file_response", side_effect=response),
            patch.object(j, "_new_operation_dir", side_effect=lambda _out, _prefix: (
                operation_dir.mkdir(mode=0o700), operation_dir)[1]),
        )

    def test_transaction_cancels_without_backup_and_only_backups_touched_partitions(self):
        with tempfile.TemporaryDirectory() as temporary:
            operation_dir = Path(temporary) / "operation"
            events = []
            patches = self._transaction_patches(operation_dir, events)
            with patches[0], patches[1], patches[2], patches[3], patches[4] as backup, \
                    patches[5], patches[6], patches[7], redirect_stdout(io.StringIO()):
                transaction = j.FileTransaction(("var", "services"), "1-2", "dfu-util",
                                                operation_dir)
                transaction.replace("var", "/jibo/mode.json", b"new")
                result = transaction.commit(False)
            self.assertEqual(result["status"], "cancelled")
            backup.assert_not_called()
            self.assertEqual(events, ["stat", "read"])

    def test_transaction_backs_up_once_before_write_ack_and_readback(self):
        with tempfile.TemporaryDirectory() as temporary:
            operation_dir = Path(temporary) / "operation"
            events = []
            patches = self._transaction_patches(operation_dir, events)
            with patches[0], patches[1], patches[2], patches[3], patches[4] as backup, \
                    patches[5], patches[6], patches[7], redirect_stdout(io.StringIO()):
                transaction = j.FileTransaction(("var", "services"), "1-2", "dfu-util",
                                                operation_dir)
                transaction.replace("var", "/jibo/mode.json", b"new!")
                # Initial and readback values differ, with stat/read stubs substituted below.
                with patch.object(j, "_read_partition_file_rpc", side_effect=[b"old", b"new!"]):
                    result = transaction.commit(True)
            self.assertEqual(result["status"], "verified")
            backup.assert_called_once()
            self.assertEqual(backup.call_args.args[3], "var")
            self.assertLess(events.index("backup:var"), events.index("write"))
            self.assertLess(events.index("write"), events.index("ack"))
            self.assertLess(events.index("ack"), events.index("post-stat"))
            self.assertEqual(events.count("backup:var"), 1)


if __name__ == "__main__":
    unittest.main()
