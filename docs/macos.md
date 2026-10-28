# macOS setup and verification

The Mac workflow runs the guided menu, RCM-to-DFU entry, and partition
operations using native tools on Intel (`x86_64`) and Apple silicon (`arm64`).
The launcher builds a native ShofEL entry helper that talks to the boot-ROM
recovery mode (`0955:7740`) through libusb, then starts the same RAM DFU loader
(`0955:701a`) the Linux workflow uses. The menu and its safety checks are the
same as on Linux; record and report how USB behaves on your Mac model while
testing coverage grows.

Once DFU is active, the implementation provides device discovery, GPT checks,
backups, image inspection and editing, settings changes, restores, and official
update transfers. These operations retain their existing identity checks,
backups, and confirmation prompts. Mac USB transfers use `dfu-util`; the faster
Linux usbfs pipeline is unavailable on macOS. The toolkit has been tested on an
Intel Mac running macOS 10.15 with MacPorts tools, including a `var` backup over
USB DFU. Apple silicon has not been tested on a robot yet.

## Choosing dependencies for your Mac

The runtime requirements are Python 3.10 or later with `curses`, `dfu-util`,
libusb 1.0, and e2fsprogs (`debugfs`, `e2fsck`, `resize2fs`, and `dumpe2fs`).
Building the native ShofEL entry helper additionally needs the Xcode Command
Line Tools (`xcode-select --install`) for `cc`, `make`, and `git`. The launcher
uses macOS's supplied Bash and does not enforce an OS version cutoff. An OS
release is usable only when these dependencies install and run there;
dependency availability alone does not establish hardware support.

