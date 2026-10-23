/* libcd: a CD drive over the disc image (psx/cd.c), the model in libcd.h. From PsyCross's src/psx/LIBCD.C (MIT,
 * Copyright (c) 2020 REDRIVER2 Project; LICENSE.PsyCross here): CdIntToPos/CdPosToInt, the shape of CdRead/CdReadSync
 * (a request CdRead queues and CdReadSync drains, mode 1 returning the sectors left), and its spooler's per-sector
 * timing (the ready callback with CdlDataReady, CdGetSector copying from the current sector, then the data
 * callback). Its SDL thread is a deterministic per-frame drive here, and the rest (the command set with results,
 * CdSync/CdReady and their callbacks, the read callback, the St ring, XA routing, the disc's discovery) is this
 * port's. */
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../cd.h"
#include "../psx_runtime.h"
#include "libcd.h"

CdlATV psx_cd_mix = {0x80, 0, 0x80, 0};
int psx_cd_muted;

/* the SPU's side (a strong definition elsewhere replaces this) */
__attribute__((weak)) void psx_cd_xa_sector(const uint8_t *raw2352) { (void)raw2352; }

typedef void (*CdCallbackFn)(int intr, uint32_t result);
typedef void (*CdVoidFn)(void);

static struct {
    int inited, have_disc;
    uint32_t loc, head; /* the Setloc position; the next sector the head reads */
    int mode, stat, last_com, debug, reading, playing;
    int pending;        /* a command whose completion is still to come */
    int sync_intr;      /* what CdSync reports once it has come */
    uint8_t sync_res[8];
    int ready_intr;     /* CdlDataReady (or CdlDataEnd) not yet reported by CdReady */
    uint8_t ready_res[8];
    uint8_t cur[PSX_CD_RAW]; /* the current sector (the drive's sector buffer) */
    int cur_valid, cur_rd, getsector_calls;
    int credit;         /* quarter sectors the head may still read */
    int rd_active, rd_left, rd_bytes, rd_error, rd_total;
    uint32_t rd_dst;
    uint32_t cb_sync, cb_ready, cb_read, cb_data;
    CdlFILTER filter;
    int in_event, fast;
    int stream;         /* CdRead2 with CdlModeStream: sectors go to the St ring */
    uint32_t res_buf;   /* 8 bytes in the window for callbacks' result pointer */
    unsigned long xa_sectors, sectors_read;
} C;

static int logs_left = 400;
static int cmd_logs_left = 40;

#define CDLOG(...) do { if (logs_left > 0) { logs_left--; psx_log(__VA_ARGS__); } } while (0)

static const char *com_name(int com) {
    static const char *names[] = {"Sync", "Nop", "Setloc", "Play", "Forward", "Backward", "ReadN", "Standby", "Stop",
                                  "Pause", "Init", "Mute", "Demute", "Setfilter", "Setmode", "Getparam", "GetlocL",
                                  "GetlocP", "SetSession", "GetTN", "GetTD", "SeekL", "SeekP", "?", "?", "Test",
                                  "GetID", "ReadS", "Reset", "?", "ReadTOC"};
    return com >= 0 && com < (int)(sizeof names / sizeof names[0]) ? names[com] : "?";
}

static const char *where(uint32_t lba, char *buf, size_t n) {
    uint32_t off = 0;
    const char *name = psx_disc_name_at(lba, &off);
    if (name) snprintf(buf, n, "%s +0x%X", name, off);
    else snprintf(buf, n, "no file");
    return buf;
}

static void call_cb(uint32_t fn, int intr, const uint8_t res[8]) {
    void *host;
    if (!fn) return;
    host = psx_host_fn(fn);
    if (C.res_buf) memcpy(psx_host(C.res_buf), res, 8);
    ((CdCallbackFn)host)(intr, C.res_buf);
}

static void put_result(uint32_t result, const uint8_t res[8]) {
    if (result) memcpy(psx_host(result), res, 8);
}

