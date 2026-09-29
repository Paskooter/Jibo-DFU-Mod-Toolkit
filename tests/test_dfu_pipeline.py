import collections
import ctypes
import errno
import os
import struct
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import jibo_dfu as toolkit
import jibo_dfu_pipeline as pipeline


class FakeUbootDfu:
    """One U-Boot DFU alternative with f_dfu.c and dfu.c request semantics."""

    def __init__(self, content, buffer_size=4096, drain_size=4 * 4096, fail_write_block=None,
                 honor_length=False):
        self.medium = bytearray(content)
        self.buffer_size = buffer_size
        # The pinned loader always reads a full ep0 buffer; dfu-queue.patch honours wLength.
        self.honor_length = honor_length
        self.written_blocks = []
        self.drain_size = drain_size
        self.fail_write_block = fail_write_block
        self.state = 2
        self.status = 0
        self.read_inited = False
        self.write_inited = False
        self.expected_block = 0
        self.offset = 0
        self.pending = bytearray()
        self.requests = []

    def control(self, request_type, request, value, length, payload):
        self.requests.append((request, value, length))
        if request == pipeline._DFU_GETSTATUS:
            if self.state == 3:
                self.state = 5
            elif self.state == 6:
                self.state = 7
            elif self.state == 7:
                self._drain()
                self.write_inited = False
                self.state = 2
            return bytes([self.status, 0, 0, 0, self.state, 0])
        if request == pipeline._DFU_UPLOAD:
            first = self.state == 2
            if first:
                self.state = 9
                value = 0
            elif self.state != 9:
                return self._stall()
            data = self._read(value, min(length, self.buffer_size) if self.honor_length
                              else self.buffer_size)
            if data is None:
                return None
            if len(data) > length:
                raise AssertionError("upload request shorter than the loader's ep0 buffer")
            # f_dfu.c returns to dfuIDLE on a short block only after the first one.
            if len(data) < length and not first:
                self.state = 2
            return data
        if request == pipeline._DFU_DNLOAD:
            if self.state == 2 and length == 0 or self.state not in (2, 5):
                return self._stall()
            if length == 0:
                self.state = 6
                return b""
            self.state = 3
            self._write(value, payload)
            return b""
        if request == pipeline._DFU_ABORT:
            if self.state in (5, 9):
                self.state = 2
            return b""
        if request == pipeline._DFU_CLRSTATUS and self.state == 10:
            self.state = 2
            self.status = 0
            return b""
        return self._stall()

    def _stall(self):
        self.state = 10
        return None

    def _read(self, block, size):
        if not self.read_inited:
            self.read_inited = True
            self.expected_block = 0
            self.offset = 0
        if block != self.expected_block:
            return None
        self.expected_block = (self.expected_block + 1) & 0xFFFF
        data = bytes(self.medium[self.offset:self.offset + size])
        self.offset += len(data)
        if len(data) < size:
            self.read_inited = False
        return data

    def _write(self, block, payload):
        if not self.write_inited:
            self.write_inited = True
            self.expected_block = 0
            self.offset = 0
            self.pending = bytearray()
        if block != self.expected_block or block == self.fail_write_block:
            self.write_inited = False
            self.state = 10
            self.status = 14
            return
        self.expected_block = (self.expected_block + 1) & 0xFFFF
        self.written_blocks.append(block)
        self.pending += payload
        if len(self.pending) >= self.drain_size:
            self._drain()

    def _drain(self):
        self.medium[self.offset:self.offset + len(self.pending)] = self.pending
        self.offset += len(self.pending)
        self.pending = bytearray()


