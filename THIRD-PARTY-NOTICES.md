# Binary distribution notices

Every release bundle contains this notice, `LICENSE`, the toolkit's source files,
and `licenses/`. A matching `Jibo-DFU-corresponding-source.tar.gz` is also attached
to the draft release. **Distribute that archive alongside the binaries.** The build
fails if any required source archive or license text is absent.

| Component | Terms | Corresponding source in the bundle |
| --- | --- | --- |
| dfu-util 0.11 | GPL-2.0-or-later | `dfu-util-0.11.tar.gz`, its COPYING |
| U-Boot 2016.05 / Jibo RAM loader | GPL-2.0-or-later; individual files retain their notices | `jibo-ram-dfu-v1-source.tar.gz`, current `firmware/` patches and `scripts/build_loader.py` |
| ShofEL2-for-T124 | Original component notices, including GPL payload code; see source COPYING/LICENSE and file headers | complete pinned commit `31ac3a260c8a1501869aff6690b3b9ad4904ef58`, toolkit entry patch and build script |
| libusb 1.0.27 | LGPL-2.1-or-later | `libusb-1.0.27.tar.bz2`, COPYING; dynamically linked, not statically incorporated |
| e2fsprogs 1.47.1 | GPL-2.0 and LGPL component licenses; see source NOTICE | `e2fsprogs-1.47.1.tar.gz`, NOTICE/COPYING |
| CPython | Python Software Foundation License and bundled dependency notices | Python LICENSE and Python distribution notices collected by the build |
| Tcl/Tk | Tcl/Tk permissive license | Tcl/Tk license.terms collected by the build |
| PyInstaller | GPL-2.0-or-later with the bootloader distribution exception | PyInstaller COPYING.txt |
| AppImage runtime (Linux) | MIT | runtime LICENSE and recorded upstream URL |

The Mac bundles also preserve their installed Homebrew interpreter dependency
license texts, formula snapshots and GPL/LGPL source archives, indexed in
`licenses/Homebrew-dependencies.json`. Matching per-architecture Mac source assets
travel with the release. Linux interpreter library copyright/terms are collected
in `licenses/system-library-notices/`. These dependency notices supplement the
main component table above.

## Source access and rebuilding

The complete corresponding source archives, patches, build scripts and recorded
source hashes travel **inside each artifact** and in the release's source archive;
this is a source-accompanied distribution, not a promise that upstream URLs will
remain online. Extract `toolkit-source.tar.gz` into a new directory first and use
its `scripts/build_loader.py`: that preserves the script's required `firmware/`
layout. The loose build-script copies in `licenses/` are reference copies, not a
replacement checkout layout. The loader source snapshot is the baseline: apply the current
entry, CID, file-RPC and queue patches using the included `build_loader.py` (flags
`--file-level-candidate --dfu-queue-candidate`), as described in `docs/loader-build.md`.
The separate manifest pins the exact distributed loader hash.

Release maintainers offer the corresponding source at no charge via the matching
release source asset; for at least three years after distribution, retain that
asset and supply it to recipients on request via a repository issue. Do not publish
binaries if their matching source asset is unavailable. This commitment supplements,
not replaces, the source already included in each download.

libusb can be replaced with a compatible modified shared library in the extracted
bundle (`runtime/lib`); no signature enforcement is implemented by the toolkit.
See `docs/release.md` for extraction and rebuilding. WSL and usbipd-win are optional
host prerequisites, installed only with user consent; their installers are not
redistributed inside the Windows executable.

## Hosted image verification

Cryptography 46.0.3 provides Ed25519 verification in the native bundles. Its Apache-2.0/BSD-3-Clause terms, bundled dependency notices and source archive are included in the runtime license directory and corresponding-source download.