static int status(void) {
    int s = C.have_disc ? CdlStatStandby : CdlStatShellOpen;
    if (C.reading) s |= CdlStatRead;
    if (C.playing) s |= CdlStatPlay;
    return s;
}

/* ---- the disc: PSX_DISC, else the path `decomp port --disc` saved in the port */
/* "SLUS_012.52" and "SLUS-01252" are the same id: letters and digits only, any case */
static void id_of(const char *s, char out[32]) {
    int i = 0;
    for (; *s && i < 31; s++)
        if (isalnum((unsigned char)*s)) out[i++] = (char)toupper((unsigned char)*s);
    out[i] = 0;
}
static int same_id(const char *a, const char *b) {
    char x[32], y[32];
    id_of(a, x);
    id_of(b, y);
    return x[0] && strcmp(x, y) == 0;
}

static void check_boot(void) {
    const char *boot = psx_disc_boot();
    uint32_t lba, size;
    char path[4096];
    const char *dir = getenv("PSX_SOURCE_DIR");
    FILE *f;
    unsigned char mine[4096], theirs[4096];
    size_t got = 0;
    if (!*boot) { psx_log("cd: no SYSTEM.CNF BOOT line on the disc\n"); return; }
    if (!psx_disc_find(boot, &lba, &size)) {
        psx_log("cd: SYSTEM.CNF boots %s, which isn't on the disc\n", boot);
        return;
    }
    snprintf(path, sizeof path, "%s/%s", dir ? dir : psx_config.source_dir, psx_config.image);
    if ((f = fopen(path, "rb"))) { got = fread(mine, 1, sizeof mine, f); fclose(f); }
    if (got == sizeof mine && psx_disc_read(lba, theirs, 2) == 2) {
        psx_log("cd: SYSTEM.CNF boots %s (LBA %u, %u bytes): %s the port's executable %s%s\n", boot, lba, size,
                memcmp(mine, theirs, sizeof mine) == 0 ? "its first 4 KB are" : "NOT",
                psx_config.image, same_id(boot, psx_config.title) ? "" : " (and its name isn't the game's id)");
    } else {
        psx_log("cd: SYSTEM.CNF boots %s (LBA %u, %u bytes)\n", boot, lba, size);
    }
}

static void open_disc(void) {
    const char *path = getenv("PSX_DISC");
    char err[1024];
    if (C.have_disc) return;
    if (!path || !*path) path = psx_config.disc;
    if (!path || !*path) {
        psx_log("cd: no disc image: set PSX_DISC to the game's .bin/.cue/.iso (or `decomp port --disc PATH`); "
                "every read fails\n");
        return;
    }
    if (!psx_disc_open(path, err, sizeof err)) {
        psx_log("cd: %s; every read fails\n", err);
        return;
    }
    C.have_disc = 1;
    psx_log("cd: disc %s: %d-byte sectors, %u of them\n", psx_disc_path(), psx_disc_sector_size(), psx_disc_sectors());
    check_boot();
}

static void init_state(void) {
    if (!C.res_buf) {
        void *p = psx_runtime_alloc(16);
        C.res_buf = p ? psx_ps1(p) : 0;
    }
    C.fast = getenv("PSX_CD_FAST") && atoi(getenv("PSX_CD_FAST")) > 0;
    if (getenv("PSX_CD_LOG") && atoi(getenv("PSX_CD_LOG")) > 0) logs_left = cmd_logs_left = 1 << 30;
}

/* ---- the St ring (MDEC movies): StSetRing's ring_size sectors of 2048 bytes, used as ring_size * 2016 bytes of
 * frame data (a frame's sectors back to back, its STR headers taken off) then ring_size headers of 32 bytes */
#define ST_DATA 2016
#define ST_FRAMES 64
typedef struct {
    int state; /* StFREE, StBUSY (filling), StCOMPLETE (ready), StLOCK (handed to the game) */
    uint32_t off, len, slot, frame, seq;
    uint16_t need, got;
} StFrame;
static struct {
    uint32_t ring, nsec, wp, seq;
    uint32_t mode, start, end, f1, f2, mask, mstart, mend;
    uint32_t channel;
    StFrame f[ST_FRAMES];
} St;

