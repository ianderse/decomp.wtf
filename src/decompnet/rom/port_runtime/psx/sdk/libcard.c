/* The memory card, from psx-spx ("Memory Card Data Format", "BIOS Memory Card Functions", "BIOS Events"), under the
 * BIOS's calls and PSY-Q's libcard names.
 *
 * The model: slot 1 is the card image PSX_MEMCARD names (128 KB, the raw frames: what emulators call .mcr/.mcd),
 * created formatted when the file is missing and written back whole after every write; slot 2 is always empty; with
 * PSX_MEMCARD unset neither slot has a card. A file of the wrong size is a broken card (ERROR events, never written).
 *
 * The asynchronous calls (_card_info, _card_load, _card_read, _card_write) answer with events a vblank later, from
 * libetc's frame (psx_card_vblank), as the card's interrupt would: the game calls _card_info and THEN polls its
 * events, and a delivery inside the call would be cleared by nothing but would arrive before the game drained its
 * events. What each delivers (specs: IOE 0x0004 done, ERROR 0x8000, TIMEOUT 0x0100 no card, NEW 0x2000):
 *   _card_info   SwCARD and HwCARD: TIMEOUT without a card, ERROR for a broken one, NEW while the card's "new card"
 *                flag is set (it is at power-on, until a write: _card_clear does _new_card + a write of sector 0x3F),
 *                else IOE.
 *   _card_load   SwCARD and HwCARD: TIMEOUT / ERROR as above, NEW for an unformatted card (no "MC" in frame 0),
 *                else IOE (the directory is "loaded": it is read in place here).
 *   _card_read / _card_write   HwCARD only: TIMEOUT / ERROR as above, NEW while the new-card flag is set and _new_card
 *                was not called first, else IOE; a write clears the flag. Sectors 0..0x3FF (0x400 accepted as on the
 *                BIOS, a no-op); a write with a null source (libcard's _card_clear passes 0) leaves the frame alone.
 *
 * The card's layout (block 0, frames of 128 bytes): frame 0 "MC" and the XOR checksum at 0x7F; frames 1-15 the
 * directory, one per data block 1-15 (0x00 state: 0x51 a file's first block, 0x52 middle, 0x53 last, 0xA0 free,
 * 0xA1-0xA3 deleted; 0x04 the file size in its first block; 0x08 the next block 0-14 or 0xFFFF; 0x0A the name, 20
 * characters; 0x7F the checksum); frames 16-35 the broken sector list (0xFFFFFFFF each: none); 36-62 0xFF; frame 63
 * a copy of frame 0 (the write test frame). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../psx_runtime.h"
#include "libapi.h"
#include "libcard.h"

enum { CARD_UNKNOWN, CARD_NONE, CARD_OK, CARD_BAD };
static int state = CARD_UNKNOWN;
static const char *path;
static uint8_t img[PSX_CARD_SIZE];
static int new_flag, allow_new, last_chan, auto_format, status = 1;
static struct { uint32_t cls, spec; int vblanks; } pending[32];
static int n_pending;
static unsigned info_logs, load_logs, io_logs, file_logs, deliver_logs;

/* ---- the image */
static uint8_t frame_sum(const uint8_t *f) {
    uint8_t x = 0;
    int i;
    for (i = 0; i < 0x7F; i++) x ^= f[i];
    return x;
}
static void put32(uint8_t *p, uint32_t v) { p[0] = v; p[1] = v >> 8; p[2] = v >> 16; p[3] = v >> 24; }
static uint32_t get32(const uint8_t *p) { return p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24; }
static uint8_t *dir(int block) { return img + block * PSX_CARD_FRAME; } /* block 1-15's directory frame */

void psx_card_format_image(uint8_t *card) {
    int i;
    memset(card, 0, PSX_CARD_SIZE);
    card[0] = 'M';
    card[1] = 'C';
    card[0x7F] = frame_sum(card);
    for (i = 1; i < 16; i++) {
        uint8_t *d = card + i * PSX_CARD_FRAME;
        memset(d, 0, PSX_CARD_FRAME);
        put32(d, 0xA0);
        d[8] = d[9] = 0xFF;
        d[0x7F] = frame_sum(d);
    }
    for (i = 16; i < 36; i++) {
        uint8_t *f = card + i * PSX_CARD_FRAME;
        put32(f, 0xFFFFFFFFu);
        f[0x7F] = frame_sum(f);
    }
    memset(card + 36 * PSX_CARD_FRAME, 0xFF, 27 * PSX_CARD_FRAME);
    memcpy(card + 63 * PSX_CARD_FRAME, card, PSX_CARD_FRAME);
}

