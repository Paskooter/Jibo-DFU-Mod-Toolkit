# Candidate loader eMMC identity

The packaged RAM DFU loader reports an empty USB serial because the USB
download gadget's serial buffer is zero-initialized and the DFU entry path
never calls `g_dnl_set_serialnumber()`. In this U-Boot tree, that setter is
called by fastboot only. A blank serial is surfaced by `dfu-util` as
`UNKNOWN`, so host backups cannot be stably associated with a robot after its
partition contents change.

The opt-in candidate entry reads the already-initialized eMMC CID and exposes
its four words as a 32-character lowercase hexadecimal USB serial. It fails
closed if the CID is all zero. This serial is stable across reboots and file
edits, allowing the host's identity-bound partition backup records to be
reused. The normal build path and pinned loader remain unchanged.

Build an isolated candidate with the reproduced Buildroot toolchain:

```sh
python3 scripts/build_loader.py \
  --source /path/to/jibo-ram-dfu-v1-source/u-boot \
  --host /path/to/buildroot/output/host \
  --out /tmp/jibo-cid-serial-candidate \
  --cid-serial-candidate
```

This candidate has only been built offline. It has not been loaded onto a
robot or validated against live DFU hardware.

The default build path was also rebuilt from the same pinned source/toolchain:
its 415,087-byte raw image pads to the pinned 415,088-byte image and exactly
matches the pinned SHA-256
`8f46062f2d201824337093a1e4c154e3048c019b147930da35b9d62e00c5e689`.
