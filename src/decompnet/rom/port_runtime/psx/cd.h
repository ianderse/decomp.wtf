/* The disc image the port reads in place (psx/cd.c): 2352-byte raw sectors (.bin, Mode 1 or Mode 2, or a .cue naming
 * one) or 2048-byte ones (.iso), the ISO 9660 file system on it, a sector cache. Plain C with no runtime dependency,
 * so the tests compile it on its own. libcd (psx/sdk/libcd.c) is the drive on top of it. */
#ifndef PSX_CD_H
#define PSX_CD_H
#include <stddef.h>
#include <stdint.h>

#define PSX_CD_RAW 2352   /* a whole sector: sync 12, header 4 (MSF BCD, mode), sub-header 8 (Mode 2), data */
#define PSX_CD_DATA 2048  /* user data of Mode 1 and Mode 2 Form 1 */
#define PSX_CD_FORM2 2324 /* user data of Mode 2 Form 2 (XA audio, STR video) */

/* open the image at path (a .cue opens the .bin its first FILE line names); 1 on success, else 0 and why in err */
int psx_disc_open(const char *path, char *err, size_t errlen);
void psx_disc_close(void);
int psx_disc_is_open(void);
const char *psx_disc_path(void);
uint32_t psx_disc_sectors(void);  /* sectors in the image */
int psx_disc_sector_size(void);   /* 2352, 2448 (with subchannel data) or 2048; 0 if none is open */

/* sector lba (LBA 0 is 00:02:00) as a whole raw sector. A 2048-byte image's sectors come back as Mode 2 Form 1 with
 * a made-up header (sync, MSF, mode 2, sub-header 00 00 08 00). NULL past the end. The pointer stays good for the
 * next 63 reads of other sectors (a 64-sector cache). */
const uint8_t *psx_disc_raw(uint32_t lba);
/* where the user data starts in a raw sector and how long it is (Mode 1: 16 and 2048; Mode 2 Form 1: 24 and 2048;
 * Form 2: 24 and 2324); 0 for a sector without data (mode 0) */
int psx_disc_user(const uint8_t *raw, int *offset);
/* copy n sectors' user data (2048 bytes each) from lba into dst; the number of sectors copied */
int psx_disc_read(uint32_t lba, void *dst, int n);

/* a file by its ISO 9660 path: '\' or '/', a leading "cdrom:" and the ";1" version optional, any case.
 * 1 with its first sector and size, or 0. */
int psx_disc_find(const char *path, uint32_t *lba, uint32_t *size);
/* the file sector lba belongs to (its path as on the disc, without ";1") and lba's byte offset in it; NULL if it is
 * in none (the system area, a directory) */
const char *psx_disc_name_at(uint32_t lba, uint32_t *offset);
/* SYSTEM.CNF's BOOT file (e.g. "SLUS_012.52"), "" if there is none */
const char *psx_disc_boot(void);

/* BCD and MSF <-> LBA (LBA 0 is 00:02:00) */
#define PSX_BCD(x) ((uint8_t)((((x) / 10) << 4) | ((x) % 10)))
#define PSX_UNBCD(x) (((((unsigned)(x)) >> 4) & 0xF) * 10 + (((unsigned)(x)) & 0xF))
void psx_lba_to_msf(uint32_t lba, uint8_t msf[3]);
uint32_t psx_msf_to_lba(const uint8_t msf[3]);
#endif