static int formatted(void) { return img[0] == 'M' && img[1] == 'C'; }

static void save(void) {
    FILE *f;
    if (state != CARD_OK || !path) return;
    f = fopen(path, "wb");
    if (!f || fwrite(img, 1, PSX_CARD_SIZE, f) != PSX_CARD_SIZE)
        psx_log("card: can't write %s; the card's contents live in memory only\n", path);
    if (f) fclose(f);
}

static void card_init(void) {
    FILE *f;
    long size;
    if (state != CARD_UNKNOWN) return;
    path = getenv("PSX_MEMCARD");
    if (!path || !*path) {
        state = CARD_NONE;
        psx_log("card: no memory card (PSX_MEMCARD=path backs slot 1 with a 128 KB card image)\n");
        return;
    }
    new_flag = 1; /* a card at power-on has the "new card" flag set until something is written */
    f = fopen(path, "rb");
    if (!f) {
        psx_card_format_image(img);
        state = CARD_OK;
        save();
        psx_log("card: slot 1 is %s: created, formatted (128 KB)\n", path);
        return;
    }
    fseek(f, 0, SEEK_END);
    size = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (size != PSX_CARD_SIZE || fread(img, 1, PSX_CARD_SIZE, f) != PSX_CARD_SIZE) {
        state = CARD_BAD;
        psx_log("card: slot 1 is %s: %ld bytes, not a 128 KB card image: a broken card (ERROR events)\n", path, size);
    } else {
        state = CARD_OK;
        psx_log("card: slot 1 is %s (%s)\n", path, formatted() ? "formatted" : "not formatted");
    }
    fclose(f);
}

/* the card behind a channel (0x00 slot 1, 0x10 slot 2; a multitap's card in the low nibble) */
static int card_at(int chan) {
    card_init();
    if ((chan & 0xFF) != 0x00) return CARD_NONE;
    return state;
}

/* ---- events, a vblank from now */
static void later(uint32_t cls, uint32_t spec) {
    if (n_pending < (int)(sizeof pending / sizeof pending[0])) {
        pending[n_pending].cls = cls;
        pending[n_pending].spec = spec;
        pending[n_pending].vblanks = 1;
        n_pending++;
    }
}

int psx_card_pending(void) { return n_pending; }

void psx_card_vblank(void) {
    int i, k = 0, n = n_pending;
    uint32_t cls[32], spec[32];
    int due = 0;
    for (i = 0; i < n; i++) {
        if (--pending[i].vblanks <= 0) {
            cls[due] = pending[i].cls;
            spec[due++] = pending[i].spec;
        } else {
            pending[k++] = pending[i];
        }
    }
    n_pending = k;
    for (i = 0; i < due; i++) {
        int got = psx_bios_DeliverEvent(cls[i], spec[i]);
        if (psx_log_due(&deliver_logs))
            psx_log("card: %s %s delivered at vblank %d (%d event%s waiting for it)\n",
                    cls[i] == PSX_EV_SwCARD ? "SwCARD" : "HwCARD",
                    spec[i] == PSX_EvSpIOE ? "IOE" : spec[i] == PSX_EvSpTIMOUT ? "TIMEOUT" :
                    spec[i] == PSX_EvSpNEW ? "NEW" : "ERROR", psx_frames(), got, got == 1 ? "" : "s");
    }
}

static uint32_t result_spec(int st) {
    return st == CARD_NONE ? PSX_EvSpTIMOUT : st == CARD_BAD ? PSX_EvSpERROR : PSX_EvSpIOE;
}
static void set_status(uint32_t spec) {
    status = spec == PSX_EvSpTIMOUT ? 0x11 : spec == PSX_EvSpERROR ? 0x21 : 1;
}
static const char *spec_name(uint32_t spec) {
    return spec == PSX_EvSpIOE ? "IOE" : spec == PSX_EvSpTIMOUT ? "TIMEOUT" : spec == PSX_EvSpNEW ? "NEW" : "ERROR";
}

