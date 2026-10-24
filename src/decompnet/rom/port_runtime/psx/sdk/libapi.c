/* The BIOS's C library calls (A0 table) the game reaches through its own stubs: plain C library behaviour, on host
 * pointers (gen/psx_sdk_stubs.c translates the game's with PSX_PTR). Also the heap (InitHeap/malloc in RAM), the
 * events (delivered by the game and by the card driver, sdk/libcard.c) and the critical sections. */
#include <stdlib.h>
#include <string.h>

#include "../psx_runtime.h"
#include "libapi.h"
#include "libcard.h"

void *psx_bios_memcpy(void *dst, const void *src, int n) { return n > 0 ? memcpy(dst, src, (size_t)n) : dst; }
void *psx_bios_memset(void *dst, int c, int n) { return n > 0 ? memset(dst, c, (size_t)n) : dst; }
void *psx_bios_memmove(void *dst, const void *src, int n) { return n > 0 ? memmove(dst, src, (size_t)n) : dst; }
void *psx_bios_memchr(const void *s, int c, int n) { return n > 0 ? memchr(s, c, (size_t)n) : NULL; }
int psx_bios_strlen(const char *s) { return s ? (int)strlen(s) : 0; }
char *psx_bios_strcpy(char *d, const char *s) { return strcpy(d, s); }
char *psx_bios_strncpy(char *d, const char *s, int n) { return strncpy(d, s, (size_t)n); }
char *psx_bios_strcat(char *d, const char *s) { return strcat(d, s); }

/* the BIOS's rand: the classic LCG, 15 bits */
static unsigned rand_state = 0x24040001;
int psx_bios_rand(void) {
    rand_state = rand_state * 1103515245u + 12345u;
    return (int)((rand_state >> 16) & 0x7FFF);
}
void psx_bios_srand(unsigned seed) { rand_state = seed; }

/* ---- the heap (A0 39 InitHeap, 33 malloc, 34 free, 37 calloc, 38 realloc): in RAM, so the game can keep what
 * it gets */
void psx_bios_InitHeap(void *addr, unsigned size) { psx_heap_init(psx_ps1(addr), size); }
void *psx_bios_malloc(unsigned n) { return psx_malloc(n); }
void psx_bios_free(void *p) { psx_free(p); }
void *psx_bios_calloc(unsigned n, unsigned size) {
    void *p = psx_malloc(n * size);
    if (p) memset(p, 0, (size_t)n * size);
    return p;
}
void *psx_bios_realloc(void *p, unsigned n) {
    void *q;
    uint32_t old;
    if (!p) return psx_malloc(n);
    old = *(uint32_t *)((char *)p - 8) - 8; /* the block header's size */
    q = psx_malloc(n);
    if (q) memcpy(q, p, old < n ? old : n);
    psx_free(p);
    return q;
}

/* ---- events (B0 07 DeliverEvent, 08 OpenEvent, 09 CloseEvent, 0A WaitEvent, 0B TestEvent, 0C EnableEvent,
 * 0D DisableEvent, 20 UnDeliverEvent), as psx-spx's "BIOS Event Functions" describe the kernel's EvCB table: a slot's
 * status is 0 free, 0x1000 disabled (OpenEvent leaves it so), 0x2000 enabled/busy, 0x4000 enabled/ready.
 * DeliverEvent(class, spec) takes every enabled/busy slot of that class and spec: mode 0x2000 (EvMdNOINTR) becomes
 * ready, mode 0x1000 (EvMdINTR) has its callback run and stays busy (never ready). TestEvent returns 1 for a ready slot
 * and makes it busy again (the test clears the ready flag), 0 for a busy or disabled one; WaitEvent the same, blocking
 * until ready (0 at once for a disabled one). The handle is 0xF1000000 + slot, the lowest free one, as the BIOS
 * numbers them. Who delivers: the game itself (B0 07), and the runtime's card driver (sdk/libcard.c) from the vblank.
 * A game spinning on TestEvent never calls VSync, where the interrupts' work happens here, so a miss while the card
 * has an event on its way waits for the next vblank, and a long run of misses with no vblank between them pumps one
 * too (the window stays alive). */
#define EV_SLOTS 64
#define EV_FREE 0
#define EV_DISABLED 0x1000u
#define EV_BUSY 0x2000u
#define EV_READY 0x4000u
static struct { uint32_t cls, spec, mode, func, status; } ev[EV_SLOTS];
static unsigned opened_logs, delivered_logs;

static int ev_slot(unsigned event) {
    unsigned i = event - 0xF1000000u;
    return (event & 0xFFFF0000u) == 0xF1000000u && i < EV_SLOTS && ev[i].status != EV_FREE ? (int)i : -1;
}

int psx_bios_OpenEvent(unsigned cls, unsigned spec, unsigned mode, unsigned handler) {
    unsigned i;
    for (i = 0; i < EV_SLOTS && ev[i].status != EV_FREE; i++) {}
    if (i == EV_SLOTS) {
        psx_log("api: OpenEvent(class 0x%08X, spec 0x%04X): no free slot of %d\n", cls, spec, EV_SLOTS);
        return -1;
    }
    ev[i].cls = cls;
    ev[i].spec = spec;
    ev[i].mode = mode;
    ev[i].func = handler;
    ev[i].status = EV_DISABLED;
    if (psx_log_due(&opened_logs))
        psx_log("api: OpenEvent(class 0x%08X, spec 0x%04X, mode 0x%04X, handler 0x%08X) -> 0x%08X\n", cls, spec, mode,
                handler, 0xF1000000u + i);
    return (int)(0xF1000000u + i);
}
int psx_bios_CloseEvent(unsigned event) {
    int i = ev_slot(event);
    if (i >= 0) ev[i].status = EV_FREE;
    return 1;
}
int psx_bios_EnableEvent(unsigned event) {
    int i = ev_slot(event);
    if (i >= 0 && ev[i].status == EV_DISABLED) ev[i].status = EV_BUSY;
    return 1;
}
int psx_bios_DisableEvent(unsigned event) {
    int i = ev_slot(event);
    if (i >= 0) ev[i].status = EV_DISABLED;
    return 1;
}

