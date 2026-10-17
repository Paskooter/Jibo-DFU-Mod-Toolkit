# Backups

The graphical window and text menu use the same flows. The main actions are
**Enter DFU with ShofEL (RAM loader)**, **Set mode and configure Wi-Fi**,
**Install a Phoenix image or an official update**, **Backups**, and
**More tools and local images**. Separate mode and Wi-Fi edits are in More tools.

## Make a backup

With this Jibo in DFU, choose **Backups → Back up every partition** or
**Back up specific partitions**. Every partition means the complete GPT partitions
this loader exposes, not raw eMMC, the partition table, unused gaps or Boot0/Boot1.
The checklist shows sizes and a selected total. Times are planning estimates,
not deadlines or promises about USB speed.

Review the partition list, total size, local free space and destination before
starting. A new run reads fresh images; it does not silently reuse an old set.
It reuses the existing partition readers and individual SHA-256 manifests.
The set manifest remains `backup-set.json`, schema 1.

Sets are saved in `Jibo-Backups/<robot label>/<UTC date-time>/`. When var is
selected it is read first and its local `/jibo/identity.json` supplies the name
(`/etc/hostname` is a fallback). A previous var backup can supply a label when
var is not selected. If the name is unavailable, a short serial-hash tag is used.
The four-word name is a display label only, never authorization to restore.

**Stop after this partition** lets the current USB read finish, then skips the
remaining partitions. Saved files are kept and the partial set is marked not
finished, not ready to restore. Keep the cable and window open until it stops.
A single large partition can take a long time. USB readers retain their existing
transfer timeouts. The text progress view shows elapsed time and the current
partition; the GUI also shows transfer output. Neither interface cancels an
active restore write midway.

## Restore a partition set

Choose **Backups → Restore partitions**, select a backup of this Jibo and check
only the partitions to replace. A set stored elsewhere can be selected using its
manifest or folder. Paths in the existing manifest format are absolute; manually
moving image files does not rewrite those paths, and missing files are refused.

Before the write review the existing backend checks:

- the connected robot's serial-hash tag against the set and each image manifest;
- partition names and live GPT first/last LBAs and byte sizes;
- image files, sizes, SHA-256 and individual backup-manifest identity;
- the var image's ext4 superblock when var is selected.

The review names the backup's robot, date and selected partitions. Cancel is the
default. After approval the existing restore writes only the selected partitions
and checks their readback. Another robot's backup is refused, even when the
four-word names match. A source set is locked throughout review and restore so
another toolkit window cannot delete it meanwhile.

## Saved backups

This list works without a connected robot. It shows robot name (or short tag),
UTC date, partitions, size and file state, newest first. Details offer:

- **Restore from this backup** — only for a complete set and the matching connected
  robot; all backend checks still run before the write review.
- **Open folder** — opens the selected local folder using the desktop file browser.
- **Check files** — hashes local image files and compares their saved SHA-256 values;
  it does not use the robot. A list entry's file-presence check is not a checksum check.
- **Delete this backup** — only for a new self-contained set inside the toolkit's
  backup root. The review names the robot, date, size and exact folder, and defaults
  to Cancel. It removes only that selected set, not parent folders, other backups
  or any robot data. In-use sets, shared image references and outside-folder images
  are refused. A host without file-lock support refuses this operation.

Old `backup-set-*` layouts and original `var-backup-*` / `partition-backup-*`
rollback images are listed without moving, rewriting or deleting them. An older
complete partition set can still be restored through its original manifest checks.
A standalone rollback image has no saved GPT extents, so the manager does not
invent a partition set or bypass those checks to restore it. It remains available
for the original recovery workflow.

Named, deletable sets are separate from the automatic original rollback store:
settings/flash operations keep their original durable rollback copies instead of
relying on a copy the manager can delete. Existing flash policy, calibration
handling and readback policy are unchanged. Backups stay on this computer; no
backup upload is added.
