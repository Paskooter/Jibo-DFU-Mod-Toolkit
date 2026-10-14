"""USB re-enumeration settling without real USB access."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jibo_dfu as d


class DfuSettleTests(unittest.TestCase):
    def entry(self, listings, timeout=120):
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
        with tempfile.TemporaryDirectory() as temp:
            loader = Path(temp) / 'loader.bin'
            loader.write_bytes(b'loader')
            states = iter([[{'port': '1-1', 'state': 'rcm'}]])
            with patch.object(d, '_shofel_dfu_tool', return_value='/tools/shofel2_t124'), \
                    patch.object(d, 'tool', return_value='dfu-util'), \
                    patch.object(d, 'devices', side_effect=lambda: next(states, [{'port': '1-1', 'state': 'dfu'}])), \
                    patch.object(d, 'run_with_progress', return_value='Starting verified ARM-state SPL at 0x80108000.'), \
                    patch.object(d, 'dfu_alternatives', side_effect=listings) as listing, \
                    patch.object(d.time, 'monotonic', side_effect=lambda: clock[0]), \
                    patch.object(d.time, 'sleep', side_effect=sleep):
                result = d.enter_shofel_dfu(loader=loader, timeout=timeout, confirm_meerkat_rev02=True)
            return result, listing.call_count, clock[0]

    def test_empty_access_listing_then_ready_succeeds(self):
        result, calls, elapsed = self.entry([
            ([], 'Cannot open DFU device: LIBUSB_ERROR_ACCESS'),
            ([], ''), ([d.MARKER], ''), ([d.MARKER, 'var'], '')])
        self.assertTrue(result['loader_verified'])
        self.assertEqual(calls, 4)
        self.assertEqual(elapsed, 0.75)

    def test_listing_exception_then_ready_succeeds(self):
        result, calls, _ = self.entry([d.DfuError('LIBUSB_ERROR_ACCESS'), ([d.MARKER, 'var'], '')])
        self.assertTrue(result['loader_verified'])
        self.assertEqual(calls, 2)

    def test_wrong_loader_reports_names_after_bounded_window(self):
        calls = []
        def listing(*args):
            calls.append(args)
            return ['vendor-dfu', 'var'], ''
        with self.assertRaisesRegex(d.DfuError, "not Jibo's recovery loader.*vendor-dfu, var"):
            self.entry(listing, timeout=1)
        self.assertEqual(len(calls), 4)

    def test_inaccessible_device_reports_usb_access_not_missing_loader(self):
        with self.assertRaisesRegex(d.DfuError, 'USB access.*not ready'):
            self.entry(lambda *args: ([], 'Cannot open DFU device: LIBUSB_ERROR_ACCESS'), timeout=1)

    def test_listing_deadline_is_capped_by_settle_window(self):
        calls = []
        with self.assertRaises(d.DfuError):
            self.entry(lambda *args: calls.append(args) or ([], ''), timeout=120)
        self.assertEqual(len(calls), int(d.DFU_SETTLE_SECONDS / 0.25))


    def test_partition_context_waits_for_usb_access_after_transition(self):
        clock = [0.0]
        with patch.object(d, 'devices', return_value=[{'port': '1-1', 'state': 'dfu'}]), \
                patch.object(d, 'dfu_alternatives', side_effect=[([], 'Cannot open DFU device'),
                    ([d.MARKER, 'var'], 'name="var", serial="example-device"')]) as listing, \
                patch.object(d.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(d.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
            result = d._dfu_context('1-1', 'dfu-util')
        self.assertEqual(result[0], '1-1')
        self.assertEqual(result[1], [d.MARKER, 'var'])
        self.assertEqual(listing.call_count, 2)
        self.assertEqual(clock[0], 0.25)

    def test_listing_subprocess_cannot_outlive_remaining_deadline(self):
        clock = [0.0]
        def run(argv, timeout=30):
            self.assertLessEqual(timeout, 0.5)
            clock[0] += timeout
            raise d.DfuError('listing timeout')
        with patch.object(d.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(d.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
                patch.object(d, 'run', side_effect=run):
            with self.assertRaisesRegex(d.DfuError, 'USB access'):
                d._settled_dfu_alternatives('dfu-util', '1-1', deadline=0.5)
        self.assertEqual(clock[0], 0.5)


if __name__ == '__main__':
    unittest.main()
