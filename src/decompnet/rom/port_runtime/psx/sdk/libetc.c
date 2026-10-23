/* libetc: the frame loop. VSync paces the game to 59.94 Hz; each vertical blank pumps the window, presents the
 * display area of VRAM (psx/gpu.c), reads the pad and runs the game's vblank callbacks on the main thread, as the
 * interrupt would have between two instructions. Interrupt and DMA callbacks other than vblank are recorded, never
 * delivered (the CD's and the SPU's belong to their libraries). Also the root counters, from the host's clock. */
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "../psx_runtime.h"
#include "libetc.h"
#include "libcd.h"
#include "libgpu.h"
#include "libpad.h"

#define NTSC_FRAME_NS 16683333L /* 59.94 Hz */
#define HBLANKS_PER_FRAME 263

static int frames;          /* vertical blanks since start */
static int last_vsync;      /* the frame of the last VSync call */
static struct timespec next, last_blank;
static psx_addr vsync_cb[8], irq_cb[11], dma_cb[8];
static int video_mode, in_callback;

static void add_ns(struct timespec *t, long ns) {
    t->tv_nsec += ns;
    while (t->tv_nsec >= 1000000000L) { t->tv_nsec -= 1000000000L; t->tv_sec++; }
}
static long long ns_between(const struct timespec *a, const struct timespec *b) {
    return (long long)(b->tv_sec - a->tv_sec) * 1000000000LL + (b->tv_nsec - a->tv_nsec);
}

static void call_ps1(psx_addr fn) { ((void (*)(void))psx_host_fn(fn))(); }

/* one vertical blank: wait for it, show the frame, then what the interrupt would run */
static void one_frame(void) {
    struct timespec now;
    int i;
    psx_gpu_present();
    host_frame();
    psx_pad_update();
    psx_cd_vblank(); /* the CD's events of this frame (libcd.h) */
    if (host_quit()) {
        psx_log("psx: window closed after %d frames\n", frames);
        exit(0);
    }
    clock_gettime(CLOCK_MONOTONIC, &now);
    if (!next.tv_sec) next = now;
    add_ns(&next, NTSC_FRAME_NS);
    if (now.tv_sec > next.tv_sec + 1) next = now; /* far behind (a debugger, a stall): don't race to catch up */
    else {
        struct timespec d;
        d.tv_sec = next.tv_sec - now.tv_sec;
        d.tv_nsec = next.tv_nsec - now.tv_nsec;
        if (d.tv_nsec < 0) { d.tv_nsec += 1000000000L; d.tv_sec--; }
        if (d.tv_sec >= 0) nanosleep(&d, NULL); /* macOS has no clock_nanosleep */
    }
    clock_gettime(CLOCK_MONOTONIC, &last_blank);
    frames++;
    if (in_callback) return;
    in_callback = 1;
    if (irq_cb[0]) call_ps1(irq_cb[0]);
    for (i = 0; i < 8; i++)
        if (vsync_cb[i]) call_ps1(vsync_cb[i]);
    in_callback = 0;
}

/* horizontal blanks since the last vertical one, from the clock */
static int hcount(void) {
    struct timespec now;
    long long ns;
    if (!last_blank.tv_sec) return 0;
    clock_gettime(CLOCK_MONOTONIC, &now);
    ns = ns_between(&last_blank, &now);
    if (ns < 0) ns = 0;
    return (int)(ns * HBLANKS_PER_FRAME / NTSC_FRAME_NS);
}

#ifndef PSX_GAME_HAS_VSync
/* 0: wait for the next vblank; n > 1: until n vblanks since the last VSync call (at least one); 1: don't wait, the
 * horizontal blanks since the last vblank; negative: the vblank counter */
int VSync(int mode) {
    static int logged;
    int h;
    if (!logged++) psx_log("etc: first VSync(%d)\n", mode);
    if (mode < 0) return frames;
    if (mode == 1) return hcount();
    h = hcount();
    if (mode == 0) one_frame();
    else {
        int target = last_vsync + mode;
        if (target <= frames) target = frames + 1;
        while (frames < target) one_frame();
    }
    last_vsync = frames;
    return h;
}
#endif

/* ---- callbacks: every one returns the previous; the address is the game's (a PS1 function) */
#ifndef PSX_GAME_HAS_VSyncCallbacks
psx_addr VSyncCallbacks(int ch, psx_addr func) {
    psx_addr old;
    ch &= 7;
    old = vsync_cb[ch];
    vsync_cb[ch] = func;
    psx_log("etc: VSyncCallbacks(%d, 0x%08X)\n", ch, func);
    return old;
}
#endif

#ifndef PSX_GAME_HAS_VSyncCallback
psx_addr VSyncCallback(psx_addr func) { return VSyncCallbacks(0, func); }
#endif

#ifndef PSX_GAME_HAS_InterruptCallback
psx_addr InterruptCallback(int irq, psx_addr func) {
    psx_addr old;
    if (irq < 0 || irq > 10) return 0;
    old = irq_cb[irq];
    irq_cb[irq] = func;
    psx_log("etc: InterruptCallback(%d, 0x%08X)%s\n", irq, func, irq ? ": recorded, not delivered" : "");
    return old;
}
#endif

