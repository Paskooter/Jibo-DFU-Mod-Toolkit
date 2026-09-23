"""Focused checks for the source-level large-skills DFU mapping."""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRY = (ROOT / "firmware/jibo_dfu_entry.h").read_text()
PATCH = (ROOT / "firmware/entry.patch").read_text()


def skills_chunk_blocks():
    match = re.search(r"#define JIBO_DFU_MAX_CHUNK_BLOCKS (0x[0-9a-fA-F]+)ULL", ENTRY)
    if not match:
        raise AssertionError("skills chunk size constant is missing")
    return int(match.group(1), 16)


def skills_chunks(start, size):
    """Model the generated names and LBA ranges for one GPT partition."""
    chunk_blocks = skills_chunk_blocks()
    if size <= 0x3fffff:
        return []
    chunks = []
    offset = 0
    while offset < size:
        if len(chunks) >= 1000:
            raise ValueError("three-digit skills chunk names are exhausted")
        count = min(chunk_blocks, size - offset)
        chunks.append((f"skills-{len(chunks):03d}", start + offset, count))
        offset += count
    return chunks


def backend_helper_source():
    lines = PATCH.splitlines()
    define = lines.index("+#define JIBO_DFU_MAX_CHUNK_BLOCKS 0x200000U")
    start = define - 1
    while start >= 0 and lines[start] != "+#ifdef CONFIG_JIBO_DFU_ENTRY":
        start -= 1
    end = define + 1
    while end < len(lines) and lines[end] != "+#endif":
        end += 1
    if start < 0 or end == len(lines):
        raise AssertionError("skills write guard block is missing")
    return "\n".join(line[1:] for line in lines[start:end + 1])


