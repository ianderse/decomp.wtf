/* Calls through the game's function pointers (PS1 addresses) to the host function at that address (psx_host_fn,
 * which tools/cc_ptr32.py puts in front of every such call), and what the run reports: the matched functions it enters (-finstrument-functions on the game's files), the unmatched and Sony
 * functions it reaches, and where it crashed. */
#define _XOPEN_SOURCE 700
#define _DARWIN_C_SOURCE
#include <dlfcn.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ucontext.h>
#include <unistd.h>

#include "psx_runtime.h"

#define NO_TRACE __attribute__((no_instrument_function))

void psx_log(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stdout, fmt, ap);
    va_end(ap);
    fflush(stdout);
}

int psx_soft(void) {
    static int soft = -1;
    if (soft < 0) {
        const char *s = getenv("PSX_PORT_SOFT");
        soft = s && *s && strcmp(s, "0") != 0;
    }
    return soft;
}

/* ---- host function -> name */
typedef struct { const void *fn; const char *name; } HostName;
static HostName *by_fn;
static size_t n_by_fn;

static int cmp_fn(const void *a, const void *b) {
    const char *x = ((const HostName *)a)->fn, *y = ((const HostName *)b)->fn;
    return x < y ? -1 : x > y;
}

const char *psx_host_name(const void *fn) {
    size_t lo = 0, hi = n_by_fn;
    Dl_info info;
    while (lo < hi) {
        size_t mid = (lo + hi) / 2;
        if ((const char *)by_fn[mid].fn < (const char *)fn) lo = mid + 1;
        else hi = mid;
    }
    if (lo < n_by_fn && by_fn[lo].fn == fn) return by_fn[lo].name;
    if (dladdr(fn, &info) && info.dli_sname) return info.dli_sname;
    return NULL;
}

/* ---- PS1 address -> host function (overlays share addresses: the one whose code is in RAM) */
const PsxFunction *psx_function_at(uint32_t vram) {
    size_t lo = 0, hi = psx_function_count, k;
    const PsxFunction *first = NULL;
    while (lo < hi) {
        size_t mid = (lo + hi) / 2;
        if (psx_functions[mid].vram < vram) lo = mid + 1;
        else hi = mid;
    }
    for (k = lo; k < psx_function_count && psx_functions[k].vram == vram; k++) {
        const uint32_t *code = psx_host(vram);
        if (!first) first = &psx_functions[k];
        if (vram >= PSX_RAM_BASE && vram + 8 <= PSX_RAM_BASE + PSX_RAM_SIZE && code[0] == psx_functions[k].word0
            && code[1] == psx_functions[k].word1)
            return &psx_functions[k];
    }
    return first;
}

static void report_recent(void);

/* the first call through each PS1 address is logged */
static void log_call(uint32_t addr, const PsxFunction *f) {
    static uint32_t logged[256];
    static unsigned n_logged;
    unsigned i;
    for (i = 0; i < n_logged && logged[i] != addr; i++) {}
    if (i == n_logged && n_logged < 256) {
        logged[n_logged++] = addr;
        psx_log("psx: call through a PS1 address 0x%08X: %s (%s)\n", addr, f->name, f->segment);
    }
}

NO_TRACE void *psx_host_fn(uint32_t addr) {
    const PsxFunction *f = psx_function_at(addr);
    Dl_info info;
    void *host;
    if (f && f->fn) {
        log_call(addr, f);
        return (void *)f->fn;
    }
    if (!addr) {
        psx_log("psx: call through a null function pointer\n");
    } else if ((addr >= PSX_RAM_BASE && addr < PSX_RAM_BASE + PSX_RAM_SIZE)
               || (addr >= PSX_SCRATCH_BASE && addr < PSX_SCRATCH_BASE + PSX_SCRATCH_SIZE)) {
        psx_log("psx: call into PS1 code at 0x%08X, where no function is known\n", addr);
    } else {
        /* a host function's address the game kept in 32 bits (the executable is in the window) */
        host = psx_host(addr);
        if (dladdr(host, &info) && info.dli_saddr == host) return host;
        psx_log("psx: call through 0x%08X, which is neither a game function nor a host one\n", addr);
    }
    report_recent();
    psx_exit_report();
    _exit(139);
}

