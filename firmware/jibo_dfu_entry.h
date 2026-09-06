/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Dedicated recovery entry for Jibo's 2016 T124 U-Boot tree. */
#include <mmc.h>
#include <part.h>
#include <watchdog.h>

static int jibo_dfu_name_ok(const unsigned char *name)
{
	int i;
	for (i = 0; i < 32 && name[i]; ++i) {
		unsigned char c = name[i];
		if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
		      (c >= '0' && c <= '9') || c == '_' || c == '-'))
			return 0;
	}
	return i > 0 && i < 32;
}

static void jibo_dfu_entry(void)
{
	struct mmc *mmc;
	disk_partition_t info;
	char alternatives[8192], entry[128];
	int i, n;
	size_t used;
	lbaint_t offset, count;

	puts("Jibo RAM DFU v1: existing GPT; no boot/environment writes\n");
	/* Never execute persisted preboot/bootcmd or an embedded flasher command. */
	mmc = find_mmc_device(0);
	if (!mmc || mmc_init(mmc) ||
	    blk_select_hwpart_devnum(IF_TYPE_MMC, 0, 0))
		goto failed;
	if (mmc->block_dev.blksz != 512 || mmc->block_dev.lba < 34 ||
	    (unsigned long long)mmc->block_dev.lba > 0xffffffffULL)
		goto failed;
	/* Marker/GPT header and complete user area are upload-only; see backend patch. */
	n = snprintf(alternatives, sizeof(alternatives), "jibo-dfu-v1 raw 0 34");
	if (n < 0 || n >= sizeof(alternatives))
		goto failed;
	used = n;
	/* This old DFU stack has signed 32-bit lengths. Use <=1 GiB chunks. */
	for (offset = 0, i = 0; offset < mmc->block_dev.lba; offset += count, ++i) {
		count = mmc->block_dev.lba - offset;
		if (count > 0x200000)
			count = 0x200000;
		n = snprintf(entry, sizeof(entry), ";emmc-%03d raw 0x%llx 0x%llx",
			i, (unsigned long long)offset, (unsigned long long)count);
		if (n < 0 || n >= sizeof(entry) || used + n >= sizeof(alternatives))
			goto failed;
		memcpy(alternatives + used, entry, n + 1);
		used += n;
	}
	for (i = 1; i <= 128; ++i) {
		if (part_get_info(&mmc->block_dev, i, &info))
			continue;
		if (!jibo_dfu_name_ok(info.name) || !info.size ||
		    info.blksz != 512 || info.size > 0x3fffff ||
		    info.start >= mmc->block_dev.lba ||
		    info.size > mmc->block_dev.lba - info.start ||
		    !strncmp((char *)info.name, "emmc-", 5) ||
		    !strcmp((char *)info.name, "jibo-dfu-v1"))
			continue;
		n = snprintf(entry, sizeof(entry), ";%s part 0 %d", info.name, i);
		if (n < 0 || n >= sizeof(entry) || used + n >= sizeof(alternatives))
			goto failed;
		memcpy(alternatives + used, entry, n + 1);
		used += n;
	}
	if (setenv("dfu_alt_info", alternatives))
		goto failed;
	for (;;) {
		/* A detach without reset returns here; remain in recovery. */
		if (run_command("dfu 0 mmc 0", 0))
			goto failed;
		WATCHDOG_RESET();
	}
failed:
	puts("DFU initialization failed. Reset into RCM to retry.\n");
	for (;;) {
		WATCHDOG_RESET();
		udelay(1000);
	}
}
