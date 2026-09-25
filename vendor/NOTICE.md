# Source and license notice

`u-boot/` is the complete corresponding U-Boot source snapshot used to build the Jibo RAM DFU loader, with generated object files and outputs removed. It reports U-Boot 2016.05 and targets the Avionic-Design Kein Baseboard / Tegra124 profile. Its original Git metadata was absent, so no upstream repository commit is claimed. The tracked source fingerprints after removing the included patch are:

- `common/main.c`: `a777b4a553bea33c9d75211f91605c68b044074eb2c0b3d0a827a3c6d8c4d7f2`
- `include/configs/kein-baseboard.h`: `a8fffb1950f8b01764e3ccfb0f95c5dbcc0dc9806fc553ed39a44ab71d0b3c42`
- `drivers/dfu/dfu_mmc.c`: `105cf58ee218e30034b266538d2bce9739ad95bd2d061daaae3126d878168dee`

A fresh build from this snapshot with the included patch and the matching Buildroot 2015.11 / GCC 4.9.3 host toolchain reproduced the pinned 415,088-byte loader exactly (SHA-256 `8f46062f2d201824337093a1e4c154e3048c019b147930da35b9d62e00c5e689`).

U-Boot source files retain their original SPDX identifiers and notices. The U-Boot license texts are included in `u-boot/Licenses/`; consult each file's header for its applicable license. The Jibo DFU header declares GPL-2.0-or-later. The project patch is included in `jibo-loader/firmware/entry.patch`.

This archive contains source and build metadata only. The source-tree scan found no PEM/private-key material or device captures. It does not include the separately signed RCM bundle, its public-key manifest, or any signing key.
