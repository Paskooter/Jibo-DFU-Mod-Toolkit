# macOS setup and verification

The experimental Mac workflow runs the guided menu, RCM-to-DFU entry, and
partition operations using native tools on Intel (`x86_64`) and Apple silicon
(`arm64`). The launcher builds a native ShofEL entry helper that talks to the
boot-ROM recovery mode (`0955:7740`) through libusb, then starts the same RAM
DFU loader (`0955:701a`) the Linux workflow uses. **The entry step and USB
transfers have not yet been verified on Mac hardware**, so treat first runs as
experiments and keep a Linux machine available as the known-good path.

Once DFU is active, the implementation provides device discovery, GPT checks,
backups, image inspection and editing, settings changes, restores, and official
update transfers. These operations retain their existing identity checks,
backups, and confirmation prompts. Mac USB transfers use `dfu-util`; the faster
Linux usbfs pipeline is unavailable on macOS. Transfer speed and real robot
behavior have not yet been measured or verified on either Mac architecture.

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
sudo port install python312 dfu-util libusb e2fsprogs
JIBO_MAC_PREFIX=/opt/local ./run.sh
```

The launcher recognizes MacPorts at `/opt/local` and its versioned
`python3.12` executable, so selecting a system-wide Python default is unnecessary.
MacPorts lists [Python 3.12](https://ports.macports.org/port/python312/),
[dfu-util](https://ports.macports.org/port/dfu-util/),
[libusb](https://ports.macports.org/port/libusb/), and
[e2fsprogs](https://ports.macports.org/port/e2fsprogs/) as separate ports.
Availability and build results vary with the installed OS.

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
The workflow has not been run as part of this implementation session. Hosted
runners have no Jibo attached, so passing CI would establish host portability,
with robot USB transfers and the RCM-to-DFU entry still requiring hardware
tests.

The first hardware target is the owner's older Intel Mac. Apple silicon
requires another tester. Support remains experimental until the tested OS
versions, USB transfers, entry results, and readback results are recorded for
each architecture.