For older Intel Macs, including a machine from 2014, use
[MacPorts](https://www.macports.org/install.php) if Homebrew cannot install the
dependencies. MacPorts offers installers for older macOS versions, including
Big Sur, Catalina, and Mojave. Install the Command Line Tools and the MacPorts
installer matching your installed OS, then install the tools:

```sh
xcode-select --install
sudo port install python312 py312-tkinter tk dfu-util libusb e2fsprogs
./run.sh --gui
```

The launcher uses MacPorts at `/opt/local` when Homebrew is not installed (set
`JIBO_MAC_PREFIX=/opt/local` if both are), and finds its versioned `python3.12`
executable, so selecting a system-wide Python default is unnecessary. The GUI
needs both `py312-tkinter` and the `tk` port: `py312-tkinter` alone leaves Tk's
script library out of Tk's search path, and the window then fails with "Can't
find a usable tk.tcl". On an Intel Mac running Catalina 10.15.8, this set
installed and the toolkit window opened.
MacPorts lists [Python 3.12](https://ports.macports.org/port/python312/),
[dfu-util](https://ports.macports.org/port/dfu-util/),
[libusb](https://ports.macports.org/port/libusb/), and
[e2fsprogs](https://ports.macports.org/port/e2fsprogs/) as separate ports.
Availability and build results vary with the installed OS.

The downloadable Mac app (DMG) is built on macOS 15 and needs macOS 15 or later.
On older macOS it stops at launch with "Symbol not found: _mkfifoat"; use the
source version above instead.

For Macs with a working [Homebrew installation](https://docs.brew.sh/Installation):

```sh
brew install python dfu-util libusb e2fsprogs
./run.sh
```

Homebrew normally uses `/usr/local` for Intel and `/opt/homebrew` for Apple
silicon. Its [e2fsprogs formula](https://formulae.brew.sh/formula/e2fsprogs) is
keg-only, so the launcher adds its `bin` and `sbin` directories to the toolkit's
PATH. Homebrew's supported macOS range changes; its current installation
requirements should guide whether it is suitable for an older Mac.

Run the launcher from the repository directory. Dependencies are installed
explicitly once; later launches check the tools and run the source directly.
The Linux `.pyz` contains native Linux executables and is unsuitable for Macs.
On Apple silicon, use a native Terminal session with Rosetta disabled. Python
and the libusb dylib must have matching architectures. The launcher checks the
Python architecture and loads libusb before opening the menu.

## Selecting tools installed elsewhere

If both package managers are installed, the launcher prefers native Homebrew.
To select MacPorts explicitly:

```sh
JIBO_MAC_PREFIX=/opt/local ./run.sh
```

`JIBO_MAC_PREFIX` also accepts a custom dependency prefix. `JIBO_PYTHON`,
`JIBO_DFU_UTIL`, and `JIBO_LIBUSB` select individual executable or library paths.
`JIBO_SHOFEL2` points the menu at an already built native entry helper and
skips the build step. For example:

```sh
JIBO_PYTHON=/opt/local/bin/python3.12 \
JIBO_LIBUSB=/opt/local/lib/libusb-1.0.dylib \
JIBO_MAC_PREFIX=/opt/local ./run.sh detect
```

### Building the RCM-to-DFU entry helper

The entry helper consists of a native host program (`shofel2_t124`) plus two ARM
payloads (`intermezzo.bin`, `dfu_stage2.bin`). The launcher builds the host
with `cc` against libusb on first run and reuses the intact pair afterwards.

The ARM payloads are device code, not Mac code, and the toolkit ships them
pinned in `assets/entry-payloads` with a SHA-256 manifest tied to the RAM
loader, the ShofEL patch, and the fork commit. The build validates that
manifest before use, so **no ARM cross-compiler is needed on a Mac**. To build
the payloads from source instead, install an ARM toolchain
(`brew install arm-none-eabi-gcc`, bottled for Apple silicon, or
`port install arm-none-eabi-gcc`) and run
`python3 scripts/build_file_level_entry.py --build-payloads` once; when
Homebrew has no bottle for your system (recent Intel macOS), use MacPorts or
the pinned payloads. A payload source can also be selected explicitly:

```sh
JIBO_PAYLOADS_FROM=/path/to/an/intact/entry/build ./run.sh detect
```

The build verifies the loader manifest, the embedded loader hash, and the
helper's launch capability before the menu opens. If the pinned payloads fall
out of date after a loader or patch change, regenerate them by running the
build once on a machine with `arm-none-eabi-gcc` and copying the resulting
`intermezzo.bin` and `dfu_stage2.bin` back into `assets/entry-payloads` with
an updated `manifest.json`.

The launcher runs as your Mac user and does not request `sudo`. An actual USB
permission error needs investigation on the affected host. Backups use
`~/Jibo-Backups`.

## Checking your Intel Mac

Start with host checks and discovery. These commands do not write robot
partitions:

```sh
sw_vers
uname -m
./run.sh --help
./run.sh detect
./run.sh list
```

Record the macOS version, Mac model, selected package manager, and tool versions.
To put the robot in RCM, follow [Put Jibo in RCM mode](../README.md#put-jibo-in-rcm-mode).
If the robot is in RCM, `detect` should show `"state": "rcm"` and a port such as
`0-3` or `20-3.2`; the menu's ShofEL entry action becomes available. With an
already active Jibo DFU loader, it should show `"state": "dfu"`. Use the
reported path for `--port`; a Linux port path is not portable between hosts.

With the robot in RCM, test the entry step once and record the result before
relying on it:

```sh
./run.sh detect
./run.sh enter-dfu-shofel --confirm-meerkat-rev02
```

With DFU active, run `list-partitions` twice to check repeatable GPT reads, then
make a `var` backup. Substitute the port from `detect`:

```sh
./run.sh list-partitions --port 20-3.2
./run.sh list-partitions --port 20-3.2
./run.sh backup-var --port 20-3.2
```

The backup should contain exactly 524,288,000 bytes and a SHA-256 manifest.
Compare it with a known backup from the same robot and unchanged filesystem
when available. Check unplug/replug detection and a direct USB connection
before investigating hubs or adapters. Local inspection and editing can be
tested on copies of that backup. Setting changes, restores, and update flashes
need separate hardware validation with their normal backup and confirmation
steps before being advertised as working on Macs.

## If an M1 iMac does not see Jibo

Apple silicon USB support is still experimental; an M1 build alone does not
prove RCM entry works on every Mac controller or hub. First find which stage
fails, rather than installing unrelated drivers or changing robot firmware:

1. Use **Jibo-DFU-macOS-arm64.dmg** on macOS 15 or later. Drag the app to
   Applications and run that copy. This download already includes libusb,
   dfu-util and the native ShofEL helper; Homebrew and Rosetta are not required.
   Source users should use native ARM Python and ARM libusb together (see above).
2. Try a known **data-capable** micro-USB cable. Connect directly where possible,
   or use one known-good USB-C data adapter rather than a dock or chain of hubs.
   Try another cable/port/adapter, one change at a time, **only while idle**, never
   during a transfer. A USB 2 hub is a diagnostic alternative, not a proven M1 fix.
3. Follow [Put Jibo in RCM mode](../README.md#put-jibo-in-rcm-mode), including
   the button photo. The lower button is RCM; the red forehead light and no
   startup chime are the confirmation. Then use **Refresh USB**.
4. Check **System Information → USB** for vendor `0x0955`, product `0x7740`
   (RCM/APX). If it is absent there too, focus on the cable, adapter, port and
   RCM procedure. If macOS sees it but the toolkit does not, save the toolkit's
   exact error; libusb loading/access or a conflicting USB app needs investigation.
5. If the toolkit sees RCM, choose **Enter DFU with ShofEL (RAM loader)** and
   record whether it changes to `0955:701a` and **Jibo recovery loader ready**.
   Detection, the ShofEL transfer, and DFU re-enumeration are different stages.
   Close other USB recovery tools first. Mac operation normally needs no `sudo`;
   do not disable SIP or install a Windows driver. A permission/claim error is
   evidence to collect, not a reason to run the whole app as root.
6. For support, send the Mac model, macOS version, toolkit build from the title,
   cable/adapter arrangement, which USB identity appears, and the operation log
   from the failed stage. Review it first; omit serial numbers and credentials.
   If entry still fails, a tested Intel Mac, Linux or Windows/WSL host is the
   fallback, not a guarantee that replacing a hub will fix the M1.

Apple's **Allow accessories to connect** approval applies to **Mac laptops with
Apple silicon**, not a setting an iMac owner should be told to find. On a laptop,
unlock it and approve the connected accessory if prompted; do not globally
weaken the security setting.[1]

## If the Phoenix image list fails after DFU is ready

This failure is on the computer's image-list path, not evidence of a USB problem or a reason
to repeat USB recovery or reflash the robot. The old message **Hosted transport
unavailable or stalled** hid several different causes. Current source distinguishes:

| Detail | Next check |
| --- | --- |
| CA certificate file missing, unreadable or invalid | Reinstall the toolkit app. Source users: check the selected Python's certifi/system certificate installation. |
| TLS certificate verification failed (verification code) | Check the computer clock and HTTPS-inspecting VPN/proxy. Never disable certificate verification. |
| DNS lookup failed | Check name resolution and any configured proxy on this computer. |
| Connection refused/reset, network route unavailable | Check network/VPN/firewall and IPv4/IPv6 connectivity. These do not prove jibo.io is down. |
| Timed out / overall deadline exceeded | Retry on a working connection; a slow proxy, TLS handshake or response can exhaust the list's request budget. |
| Proxy tunnel refused (HTTP status), invalid proxy configuration | Check proxy address, protocol and authentication. Proxy passwords are not copied into transport diagnostics. |
| HTTP status / Cloudflare block | Report the displayed status; this is distinct from TLS, DNS or a local folder error. |
| Image folder error | Check access/free space in `~/Library/Application Support/jibo-dfu/updates`. |

The result page has **Copy details** for the build, system/architecture, Python,
summary and hosted-list diagnostic. If clipboard access fails, selectable details
appear instead. Review before sharing. Local update packages remain usable when
the online list fails; TLS/signature verification is never bypassed.

Frozen apps now load their own CA file instead of requiring the build Mac's
Homebrew paths. The release self-check verifies that file offline; it is not a
network check and is not run as a prerequisite to opening the image chooser.
The transport uses Python urllib/OpenSSL, **not curl or another download
subprocess**. See [release trust checks](release.md#https-trust-in-frozen-apps).
Native DMG and real M1 USB acceptance still require testing on a Mac.

## Automated coverage and remaining work

The Mac tests simulate libusb descriptors, handles, library discovery, and
launcher dependencies. They cover both architecture choices, bus zero, device
identity rejection, cleanup, native tool selection, native entry-helper
resolution and reuse, the RCM-to-DFU menu flow, and paths containing spaces.
The ShofEL patch keeps the Linux usbfs host path byte-compatible and adds a
libusb-1.0 backend (`JIBO_LIBUSB_BACKEND`) for the Mac host build; both
backends compile with `-Wall -Werror` on Linux, and the default Linux build is
unchanged. The existing Linux test suite also checks the shared workflows.

The [macOS CI workflow](../.github/workflows/macos.yml) is configured to install
native dependencies, build the entry helper from the pinned payloads, start
the launcher, enumerate USB devices, test Mac selection logic and local ext4
preparation on Intel and ARM runners, and rebuild the payloads from source
with `arm-none-eabi-gcc` on Apple silicon. It uses GitHub's documented
[Intel and ARM macOS runner labels](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).
Hosted runners have no Jibo attached. Use CI to check host portability, and a
connected Jibo to check USB transfers and RCM-to-DFU entry.

Intel Mac source setup and a `var` backup have been tested as described above.
Apple silicon USB support remains experimental. Record the Mac model, macOS
version, USB transfers, entry results and readback results when testing it.

## Sources

[1] https://support.apple.com/en-us/102282 — Allow USB and other accessories to connect to your Mac