int psx_bios_DeliverEvent(unsigned cls, unsigned spec) {
    unsigned i;
    int n = 0;
    for (i = 0; i < EV_SLOTS; i++) {
        if (ev[i].status != EV_BUSY || ev[i].cls != cls || ev[i].spec != spec) continue;
        n++;
        if (ev[i].mode == PSX_EvMdINTR) {
            if (ev[i].func) ((void (*)(void))psx_host_fn(ev[i].func))();
        } else {
            ev[i].status = EV_READY;
        }
    }
    if (psx_log_due(&delivered_logs))
        psx_log("api: DeliverEvent(class 0x%08X, spec 0x%04X) at vblank %d: %d event%s\n", cls, spec, psx_frames(), n,
                n == 1 ? "" : "s");
    return n;
}
int psx_bios_UnDeliverEvent(unsigned cls, unsigned spec) {
    unsigned i;
    for (i = 0; i < EV_SLOTS; i++)
        if (ev[i].status == EV_READY && ev[i].mode == PSX_EvMdNOINTR && ev[i].cls == cls && ev[i].spec == spec)
            ev[i].status = EV_BUSY;
    return 1;
}

/* a miss on an enabled event: let a vblank pass when one brings an event (the card's), or after a long spin */
static void ev_spin(void) {
    static int last_frame = -1;
    static unsigned misses, spin_logs;
    if (psx_frames() != last_frame) {
        last_frame = psx_frames();
        misses = 0;
    }
    if (psx_card_pending()) {
        psx_vblank_wait();
    } else if (++misses >= 100000) {
        if (psx_log_due(&spin_logs))
            psx_log("api: 100000 event tests with no vblank and nothing on its way: a vblank pumped (vblank %d)\n",
                    psx_frames());
        misses = 0;
        psx_vblank_wait();
    }
}

int psx_bios_TestEvent(unsigned event) {
    int i = ev_slot(event);
    if (i >= 0 && ev[i].status == EV_READY) {
        ev[i].status = EV_BUSY;
        return 1;
    }
    if (i >= 0 && ev[i].status == EV_BUSY) ev_spin();
    return 0;
}
int psx_bios_WaitEvent(unsigned event) {
    static unsigned wait_logs;
    int i = ev_slot(event), waited = 0;
    if (i < 0 || ev[i].status == EV_DISABLED) return 0;
    while (ev[i].status != EV_READY) {
        if (ev[i].status != EV_BUSY) return 0; /* closed or disabled meanwhile */
        if (!psx_vblank_wait()) return 0;      /* inside a vblank callback: nothing can arrive */
        if (++waited == 600 && psx_log_due(&wait_logs))
            psx_log("api: WaitEvent(0x%08X: class 0x%08X, spec 0x%04X) still waiting after 600 vblanks\n", event,
                    ev[i].cls, ev[i].spec);
    }
    ev[i].status = EV_BUSY;
    return 1;
}

/* ---- syscall 1/2: EnterCriticalSection (1: interrupts were on and are now off) and ExitCriticalSection */
static int critical;
int psx_EnterCriticalSection(void) {
    int was = !critical;
    critical = 1;
    return was;
}
int psx_ExitCriticalSection(void) {
    critical = 0;
    return 1;
}

#ifndef PSX_GAME_HAS_OpenEvent /* the game has its own, as matched C */
int OpenEvent(unsigned cls, unsigned spec, unsigned mode, unsigned handler) {
    return psx_bios_OpenEvent(cls, spec, mode, handler);
}
#endif
#ifndef PSX_GAME_HAS_CloseEvent
int CloseEvent(unsigned event) { return psx_bios_CloseEvent(event); }
#endif
#ifndef PSX_GAME_HAS_EnableEvent
int EnableEvent(unsigned event) { return psx_bios_EnableEvent(event); }
#endif
#ifndef PSX_GAME_HAS_DisableEvent
int DisableEvent(unsigned event) { return psx_bios_DisableEvent(event); }
#endif
#ifndef PSX_GAME_HAS_EnterCriticalSection
int EnterCriticalSection(void) { return psx_EnterCriticalSection(); }
#endif
#ifndef PSX_GAME_HAS_ExitCriticalSection
int ExitCriticalSection(void) { return psx_ExitCriticalSection(); }
#endif
#ifndef PSX_GAME_HAS_DeliverEvent
int DeliverEvent(unsigned cls, unsigned spec) { return psx_bios_DeliverEvent(cls, spec); }
#endif
#ifndef PSX_GAME_HAS_UnDeliverEvent
int UnDeliverEvent(unsigned cls, unsigned spec) { return psx_bios_UnDeliverEvent(cls, spec); }
#endif
#ifndef PSX_GAME_HAS_TestEvent
int TestEvent(unsigned event) { return psx_bios_TestEvent(event); }
#endif
#ifndef PSX_GAME_HAS_WaitEvent
int WaitEvent(unsigned event) { return psx_bios_WaitEvent(event); }
#endif