class SkillsDfuLayoutTests(unittest.TestCase):
    def test_large_skills_partition_maps_to_exact_bounded_chunks(self):
        # GPT size from the official full-flash layout used by update tests.
        size_bytes = 10_991_139_328
        self.assertEqual(size_bytes % 512, 0)
        start = 8_000_000
        chunks = skills_chunks(start, size_bytes // 512)

        self.assertEqual([item[0] for item in chunks],
                         [f"skills-{index:03d}" for index in range(11)])
        self.assertTrue(all(count <= 0x200000 for _, _, count in chunks))
        self.assertEqual(chunks[0][1], start)
        self.assertEqual(chunks[-1][2], 495_549)
        self.assertEqual(sum(count for _, _, count in chunks), size_bytes // 512)
        for previous, current in zip(chunks, chunks[1:]):
            self.assertEqual(current[1], previous[1] + previous[2])

    def test_small_skills_partition_keeps_the_regular_partition_alt(self):
        self.assertEqual(skills_chunks(1000, 0x3fffff), [])
        self.assertEqual(len(skills_chunks(1000, 0x400000)), 2)

    def test_entry_checks_gpt_range_and_emits_raw_chunk_alts(self):
        self.assertIn('!strcmp((char *)info.name, "skills")', ENTRY)
        self.assertIn('info.start >= mmc->block_dev.lba', ENTRY)
        self.assertIn('info.size > mmc->block_dev.lba - info.start', ENTRY)
        self.assertIn('";skills-%03d raw 0x%llx 0x%llx"', ENTRY)
        self.assertIn('if (skill_count > JIBO_DFU_MAX_CHUNK_BLOCKS)', ENTRY)

    def test_backend_guard_compiles_and_allows_only_exact_gpt_slices(self):
        helper = backend_helper_source()
        harness = r'''#include <stdio.h>
#include <string.h>
#include <stdlib.h>

typedef unsigned long long lbaint_t;
typedef unsigned long long u64;
enum dfu_layout { DFU_RAW_ADDR = 1, DFU_FS_EXT4 = 2 };
struct block_desc { lbaint_t lba; unsigned int blksz; };
struct mmc { struct block_desc block_dev; };
typedef struct {
    char name[32];
    lbaint_t start, size;
    unsigned int blksz;
} disk_partition_t;
struct mmc_internal_data {
    int dev_num;
    unsigned int lba_start, lba_size, lba_blk_size;
    int hw_partition;
};
struct dfu_entity {
    char name[32];
    enum dfu_layout layout;
    union { struct mmc_internal_data mmc; } data;
};
static disk_partition_t g_skills;
static int g_have_skills = 1;
static int part_get_info(struct block_desc *desc, int part, disk_partition_t *out)
{
    (void)desc;
    if (part != 1 || !g_have_skills)
        return -1;
    *out = g_skills;
    return 0;
}
#define CONFIG_JIBO_DFU_ENTRY 1
%s

static void configure(struct dfu_entity *dfu, struct mmc *mmc,
                      const char *name, unsigned int start, unsigned int size)
{
    memset(dfu, 0, sizeof(*dfu));
    strcpy(dfu->name, name);
    dfu->layout = DFU_RAW_ADDR;
    dfu->data.mmc.dev_num = 0;
    dfu->data.mmc.hw_partition = -22;
    dfu->data.mmc.lba_blk_size = 512;
    dfu->data.mmc.lba_start = start;
    dfu->data.mmc.lba_size = size;
    mmc->block_dev.blksz = 512;
}

int main(void)
{
    struct dfu_entity dfu;
    struct mmc mmc;
    memset(&g_skills, 0, sizeof(g_skills));
    strcpy(g_skills.name, "skills");
    g_skills.start = 123456;
    g_skills.size = 0x200000ULL + 91;
    g_skills.blksz = 512;
    mmc.block_dev.lba = g_skills.start + g_skills.size + 7;

    configure(&dfu, &mmc, "skills-000", 123456, 0x200000);
    if (!jibo_dfu_skills_write_ok(&dfu, &mmc)) return 1;
    configure(&dfu, &mmc, "skills-001", 123456 + 0x200000, 91);
    if (!jibo_dfu_skills_write_ok(&dfu, &mmc)) return 2;

    configure(&dfu, &mmc, "skills-001", 123456 + 0x200000 + 1, 91);
    if (jibo_dfu_skills_write_ok(&dfu, &mmc)) return 3;
    configure(&dfu, &mmc, "skills-001", 123456 + 0x200000, 92);
    if (jibo_dfu_skills_write_ok(&dfu, &mmc)) return 4;
    configure(&dfu, &mmc, "skills-002", 123456 + 0x400000, 1);
    if (jibo_dfu_skills_write_ok(&dfu, &mmc)) return 5;
    configure(&dfu, &mmc, "skills-00", 123456, 1);
    if (jibo_dfu_skills_write_ok(&dfu, &mmc)) return 6;
    configure(&dfu, &mmc, "emmc-000", 123456, 1);
    if (jibo_dfu_skills_write_ok(&dfu, &mmc)) return 7;

    configure(&dfu, &mmc, "skills-000", 123456, 0x200000);
    dfu.data.mmc.dev_num = 1;
    if (jibo_dfu_skills_write_ok(&dfu, &mmc)) return 8;
    dfu.data.mmc.dev_num = 0;
    dfu.data.mmc.hw_partition = 0;
    if (jibo_dfu_skills_write_ok(&dfu, &mmc)) return 9;
    dfu.data.mmc.hw_partition = -22;
    dfu.layout = DFU_FS_EXT4;
    if (jibo_dfu_skills_write_ok(&dfu, &mmc)) return 10;

    dfu.layout = DFU_RAW_ADDR;
    mmc.block_dev.lba = g_skills.start + g_skills.size - 1;
    if (jibo_dfu_skills_write_ok(&dfu, &mmc)) return 11;
    mmc.block_dev.lba = g_skills.start + g_skills.size + 7;
    g_have_skills = 0;
    if (jibo_dfu_skills_write_ok(&dfu, &mmc)) return 12;
    return 0;
}
''' % helper
        cc = shutil.which("cc")
        if not cc:
            self.skipTest("host C compiler is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "skills_guard.c"
            binary = Path(temp) / "skills_guard"
            source.write_text(harness)
            subprocess.run([cc, "-std=c99", "-Wall", "-Wextra", "-Werror",
                            str(source), "-o", str(binary)], check=True)
            subprocess.run([str(binary)], check=True)

    def test_emmc_backup_alts_remain_read_only(self):
        self.assertIn('!strncmp(dfu->name, "emmc-", 5)', PATCH)
        self.assertIn('return -EPERM;', PATCH)


if __name__ == "__main__":
    unittest.main()
