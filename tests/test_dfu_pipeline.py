import collections
from contextlib import redirect_stderr, redirect_stdout
import ctypes
import errno
import hashlib
import io
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

    def __init__(self, device, stall_upload_block=None, timeout_upload_block=None,
                 timeout_status=-errno.ETIME, timeout_consumes=True, upload_status_block=None,
                 upload_status=None):
        self.device = device
        self.stall_upload_block = stall_upload_block
        # A USB-link timeout: usbfs hands the URB back with -ETIME or -ETIMEDOUT. With
        # timeout_consumes the loader served the request and advanced its read cursor,
        # but the reply never reached the host.
        self.timeout_upload_block = timeout_upload_block
        self.timeout_status = timeout_status
        self.timeout_consumes = timeout_consumes
        self.upload_status_block = upload_status_block
        self.upload_status = upload_status
        self.queue = collections.deque()
        self.cancelled = collections.deque()
        self.retained = []
        self.max_outstanding = 0
        self.dnload_with_queue = 0
        # A request the host never hears back about (the pipeline's own deadline).
        self.hang_upload_block = None
        self.clock = 0.0

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
        if self.hang_upload_block is not None:
            head = pipeline._SETUP.unpack(ctypes.string_at(self.queue[0].buffer, 8))
            if head[1] == pipeline._DFU_UPLOAD and head[2] == self.hang_upload_block:
                return None
        urb = self.queue.popleft()
        raw = ctypes.string_at(urb.buffer, urb.buffer_length)
        request_type, request, value, _index, length = pipeline._SETUP.unpack(raw[:8])
        if request == pipeline._DFU_UPLOAD and value == self.timeout_upload_block:
            if self.timeout_consumes:
                self.device.control(request_type, request, value, length, raw[8:])
            urb.status = self.timeout_status
            urb.actual_length = 0
            return ctypes.addressof(urb)
        if request == pipeline._DFU_UPLOAD and value == self.upload_status_block:
            urb.status = self.upload_status
            urb.actual_length = 0
            return ctypes.addressof(urb)
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

    def wait(self, seconds):
        self.clock += seconds

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

    def __init__(self, names, transfer_size=32768, vendor=0x0955, device=None, **usbfs):
        super().__init__(device or FakeUbootDfu(bytes(4096)), **usbfs)
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

    def test_slow_read_depth_only_reaches_pipelined_reads(self):
        with patch.dict(os.environ, {"JIBO_DFU_TRANSFER": "pipelined"}), \
                patch.object(toolkit, "_usbfs_available", return_value=True):
            read = toolkit._partition_transfer_argv("dfu-util", "1-1", "rootfsA", "-U",
                                                    Path("/tmp/a.img"), 8192, depth=1)
            write = toolkit._partition_transfer_argv("dfu-util", "1-1", "rootfsA", "-D",
                                                     Path("/tmp/a.img"), depth=1)
        with patch.dict(os.environ, {"JIBO_DFU_TRANSFER": "dfu-util"}):
            fallback = toolkit._partition_transfer_argv("dfu-util", "1-1", "rootfsA", "-U",
                                                        Path("/tmp/a.img"), 8192, depth=1)
        self.assertEqual(read[-4:], ["-Z", "8192", "--depth", "1"])
        self.assertNotIn("--depth", write)
        self.assertNotIn("--depth", fallback)


BLOCK = 4096
CONTENT_SIZE = 40 * BLOCK + 100