static int st_overlaps(uint32_t off, uint32_t len) {
    int i;
    for (i = 0; i < ST_FRAMES; i++)
        if (St.f[i].state != StFREE && off < St.f[i].off + St.f[i].len && St.f[i].off < off + len) return 1;
    return 0;
}

static void st_sector(const uint8_t *raw) {
    const uint8_t *u = raw + 24;
    StHEADER h;
    int i;
    StFrame *fr = NULL;
    if (!St.ring || !St.nsec) return;
    memcpy(&h, u, sizeof h);
    if (h.id != 0x0160 || h.nSectors == 0 || h.secCount >= h.nSectors) return; /* not an STR video sector */
    if (St.end && (h.frameCount < St.start || h.frameCount > St.end)) return;
    if (h.secCount == 0) {
        uint32_t len = (uint32_t)h.nSectors * ST_DATA;
        if (len > St.nsec * ST_DATA) return;
        if (St.wp + len > St.nsec * ST_DATA) St.wp = 0;
        if (st_overlaps(St.wp, len)) return; /* the ring is full: the frame is lost, as on the drive */
        for (i = 0; i < ST_FRAMES && St.f[i].state != StFREE; i++) {}
        if (i == ST_FRAMES) return;
        fr = &St.f[i];
        fr->state = StBUSY;
        fr->off = St.wp;
        fr->len = len;
        fr->slot = St.wp / ST_DATA;
        fr->frame = h.frameCount;
        fr->need = h.nSectors;
        fr->got = 0;
        fr->seq = St.seq++;
        St.wp += len;
        memcpy(psx_host(St.ring + St.nsec * ST_DATA + fr->slot * 32), u, 32);
    } else {
        for (i = 0; i < ST_FRAMES; i++)
            if (St.f[i].state == StBUSY && St.f[i].frame == h.frameCount) fr = &St.f[i];
        if (!fr) return;
    }
    memcpy(psx_host(St.ring + fr->off + (uint32_t)h.secCount * ST_DATA), u + 32, ST_DATA);
    if (++fr->got == fr->need) {
        fr->state = StCOMPLETE;
        if (St.f1) ((CdVoidFn)psx_host_fn(St.f1))();
    }
}

void StSetRing(uint32_t ring_addr, uint32_t ring_size) {
    memset(&St.f, 0, sizeof St.f);
    St.ring = ring_addr;
    St.nsec = ring_size;
    St.wp = 0;
    CDLOG("cd: StSetRing(0x%08X, %u sectors)\n", ring_addr, ring_size);
}
void StClearRing(void) { memset(&St.f, 0, sizeof St.f); St.wp = 0; }
void StUnSetRing(void) {
    StClearRing();
    St.ring = St.nsec = 0;
    C.stream = 0;
    C.cb_data = C.cb_ready = 0;
}
void StSetStream(uint32_t mode, uint32_t start_frame, uint32_t end_frame, uint32_t func1, uint32_t func2) {
    St.mode = mode;
    St.start = start_frame;
    St.end = end_frame;
    St.f1 = func1;
    St.f2 = func2;
    CDLOG("cd: StSetStream(mode %u, frames %u-%u)\n", mode, start_frame, end_frame);
}
void StSetEmulate(uint32_t addr, uint32_t mode, uint32_t start_frame, uint32_t end_frame, uint32_t func1,
                  uint32_t func2) {
    (void)addr; (void)mode; (void)func2;
    StSetStream(mode, start_frame, end_frame, func1, func2);
    CDLOG("cd: StSetEmulate (streaming from memory): not emulated, the stream reads the disc\n");
}
uint32_t StGetNext(uint32_t addr, uint32_t header) {
    int i, best = -1;
    for (i = 0; i < ST_FRAMES; i++)
        if (St.f[i].state == StCOMPLETE && (best < 0 || St.f[i].seq < St.f[best].seq)) best = i;
    if (best < 0) return 1;
    St.f[best].state = StLOCK;
    if (addr) *(uint32_t *)psx_host(addr) = St.ring + St.f[best].off;
    if (header) *(uint32_t *)psx_host(header) = St.ring + St.nsec * ST_DATA + St.f[best].slot * 32;
    return 0;
}
uint32_t StFreeRing(uint32_t base) {
    int i;
    for (i = 0; i < ST_FRAMES; i++)
        if (St.f[i].state != StFREE && St.ring + St.f[i].off == base) {
            St.f[i].state = StFREE;
            return 0;
        }
    return 1;
}
int StSetChannel(uint32_t channel) { St.channel = channel; return 0; }
void StSetMask(uint32_t mask, uint32_t start, uint32_t end) { St.mask = mask; St.mstart = start; St.mend = end; }