class FakeUsbfs:
    """The usbfs calls the pipeline makes, completing queued URBs in FIFO order."""

    def __init__(self, device, stall_upload_block=None):
        self.device = device
        self.stall_upload_block = stall_upload_block
        self.queue = collections.deque()
        self.cancelled = collections.deque()
        self.retained = []
        self.max_outstanding = 0
        self.dnload_with_queue = 0

    def submit(self, urb):
        setup = ctypes.string_at(urb.buffer, 8)
        if setup[1] == pipeline._DFU_DNLOAD and self.queue:
            self.dnload_with_queue += 1
        self.queue.append(urb)
        self.max_outstanding = max(self.max_outstanding, len(self.queue))

    def discard(self, urb):
        for item in list(self.queue):
            if item is urb:
                self.queue.remove(item)
                item.status = -errno.ENOENT
                self.cancelled.append(item)

    def reap(self):
        if self.cancelled:
            return ctypes.addressof(self.cancelled.popleft())
        if not self.queue:
            return None
        urb = self.queue.popleft()
        raw = ctypes.string_at(urb.buffer, urb.buffer_length)
        request_type, request, value, _index, length = pipeline._SETUP.unpack(raw[:8])
        if request == pipeline._DFU_UPLOAD and value == self.stall_upload_block:
            response = None
            self.device.state = 10
        else:
            response = self.device.control(request_type, request, value, length, raw[8:])
        if response is None:
            urb.status = -errno.EPIPE
            urb.actual_length = 0
        else:
            urb.status = 0
            if request_type & 0x80:
                ctypes.memmove(urb.buffer + 8, response, len(response))
                urb.actual_length = len(response)
            else:
                urb.actual_length = length
        return ctypes.addressof(urb)

    def wait(self, _seconds):
        pass

    def retain(self, item):
        self.retained.append(item)

    def control(self, request_type, request, value, index, length=0, data=b"", label=""):
        response = self.device.control(request_type, request, value, length, data)
        if response is None:
            raise pipeline.DfuTransferError(label + ": stalled")
        return response if request_type & 0x80 else len(data)

    def idle(self):
        return not self.queue and not self.cancelled and not self.retained


