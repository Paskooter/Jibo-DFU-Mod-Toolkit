#!/usr/bin/env python3
"""Pipelined DFU partition reads and writes for the Jibo RAM loader.

dfu-util sends one control transfer at a time and waits for it to finish
before it sends the next. Each wait costs a full host round trip, which over
USB/IP (WSL with usbipd-win) limits a 500 MiB var read to about 4.5 MB/s.

Reads keep several DFU_UPLOAD requests queued on endpoint 0. The host
controller still runs them one after another, so the loader sees the same
numbered request sequence that dfu-util sends, without the idle gaps.

Writes send each DFU_DNLOAD together with its DFU_GETSTATUS. The pinned loader's
ci_udc driver handles a new SETUP before it retires the previous status stage,
so a DFU_DNLOAD arriving right behind a DFU_GETSTATUS could be completed, and
written, before its data had arrived. Against that loader the next block waits
for the previous status. A loader built with firmware/dfu-queue.patch fixes
the driver, accepts 32 KiB blocks of any requested length, and lists the
read-only jibo-dfu-queue-v1 alternate; only then are several writes queued.

Transfers go straight to Linux usbfs. libusb's Linux backend refuses control
transfers larger than 4 KiB, while usbfs accepts them when they are queued.

The command line accepts the subset of dfu-util options the toolkit uses for
partition transfers and prints dfu-util's progress and completion lines.
"""

from __future__ import annotations

import argparse
import collections
import ctypes
import errno
import os
from pathlib import Path
import select
import signal
import struct
import sys
import time

import jibo_dfu_bounded as bounded


DEFAULT_UPLOAD_DEPTH = 16
MAX_UPLOAD_DEPTH = 64
DEFAULT_WRITE_DEPTH = 8
MIN_BLOCK_SIZE = 4096
QUEUE_MARKER = "jibo-dfu-queue-v1"
TRANSFER_TIMEOUT_SECONDS = 20.0
CANCEL_GRACE_SECONDS = 10.0
MANIFEST_TIMEOUT_SECONDS = 30.0
PROGRESS_STEP = 1 << 20

_REQUEST_OUT = 0x21
_REQUEST_IN = 0xA1
_DFU_DNLOAD = 0x01
_DFU_UPLOAD = 0x02
_DFU_GETSTATUS = 0x03
_DFU_CLRSTATUS = 0x04
_DFU_ABORT = 0x06
_DFU_CAN_DNLOAD = 0x01
_DFU_CAN_UPLOAD = 0x02
_STATE_IDLE = 2
_STATE_DNLOAD_IDLE = 5
_STATE_MANIFEST_SYNC = 6
_STATE_MANIFEST = 7
_STATE_UPLOAD_IDLE = 9
_STATE_ERROR = 10
_STATUS_LENGTH = 6
_SETUP = struct.Struct("<BBHHH")

# linux/usbdevice_fs.h on x86_64
_USBDEVFS_CONTROL = 0xC0185500
_USBDEVFS_SETINTERFACE = 0x80085504
_USBDEVFS_SUBMITURB = 0x8038550A
_USBDEVFS_DISCARDURB = 0x0000550B
_USBDEVFS_REAPURBNDELAY = 0x4008550D
_USBDEVFS_CLAIMINTERFACE = 0x8004550F
_USBDEVFS_RELEASEINTERFACE = 0x80045510
_USBDEVFS_URB_TYPE_CONTROL = 2
_URB_STATUS_NAMES = {
    -errno.EPIPE: "stalled",
    -errno.ENOENT: "cancelled",
    -errno.ECONNRESET: "cancelled",
    -errno.ENODEV: "device disconnected",
    -errno.ESHUTDOWN: "device disconnected",
    -errno.EOVERFLOW: "overflow",
}


class DfuTransferError(RuntimeError):
    """A pipelined DFU transfer failed or returned unexpected data."""