/* ---- trace of the game's functions */
#define SEEN_SLOTS 16384
static const void *seen[SEEN_SLOTS];
static unsigned long n_entered, n_distinct;
static long trace_limit = -1;
static const void *recent[16];
static unsigned recent_at;

NO_TRACE void __cyg_profile_func_enter(void *fn, void *site) {
    unsigned long h = ((unsigned long)fn >> 2) % SEEN_SLOTS;
    (void)site;
    n_entered++;
    recent[recent_at++ & 15] = fn;
    while (seen[h] && seen[h] != fn) h = (h + 1) % SEEN_SLOTS;
    if (seen[h]) return;
    seen[h] = fn;
    n_distinct++;
    if (trace_limit < 0) {
        const char *s = getenv("PSX_TRACE");
        trace_limit = s ? atol(s) : 200;
    }
    if ((long)n_distinct <= trace_limit) {
        const char *name = psx_host_name(fn);
        psx_log("trace: enters %s\n", name ? name : "?");
    }
}

NO_TRACE void __cyg_profile_func_exit(void *fn, void *site) { (void)fn; (void)site; }

/* ---- unmatched and Sony stubs */
#define MAX_HIT 4096
static const char *unmatched_hit[MAX_HIT];
static size_t n_unmatched;
static struct { const char *name; int *calls; } sdk_hit[MAX_HIT];
static size_t n_sdk;

int psx_unmatched(const char *name) {
    size_t i;
    for (i = 0; i < n_unmatched && unmatched_hit[i] != name; i++) {}
    if (i == n_unmatched && n_unmatched < MAX_HIT) {
        unmatched_hit[n_unmatched++] = name;
        psx_log("unmatched: %s (%zu so far)\n", name, n_unmatched);
    }
    if (!psx_soft()) {
        psx_log("psx: %s is not decompiled yet (it is still assembly in the export): stopping here. "
                "PSX_PORT_SOFT=1 goes on past it (returning 0).\n", name);
        exit(3);
    }
    return 0;
}

int psx_sdk_stub(const char *name, int *calls) {
    if (++*calls == 1 && n_sdk < MAX_HIT) {
        sdk_hit[n_sdk].name = name;
        sdk_hit[n_sdk++].calls = calls;
    }
    if (*calls <= 20) psx_log("sdk: %s (call %d): not implemented yet, returns 0\n", name, *calls);
    return 0;
}

void psx_exit_report(void) {
    size_t i;
    static int done;
    if (done++) return;
    psx_log("psx: summary: %lu calls into %lu distinct game functions; %zu unmatched functions reached; "
            "%zu Sony functions reached that are stubs\n", n_entered, n_distinct, n_unmatched, n_sdk);
    for (i = 0; i < n_unmatched; i++) psx_log("  unmatched %s\n", unmatched_hit[i]);
    for (i = 0; i < n_sdk; i++) psx_log("  sdk stub %s x%d\n", sdk_hit[i].name, *sdk_hit[i].calls);
}

/* ---- faults: a call into PS1 code is redirected; anything else is reported */
static void report_recent(void) {
    unsigned i;
    psx_log("psx: the last game functions entered (oldest first):\n");
    for (i = 0; i < 16; i++) {
        const void *fn = recent[(recent_at + i) & 15];
        const char *name;
        if (!fn) continue;
        name = psx_host_name(fn);
        psx_log("  %s\n", name ? name : "?");
    }
}

/* the safety net: psx_host_fn covers every call through a 32-bit pointer in the game's files, but a jump into PS1
 * code from anywhere else (an ordinary pointer built from a PS1 address) faults in RAM, which isn't executable;
 * that is sent to the host function too. Anything else is reported. */