class LinkTimeoutTests(unittest.TestCase):
    """A USB-link timeout is named and is not confused with stalls or short data."""

    def test_urb_timeout_at_block_5_names_a_usb_link_timeout(self):
        for status in (-errno.ETIME, -errno.ETIMEDOUT):
            with self.subTest(status=status):
                device = FakeUbootDfu(pattern(CONTENT_SIZE))
                lib = FakeUsbfs(device, timeout_upload_block=5, timeout_status=status)

                with self.assertRaises(pipeline.UsbLinkTimeout) as caught:
                    pipeline._upload_stream(lib, 0, BLOCK, CONTENT_SIZE, bytearray().extend, 16)

                error = caught.exception
                self.assertEqual((error.block, error.depth, error.block_size, error.received),
                                 (5, 16, BLOCK, 5 * BLOCK))
                message = str(error)
                self.assertIn("USB-link timeout at DFU_UPLOAD block 5", message)
                self.assertIn("16 requests queued", message)
                self.assertIn("4096-byte blocks", message)
                self.assertIn(os.strerror(-status), message)
                self.assertNotIn("interrupted earlier read", message)
                self.assertEqual(error.undrained, 0)
                self.assertTrue(lib.idle())

    def test_no_reply_within_the_transfer_timeout_is_a_link_timeout(self):
        device = FakeUbootDfu(pattern(CONTENT_SIZE))
        lib = FakeUsbfs(device)
        lib.hang_upload_block = 5

        with patch.object(pipeline.time, "monotonic", lambda: lib.clock), \
                self.assertRaises(pipeline.UsbLinkTimeout) as caught:
            pipeline._upload_stream(lib, 0, BLOCK, CONTENT_SIZE, bytearray().extend, 16)

        self.assertEqual(caught.exception.block, 5)
        self.assertIn("no reply within 20 s", str(caught.exception))
        self.assertTrue(lib.idle())

    def test_stall_disconnect_and_short_data_are_not_link_timeouts(self):
        cases = (
            (FakeUsbfs(FakeUbootDfu(pattern(CONTENT_SIZE)), stall_upload_block=3),
             CONTENT_SIZE, "block 3 stalled"),
            (FakeUsbfs(FakeUbootDfu(pattern(CONTENT_SIZE)), upload_status_block=3,
                       upload_status=-errno.ENODEV), CONTENT_SIZE, "block 3 device disconnected"),
            (FakeUsbfs(FakeUbootDfu(pattern(BLOCK + 10))), 3 * BLOCK, "block 1 returned 10 bytes"),
        )
        for lib, size, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(pipeline.DfuTransferError, expected) as caught:
                    pipeline._upload_stream(lib, 0, BLOCK, size, bytearray().extend, 16)
                self.assertNotIsInstance(caught.exception, pipeline.UsbLinkTimeout)
                self.assertNotIn("USB-link timeout", str(caught.exception))

    def test_wrapped_stale_cursor_is_refused_by_the_length_check(self):
        # The loader's cursor wraps every 65536 blocks. If the lost reply was block 65535
        # its next expected block number is 0 again, so it would accept a fresh read and
        # serve data from the middle of the partition. The byte count must refuse it.
        block, size = 64, 65536 * 64 + 1000
        device = FakeUbootDfu(pattern(size), buffer_size=block)
        lib = FakeUsbfs(device, timeout_upload_block=65535)
        with self.assertRaises(pipeline.UsbLinkTimeout):
            pipeline._upload_stream(lib, 0, block, size, bytearray().extend, 16)
        pipeline._ensure_idle(lib, 0)
        self.assertEqual((device.state, device.read_inited, device.expected_block), (2, True, 0))

        received = bytearray()
        with self.assertRaisesRegex(pipeline.DfuTransferError,
                                    "block 15 returned 40 bytes after 960 bytes") as caught:
            pipeline._upload_stream(FakeUsbfs(device), 0, block, size, received.extend, 1)
        self.assertNotIsInstance(caught.exception, pipeline.UsbLinkTimeout)


