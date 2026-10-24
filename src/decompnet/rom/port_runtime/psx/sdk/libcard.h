/* libcard and the BIOS's memory card calls (libcard.c): slot 1 backed by a 128 KB card image (PSX_MEMCARD=path,
 * created formatted when missing), slot 2 empty; without PSX_MEMCARD no card at all. The asynchronous calls deliver
 * their SwCARD/HwCARD events a vblank later (psx_card_vblank, from libetc's frame), the BIOS's file calls on "bu00:"
 * are synchronous. The psx_bios_* functions take host pointers (gen/psx_sdk_stubs.c translates the game's). */
#ifndef PSX_LIBCARD_H
#define PSX_LIBCARD_H
#include "psx_sdk_types.h"

#define PSX_CARD_SIZE 0x20000 /* 16 blocks of 8 KB: 1024 frames (sectors) of 128 bytes */
#define PSX_CARD_FRAME 128
#define PSX_CARD_BLOCK 0x2000

void psx_card_vblank(void); /* each vblank: the events whose time has come */
int psx_card_pending(void); /* events on their way (TestEvent waits a vblank for them) */
void psx_card_format_image(uint8_t *img); /* the BIOS's format: "MC", 15 free directory frames, no broken sectors */

/* B0 4A InitCARD2, 4B StartCARD2, 4C StopCARD2, A0 70 _bu_init, A0 AD _card_auto, B0 50 _new_card */
int psx_bios_InitCARD(int pad_enable);
int psx_bios_StartCARD(void);
int psx_bios_StopCARD(void);
int psx_bios_bu_init(void);
int psx_bios_card_auto(int flag);
int psx_bios_new_card(void);
/* A0 AB _card_info, A0 AC _card_load, B0 4F _card_read, B0 4E _card_write: 1 started (the event follows), 0 refused */
int psx_bios_card_info(int chan);
int psx_bios_card_load(int chan);
int psx_bios_card_read(int chan, int sector, void *dst);
int psx_bios_card_write(int chan, int sector, const void *src);
/* B0 5C _card_status, B0 5D _card_wait: 1 ready, 2/4/8 busy reading/writing/info, 0x11 timeout, 0x21 error */
int psx_bios_card_status(int slot);
int psx_bios_card_wait(int slot);
/* libcard's own: the last channel, the "new card" flag cleared (_new_card + _card_write(chan, 0x3F)), a format */
int psx_card_chan(void);
int psx_card_clear(int chan);
int psx_card_format(int chan);

/* the BIOS's file calls on the card (B0 32 open, 33 lseek, 34 read, 35 write, 36 close, 41 format, 42 firstfile,
 * 43 nextfile, 45 erase), names "bu00:NAME" (slot 1) or "bu10:NAME" (slot 2, never there). open's mode: 0x200 creates
 * a file of mode >> 16 blocks (1 if 0); read and write move a byte position inside the file. firstfile/nextfile fill a
 * 40-byte direntry (name[20], attr 0x50, size, next 0, first sector, 0) and return 1, or 0 when no more match */
int psx_bios_open(const char *name, int mode);
int psx_bios_lseek(int fd, int offset, int whence);
int psx_bios_read(int fd, void *dst, int n);
int psx_bios_write(int fd, const void *src, int n);
int psx_bios_close(int fd);
int psx_bios_format(const char *dev);
int psx_bios_firstfile(const char *pattern, void *dirent);
int psx_bios_nextfile(void *dirent);
int psx_bios_erase(const char *name);

/* PSY-Q's libcard names, for a game that has them as named Sony functions (PSY-Q's longs are 32 bits: int here,
 * as the game passes them in 32-bit registers) */
void InitCARD(int pad_enable);
int StartCARD(void);
int StopCARD(void);
void _bu_init(void);
int _card_info(int chan);
int _card_load(int chan);
int _card_auto(int flag);
int _card_read(int chan, int block, unsigned char *buf);
int _card_write(int chan, int block, unsigned char *buf);
void _new_card(void);
int _card_status(int drv);
int _card_wait(int drv);
unsigned _card_chan(void);
int _card_clear(int chan);
int _card_format(int chan);
#endif
