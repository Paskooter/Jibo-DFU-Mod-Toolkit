# Stock Jibo 13.0.0 → jibo.io: first OTA over DFU

This experimental bridge changes only existing files, so a stock robot can
reach jibo.io, pair if it is still in OOBE, and download its first Phoenix OTA.
It **does not** install the full Phoenix firmware or make every service work by
itself. Run the jibo.io OTA immediately after setup/reboot; that OTA installs the
permanent trust store and remaining cloud-service changes.

This command accepts only the exact official Release 13.0.0 file hashes used
to develop it. It refuses modified or unknown images, non-contiguous files,
too-small preallocated files, and the bundled `jibo-file-v1` loader. It requires
a separately built, opt-in `jibo-file-v2` loader. This v2 loader has **not been
validated on hardware**; start with `--dry-run` and do not rely on this branch
for an unattended recovery. Follow [the candidate build notes](loader-build.md)
and keep the original full-flash/USB recovery route available.

The workflow patches both `rootfsA` and `rootfsB`, plus `services` and
`skills`. It changes every stock server-client region configuration, gives
Node's HTTPS clients an explicit ISRG Root X1 trust anchor without disabling
certificate verification, fixes the OTA downloader's separate HTTPS path, and
fixes backup/restore TLS. Since DFU cannot create files, it replaces the
expired, 2 KiB-allocated `DST_Root_CA_X3.crt` slot with ISRG Root X1 and points
these temporary clients at that existing path. The OTA should replace this
temporary arrangement with the normal Phoenix trust setup. A file write never
grows beyond the file's existing allocated blocks or changes its permissions.

The 22 files are:

| Partition | Existing paths patched |
| --- | --- |
| `rootfsA`, `rootfsB` (each) | `/usr/share/ca-certificates/mozilla/DST_Root_CA_X3.crt`; `lib/region_config.json` and `lib/http/node.js` under each of `/usr/lib/node_modules/@jibo/jibo-server-client`, `/usr/lib/node_modules/@jibo/jibo-log-client/node_modules/@jibo/jibo-server-client`, and `/usr/lib/node_modules/@jibo/jibo-ota-updater/node_modules/@jibo/jibo-server-client`; `/usr/lib/node_modules/@jibo/jibo-ota-updater/src/download-update.js` |
| `services` | `lib/region_config.json` and `lib/http/node.js` under `/bin/jibo-ssm/node_modules/@jibo/jibo-server-client`; `/bin/jibo-system-backup`; `/bin/jibo-system-restore` |
| `skills` | `lib/region_config.json` and `lib/http/node.js` under `/jibo/Jibo/Skills/oobe-config/node_modules/@jibo/jibo-server-client` |

For a confirmed Meerkat Rev02 robot, enter DFU with the matching candidate
loader and ShofEL helper (see the linked build notes). Do not use this RAM
profile on an unverified board revision:

```sh
sudo python3 jibo_dfu.py enter-dfu-shofel \
  --shofel .build/file-level-candidate/shofel-entry/shofel2_t124 \
  --loader .build/file-level-candidate/experimental-file-rpc-loader.bin \
  --confirm-meerkat-rev02
```

With the **v2 candidate running in DFU**, first check without writes:

```sh
sudo python3 jibo_dfu.py repoint-jibo-io --dry-run
```

Review the target list, then run the confirmed transaction:

```sh
sudo python3 jibo_dfu.py repoint-jibo-io
```

The tool displays the before/after hashes and creates one verified full-partition
rollback baseline for each touched partition **before the first write**. This
can take time and several gigabytes of host disk. The robot stays in DFU after
the command; exit DFU/reboot through the normal recovery procedure. Do not
unplug it during a write. If a write or readback fails, leave it in DFU and
restore the saved partition baseline; do not blindly reboot.

If `/var/jibo/credentials.json` exists, the command submits its credential pair
over HTTPS to jibo.io's adoption endpoint after all file writes verify. It
never prints or stores those secrets in the operation record. To also link a
previously paired robot to an existing portal account, generate a fresh claim
code in the portal and pipe it on stdin with `--claim-code-stdin`; the code is
not passed as a process argument. Without a claim code, adoption registers an
unclaimed robot, so finish claiming it in the portal. If there are no robot
credentials, the command leaves account creation to the jibo.io QR/OOBE flow.
If adoption fails, keep the robot in DFU and rerun the idempotent command after
fixing network/server access.

After reboot, use the jibo.io setup flow if the robot is in OOBE, then **install
the offered OTA**. Confirm that setup, OTA download/installation, and a voice
turn complete before considering the migration finished. A repointed stock
image alone is deliberately not considered connected/fully migrated.