class _Urb(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_ubyte),
        ("endpoint", ctypes.c_ubyte),
        ("status", ctypes.c_int),
        ("flags", ctypes.c_uint),
        ("buffer", ctypes.c_void_p),
        ("buffer_length", ctypes.c_int),
        ("actual_length", ctypes.c_int),
        ("start_frame", ctypes.c_int),
        ("number_of_packets", ctypes.c_int),
        ("error_count", ctypes.c_int),
        ("signr", ctypes.c_uint),
        ("usercontext", ctypes.c_void_p),
    ]


class _CtrlTransfer(ctypes.Structure):
    _fields_ = [
        ("bRequestType", ctypes.c_uint8),
        ("bRequest", ctypes.c_uint8),
        ("wValue", ctypes.c_uint16),
        ("wIndex", ctypes.c_uint16),
        ("wLength", ctypes.c_uint16),
        ("timeout", ctypes.c_uint32),
        ("data", ctypes.c_void_p),
    ]


class _SetInterface(ctypes.Structure):
    _fields_ = [("interface", ctypes.c_uint), ("altsetting", ctypes.c_uint)]


_ioctl = None


def _libc_ioctl():
    global _ioctl
    if _ioctl is None:
        function = ctypes.CDLL(None, use_errno=True).ioctl
        function.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
        function.restype = ctypes.c_int
        _ioctl = function
    return _ioctl


class _Usbfs:
    """One Linux usbfs device node with synchronous and queued control transfers."""

    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
        self._poll = select.poll()
        self._poll.register(self.fd, select.POLLOUT)
        # Buffers of transfers the kernel never returned stay alive until close().
        self._retained = []

    def _call(self, request, argument, label, allowed=()):
        if _libc_ioctl()(self.fd, request, argument) >= 0:
            return 0
        error = ctypes.get_errno()
        if error in allowed:
            return error
        raise DfuTransferError("{}: {}".format(label, os.strerror(error)))

    def descriptors(self):
        """Return the device descriptor followed by every configuration descriptor."""
        data = bytearray()
        while True:
            chunk = os.read(self.fd, 65536)
            if not chunk:
                return bytes(data)
            data += chunk

    def claim(self, interface):
        number = ctypes.c_uint(interface)
        self._call(_USBDEVFS_CLAIMINTERFACE, ctypes.addressof(number),
                   "Could not claim the DFU interface; close any other DFU tool")

    def release(self, interface):
        number = ctypes.c_uint(interface)
        self._call(_USBDEVFS_RELEASEINTERFACE, ctypes.addressof(number),
                   "Could not release the DFU interface")

    def set_interface(self, interface, setting):
        request = _SetInterface(interface, setting)
        self._call(_USBDEVFS_SETINTERFACE, ctypes.addressof(request),
                   "Could not select the DFU alternate setting")

    def control(self, request_type, request, value, index, length=0, data=b"",
                label="USB control transfer failed"):
        """Run one small control transfer; return IN bytes or the OUT byte count."""
        size = length if request_type & 0x80 else len(data)
        buffer = (ctypes.c_ubyte * max(size, 1))()
        if data:
            ctypes.memmove(buffer, data, len(data))
        transfer = _CtrlTransfer(request_type, request, value, index, size,
                                 int(TRANSFER_TIMEOUT_SECONDS * 1000),
                                 ctypes.addressof(buffer) if size else None)
        count = _libc_ioctl()(self.fd, _USBDEVFS_CONTROL, ctypes.addressof(transfer))
        if count < 0:
            raise DfuTransferError("{}: {}".format(label, os.strerror(ctypes.get_errno())))
        return bytes(buffer[:count]) if request_type & 0x80 else count

    def submit(self, urb):
        self._call(_USBDEVFS_SUBMITURB, ctypes.addressof(urb),
                   "Could not queue a DFU control transfer")

    def discard(self, urb):
        # EINVAL means the transfer already completed and waits to be reaped.
        self._call(_USBDEVFS_DISCARDURB, ctypes.addressof(urb),
                   "Could not cancel a DFU control transfer", (errno.EINVAL,))

    def reap(self):
        """Return the address of one completed transfer, or None if none is ready."""
        pointer = ctypes.c_void_p()
        if self._call(_USBDEVFS_REAPURBNDELAY, ctypes.addressof(pointer),
                      "Could not collect a DFU control transfer", (errno.EAGAIN,)):
            return None
        return pointer.value

    def wait(self, seconds):
        self._poll.poll(int(seconds * 1000))

    def retain(self, item):
        self._retained.append(item)

    def close(self):
        # Closing the node makes the kernel cancel and wait for queued transfers.
        os.close(self.fd)
        self._retained.clear()