def pattern(size):
    return bytes((index * 7 + index // 251) & 0xFF for index in range(size))


class PipelinedUploadTests(unittest.TestCase):
    def upload(self, device, size, depth=8, lib=None):
        lib = lib or FakeUsbfs(device)
        received = bytearray()
        count = pipeline._upload_stream(lib, 0, 4096, size,
                                        received.extend, depth)
        return lib, count, bytes(received)

    def test_reads_partial_final_block_with_bounded_queue(self):
        content = pattern(3 * 4096 + 100)
        device = FakeUbootDfu(content)

        lib, count, received = self.upload(device, len(content), depth=2)

        self.assertEqual(count, len(content))
        self.assertEqual(received, content)
        self.assertEqual([value for request, value, _length in device.requests], [0, 1, 2, 3])
        self.assertEqual(lib.max_outstanding, 2)
        self.assertEqual(device.state, 2)
        self.assertFalse(device.read_inited)
        self.assertTrue(lib.idle())

    def test_exact_multiple_ends_with_one_empty_block_and_resets_cursor(self):
        content = pattern(5 * 4096)
        device = FakeUbootDfu(content)

        _lib, first_count, first = self.upload(device, len(content))
        _lib, second_count, second = self.upload(device, len(content))

        self.assertEqual((first_count, second_count), (len(content), len(content)))
        self.assertEqual(first, content)
        self.assertEqual(second, content)
        self.assertEqual(len(device.requests), 12)

    def test_block_numbers_roll_over_like_dfu_util(self):
        content = pattern(65538 * 64 + 17)
        device = FakeUbootDfu(content, buffer_size=64)
        lib = FakeUsbfs(device)
        received = bytearray()

        count = pipeline._upload_stream(lib, 0, 64, len(content),
                                        received.extend, 32)

        self.assertEqual(count, len(content))
        self.assertEqual(bytes(received), content)
        self.assertEqual(device.requests[65536][1], 0)

    def test_larger_alternative_fails_without_requesting_past_expected_size(self):
        device = FakeUbootDfu(pattern(8 * 4096))
        lib = FakeUsbfs(device)

        with self.assertRaisesRegex(pipeline.DfuTransferError, "block 2 returned 4096 bytes"):
            pipeline._upload_stream(lib, 0, 4096, 2 * 4096,
                                    bytearray().extend, 16)

        self.assertEqual(len(device.requests), 3)
        self.assertTrue(lib.idle())

    def test_shorter_alternative_is_reported(self):
        device = FakeUbootDfu(pattern(4096 + 10))
        lib = FakeUsbfs(device)

        with self.assertRaisesRegex(pipeline.DfuTransferError, "block 1 returned 10 bytes"):
            pipeline._upload_stream(lib, 0, 4096, 3 * 4096,
                                    bytearray().extend, 1)

    def test_stall_cancels_queued_requests_before_freeing(self):
        device = FakeUbootDfu(pattern(20 * 4096))
        lib = FakeUsbfs(device, stall_upload_block=3)

        with self.assertRaisesRegex(pipeline.DfuTransferError, "block 3 stalled"):
            pipeline._upload_stream(lib, 0, 4096, 20 * 4096,
                                    bytearray().extend, 8)

        self.assertTrue(lib.idle())

    def test_interrupt_cancels_queued_requests(self):
        device = FakeUbootDfu(pattern(20 * 4096))
        lib = FakeUsbfs(device)
        received = []

        def sink(data):
            received.append(data)
            if len(received) == 4:
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            pipeline._upload_stream(lib, 0, 4096, 20 * 4096, sink, 8)

        self.assertTrue(lib.idle())
        self.assertLessEqual(len(device.requests), 4 + 8)


class PipelinedDownloadTests(unittest.TestCase):
    def download(self, device, content):
        lib = FakeUsbfs(device)
        with tempfile.TemporaryFile() as stream:
            stream.write(content)
            stream.seek(0)
            sent = pipeline._download_stream(lib, 0, 4096, stream,
                                             len(content))
        return lib, sent

    def test_writes_each_block_then_manifests_and_confirms_flush(self):
        content = pattern(6 * 4096 + 300)
        device = FakeUbootDfu(bytes(len(content)))

        lib, sent = self.download(device, content)

        self.assertEqual(sent, len(content))
        self.assertEqual(bytes(device.medium), content)
        self.assertEqual(device.state, 2)
        expected = []
        for block in range(7):
            expected += [(pipeline._DFU_DNLOAD, block), (pipeline._DFU_GETSTATUS, 0)]
        expected += [(pipeline._DFU_DNLOAD, 7)] + [(pipeline._DFU_GETSTATUS, 0)] * 3
        self.assertEqual([(request, value) for request, value, _length in device.requests],
                         expected)
        self.assertEqual(lib.dnload_with_queue, 0)
        self.assertEqual(lib.max_outstanding, 2)
        self.assertTrue(lib.idle())

    def test_write_error_stops_before_the_next_block(self):
        content = pattern(6 * 4096)
        device = FakeUbootDfu(bytes(len(content)), fail_write_block=2)

        with self.assertRaisesRegex(pipeline.DfuTransferError,
                                    "block 2 reported status 14 in state 10 after 8192 bytes"):
            self.download(device, content)

        blocks = [value for request, value, _length in device.requests
                  if request == pipeline._DFU_DNLOAD]
        self.assertEqual(blocks, [0, 1, 2])

    def test_truncated_image_is_rejected(self):
        device = FakeUbootDfu(bytes(3 * 4096))
        lib = FakeUsbfs(device)
        with tempfile.TemporaryFile() as stream:
            stream.write(pattern(4096))
            stream.seek(0)
            with self.assertRaisesRegex(pipeline.DfuTransferError, "ended after 4096 of 12288"):
                pipeline._download_stream(lib, 0, 4096, stream,
                                          3 * 4096)
        self.assertTrue(lib.idle())


class QueueLoaderTests(unittest.TestCase):
    def test_queued_writes_keep_block_order_and_manifest(self):
        content = pattern(9 * 32768 + 512)
        device = FakeUbootDfu(bytes(len(content)), buffer_size=32768, drain_size=4 * 32768,
                              honor_length=True)
        lib = FakeUsbfs(device)
        with tempfile.TemporaryFile() as stream:
            stream.write(content)
            stream.seek(0)
            sent = pipeline._download_stream(lib, 0, 32768, stream,
                                             len(content), depth=4)

        self.assertEqual(sent, len(content))
        self.assertEqual(bytes(device.medium), content)
        self.assertEqual(device.written_blocks, list(range(10)))
        self.assertEqual(lib.max_outstanding, 8)
        self.assertGreater(lib.dnload_with_queue, 0)
        self.assertEqual(device.state, 2)
        self.assertTrue(lib.idle())

    def test_queued_write_error_writes_nothing_after_the_failed_block(self):
        content = pattern(8 * 32768)
        device = FakeUbootDfu(bytes(len(content)), buffer_size=32768, fail_write_block=2,
                              honor_length=True)
        lib = FakeUsbfs(device)
        with tempfile.TemporaryFile() as stream:
            stream.write(content)
            stream.seek(0)
            with self.assertRaisesRegex(pipeline.DfuTransferError, "block 2 reported status 14"):
                pipeline._download_stream(lib, 0, 32768, stream,
                                          len(content), depth=4)

        self.assertEqual(device.written_blocks, [0, 1])
        self.assertTrue(lib.idle())

    def test_queue_loader_reads_requested_block_size(self):
        content = pattern(5 * 8192 + 100)
        device = FakeUbootDfu(content, buffer_size=32768, honor_length=True)
        lib = FakeUsbfs(device)
        received = bytearray()

        count = pipeline._upload_stream(lib, 0, 8192, len(content),
                                        received.extend, 8)

        self.assertEqual((count, bytes(received)), (len(content), content))
        self.assertEqual([length for _request, _value, length in device.requests], [8192] * 6)

    def test_single_block_alternative_is_aborted_back_to_idle(self):
        marker = pattern(34 * 512)
        device = FakeUbootDfu(marker, buffer_size=32768, honor_length=True)
        lib = FakeUsbfs(device)
        received = bytearray()

        count = pipeline._upload_stream(lib, 0, 32768, len(marker), received.extend, 4)
        self.assertEqual(device.state, 9)
        pipeline._finish_upload(lib, 0)

        self.assertEqual((count, bytes(received)), (len(marker), marker))
        self.assertEqual(device.state, 2)
        self.assertEqual([request for request, _value, _length in device.requests][-3:],
                         [pipeline._DFU_GETSTATUS, pipeline._DFU_ABORT, pipeline._DFU_GETSTATUS])

    def session(self, queue_capable, transfer_size):
        session = pipeline._Session("1-1", "var")
        session.queue_capable = queue_capable
        session.transfer_size = transfer_size
        return session

    def test_pinned_loader_keeps_advertised_size_and_single_write(self):
        session = self.session(False, 4096)

        self.assertEqual(session.block_size(), 4096)
        self.assertEqual(session.write_depth(), 1)
        self.assertEqual(session.write_depth(8), 1)
        with self.assertRaisesRegex(pipeline.DfuTransferError, "needs 4096-byte"):
            session.block_size(8192)

    def test_queue_loader_accepts_smaller_power_of_two_blocks(self):
        session = self.session(True, 32768)

        self.assertEqual(session.block_size(), 32768)
        self.assertEqual(session.block_size(8192), 8192)
        self.assertEqual(session.write_depth(), pipeline.DEFAULT_WRITE_DEPTH)
        for size in (2048, 12288, 65536):
            with self.assertRaisesRegex(pipeline.DfuTransferError, "power of two"):
                session.block_size(size)


class FakeDeviceNode(FakeUsbfs):
    """A usbfs node that also serves descriptors, strings, and interface calls."""

    def __init__(self, names, transfer_size=32768, vendor=0x0955):
        super().__init__(FakeUbootDfu(bytes(4096)))
        self.names = names
        self.transfer_size = transfer_size
        self.vendor = vendor
        self.calls = []

    def descriptors(self):
        device = struct.pack("<BBHBBBBHHHBBBB", 18, 1, 0x0200, 0, 0, 0, 64, self.vendor,
                             0x701A, 0x0100, 1, 2, 3, 1)
        interfaces = b"".join(struct.pack("<9B", 9, 4, 0, setting, 0, 0xFE, 1, 2, 4 + setting)
                              for setting in range(len(self.names)))
        config = struct.pack("<BBHBBBBB", 9, 2, 9 + len(interfaces), 1, 1, 0, 0xC0, 1)
        return device + config + interfaces

    def control(self, request_type, request, value, index, length=0, data=b"", label=""):
        if request == 0x06 and value >> 8 == 3:
            if value & 0xFF == 0:
                return bytes([4, 3, 0x09, 0x04])
            name = self.names[(value & 0xFF) - 4].encode("utf-16-le")
            return bytes([2 + len(name), 3]) + name
        if request == 0x06 and value >> 8 == 0x21:
            return bytes([9, 0x21, 0x0F, 0, 0, self.transfer_size & 0xFF,
                          self.transfer_size >> 8, 0x10, 0x01])
        return super().control(request_type, request, value, index, length, data, label)

    def claim(self, interface):
        self.calls.append(("claim", interface))

    def set_interface(self, interface, setting):
        self.calls.append(("alt", interface, setting))

    def release(self, interface):
        self.calls.append(("release", interface))

    def close(self):
        self.calls.append(("close",))


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.sysfs = Path(self.temp.name)
        node = self.sysfs / "1-1"
        node.mkdir()
        for name, value in (("idVendor", "0955"), ("idProduct", "701a"), ("busnum", "1"),
                            ("devnum", "5"), ("bConfigurationValue", "1")):
            (node / name).write_text(value + "\n")
        self.opened = []

    def tearDown(self):
        self.temp.cleanup()

    def open_session(self, node, alternate="var"):
        def opener(path):
            self.opened.append(path)
            return node
        return pipeline._Session("1-1", alternate, sysfs_root=self.sysfs,
                                 usb_root=Path("/dev/bus/usb"), opener=opener)

    def test_selects_named_setting_and_detects_queue_loader(self):
        node = FakeDeviceNode(["jibo-dfu-v1", "var", pipeline.QUEUE_MARKER])

        with self.open_session(node) as session:
            self.assertTrue(session.queue_capable)
            self.assertEqual((session.interface, session.transfer_size), (0, 32768))

        self.assertEqual(self.opened, [Path("/dev/bus/usb/001/005")])
        self.assertEqual(node.calls, [("claim", 0), ("alt", 0, 1), ("release", 0), ("close",)])

    def test_pinned_loader_is_not_queue_capable(self):
        node = FakeDeviceNode(["jibo-dfu-v1", "var"], transfer_size=4096)

        with self.open_session(node) as session:
            self.assertFalse(session.queue_capable)
            self.assertEqual(session.transfer_size, 4096)

    def test_missing_or_repeated_alternate_closes_the_node(self):
        for names in (["jibo-dfu-v1"], ["var", "var"]):
            node = FakeDeviceNode(names)
            with self.assertRaisesRegex(pipeline.DfuTransferError, "alternate 'var'"):
                with self.open_session(node):
                    pass
            self.assertEqual(node.calls, [("close",)])

    def test_other_usb_device_is_rejected(self):
        node = FakeDeviceNode(["var"], vendor=0x1234)

        with self.assertRaisesRegex(pipeline.DfuTransferError, "not Jibo DFU"):
            with self.open_session(node):
                pass
        self.assertEqual(node.calls, [("close",)])


class TransferCommandTests(unittest.TestCase):
    def test_partition_transfers_use_pipelined_helper_by_default(self):
        with patch.dict(os.environ, {}, clear=False), \
                patch.object(toolkit, "_usbfs_available", return_value=True):
            os.environ.pop("JIBO_DFU_TRANSFER", None)
            read = toolkit._partition_transfer_argv("dfu-util", "1-1", "var", "-U",
                                                    Path("/tmp/var.img"), 524288000)
            write = toolkit._partition_transfer_argv("dfu-util", "1-1", "var", "-D",
                                                     Path("/tmp/var.img"))
        helper = str(toolkit.PIPELINE_HELPER)
        self.assertEqual(read[1:], [helper, "-d", "0955:701a", "--path", "1-1", "-a", "var",
                                    "-U", "/tmp/var.img", "-Z", "524288000"])
        self.assertEqual(write[1:], [helper, "-d", "0955:701a", "--path", "1-1", "-a", "var",
                                     "-D", "/tmp/var.img"])

    def test_prefix_read_covers_the_whole_alternative_only_in_the_helper(self):
        with patch.dict(os.environ, {"JIBO_DFU_TRANSFER": "pipelined"}), \
                patch.object(toolkit, "_usbfs_available", return_value=True):
            helper = toolkit._partition_transfer_argv("dfu-util", "1-1", "rootfsA", "-U",
                                                      Path("/tmp/a.img"), 838860800, 1048576000)
        with patch.dict(os.environ, {"JIBO_DFU_TRANSFER": "dfu-util"}):
            fallback = toolkit._partition_transfer_argv("dfu-util", "1-1", "rootfsA", "-U",
                                                        Path("/tmp/a.img"), 838860800, 1048576000)
        self.assertEqual(helper[-4:], ["-Z", "1048576000", "--prefix", "838860800"])
        self.assertEqual(fallback[-2:], ["-Z", "838860800"])

    def test_prefix_sink_keeps_only_leading_bytes(self):
        kept = bytearray()
        sink = pipeline._prefix_sink(kept.extend, 5000)
        for block in (b"a" * 4096, b"b" * 4096, b"c" * 4096):
            sink(block)
        self.assertEqual(bytes(kept), b"a" * 4096 + b"b" * 904)

    def test_environment_selects_dfu_util(self):
        with patch.dict(os.environ, {"JIBO_DFU_TRANSFER": "dfu-util"}):
            argv = toolkit._partition_transfer_argv("dfu-util", "1-1", "var", "-U",
                                                    Path("/tmp/var.img"), 4096)
        self.assertEqual(argv, ["dfu-util", "-d", "0955:701a", "--path", "1-1", "-a", "var",
                                "-U", "/tmp/var.img", "-Z", "4096"])

    def test_missing_usbfs_falls_back_to_dfu_util(self):
        with patch.dict(os.environ, {"JIBO_DFU_TRANSFER": "pipelined"}), \
                patch.object(toolkit, "_usbfs_available", return_value=False):
            argv = toolkit._partition_transfer_argv("dfu-util", "1-1", "var", "-D",
                                                    Path("/tmp/var.img"))
        self.assertEqual(argv[0], "dfu-util")

    def test_unknown_transfer_mode_is_rejected(self):
        with patch.dict(os.environ, {"JIBO_DFU_TRANSFER": "fast"}):
            with self.assertRaisesRegex(toolkit.DfuError, "JIBO_DFU_TRANSFER"):
                toolkit._partition_transfer_argv("dfu-util", "1-1", "var", "-D",
                                                 Path("/tmp/var.img"))


if __name__ == "__main__":
    unittest.main()
