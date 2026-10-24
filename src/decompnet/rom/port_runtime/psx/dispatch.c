/* Calls through the game's function pointers (PS1 addresses) to the host function at that address (psx_host_fn,
 * which tools/cc_ptr32.py puts in front of every such call), and what the run reports: the matched functions it enters
 * (psx_trace_enter, with cmake -DPSX_TRACE=ON: tools/cc_ptr32.py --trace and psx/trace.S), the unmatched and Sony
 * functions it reaches, and where it crashed. */
#define _XOPEN_SOURCE 700
#define _DARWIN_C_SOURCE
#include <dlfcn.h>
#include <execinfo.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ucontext.h>
#include <unistd.h>
#if defined(__APPLE__)
#include <mach-o/dyld.h>
#include <mach-o/getsect.h>
#endif

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
        psx_log("psx: call through PS1 address 0x%08X, which is no game function\n", addr);
    }
    report_recent();
    psx_exit_report();
    _exit(139);
}

/* psx/call32.S's lookup: psx_host_fn (the log, and the end of the run when there is no function), the answer kept in
 * the trampoline's cache with the two code words in RAM at the address now. psx_function_at depends on nothing
 * else, so a later call that finds the same words there gets the same function. Only addresses in RAM are kept (the
 * trampoline reads the words without a range check). The slot's function goes last and is cleared first: a reader
 * that sees a function sees the address and words that go with it (the game's code runs on one thread). */
PsxCall32Slot psx_call32_cache[PSX_CALL32_SLOTS];

NO_TRACE void *psx_call32_lookup(uint32_t addr) {
    void *fn = psx_host_fn(addr);
    if (addr >= PSX_RAM_BASE && addr + 8 <= PSX_RAM_BASE + PSX_RAM_SIZE && !(addr & 3)) {
        PsxCall32Slot *slot = &psx_call32_cache[(addr >> 2) & (PSX_CALL32_SLOTS - 1)];
        const uint32_t *code = psx_host(addr);
        __atomic_store_n(&slot->fn, NULL, __ATOMIC_RELEASE);
        slot->ps1 = addr;
        slot->word0 = code[0];
        slot->word1 = code[1];
        __atomic_store_n(&slot->fn, fn, __ATOMIC_RELEASE);
    }
    return fn;
}

/* ---- trace of the game's functions */
#define SEEN_SLOTS 16384
static const void *seen[SEEN_SLOTS];
static unsigned long n_entered, n_distinct;
static long trace_limit = -1;
static const void *recent[16];
static unsigned recent_at;

/* psx_trace_enter: from psx_trace_thunk (psx/trace.S), which tools/cc_ptr32.py --trace (cmake -DPSX_TRACE=ON) calls
 * first in every game function, every argument register kept. ps1: the function's PS1 address, fn: its host code. */
