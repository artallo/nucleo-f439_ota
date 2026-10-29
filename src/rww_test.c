/*
 * Проверка read-while-write: стирание/запись slot1 (банк 2) из shell,
 * пока в банке 1 исполняется код и крутятся сетевые потоки.
 *
 * Зачем: на F4 dual-bank означает, что CPU не останавливается на выборке
 * команд из банка 1, пока контроллер стирает сектор банка 2. Если бы это
 * было не так, каждое стирание 128 КБ сектора (~1-2 с) замораживало бы
 * сеть. Это также проверяет, что partitions из DTS совпадают с реальными
 * секторами (стирание ложится ровно на границы страниц).
 *
 * Команды: rww info | rww erase [pages] | rww write [kb] | rww verify [kb]
 * Все печатают строку "#RWW <cmd> done <ms>" — маркер для хост-скрипта.
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <zephyr/kernel.h>
#include <zephyr/shell/shell.h>
#include <zephyr/storage/flash_map.h>
#include <zephyr/drivers/flash.h>
#include <zephyr/logging/log.h>
#include <stdlib.h>
#include <string.h>

LOG_MODULE_REGISTER(rww, LOG_LEVEL_INF);

#define TARGET_ID    PARTITION_ID(slot1_partition)
#define CHUNK        1024U

/* Детерминированный шаблон: слово зависит от абсолютного смещения в разделе */
static inline uint32_t pattern(uint32_t off)
{
	return (off * 2654435761U) ^ 0xA5A5A5A5U;
}

static void fill_pattern(uint8_t *buf, uint32_t off, size_t len)
{
	for (size_t i = 0; i < len; i += 4) {
		uint32_t w = pattern(off + i);

		memcpy(buf + i, &w, 4);
	}
}

static int open_target(const struct shell *sh, const struct flash_area **fa)
{
	int rc = flash_area_open(TARGET_ID, fa);

	if (rc) {
		shell_error(sh, "flash_area_open: %d", rc);
	}
	return rc;
}

static int cmd_info(const struct shell *sh, size_t argc, char **argv)
{
	const struct flash_area *fa;
	struct flash_pages_info pi;
	uint32_t off = 0;

	if (open_target(sh, &fa)) {
		return -EIO;
	}
	shell_print(sh, "slot1: dev=%s off=0x%08lx size=%u KB", fa->fa_dev->name,
		    (unsigned long)fa->fa_off, (unsigned int)(fa->fa_size / 1024));
	while (off < fa->fa_size) {
		if (flash_get_page_info_by_offs(fa->fa_dev, fa->fa_off + off, &pi)) {
			break;
		}
		shell_print(sh, "  sector %2u: 0x%08lx + %3u KB", pi.index,
			    (unsigned long)pi.start_offset, (unsigned int)(pi.size / 1024));
		off += pi.size;
	}
	flash_area_close(fa);
	return 0;
}

static int cmd_erase(const struct shell *sh, size_t argc, char **argv)
{
	const struct flash_area *fa;
	struct flash_pages_info pi;
	uint32_t off = 0, n = 0;
	uint32_t limit = (argc > 1) ? strtoul(argv[1], NULL, 0) : UINT32_MAX;
	int64_t t0 = k_uptime_get();

	if (open_target(sh, &fa)) {
		return -EIO;
	}
	while (off < fa->fa_size && n < limit) {
		int64_t ts = k_uptime_get();
		int rc;

		if (flash_get_page_info_by_offs(fa->fa_dev, fa->fa_off + off, &pi)) {
			break;
		}
		rc = flash_area_erase(fa, off, pi.size);
		shell_print(sh, "#RWW erase sector %u (%u KB) rc=%d %lld ms", pi.index,
			    (unsigned int)(pi.size / 1024), rc, k_uptime_get() - ts);
		if (rc) {
			break;
		}
		off += pi.size;
		n++;
	}
	flash_area_close(fa);
	shell_print(sh, "#RWW erase done %lld", k_uptime_get() - t0);
	return 0;
}

static int cmd_write(const struct shell *sh, size_t argc, char **argv)
{
	static uint8_t buf[CHUNK];
	const struct flash_area *fa;
	uint32_t len, off;
	int64_t t0 = k_uptime_get();
	int rc = 0;

	if (open_target(sh, &fa)) {
		return -EIO;
	}
	len = (argc > 1) ? strtoul(argv[1], NULL, 0) * 1024 : fa->fa_size;
	len = MIN(len, fa->fa_size);

	for (off = 0; off < len; off += CHUNK) {
		fill_pattern(buf, off, CHUNK);
		rc = flash_area_write(fa, off, buf, CHUNK);
		if (rc) {
			shell_error(sh, "write @0x%x rc=%d", off, rc);
			break;
		}
	}
	flash_area_close(fa);

	int64_t dt = k_uptime_get() - t0;

	shell_print(sh, "#RWW write done %lld ms, %u KB, %lld KB/s rc=%d", dt, off / 1024,
		    dt ? (off / 1024) * 1000 / dt : 0, rc);
	return rc;
}

static int cmd_verify(const struct shell *sh, size_t argc, char **argv)
{
	static uint8_t buf[CHUNK], ref[CHUNK];
	const struct flash_area *fa;
	uint32_t len, off, bad = 0;
	int64_t t0 = k_uptime_get();

	if (open_target(sh, &fa)) {
		return -EIO;
	}
	len = (argc > 1) ? strtoul(argv[1], NULL, 0) * 1024 : fa->fa_size;
	len = MIN(len, fa->fa_size);

	for (off = 0; off < len; off += CHUNK) {
		int rc = flash_area_read(fa, off, buf, CHUNK);

		if (rc) {
			shell_error(sh, "read @0x%x rc=%d", off, rc);
			break;
		}
		fill_pattern(ref, off, CHUNK);
		if (memcmp(buf, ref, CHUNK)) {
			if (bad < 5) {
				shell_warn(sh, "mismatch @0x%x", off);
			}
			bad++;
		}
	}
	flash_area_close(fa);
	shell_print(sh, "#RWW verify done %lld ms, %u KB, bad chunks %u", k_uptime_get() - t0,
		    off / 1024, bad);
	return bad ? -EIO : 0;
}

SHELL_STATIC_SUBCMD_SET_CREATE(rww_cmds,
	SHELL_CMD(info, NULL, "slot1 sectors", cmd_info),
	SHELL_CMD(erase, NULL, "erase slot1 [pages]", cmd_erase),
	SHELL_CMD(write, NULL, "write pattern [kb]", cmd_write),
	SHELL_CMD(verify, NULL, "verify pattern [kb]", cmd_verify),
	SHELL_SUBCMD_SET_END);
SHELL_CMD_REGISTER(rww, &rww_cmds, "read-while-write test on slot1", NULL);
