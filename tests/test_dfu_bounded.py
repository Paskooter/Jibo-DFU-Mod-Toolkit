import ctypes
import tempfile
import unittest
from pathlib import Path

import jibo_dfu_bounded as bounded


class FakeLibusb:
    def __init__(self, *, short_after=None, upload_error=None, upload_exception=None,
                 abort_error=None, state=2, marker_bytes=bounded.MARKER_BYTES):
        self.calls = []
        self.short_after = short_after
        self.upload_error = upload_error
        self.upload_exception = upload_exception
        self.abort_error = abort_error
        self.state = state
        self.marker_bytes = marker_bytes
        self.remaining = marker_bytes

    def libusb_control_transfer(self, _handle, request_type, request, value, interface,
                                buffer, length, _timeout):
        self.calls.append((request_type, request, value, interface, length))
        if request == bounded._DFU_UPLOAD:
            if self.upload_exception is not None:
                raise self.upload_exception
            if self.upload_error is not None:
                return self.upload_error
            actual = min(length, self.remaining)
            if self.short_after is not None:
                actual = min(actual, self.short_after)
            if actual and buffer is not None:
                target = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
                for index in range(actual):
                    target[index] = index & 0xFF
            self.remaining -= actual
            if actual < length:
                self.remaining = self.marker_bytes
            return actual
        if request == bounded._DFU_ABORT:
            return self.abort_error if self.abort_error is not None else 0
        if request == bounded._DFU_GETSTATUS:
            status = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
            status[0] = 0
            status[4] = self.state
            return 6
        raise AssertionError("unexpected USB request {}".format(request))

    @staticmethod
    def libusb_error_name(code):
        return ("FAKE_USB_ERROR_" + str(code)).encode("ascii")


class BoundedUploadTests(unittest.TestCase):
    def test_complete_marker_upload_resets_for_repeat_and_checks_idle(self):
        lib = FakeLibusb()

        result = bounded._upload_bounded(lib, object(), interface=3, transfer_size=4096)
        repeated = bounded._upload_bounded(lib, object(), interface=3, transfer_size=4096)

        self.assertEqual(len(result), bounded.MARKER_BYTES)
        self.assertEqual(result, repeated)
        upload_calls = [call for call in lib.calls if call[1] == bounded._DFU_UPLOAD]
        self.assertEqual([call[2] for call in upload_calls], list(range(5)) * 2)
        self.assertEqual([call[4] for call in upload_calls], [4096] * 10)
        self.assertEqual(lib.calls[-2][1], bounded._DFU_ABORT)
        self.assertEqual(lib.calls[-1][1], bounded._DFU_GETSTATUS)
        self.assertTrue(all(call[3] == 3 for call in lib.calls))

    def test_early_short_upload_is_rejected_but_still_runs_cleanup(self):
        lib = FakeLibusb(short_after=1024)

        with self.assertRaisesRegex(bounded.BoundedDfuError, "expected 17408 bytes"):
            bounded._upload_bounded(lib, object(), interface=1, transfer_size=4096)

        self.assertEqual(len([call for call in lib.calls if call[1] == bounded._DFU_UPLOAD]), 1)
        self.assertEqual([call[1] for call in lib.calls[-2:]], [bounded._DFU_ABORT, bounded._DFU_GETSTATUS])

    def test_marker_that_exceeds_read_limit_is_rejected(self):
        lib = FakeLibusb(marker_bytes=bounded.MAX_UPLOAD_BYTES + 512)

        with self.assertRaisesRegex(bounded.BoundedDfuError, "did not finish"):
            bounded._upload_bounded(lib, object(), interface=1, transfer_size=4096)

        self.assertEqual(len([call for call in lib.calls if call[1] == bounded._DFU_UPLOAD]), 8)
        self.assertEqual([call[1] for call in lib.calls[-2:]], [bounded._DFU_ABORT, bounded._DFU_GETSTATUS])

    def test_upload_failure_still_aborts_and_checks_state(self):
        lib = FakeLibusb(upload_error=-7)

        with self.assertRaisesRegex(bounded.BoundedDfuError, "DFU_UPLOAD failed"):
            bounded._upload_bounded(lib, object(), interface=0, transfer_size=4096)

        self.assertEqual([call[1] for call in lib.calls], [
            bounded._DFU_UPLOAD, bounded._DFU_ABORT, bounded._DFU_GETSTATUS
        ])

    def test_keyboard_interrupt_still_runs_cleanup_then_propagates(self):
        lib = FakeLibusb(upload_exception=KeyboardInterrupt())

        with self.assertRaises(KeyboardInterrupt):
            bounded._upload_bounded(lib, object(), interface=0, transfer_size=4096)

        self.assertEqual([call[1] for call in lib.calls], [
            bounded._DFU_UPLOAD, bounded._DFU_ABORT, bounded._DFU_GETSTATUS
        ])

    def test_cleanup_fails_if_device_does_not_return_to_idle(self):
        lib = FakeLibusb(state=9)

        with self.assertRaisesRegex(bounded.BoundedDfuError, "dfuIDLE"):
            bounded._upload_bounded(lib, object(), interface=0, transfer_size=4096)

    def test_cleanup_reports_abort_error_and_still_checks_state(self):
        lib = FakeLibusb(abort_error=-7)

        with self.assertRaisesRegex(bounded.BoundedDfuError, "DFU_ABORT cleanup failed"):
            bounded._upload_bounded(lib, object(), interface=0, transfer_size=4096)

        self.assertEqual(lib.calls[-1][1], bounded._DFU_GETSTATUS)

    def test_helper_rejects_alternate_names_other_than_marker(self):
        with self.assertRaisesRegex(bounded.BoundedDfuError, "only permits the read-only Jibo marker"):
            bounded.read_dfu_alt_prefix("1-1", alternate="emmc-000", libusb=object())

    def test_sysfs_port_check_requires_exact_jibo_dfu_vid_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            node = Path(directory) / "1-1"
            node.mkdir()
            (node / "idVendor").write_text("0955\n")
            (node / "idProduct").write_text("701a\n")
            bounded._verify_sysfs_device("1-1", directory)

            (node / "idProduct").write_text("7740\n")
            with self.assertRaisesRegex(bounded.BoundedDfuError, "not Jibo DFU"):
                bounded._verify_sysfs_device("1-1", directory)

    def test_port_parser_rejects_paths_and_accepts_nested_usb_path(self):
        self.assertEqual(bounded._parse_port("2-3.1"), (2, (3, 1)))
        for value in ("../1-1", "1-1/../../x", "1-0", "x-1"):
            with self.subTest(value=value), self.assertRaises(bounded.BoundedDfuError):
                bounded._parse_port(value)


if __name__ == "__main__":
    unittest.main()
