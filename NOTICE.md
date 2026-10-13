# License notice

Jibo DFU Mod Toolkit
Copyright (C) 2026 Paskooter and contributors

The toolkit is free software: you can redistribute it and/or modify it under the
terms of the GNU General Public License, version 3, as published by the Free
Software Foundation (SPDX: `GPL-3.0-only`). The full text is in [LICENSE](LICENSE).

The toolkit is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.

In short: anyone may use, study, change and share the toolkit. If you distribute
the toolkit or a modified version of it, including as part of another project,
you must do so under the GPL-3.0, keep this copyright and license notice, mark
your changes, and provide the complete corresponding source code.

## Parts under other licenses

The GPL-3.0 grant above covers the toolkit's own work. These parts keep their
own licenses and notices, which are not replaced by this file:

| Path | What it is | License |
| --- | --- | --- |
| `vendor/jibo-ram-dfu-v1-source.tar.gz`, `assets/loader.bin` | U-Boot 2016.05 source snapshot and the RAM DFU loader built from it | U-Boot's licenses: GPL-2.0-or-later for most files, per-file exceptions as marked; texts in the archive's `u-boot/Licenses/` |
| `firmware/*.patch`, `firmware/jibo_dfu_entry.h` | The toolkit's changes to U-Boot | GPL-2.0-or-later, matching the U-Boot files they modify |
| `patches/shofel2-dfu-entry.patch`, `assets/entry-payloads/*.bin` | Changes to fail0verflow's ShofEL2 and the ARM payloads built from them | The license terms of ShofEL2 and of each modified file; several files carry NVIDIA and Google GPL-2.0 headers |
| `assets/boot-network-status/startup-view.js`, `startup-view-x11-early.js`, `startup-view-dom.js` | Modified versions of the robot's stock `startup-view.js` | The toolkit's changes are GPL-3.0; the stock Jibo code they contain remains under its original owner's terms |

Downloadable single-file builds also bundle third-party runtimes and tools. Their
licenses, notices and source information ship inside each download.