/* a read that brought an overlay's code into RAM: which one, and do the dispatch's lookups (psx_function_at, which
 * tells overlays at one address apart by their first two instructions in RAM) now find its functions? */
static void overlay_report(uint32_t lo, uint32_t hi) {
    const char *best = NULL, *seen[64];
    size_t i, j, n_seen = 0, k;
    unsigned in = 0, hit = 0, same = 0;
    for (i = 0; i < psx_function_count; i++) {
        const PsxFunction *f = &psx_functions[i];
        unsigned n = 0, code_ok = 0, ok = 0;
        if (!strcmp(f->segment, "main") || f->vram < lo || f->vram + 8 > hi) continue;
        for (k = 0; k < n_seen && strcmp(seen[k], f->segment); k++) {}
        if (k < n_seen || n_seen == 64) continue;
        seen[n_seen++] = f->segment;
        for (j = 0; j < psx_function_count; j++) {
            const PsxFunction *g = &psx_functions[j];
            const uint32_t *code;
            if (strcmp(g->segment, f->segment) || g->vram < lo || g->vram + 8 > hi) continue;
            code = psx_host(g->vram);
            n++;
            if (code[0] == g->word0 && code[1] == g->word1) {
                code_ok++;
                if (psx_function_at(g->vram) == g) ok++;
            }
        }
        /* the overlay is the one whose functions' code is all there (others share some library code with it) */
        if (n && (!best || (uint64_t)code_ok * in > (uint64_t)same * n)) { best = f->segment; hit = ok; same = code_ok; in = n; }
    }
    if (best && same * 2 > in)
        CDLOG("cd: the read holds overlay %s: its code is there for %u of its %u functions, and %u of them resolve "
              "to it (psx_function_at)\n", best, same, in, hit);
}

/* ---- the head reads one sector */
static void finish_read(int error) {
    uint8_t res[8] = {0};
    char w[160];
    C.rd_active = 0;
    C.reading = 0; /* CdRead pauses the drive at the end */
    C.rd_error = error;
    res[0] = (uint8_t)status();
    CDLOG("cd: CdRead done: %d of %d sectors%s (now at %s)\n", C.rd_total - C.rd_left, C.rd_total,
          error ? ", an error" : "", where(C.head, w, sizeof w));
    if (!error) overlay_report(C.rd_dst - (uint32_t)(C.rd_total * C.rd_bytes), C.rd_dst);
    call_cb(C.cb_read, error ? CdlDiskError : CdlComplete, res);
}

