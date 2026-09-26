# Experimental file-level DFU protocol

The file RPC uses paired MMC/ext4 DFU alternatives. `jibo-file-<partition>-in`
accepts a request; the matching `jibo-file-<partition>-out` uploads its
response. The read-only `jibo-file-v1` or `jibo-file-v2` raw alternate advertises the protocol
to the host. No request data is interpreted as a shell command or a U-Boot
filename, and the existing packaged loader does not advertise this capability.

All multi-byte wire fields are little-endian unless marked `digest`. Fixed
integers have no native-C padding; the implementation parses and writes the
fields explicitly.

The request begins with a 31-byte header:

| Offset | Size | Field |
| ---: | ---: | --- |
| 0 | 8 | Magic `JIBOFL1\0` |
| 8 | 1 | Operation: 1 read, 2 replace, 3 stat |
| 9 | 2 | UTF-8 path byte length |
| 11 | 4 | Replacement byte length (zero for read/stat) |
| 15 | 16 | Request nonce |

The header is followed by the absolute path bytes. Paths are 1–255 bytes,
valid UTF-8, contain no NUL/control character, and have no empty, `.` or `..`
component. Replacement requests then contain a 76-byte compare-and-write
precondition followed by 1–4096 replacement bytes on v1, or 1–12288 on v2:

| Offset in precondition | Size | Field |
| ---: | ---: | --- |
| 0 | 8 | Existing inode number |
| 8 | 4 | Existing file size |
| 12 | 4 | Allocated bytes |
| 16 | 4 | UID |
| 20 | 4 | GID |
| 24 | 4 | Mode including file type |
| 28 | 16 | Ext4 UUID bytes |
| 44 | 32 | SHA-256 of current file contents |

The response has a 61-byte header followed by its body: 8-byte magic
`JIBOR1\0\0`, the 16-byte request nonce, 1-byte status, 4-byte body length,
and a 32-byte SHA-256 digest of the body. A successful read body contains file
bytes; a successful stat body contains the 52-byte packed values
`<QIIIIIII16s` (inode, size, allocated bytes, UID, GID, mode, link count,
extent count, UUID). A successful write response has an empty body. Nonzero
status indicates rejection; the nonce still identifies that request.

The first implementation (v1) is deliberately narrow: only the five named GPT
partitions are exposed, and writes can replace existing regular files up to
4096 bytes when they occupy one allocated ext4 extent/block and have one hard
link. The experimental v2 candidate raises the bound to 12288 bytes and supports
either one initialized, contiguous extent or up to 12 contiguous legacy ext2
direct pointers. It still cannot grow a file beyond its existing allocated
blocks or alter its owner, mode, directory entries, or allocation bitmap. The
inode, every data block, block bitmap, inode bitmap, group descriptors,
and journal are checked before a write; symlink traversal, unallocated inodes
or blocks, uninitialized groups, and superblock/GDT/reserved-GDT blocks are
rejected. Group bitmap and inode-table pointers are also rejected if they
overlap those reserved ranges. META_BG descriptor locations and group
descriptor checksums are validated. No create, delete, rename, symlink, hole,
multi-extent, or allocation operation is provided. The ext4 volume must have
supported feature bits and a clean superblock/journal. The writer bypasses
ext4's journal entirely. It writes the existing data blocks and inode-size field directly
with checked block I/O, preserving the rest of the inode; host readback
checks content and metadata after each write. This has a power-loss window
between the data and inode writes. Recover by restoring an explicitly saved
partition image or reflashing the matching official system image. The host transaction saves or reuses one
verified `var` baseline if `var` is touched. Other partitions are backed up
only when explicitly selected with `backup-partitions`.

An interrupted DFU mailbox transfer can be retried from block zero; the
candidate loader resets file-RPC upload/download state for that retry. If the
USB DFU session itself has been reset, re-enter the DFU loader before retrying.

The Aero var fixture used during offline checks has 69 groups, 1024-byte
blocks, 7488 blocks/group, and `s_first_meta_bg=1`. The descriptor for inode
group 66 is descriptor block index 2: its META_BG location is group 64's first
block plus the backup superblock, physical block 479234. A classic-GDT fallback
would look at block 4. Group 66's inode bitmap is block 494211, and the Wi-Fi
file's data block 494272 is offset 63 in that group; the mode and Wi-Fi extents
are both allocated. These fixture facts are asserted without including the
private backup image in the repository.

On 2026-09-25, Moth entered DFU with this candidate and a read-only request for
`/jibo/mode.json` returned `{"mode":"normal"}` (17 bytes, SHA-256
`fa76e6a145e80d3ef8d955abd5a17426feba569df832d2cf6af7dbd5b86c5240`).
The saved Moth var image from 2026-09-24 contained `{"mode":"int-developer"}`;
because the files differ, that older image cannot serve as a same-state byte
comparison for the live read. A subsequent direct write changed the live file
from `normal` to `developer`; the immediate file-level readback matched SHA-256
`d6c405b54ef96016170d89a9095189a27bb031d6a5f9e134d0d7c91ce641a47c`.
Before and after, the file was inode 1455, owned by UID/GID 0:0, with mode
0600, one link, and the same ext4 UUID. The current 500 MiB var dump matched
an existing backup, so the write reused that rollback image. Other direct
file writes, including v2, have not yet been tested on hardware. Neither
candidate is enabled by the pinned loader.