#ifndef PSX_GAME_HAS_DMACallback
psx_addr DMACallback(int ch, psx_addr func) {
    psx_addr old;
    ch &= 7;
    old = dma_cb[ch];
    dma_cb[ch] = func;
    psx_log("etc: DMACallback(%d, 0x%08X): recorded, not delivered\n", ch, func);
    return old;
}
#endif

#ifndef PSX_GAME_HAS_ResetCallback
int ResetCallback(void) {
    memset(vsync_cb, 0, sizeof vsync_cb);
    memset(irq_cb, 0, sizeof irq_cb);
    memset(dma_cb, 0, sizeof dma_cb);
    psx_log("etc: ResetCallback\n");
    return 0;
}
#endif

#ifndef PSX_GAME_HAS_StopCallback
void StopCallback(void) {}
#endif
#ifndef PSX_GAME_HAS_RestartCallback
int RestartCallback(void) { return 0; }
#endif
#ifndef PSX_GAME_HAS_CheckCallback
int CheckCallback(void) { return in_callback; }
#endif

#ifndef PSX_GAME_HAS_SetVideoMode
int SetVideoMode(int mode) {
    int old = video_mode;
    video_mode = mode; /* 0 NTSC, 1 PAL: paced at NTSC's rate either way */
    return old;
}
#endif
#ifndef PSX_GAME_HAS_GetVideoMode
int GetVideoMode(void) { return video_mode; }
#endif

/* ---- the old pad interface: PadRead's bits are the pad's halfword byte-swapped, 1 = pressed */
#ifndef PSX_GAME_HAS_PadInit
void PadInit(int mode) { (void)mode; }
#endif
#ifndef PSX_GAME_HAS_PadRead
uint32_t PadRead(int id) {
    uint16_t b = host_pad();
    (void)id;
    return (uint32_t)(uint16_t)((b << 8) | (b >> 8));
}
#endif
#ifndef PSX_GAME_HAS_PadStop
void PadStop(void) {}
#endif

/* ---- root counters: RCntCNT0-3 (0xF2000000-3); 0 the system clock (33.8688 MHz, or the dot clock), 1 hblanks
 * (or the system clock), 2 the system clock or /8, 3 vblanks. Read from the host's clock since StartRCnt/ResetRCnt,
 * wrapping at the target when it was set to stop there (the usual RCntMdINTR mode), else at 0x10000. */
static struct { uint32_t target, mode; int running; struct timespec base; } rcnt[4];

static int rc_index(uint32_t spec) { return (spec & 0xFFFFFFF0u) == 0xF2000000u && (spec & 0xF) < 4 ? (int)(spec & 3) : -1; }

static uint32_t rc_ticks(int i) {
    struct timespec now;
    long long ns, t;
    clock_gettime(CLOCK_MONOTONIC, &now);
    ns = ns_between(&rcnt[i].base, &now);
    switch (i) {
    case 0: t = ns * 338688LL / 10000000LL; break;                                   /* 33.8688 MHz */
    case 1: t = (rcnt[i].mode & 0x100) ? ns * 338688LL / 10000000LL : ns * HBLANKS_PER_FRAME / NTSC_FRAME_NS; break;
    case 2: t = (rcnt[i].mode & 0x200) ? ns * 338688LL / 80000000LL : ns * 338688LL / 10000000LL; break;
    default: t = ns / NTSC_FRAME_NS; break;
    }
    if (rcnt[i].target && (rcnt[i].mode & 0x1000)) return (uint32_t)(t % ((long long)rcnt[i].target + 1));
    return (uint32_t)(t & 0xFFFF);
}

#ifndef PSX_GAME_HAS_SetRCnt
int SetRCnt(uint32_t spec, int target, int mode) {
    int i = rc_index(spec);
    if (i < 0) return 0;
    rcnt[i].target = (uint32_t)target & 0xFFFF;
    rcnt[i].mode = (uint32_t)mode;
    clock_gettime(CLOCK_MONOTONIC, &rcnt[i].base);
    psx_log("etc: SetRCnt(%d, target %d, mode 0x%X)\n", i, target, mode);
    return 1;
}
#endif
#ifndef PSX_GAME_HAS_GetRCnt
int GetRCnt(uint32_t spec) {
    int i = rc_index(spec);
    return i < 0 ? 0 : (int)rc_ticks(i);
}
#endif
#ifndef PSX_GAME_HAS_StartRCnt
int StartRCnt(uint32_t spec) {
    int i = rc_index(spec);
    if (i < 0) return 0;
    if (!rcnt[i].running) clock_gettime(CLOCK_MONOTONIC, &rcnt[i].base);
    rcnt[i].running = 1;
    return 1;
}
#endif
#ifndef PSX_GAME_HAS_StopRCnt
int StopRCnt(uint32_t spec) {
    int i = rc_index(spec);
    if (i < 0) return 0;
    rcnt[i].running = 0;
    return 1;
}
#endif
#ifndef PSX_GAME_HAS_ResetRCnt
int ResetRCnt(uint32_t spec) {
    int i = rc_index(spec);
    if (i < 0) return 0;
    clock_gettime(CLOCK_MONOTONIC, &rcnt[i].base);
    return 1;
}
#endif