class _ControlPipeline:
    """Queue control transfers on endpoint 0 and retire them in order."""

    def __init__(self, device, depth, data_size, timeout=TRANSFER_TIMEOUT_SECONDS):
        self.device = device
        self.timeout = timeout
        self._done = [False] * depth
        self._urbs = []
        self._buffers = []
        self._slots = {}
        self._free = collections.deque(range(depth))
        self._queued = collections.deque()
        for slot in range(depth):
            buffer = (ctypes.c_ubyte * (_SETUP.size + data_size))()
            urb = _Urb()
            urb.type = _USBDEVFS_URB_TYPE_CONTROL
            urb.buffer = ctypes.addressof(buffer)
            self._urbs.append(urb)
            self._buffers.append(buffer)
            self._slots[ctypes.addressof(urb)] = slot

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is not None:
            self.cancel_all()
        return False

    @property
    def can_submit(self):
        return bool(self._free)

    def submit(self, request_type, request, value, index, length, payload=b""):
        slot = self._free.popleft()
        urb, buffer = self._urbs[slot], self._buffers[slot]
        _SETUP.pack_into(buffer, 0, request_type, request, value & 0xFFFF, index, length)
        if payload:
            ctypes.memmove(ctypes.addressof(buffer) + _SETUP.size, payload, len(payload))
        urb.buffer_length = _SETUP.size + length
        urb.status = 0
        urb.actual_length = 0
        self._done[slot] = False
        try:
            self.device.submit(urb)
        except BaseException:
            self._free.appendleft(slot)
            raise
        self._queued.append(slot)

    def _collect(self):
        address = self.device.reap()
        if address is None:
            return False
        slot = self._slots.get(address)
        if slot is None:
            raise DfuTransferError("usbfs returned a transfer this pipeline did not queue.")
        self._done[slot] = True
        return True

    def retire(self, label):
        """Wait for the oldest queued transfer and return its data stage bytes."""
        slot = self._queued[0]
        deadline = time.monotonic() + self.timeout
        while not self._done[slot]:
            if not self._collect():
                if time.monotonic() > deadline:
                    raise DfuTransferError("{} timed out.".format(label))
                self.device.wait(0.1)
        self._queued.popleft()
        self._free.append(slot)
        urb = self._urbs[slot]
        if urb.status:
            name = _URB_STATUS_NAMES.get(urb.status, os.strerror(-urb.status))
            raise DfuTransferError("{} {}.".format(label, name))
        return ctypes.string_at(ctypes.addressof(self._buffers[slot]) + _SETUP.size,
                                urb.actual_length)

    def cancel_all(self):
        """Cancel queued transfers and wait for the kernel to hand each one back."""
        try:
            for slot in self._queued:
                if not self._done[slot]:
                    self.device.discard(self._urbs[slot])
            deadline = time.monotonic() + CANCEL_GRACE_SECONDS
            while any(not self._done[slot] for slot in self._queued) and \
                    time.monotonic() < deadline:
                if not self._collect():
                    self.device.wait(0.1)
        except DfuTransferError:
            pass
        if any(not self._done[slot] for slot in self._queued):
            self.device.retain(self)
        self._free.extend(slot for slot in self._queued if self._done[slot])
        self._queued.clear()


