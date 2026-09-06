# Jibo DFU entry utility

Development candidate: builds and offline tests pass; USB hardware entry is
not yet verified. This is the DFU entry component of the planned mod toolkit.

The utility loads a dedicated T124 recovery program into RAM and leaves the
robot in USB DFU. The runtime uses pre-signed artifacts and contains no private
signing key. Its operation does not depend on the installed Linux version.

## Run

The packaged Linux x86_64 candidate needs Python 3 and the usual system
libusb, libudev, libstdc++ and glibc libraries. It includes tegrarcm,
dfu-util, Crypto++ and a pre-signed candidate loader bundle.

~~~sh
python3 dist/jibo-dfu-linux-x86_64.pyz detect
sudo python3 dist/jibo-dfu-linux-x86_64.pyz enter --allow-untested
~~~

Connect the robot by USB and use its recovery/reset controls to enter RCM.
The utility cannot force a powered-off or disconnected robot into RCM.
Use --port 1-2 (the Linux USB topology path) if multiple robots are connected.
On WSL, USB forwarding must retain or reattach the device after re-enumeration.

The --allow-untested flag is required for this candidate's hardware validation.
It must not be removed from the manifest until the particular bundle has a
recorded successful hardware test. A successful signature check alone is
not a hardware test.

For source use:

~~~sh
python3 jibo_dfu.py detect
python3 jibo_dfu.py verify-bundle bundles/default
sudo python3 jibo_dfu.py enter --bundle bundles/default \
  --tegrarcm /path/to/tegrarcm --dfu-util /path/to/dfu-util --allow-untested
~~~

An already-running DFU device is listed without loading a bundle. The tool
reports whether the custom loader marker is present. It leaves the robot
in DFU and performs no host-side partition download or automatic reset.

## Recovery behavior

The custom U-Boot entry bypasses preboot, bootcmd, normal Linux boot, and the
vendor flasher command. Its environment backend is RAM-only, and bootcount
persistence is disabled. It does not run mmc write, gpt write, saveenv, or
fuse commands on entry.

It reads the current GPT and exposes named partitions that fit the old
DFU implementation's signed 32-bit length limit. This includes the usual var,
rootfsA, rootfsB, recovery and services partitions. Large partitions such as
skills remain accessible through the raw eMMC chunks.

- jibo-dfu-v1: upload-only first 34 sectors, including the primary GPT.
- emmc-000, emmc-001, ...: consecutive upload-only chunks of the eMMC user
  area, each at most 1 GiB. The last chunk may be smaller.
- GPT partition names: partition read/write alternatives, obtained from the
  actual table, without rewriting that table or assuming a particular OS.

The raw chunks do not include eMMC boot0 or boot1. A partition write, if the
operator later invokes dfu-util -D, is persistent by design. The entry utility
itself does not perform such a write.

## Coverage and remaining work

The current bundled candidate uses the locally available signing key and the
Meerkat rev02 BCT. It has not been proven on a connected robot. Do not describe
it as universal yet.

Jibo's archive explicitly documents development-fused, production-fused and
unfused robots. A single application can contain pre-signed loaders for those
populations. One key does not sign for a different fused key. The archive also
distinguishes EVT and DVT2 hardware configuration. Coverage still requires
verified loader artifacts and hardware tests for those populations.

There is no implemented ShofEL-to-DFU fallback in this candidate. The existing
ShofEL raw eMMC server demonstrates unsigned code execution but is not itself
a DFU loader. A universal exploit-based route remains a separate engineering
task if matching signed loaders cannot cover the intended robots.

Remaining acceptance work:

1. Observe RCM-to-DFU on a robot matching the current bundle's key/BCT.
2. Upload the marker and var; confirm sizes and repeat-read hashes.
3. Compare boot0, boot1/environment and GPT before/after entry using an
   independent read method to verify preservation on hardware.
4. Establish which factory population the available key matches and obtain
   compatible entry artifacts for the other population, or implement the
   exploit-based DFU loader.
5. Test board revisions and package automatic profile selection based on
   evidence available before loading the BCT. Do not blindly cycle BCTs.

## Build and signing

scripts/build_loader.py copies a local Jibo U-Boot source tree to a new build
directory, validates the relevant baseline source hashes, applies entry.patch,
and builds using the archived Buildroot host toolchain. It never modifies the
source directory. It supports the locally available audit tree by removing
that diagnostic patch in its private build copy.

~~~sh
python3 scripts/build_loader.py --source /path/to/uboot-master \
  --host /path/to/output/host --out .build/recovery
python3 scripts/sign_bundle.py --loader .build/recovery/u-boot-dtb-tegra.bin \
  --bct /path/to/compatible-flasher.bct --key /external/private-key.pem \
  --tegrarcm /path/to/tegrarcm --mkbctpart /path/to/mkbctpart \
  --profile meerkat-rev02-key-profile --out bundles/default
~~~

The base BCT needs the compatible populated bootloader descriptor, as in the
known signed-flasher BCT. A BCT without that descriptor is rejected. Signing
checks the output descriptor, verifies both stored RSA-PSS signatures, and
generates the pre-signed RCM message set entirely offline. Temporary private
key conversion is removed after signing; the output contains only five
public artifacts and a manifest. Manifest hashes detect accidental corruption;
they are not an independent publisher authenticity signature.

~~~sh
python3 scripts/package.py --bundle bundles/default \
  --tegrarcm /path/to/tegrarcm --dfu-util /path/to/dfu-util \
  --libcryptopp /path/to/libcryptopp.so --out dist/jibo-dfu-linux-x86_64.pyz
python3 -m unittest discover -s tests -v
~~~

The package builder uses an explicit file allowlist. PROJECT_SPEC.md, source
build directories, signing scripts, private keys and robot captures are not
included. PROJECT_SPEC.md is ignored by Git and must never be force-added.
All work is local; do not push to a remote without the owner's instruction.

## Sources and provenance

Research was performed through the Jibo MCP archive:

- [Original flash-dfu.sh](https://pvindex.org/gitea/PlatformTeam/buildroot.jibo/src/branch/master/board/nvidia/avionic/flash-dfu.sh): explicit support for pre-signed RCM messages, USB port selection, and RCM/DFU IDs.
- [Fused vs. Un-fused robots](https://pvindex.org/confluence/display/ENG/Fused+vs.+Un-fused+robots): development and production signing populations.
- [Single-step flash system](https://pvindex.org/confluence/display/ENG/Building+a+single+step+flash+system+for+Jibo): EVT versus DVT2 configuration.
- [Partition and filesystem scheme](https://pvindex.org/confluence/display/ENG/Embedded+Platform+Partition+and+File+System+Scheme): GPT and dfu_alt_info relationship.
- [Jibo U-Boot source](https://pvindex.org/gitea/PlatformTeam/uboot.jibo): recovery base.

This source project was created independently of Jibo AutoMod. Firmware changes
target GPL-licensed U-Boot. The local package is an engineering candidate;
before public binary distribution, provide corresponding source and notices
for U-Boot and the bundled host components. No redistribution of a private
signing key is required.
