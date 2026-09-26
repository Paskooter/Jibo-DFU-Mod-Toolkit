# Stock Jibo → jibo.io: first OTA over DFU

This experimental bridge changes only existing files to aim a stock robot's
server client and OTA downloader at jibo.io. Pairing and the first Phoenix OTA
still depend on the target server accepting the robot's region, credentials,
OTA filter, and update API. Those server-side requirements are not verified by
the local file checks.
It **does not** install the full Phoenix firmware or make every service work by
itself. Run the jibo.io OTA immediately after setup/reboot; that OTA installs the
permanent trust store and remaining cloud-service changes.

The helper currently recognizes the exact official 3.3.4 RTM, 5.4.0 EFT,
5.4.2 production, [12.10.0 production](https://pvindex.org/repository/platformos/builds/sqa-testing/jibo-pvt-flash-build-12.10.0-20180823-production.tar.bz2),
and 13.0.0 production file layouts checked offline. 12.10.0 shares the checked
13.0.0 OTA file hashes. It
detects each rootfs slot separately and probes the known nested client copies
when present. It also probes four optional client copies seen in an archived
3.3.0 skills package.
It refuses modified or unknown images, non-contiguous files,
too-small preallocated files, and the bundled `jibo-file-v1` loader. It requires
a separately built, opt-in `jibo-file-v2` loader. This v2 loader has **not been
validated on hardware**; start with `--dry-run` and do not rely on this branch
for an unattended recovery. Follow [the candidate build notes](loader-build.md)
and keep the original full-flash/USB recovery route available.

The workflow patches both `rootfsA` and `rootfsB`, plus `services` and
`skills`. It changes every stock server-client region configuration, gives
Node's HTTPS clients an explicit ISRG Root X1 trust anchor for `jibo.io` hosts
without disabling certificate verification, and gives the OTA downloader that
anchor only for `jibo.io` URLs. Other download hosts keep their existing TLS handling. Since
DFU cannot create files, it replaces the
expired, 2 KiB-allocated `DST_Root_CA_X3.crt` slot with ISRG Root X1 and points
these temporary clients at that existing path. The OTA should replace this
temporary arrangement with the normal Phoenix trust setup. A file write never
grows beyond the file's existing allocated blocks or changes its permissions.

The required files vary by installed layout:

| Partition | Existing paths patched |
| --- | --- |
| `rootfsA`, `rootfsB` (each) | `/usr/share/ca-certificates/mozilla/DST_Root_CA_X3.crt`; `lib/region_config.json` and `lib/http/node.js` under `/usr/lib/node_modules/@jibo/jibo-server-client`; `/usr/lib/node_modules/@jibo/jibo-ota-updater/src/download-update.js`. Known nested client copies under `jibo-log-client` and `jibo-ota-updater` are patched when present. |
| `services` | `lib/region_config.json` and `lib/http/node.js` under `/bin/jibo-ssm/node_modules/@jibo/jibo-server-client` |
| `skills` | `lib/region_config.json` and `lib/http/node.js` under `/jibo/Jibo/Skills/oobe-config/node_modules/@jibo/jibo-server-client`. Four older `@be/be` package copies are checked when present. |

For a confirmed Meerkat Rev02 robot, enter DFU with the matching candidate
loader and ShofEL helper (see the linked build notes). Do not use this RAM
profile on an unverified board revision:

```sh
sudo python3 jibo_dfu.py enter-dfu-shofel \
  --shofel .build/file-level-v2/shofel-entry/shofel2_t124 \
  --loader .build/file-level-v2/experimental-file-rpc-loader.bin \
  --confirm-meerkat-rev02
```

With the **v2 candidate running in DFU**, choose **More tools → Prepare a stock
robot for jibo.io OTA → Check this robot** in the guided menu, or first check
from the CLI without writes:

```sh
sudo python3 jibo_dfu.py repoint-jibo-io --dry-run
```

Review the target list, then run the confirmed transaction:

```sh
sudo python3 jibo_dfu.py repoint-jibo-io
```

The tool displays the before/after hashes. It does not automatically back up
the four system partitions: only `var` receives an automatic baseline when
it is changed, and this workflow does not change `var`. If you want rollback
images, use `backup-partitions` to select these partitions before repointing.
Keep the official update package available so the system partitions can be
reflashed if needed. The robot stays in DFU after the command; exit DFU/reboot
through the normal recovery procedure. Do not unplug it during a write. If a
write or readback fails, leave it in DFU and repair the affected partition
before rebooting.

If `/var/jibo/credentials.json` exists, add `--adopt-existing` to submit its
credential pair over HTTPS to `api.jibo.io`'s adoption endpoint after all file
writes verify. This optional endpoint is defined in the
[Phoenix account server source](https://pvindex.org/gitea/pasketti/phoenix/src/branch/main/packages/account/src/robotAdoption.js);
live deployment has not been checked. The default command does not read
or send credentials. The
adoption option never prints or stores those secrets in the operation record. To also link a
previously paired robot to an existing portal account, generate a fresh claim
code in the portal and pipe it on stdin with `--claim-code-stdin`; the code is
not passed as a process argument. Without a claim code, adoption registers an
unclaimed robot, so finish claiming it in the portal. A valid `friendlyId` in
the saved credentials is included; otherwise Phoenix assigns a new display ID.
If there are no robot
credentials, the command leaves account creation to the jibo.io QR/OOBE flow.
If adoption fails, keep the robot in DFU and rerun the idempotent command after
fixing network/server access.

After reboot, use the jibo.io setup flow if the robot is in OOBE, then **install
the offered OTA**. Confirm that setup, OTA download/installation, and a voice
turn complete before considering the migration finished. A repointed stock
image alone is deliberately not considered connected/fully migrated.