def _get_status(device, interface):
    return _parse_status(device.control(_REQUEST_IN, _DFU_GETSTATUS, 0, interface,
                                        _STATUS_LENGTH, label="DFU_GETSTATUS failed"))


def _parse_status(data):
    if len(data) != _STATUS_LENGTH:
        raise DfuTransferError("DFU_GETSTATUS returned {} bytes; expected 6.".format(len(data)))
    return data[0], data[4], data[1] | (data[2] << 8) | (data[3] << 16)


def _ensure_idle(device, interface):
    """Leave dfuIDLE with status OK, as dfu-util does before a transfer."""
    status, state, _poll = _get_status(device, interface)
    if state == _STATE_ERROR:
        device.control(_REQUEST_OUT, _DFU_CLRSTATUS, 0, interface, label="DFU_CLRSTATUS failed")
    elif state in (_STATE_DNLOAD_IDLE, _STATE_UPLOAD_IDLE):
        device.control(_REQUEST_OUT, _DFU_ABORT, 0, interface, label="DFU_ABORT failed")
    elif state != _STATE_IDLE:
        raise DfuTransferError("The DFU interface is in state {}, not dfuIDLE.".format(state))
    status, state, _poll = _get_status(device, interface)
    if status != 0 or state != _STATE_IDLE:
        raise DfuTransferError("The DFU interface reports status {} in state {}; "
                               "expected status 0 in dfuIDLE (2).".format(status, state))


def _recovery_note(device, interface):
    try:
        _ensure_idle(device, interface)
    except Exception as exc:
        return " DFU cleanup also failed: {}".format(exc)
    return ""


def _functional_descriptor(device, interface):
    descriptor = device.control(0x81, 0x06, 0x21 << 8, interface, 9,
                                label="Could not read the DFU functional descriptor")
    if len(descriptor) < 9 or descriptor[0] != 9 or descriptor[1] != 0x21:
        raise DfuTransferError("The DFU functional descriptor is missing or truncated.")
    transfer_size = descriptor[5] | (descriptor[6] << 8)
    if transfer_size < 1:
        raise DfuTransferError("The DFU descriptor reports a zero transfer size.")
    return descriptor[2], transfer_size


def _active_configuration(descriptors, value):
    """Return (vendor, product, configuration descriptor) for the active configuration."""
    if len(descriptors) < 18 or descriptors[0] != 18 or descriptors[1] != 1:
        raise DfuTransferError("The USB device descriptor is missing or malformed.")
    vendor, product = struct.unpack_from("<HH", descriptors, 8)
    offset = 18
    for _index in range(descriptors[17]):
        if offset + 9 > len(descriptors) or descriptors[offset + 1] != 2:
            raise DfuTransferError("The USB configuration descriptors are truncated.")
        total = struct.unpack_from("<H", descriptors, offset + 2)[0]
        config = descriptors[offset:offset + total]
        if total < 9 or len(config) != total:
            raise DfuTransferError("The USB configuration descriptors are truncated.")
        if config[5] == value:
            return vendor, product, config
        offset += total
    raise DfuTransferError("The active USB configuration {} was not found.".format(value))


def _dfu_settings(config):
    """Yield (interface, alternate setting, name string index) for DFU-mode settings."""
    offset = config[0]
    while offset + 2 <= len(config):
        length, kind = config[offset], config[offset + 1]
        if length < 2 or offset + length > len(config):
            raise DfuTransferError("A USB configuration descriptor is malformed.")
        if kind == 4 and length >= 9 and config[offset + 8] and \
                tuple(config[offset + 5:offset + 8]) == bounded.DFU_INTERFACE:
            yield config[offset + 2], config[offset + 3], config[offset + 8]
        offset += length