static void one_sector(void) {
    const uint8_t *raw = psx_disc_raw(C.head);
    uint8_t res[8] = {0};
    int off, n;
    if (!raw) { /* past the end of the disc */
        C.reading = 0;
        res[0] = (uint8_t)(status() | CdlStatError);
        if (C.rd_active) { finish_read(1); return; }
        C.ready_intr = CdlDataEnd;
        memcpy(C.ready_res, res, 8);
        call_cb(C.cb_ready, CdlDataEnd, res);
        return;
    }
    C.head++;
    C.sectors_read++;
    memcpy(C.cur, raw, PSX_CD_RAW); /* GetlocL reads its header */
    if ((C.mode & CdlModeRT) && raw[15] == 2 && (raw[18] & 0x20) && (raw[18] & 0x04)) { /* XA ADPCM */
        if (!(C.mode & CdlModeSF) || (raw[16] == C.filter.file && raw[17] == C.filter.chan)) {
            if (!C.xa_sectors++) CDLOG("cd: first XA sector (LBA %u, file %u channel %u) to psx_cd_xa_sector\n",
                                       C.head - 1, raw[16], raw[17]);
            psx_cd_xa_sector(raw);
        }
        return;
    }
    if (C.rd_active) {
        n = psx_disc_user(raw, &off);
        if (C.rd_bytes == 2340) off = 12;
        else if (C.rd_bytes == 2328) off = 24;
        else if (!n) off = 24;
        memcpy(psx_host(C.rd_dst), raw + off, (size_t)C.rd_bytes);
        C.rd_dst += (uint32_t)C.rd_bytes;
        if (--C.rd_left == 0) finish_read(0);
        return;
    }
    if (C.stream) { st_sector(raw); return; }
    C.cur_valid = 1;
    C.cur_rd = 0;
    res[0] = (uint8_t)status();
    C.ready_intr = CdlDataReady;
    memcpy(C.ready_res, res, 8);
    if (C.cb_ready) {
        int before = C.getsector_calls;
        C.ready_intr = CdlNoIntr; /* the callback is the interrupt handler that takes it */
        call_cb(C.cb_ready, CdlDataReady, res);
        if (C.getsector_calls != before && C.cb_data) ((CdVoidFn)psx_host_fn(C.cb_data))();
    }
}

/* a CD event point: the pending command completes, then the head reads what its credit allows */
static void event(int quarters) {
    if (C.in_event) return;
    C.in_event = 1;
    if (C.pending) {
        C.pending = 0;
        C.sync_intr = CdlComplete;
        C.sync_res[0] = (uint8_t)status();
        call_cb(C.cb_sync, CdlComplete, C.sync_res);
    }
    if (C.reading) {
        C.credit += quarters;
        if (C.fast && C.rd_active) C.credit = 4 * (C.rd_left + 1);
        while (C.credit >= 4 && C.reading) {
            C.credit -= 4;
            one_sector();
        }
        if (C.credit > 64) C.credit = 64;
    } else {
        C.credit = 0;
    }
    C.in_event = 0;
}

void psx_cd_vblank(void) {
    if (C.inited) event((C.mode & CdlModeSpeed) ? 10 : 5);
}