/* ---- the BIOS's card calls */
int psx_bios_InitCARD(int pad_enable) {
    (void)pad_enable;
    card_init();
    return 0;
}
int psx_bios_StartCARD(void) { card_init(); return 1; }
int psx_bios_StopCARD(void) { return 1; }
int psx_bios_bu_init(void) { card_init(); return 0; }
int psx_bios_card_auto(int flag) { int old = auto_format; auto_format = flag; return old; }
int psx_bios_new_card(void) { allow_new = 1; return 0; }

int psx_bios_card_info(int chan) {
    int st = card_at(chan);
    uint32_t spec = st == CARD_OK && new_flag ? PSX_EvSpNEW : result_spec(st);
    last_chan = chan;
    set_status(spec);
    later(PSX_EV_HwCARD, spec);
    later(PSX_EV_SwCARD, spec);
    if (psx_log_due(&info_logs))
        psx_log("card: _card_info(0x%02X) at vblank %d: %s next vblank\n", chan, psx_frames(), spec_name(spec));
    return 1;
}

int psx_bios_card_load(int chan) {
    int st = card_at(chan);
    uint32_t spec = st == CARD_OK && !formatted() ? PSX_EvSpNEW : result_spec(st);
    last_chan = chan;
    set_status(spec);
    later(PSX_EV_HwCARD, spec);
    later(PSX_EV_SwCARD, spec);
    if (psx_log_due(&load_logs))
        psx_log("card: _card_load(0x%02X) at vblank %d: %s next vblank\n", chan, psx_frames(), spec_name(spec));
    return 1;
}

static int sector_io(int chan, int sector, void *dst, const void *src) {
    int st = card_at(chan);
    uint32_t spec = result_spec(st);
    if (sector < 0 || sector > 0x400) return 0;
    last_chan = chan;
    if (st == CARD_OK && new_flag && !allow_new) spec = PSX_EvSpNEW;
    if (spec == PSX_EvSpIOE && sector < 0x400) {
        if (dst) memcpy(dst, img + sector * PSX_CARD_FRAME, PSX_CARD_FRAME);
        if (src) memcpy(img + sector * PSX_CARD_FRAME, src, PSX_CARD_FRAME);
    }
    if (spec == PSX_EvSpIOE && !dst) {
        new_flag = 0;
        if (src) save();
    }
    allow_new = 0;
    set_status(spec);
    later(PSX_EV_HwCARD, spec);
    if (psx_log_due(&io_logs))
        psx_log("card: _card_%s(0x%02X, sector 0x%03X) at vblank %d: HwCARD %s next vblank\n", dst ? "read" : "write",
                chan, sector, psx_frames(), spec_name(spec));
    return 1;
}
int psx_bios_card_read(int chan, int sector, void *dst) {
    static uint8_t scratch[PSX_CARD_FRAME];
    return sector_io(chan, sector, dst ? dst : scratch, NULL);
}
int psx_bios_card_write(int chan, int sector, const void *src) { return sector_io(chan, sector, NULL, src); }

int psx_bios_card_status(int slot) {
    (void)slot;
    return n_pending ? 8 : status;
}
int psx_bios_card_wait(int slot) {
    while (n_pending && psx_vblank_wait()) {}
    return psx_bios_card_status(slot);
}

int psx_card_chan(void) { return last_chan; }
int psx_card_clear(int chan) {
    psx_bios_new_card();
    return psx_bios_card_write(chan, 0x3F, NULL);
}
int psx_card_format(int chan) {
    if (card_at(chan) != CARD_OK) return 0;
    psx_card_format_image(img);
    new_flag = 0;
    save();
    psx_log("card: formatted (0x%02X)\n", chan);
    return 1;
}

/* ---- the BIOS's file calls on "bu00:" */
#define FDS 16
static struct { int open, block0, size, pos; } fds[FDS];

/* "buXY:NAME" on slot 1 (bu00) with a formatted card: NAME, else NULL */
static const char *card_file(const char *name) {
    if (!name || strncmp(name, "bu", 2) != 0 || !name[2] || !name[3] || name[4] != ':') return NULL;
    if (card_at(((name[2] - '0') << 4) | (name[3] - '0')) != CARD_OK || !formatted()) return NULL;
    return name + 5;
}

static int dir_name_is(int block, const char *name) {
    return strncmp((const char *)dir(block) + 0x0A, name, 20) == 0;
}

static int find_file(const char *name) {
    int b;
    for (b = 1; b < 16; b++)
        if (get32(dir(b)) == 0x51 && dir_name_is(b, name)) return b;
    return 0;
}