def _string_descriptor(device, index, language):
    data = device.control(0x80, 0x06, 0x0300 | index, language, 255,
                          label="Could not read a USB string descriptor")
    if len(data) < 2 or data[1] != 3 or data[0] > len(data):
        raise DfuTransferError("A USB string descriptor is malformed.")
    return data[2:data[0]]


def _upload_stream(device, interface, transfer_size, expected_size, sink,
                   depth=DEFAULT_UPLOAD_DEPTH, progress=None):
    """Read exactly expected_size bytes with up to depth DFU_UPLOAD requests queued.

    The loader ends an upload with its first short block. Knowing the size
    lets every request be queued in advance without asking for data past the
    end, which would start a new upload of the same alternative.
    """
    if not 1 <= depth <= MAX_UPLOAD_DEPTH:
        raise DfuTransferError("Upload depth must be between 1 and {}.".format(MAX_UPLOAD_DEPTH))
    if expected_size < 0:
        raise DfuTransferError("The expected upload size must not be negative.")
    requests = expected_size // transfer_size + 1
    final_length = expected_size % transfer_size
    received = 0
    with _ControlPipeline(device, min(depth, requests), transfer_size) as pipeline:
        queued = 0
        for block in range(requests):
            while queued < requests and pipeline.can_submit:
                pipeline.submit(_REQUEST_IN, _DFU_UPLOAD, queued, interface, transfer_size)
                queued += 1
            try:
                data = pipeline.retire("DFU_UPLOAD block {}".format(block))
            except DfuTransferError as exc:
                if block == 0:
                    raise DfuTransferError(str(exc) + " An interrupted earlier read of this "
                                           "alternative can cause this; re-enter DFU and "
                                           "retry.") from exc
                raise
            wanted = transfer_size if block < requests - 1 else final_length
            if len(data) != wanted:
                raise DfuTransferError(
                    "DFU_UPLOAD block {} returned {} bytes after {} bytes; expected {} bytes "
                    "for a {}-byte alternative.".format(
                        block, len(data), received, wanted, expected_size))
            if data:
                sink(data)
            received += len(data)
            if progress:
                progress(received)
    return received


def _finish_upload(device, interface):
    """Confirm dfuIDLE after the final short block.

    This U-Boot returns to dfuIDLE on a short block only after the first one.
    When the whole alternative fits in the first block the read cursor is
    already reset but the state stays dfuUPLOAD-IDLE, so abort that state.
    """
    status, state, _poll = _get_status(device, interface)
    if status == 0 and state == _STATE_UPLOAD_IDLE:
        device.control(_REQUEST_OUT, _DFU_ABORT, 0, interface, label="DFU_ABORT failed")
        status, state, _poll = _get_status(device, interface)
    if status != 0 or state != _STATE_IDLE:
        raise DfuTransferError("After the upload the DFU interface reports status {} "
                               "in state {}.".format(status, state))