/* ---- commands */
static int issue(int com, uint32_t param, uint32_t result) {
    uint8_t res[8] = {0};
    const uint8_t *p = param ? (const uint8_t *)psx_host(param) : NULL;
    uint8_t msf[3];
    char w[160];
    C.last_com = com;
    if (p && (com == CdlReadN || com == CdlReadS || com == CdlSeekL || com == CdlSeekP || com == CdlPlay))
        C.loc = psx_msf_to_lba(p); /* libcd's CdControl sets the position first for these */
    switch (com) {
    case CdlNop: case CdlForward: case CdlBackward: case CdlReadTOC: break;
    case CdlSetloc: if (p) C.loc = psx_msf_to_lba(p); break;
    case CdlPlay:
        C.playing = 1;
        C.reading = 0;
        CDLOG("cd: CdlPlay (CD-DA) at LBA %u: not played (no audio track is read)\n", C.loc);
        break;
    case CdlReadN: case CdlReadS:
        C.head = C.loc;
        C.reading = 1;
        C.playing = 0;
        break;
    case CdlStandby: case CdlPause: C.reading = C.playing = 0; break;
    case CdlStop: C.reading = C.playing = 0; break;
    case CdlInit: case CdlReset: C.reading = C.playing = 0; C.mode = 0; break;
    case CdlMute: psx_cd_muted = 1; break;
    case CdlDemute: psx_cd_muted = 0; break;
    case CdlSetfilter: if (p) { C.filter.file = p[0]; C.filter.chan = p[1]; } break;
    case CdlSetmode: if (p) C.mode = p[0]; break;
    case CdlGetparam:
        res[1] = (uint8_t)C.mode;
        res[3] = C.filter.file;
        res[4] = C.filter.chan;
        break;
    case CdlGetlocL: memcpy(res, C.cur + 12, 8); break;
    case CdlGetlocP: {
        uint32_t at = C.head ? C.head - 1 : 0, rel = at;
        res[0] = 1; /* track */
        res[1] = 1; /* index */
        res[2] = PSX_BCD(rel / 4500 % 100);
        res[3] = PSX_BCD(rel / 75 % 60);
        res[4] = PSX_BCD(rel % 75);
        psx_lba_to_msf(at, msf);
        memcpy(res + 5, msf, 3);
        break;
    }
    case CdlGetTN: res[1] = 1; res[2] = 1; break; /* the one data track */
    case CdlGetTD:
        psx_lba_to_msf(p && p[0] ? 0 : psx_disc_sectors(), msf);
        res[1] = msf[0];
        res[2] = msf[1];
        break;
    case CdlSeekL: case CdlSeekP: C.head = C.loc; C.reading = C.playing = 0; break;
    case CdlTest: res[0] = 0x94; res[1] = 0x09; res[2] = 0x19; res[3] = 0xC0; break;
    case CdlGetID: memcpy(res, "\x02\x00\x20\x00SCEA", 8); break;
    default: CDLOG("cd: command 0x%02X isn't known: acknowledged, nothing done\n", com); break;
    }
    if (com != CdlGetID && com != CdlTest && com != CdlGetlocL && com != CdlGetlocP) res[0] = (uint8_t)status();
    put_result(result, res);
    memcpy(C.sync_res, res, 8);
    C.pending = com;
    C.sync_intr = CdlNoIntr;
    if (cmd_logs_left > 0 && com != CdlNop && com != CdlGetlocP && com != CdlGetlocL) {
        cmd_logs_left--;
        if (com == CdlSetloc || com == CdlReadN || com == CdlReadS || com == CdlSeekL || com == CdlSeekP)
            psx_log("cd: Cdl%s LBA %u (%s)\n", com_name(com), C.loc, where(C.loc, w, sizeof w));
        else if (com == CdlSetmode) psx_log("cd: CdlSetmode 0x%02X\n", C.mode);
        else if (com == CdlSetfilter) psx_log("cd: CdlSetfilter file %u channel %u\n", C.filter.file, C.filter.chan);
        else psx_log("cd: Cdl%s\n", com_name(com));
    }
    return 1;
}

int CdControl(int com, uint32_t param, uint32_t result) { return issue(com & 0xFF, param, result); }
int CdControlF(int com, uint32_t param) { return issue(com & 0xFF, param, 0); }
int CdControlB(int com, uint32_t param, uint32_t result) {
    issue(com & 0xFF, param, result);
    event(0);
    put_result(result, C.sync_res);
    return C.sync_intr == CdlComplete;
}
int psx_CdControlCore(int com, uint32_t param, uint32_t result, int nowait) {
    (void)nowait;
    return issue(com & 0xFF, param, result) ? 0 : -1;
}

int CdSync(int mode, uint32_t result) {
    if (mode == 0) { if (C.pending) event(0); }
    else event(4);
    put_result(result, C.sync_res);
    return C.pending ? CdlNoIntr : C.sync_intr;
}

int CdReady(int mode, uint32_t result) {
    int r, guard = 0;
    if (mode == 0) while (!C.ready_intr && C.reading && guard++ < 1000000) event(4);
    else event(4);
    r = C.ready_intr;
    if (r) {
        put_result(result, C.ready_res);
        C.ready_intr = CdlNoIntr;
    }
    return r;
}

uint32_t CdSyncCallback(uint32_t func) { uint32_t old = C.cb_sync; C.cb_sync = func; return old; }
uint32_t CdReadyCallback(uint32_t func) { uint32_t old = C.cb_ready; C.cb_ready = func; return old; }
uint32_t CdReadCallback(uint32_t func) { uint32_t old = C.cb_read; C.cb_read = func; return old; }
uint32_t CdDataCallback(uint32_t func) { uint32_t old = C.cb_data; C.cb_data = func; return old; }