/* the file's n-th block (0 = its first), following the directory's links */
static int nth_block(int block0, int n) {
    int b = block0;
    while (n-- > 0 && b) {
        unsigned next = dir(b)[8] | dir(b)[9] << 8;
        b = next < 15 ? (int)next + 1 : 0;
    }
    return b;
}

static int create_file(const char *name, int blocks) {
    int free_blocks[15], n = 0, b, i;
    if (blocks < 1) blocks = 1;
    for (b = 1; b < 16; b++) {
        uint32_t st = get32(dir(b));
        if (st == 0xA0 || (st >= 0xA1 && st <= 0xA3)) free_blocks[n++] = b;
    }
    if (n < blocks || strlen(name) > 20) return 0;
    for (i = 0; i < blocks; i++) {
        uint8_t *d = dir(free_blocks[i]);
        memset(d, 0, PSX_CARD_FRAME);
        put32(d, i == 0 ? 0x51 : i == blocks - 1 ? 0x53 : 0x52);
        if (i == 0) {
            put32(d + 4, (uint32_t)blocks * PSX_CARD_BLOCK);
            strncpy((char *)d + 0x0A, name, 20);
        }
        if (i < blocks - 1) {
            d[8] = (uint8_t)(free_blocks[i + 1] - 1);
            d[9] = 0;
        } else {
            d[8] = d[9] = 0xFF;
        }
        d[0x7F] = frame_sum(d);
        memset(img + free_blocks[i] * PSX_CARD_BLOCK, 0, PSX_CARD_BLOCK);
    }
    new_flag = 0;
    save();
    return free_blocks[0];
}

int psx_bios_open(const char *name, int mode) {
    const char *file = card_file(name);
    int fd, b;
    if (!file) return -1;
    b = find_file(file);
    if (mode & 0x200) {
        if (b) return -1; /* the BIOS doesn't create over a file */
        b = create_file(file, (unsigned)mode >> 16);
    }
    if (psx_log_due(&file_logs))
        psx_log("card: open(\"%s\", 0x%X): %s\n", name, mode, b ? (mode & 0x200 ? "created" : "found") : "no file");
    if (!b) return -1;
    for (fd = 2; fd < FDS && fds[fd].open; fd++) {}
    if (fd == FDS) return -1;
    fds[fd].open = 1;
    fds[fd].block0 = b;
    fds[fd].size = (int)get32(dir(b) + 4);
    fds[fd].pos = 0;
    return fd;
}

static int fd_ok(int fd) { return fd >= 2 && fd < FDS && fds[fd].open; }

int psx_bios_lseek(int fd, int offset, int whence) {
    int pos;
    if (!fd_ok(fd)) return -1;
    pos = whence == 1 ? fds[fd].pos + offset : offset;
    if (pos < 0 || pos > fds[fd].size) return -1;
    return fds[fd].pos = pos;
}

static int file_io(int fd, uint8_t *dst, const uint8_t *src, int n) {
    int done = 0;
    if (!fd_ok(fd) || n < 0) return -1;
    if (n > fds[fd].size - fds[fd].pos) n = fds[fd].size - fds[fd].pos;
    while (done < n) {
        int pos = fds[fd].pos, b = nth_block(fds[fd].block0, pos / PSX_CARD_BLOCK), off = pos % PSX_CARD_BLOCK;
        int k = PSX_CARD_BLOCK - off < n - done ? PSX_CARD_BLOCK - off : n - done;
        if (!b) break;
        if (dst) memcpy(dst + done, img + b * PSX_CARD_BLOCK + off, (size_t)k);
        else memcpy(img + b * PSX_CARD_BLOCK + off, src + done, (size_t)k);
        done += k;
        fds[fd].pos += k;
    }
    if (src && done) save();
    return done;
}
int psx_bios_read(int fd, void *dst, int n) { return file_io(fd, dst, NULL, n); }
int psx_bios_write(int fd, const void *src, int n) { return file_io(fd, NULL, src, n); }
int psx_bios_close(int fd) {
    if (!fd_ok(fd)) return -1;
    fds[fd].open = 0;
    return fd;
}

int psx_bios_format(const char *dev) {
    if (!dev || strncmp(dev, "bu", 2) != 0 || !dev[2] || !dev[3]) return 0;
    return psx_card_format(((dev[2] - '0') << 4) | (dev[3] - '0'));
}