class LinkTimeoutUploadTests(unittest.TestCase):
    """upload() after a link timeout: the loader's cursor and the session boundary."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        node = self.root / "sys" / "1-1"
        node.mkdir(parents=True)
        for name, value in (("idVendor", "0955"), ("idProduct", "701a"), ("busnum", "1"),
                            ("devnum", "5"), ("bConfigurationValue", "1")):
            (node / name).write_text(value + "\n")
        self.content = pattern(CONTENT_SIZE)

    def tearDown(self):
        self.temp.cleanup()

    def upload(self, node, depth, name="rootfsA.img"):
        destination = self.root / name
        original = pipeline._Session

        def session(port, alternate):
            return original(port, alternate, sysfs_root=self.root / "sys",
                            usb_root=Path("/dev/bus/usb"), opener=lambda _path: node)
        with patch.object(pipeline, "_Session", session):
            result = pipeline.upload("1-1", "rootfsA", destination, CONTENT_SIZE, depth)
        return destination, result

    def node(self, device, **usbfs):
        return FakeDeviceNode(["jibo-dfu-v1", "rootfsA"], transfer_size=BLOCK, device=device,
                              **usbfs)

    def test_timeout_leaves_dfu_idle_with_a_stale_cursor_that_is_refused(self):
        loader = FakeUbootDfu(self.content)
        destination = self.root / "rootfsA.img"

        with self.assertRaises(pipeline.UsbLinkTimeout) as caught:
            self.upload(self.node(loader, timeout_upload_block=5), 16)
        message = str(caught.exception)
        self.assertIn("rootfsA", message)
        self.assertIn("The loader reports dfuIDLE", message)
        self.assertIn("until Jibo re-enters DFU", message)
        self.assertFalse(destination.exists())
        # DFU_ABORT returned the loader to dfuIDLE but did not reset its read cursor.
        self.assertEqual((loader.state, loader.read_inited, loader.expected_block), (2, True, 6))

        # The same loader session (USB reopened, robot not restarted) must refuse a re-read.
        with self.assertRaisesRegex(pipeline.DfuTransferError, "block 0 stalled") as caught:
            self.upload(self.node(loader), 1)
        self.assertNotIsInstance(caught.exception, pipeline.UsbLinkTimeout)
        self.assertFalse(destination.exists())

    def test_slower_read_after_reentering_dfu_yields_exact_bytes(self):
        with self.assertRaises(pipeline.UsbLinkTimeout):
            self.upload(self.node(FakeUbootDfu(self.content), timeout_upload_block=5), 16)

        fresh = self.node(FakeUbootDfu(self.content))
        destination, (count, block_size) = self.upload(fresh, 1)

        self.assertEqual((count, block_size), (CONTENT_SIZE, BLOCK))
        self.assertEqual(destination.read_bytes(), self.content)
        self.assertEqual(fresh.max_outstanding, 1)

    def test_repeated_timeout_at_depth_1_fails_cleanly_and_deletes_the_file(self):
        node = self.node(FakeUbootDfu(self.content), timeout_upload_block=5)

        with self.assertRaises(pipeline.UsbLinkTimeout) as caught:
            self.upload(node, 1)

        self.assertEqual((caught.exception.block, caught.exception.depth), (5, 1))
        self.assertIn("1 request queued", str(caught.exception))
        self.assertFalse((self.root / "rootfsA.img").exists())
        self.assertTrue(node.idle())


class ToolkitSlowReadTests(unittest.TestCase):
    """jibo_dfu arranges one depth-1 read of a timed-out alternative after DFU re-entry."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        node = self.root / "sys" / "1-1"
        node.mkdir(parents=True)
        for name, value in (("idVendor", "0955"), ("idProduct", "701a"), ("busnum", "1"),
                            ("devnum", "5"), ("bConfigurationValue", "1")):
            (node / name).write_text(value + "\n")
        self.content = pattern(CONTENT_SIZE)
        self.nodes = {}
        self.argvs = []
        for item in (patch.object(toolkit, "BACKUP_ROOT", self.root / "Jibo-Backups"),
                     patch.object(toolkit, "_usbfs_available", return_value=True),
                     patch.object(toolkit, "run_with_progress", self.run_helper),
                     patch.dict(os.environ, {"JIBO_DFU_TRANSFER": "pipelined"})):
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.temp.cleanup)

    def run_helper(self, argv, timeout, label, **_kwargs):
        """Run the real helper in-process and fail the way run_with_progress does."""
        self.argvs.append(argv)
        self.assertEqual(argv[1], str(toolkit.PIPELINE_HELPER))
        original = pipeline._Session

        def session(port, alternate):
            return original(port, alternate, sysfs_root=self.root / "sys",
                            usb_root=Path("/dev/bus/usb"), opener=lambda _path: self.nodes[alternate])
        output = io.StringIO()
        with patch.object(pipeline, "_Session", session), redirect_stdout(output), \
                redirect_stderr(output):
            code = pipeline.main(argv[2:])
        if code:
            raise toolkit.DfuError("Command failed: {}\n{}".format(
                Path(argv[1]).name, toolkit._transfer_error_detail(output.getvalue())))
        return output.getvalue()

    def attach(self, device, name="rootfsA", **usbfs):
        self.nodes[name] = FakeDeviceNode(["jibo-dfu-v1", name], transfer_size=BLOCK,
                                          device=device, **usbfs)
        return self.nodes[name]

    def read(self, name="rootfsA"):
        destination = self.root / (name + ".img")
        digest = toolkit._upload_partition("dfu-util", "1-1", name, CONTENT_SIZE, destination)
        return destination, digest

    def depth_of_last_read(self):
        argv = self.argvs[-1]
        return int(argv[argv.index("--depth") + 1]) if "--depth" in argv else None

    def test_link_timeout_arranges_one_slower_read_after_reentering_dfu(self):
        loader = FakeUbootDfu(self.content)
        self.attach(loader, timeout_upload_block=5)
        with self.assertRaises(toolkit.UsbLinkTimeoutError) as caught:
            self.read()
        message = str(caught.exception)
        for text in ("rootfsA", "USB-link timeout at DFU_UPLOAD block 5", "16 requests queued",
                     "4096-byte blocks", "partial file was deleted", "RCM",
                     "Enter DFU with ShofEL", "one request at a time"):
            self.assertIn(text, message)
        self.assertIsNone(self.depth_of_last_read())
        self.assertFalse((self.root / "rootfsA.img").exists())

        # Without re-entering DFU the loader's stale cursor refuses the read; the slower
        # read stays arranged for the next session.
        self.attach(loader)
        with self.assertRaisesRegex(toolkit.DfuError, "block 0 stalled") as caught:
            self.read()
        self.assertNotIsInstance(caught.exception, toolkit.UsbLinkTimeoutError)
        self.assertEqual(self.depth_of_last_read(), 1)
        self.assertFalse((self.root / "rootfsA.img").exists())

        fresh = self.attach(FakeUbootDfu(self.content))
        destination, digest = self.read()
        self.assertEqual(self.depth_of_last_read(), 1)
        self.assertEqual(fresh.max_outstanding, 1)
        self.assertEqual(destination.read_bytes(), self.content)
        self.assertEqual(digest, hashlib.sha256(self.content).hexdigest())

        self.attach(FakeUbootDfu(self.content))
        self.read()
        self.assertIsNone(self.depth_of_last_read())

    def test_repeated_timeout_at_depth_1_fails_cleanly_without_another_retry(self):
        self.attach(FakeUbootDfu(self.content), timeout_upload_block=5)
        with self.assertRaises(toolkit.UsbLinkTimeoutError):
            self.read()

        self.attach(FakeUbootDfu(self.content), timeout_upload_block=5,
                    timeout_status=-errno.ETIMEDOUT)
        with self.assertRaises(toolkit.UsbLinkTimeoutError) as caught:
            self.read()
        message = str(caught.exception)
        self.assertEqual(self.depth_of_last_read(), 1)
        self.assertIn("timed out again", message)
        self.assertIn("will not retry", message)
        self.assertIn("1 request queued", message)
        self.assertFalse((self.root / "rootfsA.img").exists())

        self.attach(FakeUbootDfu(self.content))
        self.read()
        self.assertIsNone(self.depth_of_last_read())

    def test_stall_disconnect_and_short_data_do_not_arrange_a_slower_read(self):
        for usbfs, content in (({"stall_upload_block": 3}, self.content),
                               ({"upload_status_block": 3, "upload_status": -errno.ENODEV},
                                self.content),
                               ({}, self.content[:BLOCK + 10])):
            with self.subTest(usbfs=usbfs):
                self.attach(FakeUbootDfu(content), **usbfs)
                with self.assertRaises(toolkit.DfuError) as caught:
                    self.read()
                self.assertNotIsInstance(caught.exception, toolkit.UsbLinkTimeoutError)
                self.assertFalse((self.root / "rootfsA.img").exists())
                self.attach(FakeUbootDfu(self.content))
                self.read()
                self.assertIsNone(self.depth_of_last_read())

    def test_skills_chunk_timeout_slows_only_that_chunk(self):
        chunks = [{"name": "skills-001", "offset_bytes": 0, "size_bytes": CONTENT_SIZE},
                  {"name": "skills-002", "offset_bytes": CONTENT_SIZE, "size_bytes": CONTENT_SIZE}]
        second = pattern(2 * CONTENT_SIZE)[CONTENT_SIZE:]
        destination = self.root / "skills.img"
        self.attach(FakeUbootDfu(self.content), "skills-001")
        self.attach(FakeUbootDfu(second), "skills-002", timeout_upload_block=5)
        with self.assertRaises(toolkit.UsbLinkTimeoutError) as caught:
            toolkit._upload_skills_partition("dfu-util", "1-1", 2 * CONTENT_SIZE, chunks,
                                             destination, self.root)
        self.assertIn("skills-002", str(caught.exception))
        self.assertFalse(destination.exists())

        self.argvs.clear()
        self.attach(FakeUbootDfu(self.content), "skills-001")
        self.attach(FakeUbootDfu(second), "skills-002")
        result = toolkit._upload_skills_partition("dfu-util", "1-1", 2 * CONTENT_SIZE, chunks,
                                                  destination, self.root)
        self.assertEqual(["--depth" in argv for argv in self.argvs], [False, True])
        self.assertEqual(destination.read_bytes(), self.content + second)
        self.assertEqual(result["sha256"], hashlib.sha256(self.content + second).hexdigest())


if __name__ == "__main__":
    unittest.main()