int CdGetSector(uint32_t madr, int words) {
    int base = 24, len, n = words * 4;
    C.getsector_calls++;
    if (!C.cur_valid || n <= 0) return 1;
    if (C.mode & CdlModeSize1) base = 12;
    else if (C.cur[15] == 1) base = 16;
    len = PSX_CD_RAW - base - C.cur_rd;
    if (n > len) n = len;
    if (n > 0 && madr) memcpy(psx_host(madr), C.cur + base + C.cur_rd, (size_t)n);
    C.cur_rd += n > 0 ? n : 0;
    return 1;
}
int CdGetSector2(uint32_t madr, int words) { return CdGetSector(madr, words); }
int CdDataSync(int mode) { (void)mode; return 0; }

/* PsyCross's */
uint32_t CdIntToPos(int i, uint32_t p) {
    CdlLOC *l = (CdlLOC *)psx_host(p);
    if (!l) return 0;
    i += 150;
    l->sector = PSX_BCD(i % 75);
    i /= 75;
    l->second = PSX_BCD(i % 60);
    l->minute = PSX_BCD(i / 60);
    return p;
}
int CdPosToInt(uint32_t p) {
    const CdlLOC *l = (const CdlLOC *)psx_host(p);
    if (!l) return 0;
    return (75 * (60 * (int)PSX_UNBCD(l->minute) + (int)PSX_UNBCD(l->second))) + (int)PSX_UNBCD(l->sector) - 150;
}

int CdRead(int sectors, uint32_t buf, int mode) {
    char w[160];
    int size = mode & 0x30;
    if (!C.have_disc || sectors <= 0) {
        CDLOG("cd: CdRead(%d sectors to 0x%08X): %s\n", sectors, buf, C.have_disc ? "nothing to read" : "no disc");
        C.rd_error = 1;
        return 0;
    }
    C.mode = mode & 0xFF;
    C.rd_bytes = size == 0 ? 2048 : size == 0x20 ? 2340 : 2328;
    C.rd_active = 1;
    C.rd_left = C.rd_total = sectors;
    C.rd_dst = buf;
    C.rd_error = 0;
    C.stream = 0;
    C.head = C.loc;
    C.reading = 1;
    C.last_com = CdlReadN;
    CDLOG("cd: CdRead %d sectors (%d bytes) from LBA %u (%s) to 0x%08X, mode 0x%02X\n", sectors,
          sectors * C.rd_bytes, C.loc, where(C.loc, w, sizeof w), buf, mode & 0xFF);
    return 1;
}

int CdReadSync(int mode, uint32_t result) {
    uint8_t res[8] = {0};
    if (mode == 0) while (C.rd_active) event(4 * (C.rd_left + 1));
    else event(4);
    res[0] = (uint8_t)status();
    put_result(result, res);
    if (C.rd_active) return C.rd_left;
    return C.rd_error ? -1 : 0;
}

int CdRead2(int mode) {
    uint8_t m = (uint8_t)mode;
    char w[160];
    C.mode = m;
    C.rd_active = 0;
    C.stream = (mode & CdlModeStream) != 0;
    if (C.stream) {
        C.cb_ready = 0; /* the ring takes the sectors (libcd installs StCdInterrupt here) */
        C.cb_data = 0;
    }
    C.head = C.loc;
    C.reading = 1;
    C.last_com = C.stream ? CdlReadS : CdlReadN;
    CDLOG("cd: CdRead2(mode 0x%X) from LBA %u (%s)%s\n", mode, C.loc, where(C.loc, w, sizeof w),
          C.stream ? ": streaming into the St ring" : "");
    return 1;
}