def _download_stream(device, interface, transfer_size, source, total_size,
                     progress=None, depth=1):
    """Write source in transfer_size blocks, then complete DFU manifestation.

    Each DFU_DNLOAD is queued with its DFU_GETSTATUS; depth is the number of
    such pairs in flight. Only a jibo-dfu-queue-v1 loader may use more than one.
    """
    sent = 0
    read = 0
    block = 0
    lengths = collections.deque()
    with _ControlPipeline(device, 2 * depth, transfer_size) as pipeline:
        while sent < total_size:
            while read < total_size and len(lengths) < depth:
                chunk = source.read(min(transfer_size, total_size - read))
                if not chunk:
                    raise DfuTransferError("The image ended after {} of {} bytes.".format(
                        read, total_size))
                pipeline.submit(_REQUEST_OUT, _DFU_DNLOAD, block + len(lengths), interface,
                                len(chunk), chunk)
                pipeline.submit(_REQUEST_IN, _DFU_GETSTATUS, 0, interface, _STATUS_LENGTH)
                lengths.append(len(chunk))
                read += len(chunk)
            length = lengths.popleft()
            label = "DFU_DNLOAD block {}".format(block)
            accepted = len(pipeline.retire(label))
            status, state, _poll = _parse_status(pipeline.retire(label + " status"))
            if accepted != length:
                raise DfuTransferError("{} accepted {} of {} bytes.".format(
                    label, accepted, length))
            if status != 0 or state != _STATE_DNLOAD_IDLE:
                raise DfuTransferError(
                    "{} reported status {} in state {} after {} bytes; expected status 0 "
                    "in dfuDNLOAD-IDLE (5).".format(label, status, state, sent))
            sent += length
            block += 1
            if progress:
                progress(sent)
    if source.read(1):
        raise DfuTransferError("The image grew while it was being written.")

    # A zero-length DFU_DNLOAD ends the transfer. The loader flushes its last
    # buffer after the following status request, so one more status request
    # after dfuIDLE confirms that the final eMMC write has finished.
    device.control(_REQUEST_OUT, _DFU_DNLOAD, block & 0xFFFF, interface,
                   label="Final zero-length DFU_DNLOAD failed")
    deadline = time.monotonic() + MANIFEST_TIMEOUT_SECONDS
    while True:
        status, state, poll = _get_status(device, interface)
        if status != 0:
            raise DfuTransferError("DFU manifestation reported status {} in state {}.".format(
                status, state))
        if state == _STATE_IDLE:
            break
        if state not in (_STATE_MANIFEST_SYNC, _STATE_MANIFEST):
            raise DfuTransferError("DFU manifestation entered unexpected state {}.".format(state))
        if time.monotonic() > deadline:
            raise DfuTransferError("DFU manifestation did not return to dfuIDLE.")
        time.sleep(min(poll, 1000) / 1000.0)
    status, state, _poll = _get_status(device, interface)
    if status != 0 or state != _STATE_IDLE:
        raise DfuTransferError("After the final flush the DFU interface reports status {} in "
                               "state {}.".format(status, state))
    return sent


class _Session:
    """Open one named DFU alternative on a sysfs USB port."""

    def __init__(self, port, alternate, sysfs_root="/sys/bus/usb/devices",
                 usb_root="/dev/bus/usb", opener=_Usbfs):
        self.port = port
        self.alternate = alternate
        self.sysfs_root = Path(sysfs_root)
        self.usb_root = Path(usb_root)
        self.opener = opener
        self.device = None
        self.interface = None
        self.claimed = False
        self.queue_capable = False

    def __enter__(self):
        bounded._verify_sysfs_device(self.port, self.sysfs_root)
        node = self.sysfs_root / self.port
        try:
            bus, number, config_value = (int((node / name).read_text())
                                         for name in ("busnum", "devnum", "bConfigurationValue"))
        except (OSError, ValueError) as exc:
            raise DfuTransferError("Could not read USB port {} from sysfs: {}".format(
                self.port, exc)) from exc
        self.device = self.opener(self.usb_root / "{:03d}".format(bus) / "{:03d}".format(number))
        try:
            vendor, product, config = _active_configuration(self.device.descriptors(),
                                                            config_value)
            if (vendor, product) != (bounded.VID, bounded.PID):
                raise DfuTransferError("The opened USB device is not Jibo DFU (0955:701a).")
            languages = _string_descriptor(self.device, 0, 0)
            if len(languages) < 2:
                raise DfuTransferError("The DFU device reports no string language.")
            language = struct.unpack_from("<H", languages)[0]
            alternates = {}
            for interface, setting, index in _dfu_settings(config):
                name = _string_descriptor(self.device, index, language).decode(
                    "utf-16-le", "replace")
                alternates.setdefault(name, []).append((interface, setting))
            self.queue_capable = QUEUE_MARKER in alternates
            found = alternates.get(self.alternate, [])
            if len(found) != 1:
                raise DfuTransferError("The DFU device lists the alternate {!r} {} times.".format(
                    self.alternate, len(found)))
            self.interface, setting = found[0]
            self.device.claim(self.interface)
            self.claimed = True
            self.device.set_interface(self.interface, setting)
            self.attributes, self.transfer_size = _functional_descriptor(
                self.device, self.interface)
            _ensure_idle(self.device, self.interface)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def block_size(self, requested=None):
        """Return the DFU block size to use; only a queue loader accepts smaller ones."""
        if requested is None or requested == self.transfer_size:
            return self.transfer_size
        if not self.queue_capable:
            raise DfuTransferError("This loader needs {}-byte DFU blocks; other sizes need a "
                                   "{} loader.".format(self.transfer_size, QUEUE_MARKER))
        if requested & (requested - 1) or not MIN_BLOCK_SIZE <= requested <= self.transfer_size:
            raise DfuTransferError("The DFU block size must be a power of two from {} to {} "
                                   "bytes.".format(MIN_BLOCK_SIZE, self.transfer_size))
        return requested

    def write_depth(self, requested=None):
        """Queue several writes only when the loader retires stale completions."""
        if not self.queue_capable:
            return 1
        depth = DEFAULT_WRITE_DEPTH if requested is None else requested
        if not 1 <= depth <= MAX_UPLOAD_DEPTH:
            raise DfuTransferError("Write depth must be between 1 and {}.".format(MAX_UPLOAD_DEPTH))
        return depth

    def __exit__(self, exc_type, exc, traceback):
        if self.claimed:
            self.claimed = False
            try:
                self.device.release(self.interface)
            except DfuTransferError:
                pass
        if self.device is not None:
            self.device.close()
            self.device = None
        return False


