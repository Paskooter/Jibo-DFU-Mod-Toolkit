# Calibration sharing and lookup (v3)

After a verified rollback `var` backup, the graphical and text menus can offer
**Share this Jibo's factory calibration?** Sharing is recommended and initially
selected, but nothing is uploaded until you press **Share calibration
(recommended)**. **Keep local only**, Escape, and closing the prompt do not
upload. Declining or a network failure never blocks your local backup or flash.

Only strictly validated measurements actually found in this robot's backup are
eligible. Identity, credentials, Wi-Fi, faces, photos, recordings, camera captures
and distortion caches are never upload inputs. Unsupported formats stay local.
The hardware lookup code is robot-linkable, not a promise of absolute anonymity.

**More tools → Calibration sharing** changes the choice for the connected robot.
A remembered decline survives later backups. A saved contribution is not silently
re-uploaded. New measurements require another visible consent prompt.

A submission awaiting operator review is **Submitted for review**, not an available
recovery copy. The client reads the exact candidate status before claiming it was
stored. An ambiguous network/readback result is **Submission outcome unknown;
your local backup is safe**. There is no automatic retry queue.

## Removal receipts

The toolkit creates a separate Ed25519 removal key for each robot/calibration
contribution and durably saves its private receipt **before** submitting. Receipts
live under `Jibo-Backups/calibration/receipts/`, with directory mode 0700 and file
mode 0600. Keep this file private and backed up. Never send it in diagnostics.
**More tools → Remove my shared calibration** uses the receipt and verifies the
candidate status and approved lookup after removal. Already-downloaded copies
cannot be recalled. A duplicate submitted from another computer does not grant
that computer the original contributor's removal authority. Without a receipt,
contact `support@jibo.io`; do not send a backup, identity, or private key.

## Fresh setup

The toolkit prefers this robot's verified local calibration. When it cannot find
that camera set, it can offer **Look up this Jibo's calibration**. Lookup and using
the verified result are separate affirmative choices. Results display
**Community reviewed; not factory-authenticated**. A signature proves which
service signed the approved measurements, not that the measurements are factory
true or physically safe.

A database result cannot recover lost identity. Fresh setup still requires this
robot's own verified local identity, and refuses identity fields outside the
new path's audited profile. The offline candidate retains the exact measurement
bytes, removes stale distortion caches, is checked/read back locally, and requires
a new resolved-input write review. Database-sourced writes require USB readback.
Remote rootfs/skills items currently refuse restoration until their partition
ownership/mode profile is audited; they are never silently dropped.

## Install and release gates

The optional client uses `cryptography` Ed25519, tested here with **49.0.0**.
If it or the calibration core is absent, sharing/retrieval is unavailable; ordinary
offline toolkit actions remain usable. No automatic package installation occurs.
Install it in your invoking-user Python environment (`python3 -m pip install
cryptography==49.0.0`), not as root. Native/frozen packaging must be tested separately.

The production signing pin list is intentionally empty and marked TODO. Until
root publishes reviewed public key pins, all production downloads fail closed.
`JIBO_CALIBRATION_ORIGIN` is a test override: HTTPS origins or exact
`http://127.0.0.1:<ephemeral-port>` only, no redirects or proxy environment routing.
No public service deployment, signing-key creation, or robot write is part of this
change.
