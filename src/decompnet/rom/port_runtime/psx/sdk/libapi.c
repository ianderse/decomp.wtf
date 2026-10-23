/* The BIOS's C library calls (A0 table) the game reaches through its own stubs: plain C library behaviour, on host
 * pointers (gen/psx_sdk_stubs.c translates the game's with PSX_PTR). Also the heap (InitHeap/malloc in RAM), the
 * event calls and the critical sections, as plausible answers: nothing delivers events yet. */
#include <stdlib.h>
#include <string.h>

#include "../psx_runtime.h"
#include "libapi.h"

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

/* ---- events (B0 08 OpenEvent, 09 CloseEvent, 0C EnableEvent, 0D DisableEvent): a handle each, as the BIOS
 * numbers them (0xF1000000 + slot); none is ever delivered yet */
static unsigned n_events;
int psx_bios_OpenEvent(unsigned cls, unsigned spec, unsigned mode, unsigned handler) {
    static int logged;
    if (logged++ < 8)
        psx_log("api: OpenEvent(class 0x%08X, spec 0x%04X, mode 0x%04X, handler 0x%08X) -> 0x%08X\n", cls, spec, mode,
                handler, 0xF1000000u + n_events);
    return (int)(0xF1000000u + n_events++);
}
int psx_bios_CloseEvent(unsigned event) { (void)event; return 1; }
int psx_bios_EnableEvent(unsigned event) { (void)event; return 1; }
int psx_bios_DisableEvent(unsigned event) { (void)event; return 1; }

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