def _prefix_sink(write, keep):
    """Pass on only the first keep bytes of an upload."""
    remaining = [keep]

    def sink(data):
        if remaining[0] > 0:
            piece = data[:remaining[0]]
            write(piece)
            remaining[0] -= len(piece)
    return sink


def upload(port, alternate, destination, size, depth=DEFAULT_UPLOAD_DEPTH, progress=None,
           transfer_size=None, prefix=None):
    """Read one DFU alternative of known size into a new private file.

    With prefix, only that many leading bytes are saved. The alternative is
    still read to its end: stopping early would leave this loader's read
    cursor mid-alternative, and its next read or write would fail.
    """
    keep = size if prefix is None else prefix
    if not 0 <= keep <= size:
        raise DfuTransferError("The prefix must be between 0 and {} bytes.".format(size))
    destination = Path(destination)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    completed = False
    try:
        with os.fdopen(descriptor, "wb", buffering=PROGRESS_STEP) as stream:
            with _Session(port, alternate) as session:
                if not session.attributes & _DFU_CAN_UPLOAD:
                    raise DfuTransferError("The DFU interface does not advertise upload support.")
                block_size = session.block_size(transfer_size)
                try:
                    received = _upload_stream(session.device, session.interface, block_size,
                                              size, _prefix_sink(stream.write, keep), depth,
                                              progress)
                except DfuTransferError as exc:
                    raise DfuTransferError(str(exc) + _recovery_note(
                        session.device, session.interface)) from exc
                _finish_upload(session.device, session.interface)
        completed = True
        return received, block_size
    finally:
        if not completed:
            destination.unlink(missing_ok=True)


def download(port, alternate, source, progress=None, transfer_size=None, depth=None):
    """Write one complete image to a DFU alternative."""
    with open(source, "rb") as stream:
        total_size = os.fstat(stream.fileno()).st_size
        if total_size <= 0:
            raise DfuTransferError("The image to write is empty.")
        with _Session(port, alternate) as session:
            if not session.attributes & _DFU_CAN_DNLOAD:
                raise DfuTransferError("The DFU interface does not advertise download support.")
            block_size = session.block_size(transfer_size)
            write_depth = session.write_depth(depth)
            try:
                sent = _download_stream(session.device, session.interface, block_size, stream,
                                        total_size, progress, write_depth)
            except DfuTransferError as exc:
                raise DfuTransferError(str(exc) + _recovery_note(
                    session.device, session.interface)) from exc
    return sent, block_size