uint32_t CdSearchFile(uint32_t fp, uint32_t name) {
    const char *n = (const char *)psx_host(name);
    CdlFILE *f = (CdlFILE *)psx_host(fp);
    uint32_t lba, size;
    const char *base;
    if (!f || !n) return 0;
    memset(f, 0, sizeof *f);
    if (!C.have_disc || !psx_disc_find(n, &lba, &size)) {
        CDLOG("cd: CdSearchFile(\"%s\"): not found\n", n);
        return 0;
    }
    psx_lba_to_msf(lba, &f->pos.minute);
    f->size = size;
    base = n;
    for (const char *s = n; *s; s++) if (*s == '\\' || *s == '/' || *s == ':') base = s + 1;
    snprintf(f->name, sizeof f->name, "%s", base);
    CDLOG("cd: CdSearchFile(\"%s\") -> LBA %u, %u bytes\n", n, lba, size);
    return fp;
}

int CdReadFile(uint32_t file, uint32_t addr, int nbyte) {
    const char *n = (const char *)psx_host(file);
    uint32_t lba, size, sectors;
    if (!C.have_disc || !n || !psx_disc_find(n, &lba, &size)) return -1;
    if (nbyte <= 0 || (uint32_t)nbyte > size) nbyte = (int)size;
    sectors = ((uint32_t)nbyte + 2047) / 2048;
    C.loc = lba;
    CdRead((int)sectors, addr, 0x80);
    CdReadSync(0, 0);
    return nbyte;
}

/* PSX_CD_LOAD="FILE@ADDR[,FILE@ADDR...]": read those files into RAM at CdInit, through CdRead, as the game would
 * (a debugging aid: e.g. SMITH.BIN@0x80021000 puts an overlay in place and logs whether its functions resolve) */
static void debug_load(void) {
    const char *spec = getenv("PSX_CD_LOAD");
    char buf[1024], *item, *save = NULL;
    if (!spec || !*spec || !C.have_disc) return;
    snprintf(buf, sizeof buf, "%s", spec);
    for (item = strtok_r(buf, ",", &save); item; item = strtok_r(NULL, ",", &save)) {
        char *at = strchr(item, '@');
        uint32_t lba, size, addr;
        if (!at) continue;
        *at = 0;
        addr = (uint32_t)strtoul(at + 1, NULL, 0);
        if (!psx_disc_find(item, &lba, &size) || addr < PSX_RAM_BASE || addr + size > PSX_RAM_BASE + PSX_RAM_SIZE) {
            psx_log("cd: PSX_CD_LOAD %s@0x%08X: not on the disc or not in RAM\n", item, addr);
            continue;
        }
        psx_log("cd: PSX_CD_LOAD %s (LBA %u, %u bytes) to 0x%08X\n", item, lba, size, addr);
        C.loc = lba;
        CdRead((int)((size + 2047) / 2048), addr, CdlModeSpeed);
        CdReadSync(0, 0);
    }
}

int CdInit(void) {
    uint8_t res[8] = {0};
    init_state();
    open_disc();
    C.inited = 1;
    C.mode = 0;
    C.reading = C.playing = C.pending = 0;
    C.cb_sync = C.cb_ready = C.cb_read = C.cb_data = 0;
    C.sync_intr = CdlComplete;
    res[0] = (uint8_t)status();
    memcpy(C.sync_res, res, 8);
    CDLOG("cd: CdInit -> %d\n", C.have_disc);
    debug_load();
    return 1;
}
int CdReset(int mode) { (void)mode; C.reading = C.playing = 0; C.mode = 0; return 1; }
void CdFlush(void) {}
int CdStatus(void) { return status(); }
int CdMode(void) { return C.mode; }
int CdLastCom(void) { return C.last_com; }
int CdSetDebug(int level) { int old = C.debug; C.debug = level; return old; }
int CdMix(uint32_t vol) { if (vol) memcpy(&psx_cd_mix, psx_host(vol), 4); return 1; }
int CdDiskReady(int mode) { (void)mode; return C.have_disc ? CdlComplete : CdlDiskError; }
int CdGetDiskType(void) { return C.have_disc ? 2 : 0; } /* CdlCdromFormat, CdlStatNoDisk */