/* the BIOS's wildcards: '?' any one character, '*' the rest */
static int wild(const char *pat, const char *name) {
    for (; *pat; pat++, name++) {
        if (*pat == '*') return 1;
        if (!*name || (*pat != '?' && *pat != *name)) return 0;
    }
    return !*name;
}

static char find_pattern[32];
static int find_next;

static int find_from(int start, void *dirent) {
    int b;
    for (b = start; b < 16; b++) {
        char name[21];
        uint8_t *e = dirent;
        if (get32(dir(b)) != 0x51) continue;
        memcpy(name, dir(b) + 0x0A, 20);
        name[20] = 0;
        if (!wild(find_pattern, name)) continue;
        memset(e, 0, 40);
        memcpy(e, name, 20);
        put32(e + 0x14, 0x50);
        put32(e + 0x18, get32(dir(b) + 4));
        put32(e + 0x20, (uint32_t)b * (PSX_CARD_BLOCK / PSX_CARD_FRAME));
        find_next = b + 1;
        return 1;
    }
    find_next = 16;
    return 0;
}

int psx_bios_firstfile(const char *pattern, void *dirent) {
    const char *file = card_file(pattern);
    int found;
    find_next = 16;
    if (!file || !dirent) return 0;
    strncpy(find_pattern, file, sizeof find_pattern - 1);
    find_pattern[sizeof find_pattern - 1] = 0;
    found = find_from(1, dirent);
    if (psx_log_due(&file_logs))
        psx_log("card: firstfile(\"%s\"): %s\n", pattern, found ? "a file" : "none");
    return found;
}
int psx_bios_nextfile(void *dirent) { return dirent && find_next < 16 ? find_from(find_next, dirent) : 0; }

int psx_bios_erase(const char *name) {
    const char *file = card_file(name);
    int b = file ? find_file(file) : 0, n = 0;
    while (b && n++ < 15) {
        uint8_t *d = dir(b);
        uint32_t st = get32(d);
        unsigned next = d[8] | d[9] << 8;
        put32(d, st == 0x51 ? 0xA1 : st == 0x52 ? 0xA2 : 0xA3);
        d[0x7F] = frame_sum(d);
        b = st != 0x53 && next < 15 ? (int)next + 1 : 0;
    }
    if (n) save();
    return n > 0;
}

/* ---- PSY-Q's names (a game that has libcard as named functions) */
#ifndef PSX_GAME_HAS_InitCARD
void InitCARD(int pad_enable) { psx_bios_InitCARD(pad_enable); }
#endif
#ifndef PSX_GAME_HAS_StartCARD
int StartCARD(void) { return psx_bios_StartCARD(); }
#endif
#ifndef PSX_GAME_HAS_StopCARD
int StopCARD(void) { return psx_bios_StopCARD(); }
#endif
#ifndef PSX_GAME_HAS__bu_init
void _bu_init(void) { psx_bios_bu_init(); }
#endif
#ifndef PSX_GAME_HAS__card_info
int _card_info(int chan) { return psx_bios_card_info(chan); }
#endif
#ifndef PSX_GAME_HAS__card_load
int _card_load(int chan) { return psx_bios_card_load(chan); }
#endif
#ifndef PSX_GAME_HAS__card_auto
int _card_auto(int flag) { return psx_bios_card_auto(flag); }
#endif
#ifndef PSX_GAME_HAS__card_read
int _card_read(int chan, int block, unsigned char *buf) { return psx_bios_card_read(chan, block, PSX_PTR(buf)); }
#endif
#ifndef PSX_GAME_HAS__card_write
int _card_write(int chan, int block, unsigned char *buf) { return psx_bios_card_write(chan, block, PSX_PTR(buf)); }
#endif
#ifndef PSX_GAME_HAS__new_card
void _new_card(void) { psx_bios_new_card(); }
#endif
#ifndef PSX_GAME_HAS__card_status
int _card_status(int drv) { return psx_bios_card_status(drv); }
#endif
#ifndef PSX_GAME_HAS__card_wait
int _card_wait(int drv) { return psx_bios_card_wait(drv); }
#endif
#ifndef PSX_GAME_HAS__card_chan
unsigned _card_chan(void) { return (unsigned)psx_card_chan(); }
#endif
#ifndef PSX_GAME_HAS__card_clear
int _card_clear(int chan) { return psx_card_clear(chan); }
#endif
#ifndef PSX_GAME_HAS__card_format
int _card_format(int chan) { return psx_card_format(chan); }
#endif
