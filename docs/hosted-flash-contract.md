# Hosted flash v1: publisher/consumer contract

The toolkit's canonical machine-readable schema is
`assets/flash-images/contract-v1.json`. The publisher must use the identical schema
when exporting signed metadata and manifests. The two-choice calibration policy
replaces the older single-policy contract; older artifacts are refused rather
than reinterpreted. Updated policy metadata must be re-signed and rebound to
new archive/manifest hashes before it is offered.

## Documents and signatures

| Document | Schema | Signed `kind` | Version |
|---|---|---|---|
| Index | integer `1` | `phoenix-flash-index-v1` | No top-level version; each image row has its release label |
| Manifest | integer `1` | `phoenix-full-flash-v1` | Required `version`, exactly equal to selected index row |
| Embedded metadata | integer `1` | `phoenix-flash-metadata-v1` | `version` is **forbidden**; identity is `id` plus `components` |
| Signature envelope | integer `1` | None | Exactly `schema`, `key_id`, `payload`, `signature` |

The fixed v1 `key_id` is `phoenix-flash-2026-10`. `payload` and `signature` use
canonical base64. Ed25519 signs **the exact decoded UTF-8 payload bytes**: no
prefix/domain tag, prehash, or verifier-side JSON reserialization. Signed `kind`
and the strict expected-document schema provide semantic domain separation.
An index, manifest or metadata envelope cannot substitute for another document.
Publisher serialization uses sorted keys, compact separators, ASCII escapes and
no NaN. Crypto verification precedes parsing the decoded payload. Duplicate JSON
keys, floats/nonfinite numbers, control text, invalid types and unknown fields
are rejected. JSON boolean `true` is not schema/version integer `1`.

## Index and manifest binding

All timestamps are **integer Unix UTC seconds**, not ISO strings. The public DTO
formats them for display only. Index expiry is at most 86,400 seconds after
creation; future skew is at most 300 seconds. Sequence is monotonic, positive and
bounded. There are at most 128 rows; `latest_image_id` names one offered row.

Routes remain pinned to `https://jibo.io`:

- `/flash/index.json`
- `/flash/manifests/<id>.json`
- `/flash/images/<id>/<archive-sha256>.tar.bz2`

The row pins the **manifest envelope** byte length and SHA-256. The manifest pins
the entire archive byte length and SHA-256, and pins the **metadata envelope**
bytes with `embedded_metadata_sha256` (not `metadata_sha256`). Every shared
index-row/manifest field must agree, including release label, component tuple,
minimum toolkit version/capabilities, profile/strategy, URLs and release notes.
Every shared metadata/manifest field must agree except the intentionally distinct
`kind`. No unsigned display or provenance field can replace these bindings.

`version` is a bounded release label, not an inferred toolkit version.
`minimum_toolkit_version` follows the schema's version grammar. Installation
requires an explicitly assigned stable toolkit version meeting that floor and
all required capabilities. It does not manufacture a release version for an
unreleased toolkit. Standalone local package display uses the signed `id`, not a
fabricated metadata version. Hosted catalog display retains the signed row label.

## Archive member manifest and var policy

The tar.bz2 contains exactly four regular filesystem payloads and one signed
metadata envelope (bounded ancestor directory headers are also accepted):

- `phoenix-<id>/flash_jibo/output/images/{rootfs,services,skills,var}.ext4`
- `phoenix-<id>/phoenix-flash.json`

Each filesystem member record is exactly `name`, `role`, `size`, `sha256`,
`geometry`. Geometry is exactly `block_size`, `block_count`, `features`; allowed
block sizes are 1024/2048/4096 and `size == block_size * block_count`, sector
aligned. Four unique roles must be complete. `unpacked_size` is their exact sum.
Member bytes are independently hashed, and extraction reauthenticates the open
source descriptor. Unknown files/hooks, duplicate or traversal paths, links,
devices, missing members/markers and hash/size mismatches are refused. Hosted
verification never treats a missing signed marker as legacy.

Wire `var_policies` is exactly `["keep-calibration", "without-calibration"]`, in
that order, in both the manifest and embedded metadata. UI and API use the same
names. Subsets, duplicates, reordered arrays and obsolete values are refused.

The default **Keep this Jibo's calibration (recommended)** builds fresh package
var with this robot's verified local identity/camera calibration, supported IMU
and audio bytes when present, OOBE mode, hostname and hosts. Touchscreen and the
factory record are applied only from verified same-robot partition backups.
**Flash without calibration (unsafe)** writes the authenticated package var
unchanged and requires additional Cancel-default GUI/TUI confirmation or the CLI
`--without-calibration` flag plus `--confirm 'FLASH UPDATE'` (not `--yes`).
Both policies always capture a fresh verified current-var rollback before writing.
Interrupted flash resume
is refused before USB action; old records are not reinterpreted under a new policy.

Authenticated package prefixes must match the signed member hashes before local
calibration edits. Personalized candidates retain the prefix lengths; operation
records and mandatory Phoenix readback use their final candidate hashes. The
original signed package remains unchanged. Profile is
`jibo-t124-stock-gpt-13-v1`; strategy is `compact-prefix-v1`; capability is
`hosted-flash-v1`. Official base provenance, three source revisions, recipe and
gate-receipt hashes are required in both metadata and manifest.

Envelope/payload bounds are 262144/196608 bytes for index and manifest, and
131072/65536 bytes for metadata. Archive maximum is 4294967296 bytes, member
maximum 8589934592, unpacked maximum 34359738368, declared workspace maximum
137438953472. The toolkit reserves the greater of the signed workspace request
and its locally computed preparation/rollback requirement.

## Build boundary and release holds

Unsigned staging archives are not hosted firmware. The publisher validates inputs
and exports signed metadata before separate-output archive assembly. Assembly
derives a new signed manifest and index without editing already-signed bytes.
Publication remains a separate gate, never part of the toolkit's flash flow.

The shipped trust store pins the `phoenix-flash-2026-10` production public key.
Toolkit compatibility version 1.0.0 supports these calibration policies.
Tests inject their own ephemeral keys and loopback transport; they never use
production private keys. The signed index must be live before hosted downloads
are available. Calibration backups and restores stay on this computer.
Hardware boot, readback, recovery, official-input admission,
legal redistribution, trust custody and production hosting remain separate gates.