class _ProgressPrinter:
    def __init__(self, verb, total):
        self.verb = verb
        self.total = total
        self.next_report = 0

    def __call__(self, done):
        if done < self.next_report and done != self.total:
            return
        self.next_report = done + PROGRESS_STEP
        percent = 100 * done // self.total if self.total else 100
        bar = "=" * (percent // 4)
        print("{}\t[{:<25}] {:3d}% {:12d} bytes".format(self.verb, bar, percent, done), flush=True)


def _terminate(_signum, _frame):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pipelined DFU transfer for the Jibo RAM loader")
    parser.add_argument("-d", "--device", default="0955:701a")
    parser.add_argument("-p", "--path", required=True, help="sysfs USB port, for example 1-1")
    parser.add_argument("-a", "--alt", required=True, help="DFU alternate name")
    direction = parser.add_mutually_exclusive_group(required=True)
    direction.add_argument("-U", "--upload", type=Path, help="read the alternative into a new file")
    direction.add_argument("-D", "--download", type=Path, help="write this file to the alternative")
    parser.add_argument("-Z", "--upload-size", type=int, help="exact byte size of the alternative")
    parser.add_argument("--prefix", type=int,
                        help="save only this many leading bytes; the whole alternative is still read")
    parser.add_argument("--depth", type=int, default=DEFAULT_UPLOAD_DEPTH,
                        help="DFU_UPLOAD requests kept queued (default {})".format(DEFAULT_UPLOAD_DEPTH))
    parser.add_argument("--write-depth", type=int,
                        help="DFU_DNLOAD blocks kept queued on a {} loader (default {})".format(
                            QUEUE_MARKER, DEFAULT_WRITE_DEPTH))
    parser.add_argument("-t", "--transfer-size", type=int,
                        help="DFU block size; defaults to the loader's advertised size")
    args = parser.parse_args(argv)
    if args.device.lower() != "0955:701a":
        parser.error("only the Jibo RAM loader (0955:701a) is supported")
    if args.upload and args.upload_size is None:
        parser.error("-U requires -Z with the alternative's exact size")
    signal.signal(signal.SIGTERM, _terminate)
    started = time.monotonic()
    try:
        if args.upload:
            print("Pipelined DFU upload of {} ({} bytes, {} requests queued)".format(
                args.alt, args.upload_size, args.depth), flush=True)
            count, transfer_size = upload(args.path, args.alt, args.upload, args.upload_size,
                                          args.depth, _ProgressPrinter("Upload", args.upload_size),
                                          transfer_size=args.transfer_size, prefix=args.prefix)
            print("Upload done.\nReceived a total of {} bytes in {}-byte blocks".format(
                count, transfer_size), flush=True)
        else:
            total = args.download.stat().st_size
            print("Pipelined DFU download to {} ({} bytes)".format(args.alt, total), flush=True)
            count, transfer_size = download(args.path, args.alt, args.download,
                                            _ProgressPrinter("Download", total),
                                            transfer_size=args.transfer_size,
                                            depth=args.write_depth)
            print("Download done.\nSent a total of {} bytes in {}-byte blocks".format(
                count, transfer_size), flush=True)
            print("state(2) = dfuIDLE, status(0) = No error condition is present\nDone!", flush=True)
    except KeyboardInterrupt:
        print("Transfer interrupted.", file=sys.stderr, flush=True)
        return 130
    except (DfuTransferError, bounded.BoundedDfuError, OSError) as exc:
        print("Error: {}".format(exc), file=sys.stderr, flush=True)
        return 1
    elapsed = time.monotonic() - started
    print("{:.1f} s, {:.2f} MiB/s".format(elapsed, count / (1 << 20) / max(elapsed, 0.001)),
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