static uint32_t window_addr(uintptr_t a, int *in) {
    *in = psx_in_window((const void *)a);
    return (uint32_t)a;
}

static void on_fault(int sig, siginfo_t *info, void *ctx) {
    uintptr_t pc = 0;
    ucontext_t *uc = ctx;
    int in_pc, in_addr;
    uint32_t ps1_pc, ps1_addr;
#if defined(__APPLE__) && defined(__x86_64__)
    pc = uc->uc_mcontext->__ss.__rip;
#elif defined(__APPLE__) && defined(__aarch64__)
    pc = uc->uc_mcontext->__ss.__pc;
#elif defined(__linux__) && defined(__x86_64__)
    pc = uc->uc_mcontext.gregs[REG_RIP];
#elif defined(__linux__) && defined(__aarch64__)
    pc = uc->uc_mcontext.pc;
#endif
    ps1_pc = window_addr(pc, &in_pc);
    if (in_pc && ps1_pc >= PSX_RAM_BASE && ps1_pc < PSX_RAM_BASE + PSX_RAM_SIZE) {
        const PsxFunction *f = psx_function_at(ps1_pc);
        if (f && f->fn) {
            log_call(ps1_pc, f);
#if defined(__APPLE__) && defined(__x86_64__)
            uc->uc_mcontext->__ss.__rip = (uintptr_t)f->fn;
#elif defined(__APPLE__) && defined(__aarch64__)
            uc->uc_mcontext->__ss.__pc = (uintptr_t)f->fn;
#elif defined(__linux__) && defined(__x86_64__)
            uc->uc_mcontext.gregs[REG_RIP] = (uintptr_t)f->fn;
#elif defined(__linux__) && defined(__aarch64__)
            uc->uc_mcontext.pc = (uintptr_t)f->fn;
#endif
            return;
        }
        psx_log("psx: call into PS1 code at 0x%08X, where no function is known\n", ps1_pc);
    } else {
        const char *where = psx_host_name((void *)pc);
        ps1_addr = window_addr((uintptr_t)info->si_addr, &in_addr);
        psx_log("psx: %s at address %p (pc %p, in %s)\n", sig == SIGBUS ? "bus error" : "segmentation fault",
                info->si_addr, (void *)pc, where ? where : "?");
        if (in_addr)
            psx_log("psx: that is PS1 address 0x%08X%s\n", ps1_addr,
                    ps1_addr < 0x10000 ? " (a null pointer's field, in the game's terms)" : "");
        else if ((uintptr_t)info->si_addr < PSX_HOST_BASE && (uintptr_t)info->si_addr >= 0x10000)
            psx_log("psx: below the window: a PS1 address used as a host pointer without the translation\n");
    }
    report_recent();
    psx_exit_report();
    _exit(139);
}

void psx_dispatch_init(void) {
    size_t i;
    struct sigaction sa;
    static char altstack[1 << 16];
    stack_t ss;
    by_fn = malloc(sizeof *by_fn * (psx_function_count ? psx_function_count : 1));
    for (i = 0; i < psx_function_count; i++)
        if (psx_functions[i].fn) {
            by_fn[n_by_fn].fn = (const void *)psx_functions[i].fn;
            by_fn[n_by_fn++].name = psx_functions[i].name;
        }
    qsort(by_fn, n_by_fn, sizeof *by_fn, cmp_fn);
    ss.ss_sp = altstack;
    ss.ss_size = sizeof altstack;
    ss.ss_flags = 0;
    sigaltstack(&ss, NULL);
    memset(&sa, 0, sizeof sa);
    sa.sa_sigaction = on_fault;
    sa.sa_flags = SA_SIGINFO | SA_ONSTACK | SA_NODEFER;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGSEGV, &sa, NULL);
    sigaction(SIGBUS, &sa, NULL);
}