NO_TRACE void psx_trace_enter(uint32_t ps1, const void *fn) {
    unsigned long h = ((unsigned long)fn >> 2) % SEEN_SLOTS;
    (void)ps1;
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

/* -finstrument-functions' hook: only a build with -DPSX_PTR32=OFF -DPSX_TRACE=ON uses it (without the IR pass there
 * is no thunk). It puts fn in x0, so matched C that passes an argument through breaks under it. */
NO_TRACE void __cyg_profile_func_enter(void *fn, void *site) {
    (void)site;
    psx_trace_enter(0, fn);
}

NO_TRACE void __cyg_profile_func_exit(void *fn, void *site) { (void)fn; (void)site; }

/* ---- unmatched and Sony stubs */
#define MAX_HIT 4096
static const char *unmatched_hit[MAX_HIT];
static size_t n_unmatched;
static struct { const char *name; unsigned *calls; } sdk_hit[MAX_HIT];
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

/* the count saturates (psx_log_due): a stub the game spins on 2^31 times no longer wraps negative and logs every
 * call (a 7 GB log once); after the first 20 every 10,000th call is logged with its count, up to the 10,000,000th */
int psx_sdk_stub(const char *name, unsigned *calls) {
    if (psx_log_due(calls)) {
        if (*calls == 1 && n_sdk < MAX_HIT) {
            sdk_hit[n_sdk].name = name;
            sdk_hit[n_sdk++].calls = calls;
        }
        if (*calls <= PSX_LOG_FIRST)
            psx_log("sdk: %s (call %u): not implemented yet, returns 0\n", name, *calls);
        else
            psx_log("sdk: %s: %u calls (logged every %u from here)\n", name, *calls, PSX_LOG_EVERY);
    }
    return 0;
}

void psx_exit_report(void) {
    size_t i;
    static int done;
    if (done++) return;
    if (n_entered)
        psx_log("psx: summary: %lu calls into %lu distinct game functions; %zu unmatched functions reached; "
                "%zu Sony functions reached that are stubs\n", n_entered, n_distinct, n_unmatched, n_sdk);
    else
        psx_log("psx: summary: the trace is off (cmake -DPSX_TRACE=ON, decomp port --trace); %zu unmatched "
                "functions reached; %zu Sony functions reached that are stubs\n", n_unmatched, n_sdk);
    psx_log("psx: %d frames (vertical blanks)\n", psx_frames());
    for (i = 0; i < n_unmatched; i++) psx_log("  unmatched %s\n", unmatched_hit[i]);
    for (i = 0; i < n_sdk; i++)
        psx_log("  sdk stub %s x%u%s\n", sdk_hit[i].name, *sdk_hit[i].calls,
                *sdk_hit[i].calls >= PSX_COUNT_MAX ? " (or more)" : "");
    psx_mdec_exit(); /* the movie frames the MDEC decoded */
    psx_gpu_exit(); /* what was drawn; PSX_PORT_DUMP_VRAM=path writes the VRAM as a PNG */
}

/* ---- faults: a call into PS1 code is redirected; anything else is reported */
static void report_recent(void) {
    unsigned i;
    if (!n_entered) return; /* the trace is off */
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

/* the executable's code (__TEXT), for telling a truncated host code address in the fault report */
static void host_text(uintptr_t *start, uintptr_t *end) {
#if defined(__APPLE__)
    unsigned long size = 0;
    uint8_t *p = getsegmentdata((const void *)_dyld_get_image_header(0), "__TEXT", &size);
    *start = (uintptr_t)p;
    *end = (uintptr_t)p + size;
#elif defined(__linux__)
    extern char __executable_start[], etext[];
    *start = (uintptr_t)__executable_start;
    *end = (uintptr_t)etext;
#else
    *start = *end = 0;
#endif
}

/* a 32-bit value as the host code address it was cut from: an address in the executable's code whose low 32 bits
 * are v, 0 when there is none */
static uintptr_t truncated_host_code(uint32_t v) {
    uintptr_t start, end, hi;
    host_text(&start, &end);
    if (start >= end) return 0;
    for (hi = start >> 32; hi <= (end - 1) >> 32; hi++) {
        uintptr_t a = (hi << 32) | v;
        if (a >= start && a < end) return a;
    }
    return 0;
}

/* what an address in the window says about the bug: a PS1 function's own address used as data, or the low half of
 * a host code address (a function pointer or a return address that went through 32 bits) */
static void explain_window_addr(uint32_t ps1) {
    const PsxFunction *f = psx_function_at(ps1);
    uintptr_t host = truncated_host_code(ps1);
    if (f && f->vram == ps1)
        psx_log("psx: that is the PS1 address of %s's code (%s): a function's address used as a data pointer\n",
                f->name, f->segment);
    if (host) {
        const char *name = psx_host_name((void *)host);
        Dl_info info;
        long off = 0;
        if (dladdr((void *)host, &info) && info.dli_saddr) off = (long)(host - (uintptr_t)info.dli_saddr);
        psx_log("psx: looks like a host code address truncated: a function pointer or return address stored as 32 "
                "bits (0x%08X is the low half of %p, %s+0x%lX)\n", ps1, (void *)host, name ? name : "?", off);
    }
}

/* the faulting code's callers. backtrace() stops at the first frame outside the thread's own stack, and the game's
 * functions run on the PS1 stack in the window, so the chain is walked here from the interrupted registers: pc, lr,
 * then each frame record {previous fp, return address}, as long as fp is aligned and inside the game's stack, PS1
 * RAM or the thread's host stack, increasing within each (the game's main runs on the PS1 stack, called from the host one; nothing else
 * is read). x86_64 has no lr: pc, then the frames. */
static void host_stack(uintptr_t *lo, uintptr_t *hi) {
#if defined(__APPLE__)
    pthread_t self = pthread_self();
    *hi = (uintptr_t)pthread_get_stackaddr_np(self);
    *lo = *hi - pthread_get_stacksize_np(self);
#else
    *lo = *hi = 0;
#endif
}

static int frame_ok(uintptr_t fp, uintptr_t prev, uintptr_t lo, uintptr_t hi) {
    int in_ram = 0, in_host;
    if (!fp || (fp & 7)) return 0;
    if (psx_in_window((const void *)fp)) {
        uint32_t a = (uint32_t)fp;
        in_ram = (a >= PSX_RAM_BASE && a + 16 <= PSX_RAM_BASE + PSX_RAM_SIZE)
                 || (psx_game_stack_bottom && a >= psx_game_stack_bottom
                     && a + 16 <= psx_game_stack_bottom + PSX_STACK_SIZE);
    }
    in_host = fp >= lo && fp + 16 <= hi;
    if (!in_ram && !in_host) return 0;
    /* increasing within one stack; a move from one stack to the other is allowed */
    if (prev && psx_in_window((const void *)prev) == psx_in_window((const void *)fp) && fp <= prev) return 0;
    return 1;
}

static void host_backtrace(const ucontext_t *uc) {
    void *frames[64];
    int n = 0;
    uintptr_t fp = 0, lr = 0, pc = 0, prev = 0, lo, hi;
    host_stack(&lo, &hi);
#if defined(__APPLE__) && defined(__aarch64__)
    pc = uc->uc_mcontext->__ss.__pc; lr = uc->uc_mcontext->__ss.__lr; fp = uc->uc_mcontext->__ss.__fp;
#elif defined(__APPLE__) && defined(__x86_64__)
    pc = uc->uc_mcontext->__ss.__rip; fp = uc->uc_mcontext->__ss.__rbp;
#elif defined(__linux__) && defined(__aarch64__)
    pc = uc->uc_mcontext.pc; lr = uc->uc_mcontext.regs[30]; fp = uc->uc_mcontext.regs[29];
#elif defined(__linux__) && defined(__x86_64__)
    pc = uc->uc_mcontext.gregs[REG_RIP]; fp = uc->uc_mcontext.gregs[REG_RBP];
#endif
    if (pc) frames[n++] = (void *)pc;
    if (lr) frames[n++] = (void *)lr;
    while (n < 64 && frame_ok(fp, prev, lo, hi)) {
        const uintptr_t *rec = (const uintptr_t *)fp;
        if (!rec[1]) break;
        if (!(n == 2 && lr && rec[1] == lr)) frames[n++] = (void *)rec[1]; /* a leaf's lr is its frame's too */
        prev = fp;
        fp = rec[0];
    }
    psx_log("psx: host backtrace (the faulting pc, its return address, then the callers' frames):\n");
    backtrace_symbols_fd(frames, n, STDOUT_FILENO);
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
        host_backtrace(uc);
    } else {
        const char *where = psx_host_name((void *)pc);
        ps1_addr = window_addr((uintptr_t)info->si_addr, &in_addr);
        psx_log("psx: %s at address %p (pc %p, in %s)\n", sig == SIGBUS ? "bus error" : "segmentation fault",
                info->si_addr, (void *)pc, where ? where : "?");
        if (in_addr && ps1_addr < 0x10000)
            psx_log("psx: that is a null PS1 pointer + 0x%X (the window's base is never mapped)\n", ps1_addr);
        else if (in_addr) {
            psx_log("psx: that is PS1 address 0x%08X\n", ps1_addr);
            explain_window_addr(ps1_addr);
        }
        else if ((uintptr_t)info->si_addr < 0x100000000ull && (uintptr_t)info->si_addr >= 0x10000) {
            psx_log("psx: below 4 GB: a PS1 address used as a host pointer without the translation\n");
            explain_window_addr((uint32_t)(uintptr_t)info->si_addr);
        }
        host_backtrace(uc);
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
