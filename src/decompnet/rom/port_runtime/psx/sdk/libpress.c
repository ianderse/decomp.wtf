/* libpress over the software MDEC (psx/mdec.c), from psx-spx's MDEC and BS sections and PSY-Q's libpress API.
 *
 * DecDCTvlc does the Huffman stage on the CPU as Sony's does: the BS frame (from the St ring) to run-length codes in
 * the game's buffer, headed by the MDEC(1) command word. DecDCTin hands that buffer to the MDEC (the mode bits set
 * into the command word as the library does: bit 0 24-bit, bit 1 the STP bit); the codes are copied at once, as the
 * DMA would have sent them. DecDCTout(buf, size) is DMA1: the next `size` words of decoded macroblocks (16x16
 * pixels, rows of 16, one after another: a column strip of the picture when the frame's macroblocks run top to
 * bottom) written into the game's buffer, decoded right there.
 *
 * Callbacks: the out-callback (DMACallback(1) on the PS1, from the DMA interrupt) runs synchronously at the end of
 * DecDCTout, on the main thread. A callback that starts the next DecDCTout (the usual chain: LoadImage this strip,
 * decode the next) doesn't nest: its completion is queued and the outermost DecDCTout runs the callbacks one after
 * another until no transfer is left. So a frame's strips are all decoded and loaded by the time the first
 * DecDCTout returns, and a game spinning on a flag its callback sets (without calling anything) sees it set. The
 * in-callback runs at the end of DecDCTin the same way. The *Sync calls have nothing to wait for. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../mdec.h"
#include "../psx_runtime.h"
#include "libpress.h"

#ifndef PSX_SDK_WEAK
#define PSX_SDK_WEAK __attribute__((weak))
#endif

#define MAX_RL 0x20000 /* halfwords: MDEC(1)'s 16-bit word count, twice */

static MdecTables T;
static int tables_set;
static struct {
    uint16_t rl[MAX_RL]; /* the MDEC's input: run-length codes DecDCTin sent */
    int n, pos, active, depth, signed_out, set15;
    uint8_t pend[16 * 16 * 3]; /* a decoded macroblock not yet all transferred */
    int pend_len, pend_pos;
    uint32_t cb_in, cb_out;
    int out_queued, in_out_cb, in_in_cb;
    /* DecDCTvlc's output, resumable when DecDCTvlcSize limits a call */
    uint16_t vlc[MAX_RL + 2];
    int vlc_total, vlc_done, vlc_limit;
    uint32_t vlc_dst;
} M;
static struct {
    unsigned long vlc_frames, ins, outs, macroblocks, short_outs;
    int first_out_vblank;
} S = {0, 0, 0, 0, 0, -1};
static int logs_left = 12;

#define PLOG(...) do { if (logs_left > 0) { logs_left--; psx_log(__VA_ARGS__); } } while (0)

static void tables(void) {
    if (!tables_set) {
        mdec_default_tables(&T);
        tables_set = 1;
    }
}
static uint32_t rd32(uint32_t a) { uint32_t v; memcpy(&v, psx_host(a), 4); return v; }
static void wr32(uint32_t a, uint32_t v) { memcpy(psx_host(a), &v, 4); }
static void call_ps1(uint32_t fn) { ((void (*)(void))psx_host_fn(fn))(); }

static void abort_decode(void) {
    M.active = 0;
    M.n = M.pos = 0;
    M.pend_len = M.pend_pos = 0;
}

/* ---- reset and the environment */
PSX_SDK_WEAK void MDEC_reset(int mode) {
    /* 0: reset, the standard tables uploaded (Sony's library sends its quant table and the scale matrix); 1: abort
     * what is in progress; otherwise (the library's time-out path) the same */
    abort_decode();
    if (mode == 0) {
        mdec_default_tables(&T);
        tables_set = 1;
    }
}
PSX_SDK_WEAK void DecDCTReset(int mode) { MDEC_reset(mode); }

PSX_SDK_WEAK uint32_t DecDCTGetEnv(uint32_t env) {
    PsxDecDctEnv *e = (PsxDecDctEnv *)psx_host(env);
    tables();
    if (e) {
        memcpy(e->iq_y, T.iq_y, 64);
        memcpy(e->iq_c, T.iq_c, 64);
        memcpy(e->dct, T.scale, sizeof e->dct);
    }
    return env;
}
PSX_SDK_WEAK uint32_t DecDCTPutEnv(uint32_t env) {
    const PsxDecDctEnv *e = (const PsxDecDctEnv *)psx_host(env);
    tables();
    if (e) {
        memcpy(T.iq_y, e->iq_y, 64);
        memcpy(T.iq_c, e->iq_c, 64);
        memcpy(T.scale, e->dct, sizeof T.scale);
    }
    return env;
}

/* ---- the Huffman stage */
PSX_SDK_WEAK int DecDCTBufSize(uint32_t bs) { return bs ? *(const uint16_t *)psx_host(bs) : 0; }

PSX_SDK_WEAK int DecDCTvlcSize(int size) {
    int old = M.vlc_limit;
    M.vlc_limit = size > 0 ? size : 0;
    return old;
}
PSX_SDK_WEAK int DecDCTvlcSize2(int size) { return DecDCTvlcSize(size); }

/* bs: a BS frame, or 0 to go on with the last one (after a call DecDCTvlcSize cut short); 0 when done, 1 when more
 * is left */
PSX_SDK_WEAK int DecDCTvlc(uint32_t bs, uint32_t buf) {
    int left, n;
    if (bs) {
        const uint8_t *p = (const uint8_t *)psx_host(bs);
        uint32_t hdr = rd32(bs), words = hdr & 0xFFFF;
        size_t avail = (size_t)words * 8 + 64; /* the bitstream is shorter than the codes it makes */
        int target = (int)words * 2;
        if (bs >= PSX_RAM_BASE && bs < PSX_RAM_BASE + PSX_RAM_SIZE && avail > PSX_RAM_BASE + PSX_RAM_SIZE - bs)
            avail = PSX_RAM_BASE + PSX_RAM_SIZE - bs;
        n = mdec_vlc(p, avail, M.vlc + 2, MAX_RL);
        if (n < 0) {
            PLOG("press: DecDCTvlc(0x%08X): not a BS v1-v3 frame (header %08X %08X)\n", bs, hdr, rd32(bs + 4));
            M.vlc_total = M.vlc_done = 0;
            return -1;
        }
        if (n > target) target = (n + 1) & ~1; /* more than the header said: the command word says what there is */
        while (n < target) M.vlc[2 + n++] = 0xFE00; /* MDEC padding */
        hdr = (hdr & 0xFFFF0000u) | (uint32_t)(target / 2);
        memcpy(M.vlc, &hdr, 4);
        M.vlc_total = 2 + target;
        M.vlc_done = 0;
        M.vlc_dst = buf;
        if (!S.vlc_frames++)
            PLOG("press: first DecDCTvlc: BS v%u, quant %u, %d run-length codes into 0x%08X\n",
                 *(const uint16_t *)(p + 6), *(const uint16_t *)(p + 4) & 0x3F, target, buf);
    } else if (buf) {
        M.vlc_dst = buf;
    }
    left = M.vlc_total - M.vlc_done;
    if (left <= 0) return 0;
    n = M.vlc_limit ? M.vlc_limit * 2 : left;
    if (n > left) n = left;
    memcpy(psx_host(M.vlc_dst), M.vlc + M.vlc_done, (size_t)n * 2);
    M.vlc_dst += (uint32_t)n * 2;
    M.vlc_done += n;
    return M.vlc_done < M.vlc_total;
}
PSX_SDK_WEAK int DecDCTvlc2(uint32_t bs, uint32_t buf, uint32_t table) { (void)table; return DecDCTvlc(bs, buf); }
PSX_SDK_WEAK void DecDCTvlcBuild(uint32_t table) { (void)table; } /* the table DecDCTvlc2 would use: not needed */

/* ---- the MDEC */
static void command(uint32_t addr) {
    uint32_t w = rd32(addr), i;
    const uint8_t *p = (const uint8_t *)psx_host(addr + 4);
    tables();
    switch (w >> 29) {
    case 1: /* decode macroblocks */
        abort_decode();
        M.n = (int)(w & 0xFFFF) * 2;
        memcpy(M.rl, p, (size_t)M.n * 2);
        M.depth = (int)(w >> 27) & 3;
        M.signed_out = (int)(w >> 26) & 1;
        M.set15 = (int)(w >> 25) & 1;
        M.active = 1;
        if (!S.ins++)
            PLOG("press: first DecDCTin: %d codes, %s output%s\n", M.n,
                 M.depth == MDEC_DEPTH24 ? "24-bit" : M.depth == MDEC_DEPTH15 ? "15-bit" : "mono",
                 M.signed_out ? " (signed)" : "");
        break;
    case 2: /* the quant tables */
        memcpy(T.iq_y, p, 64);
        if (w & 1) memcpy(T.iq_c, p + 64, 64);
        break;
    case 3: /* the scale matrix */
        for (i = 0; i < 64; i++) memcpy(&T.scale[i], p + 2 * i, 2);
        break;
    default: break;
    }
}

PSX_SDK_WEAK void DecDCTin(uint32_t buf, int mode) {
    uint32_t w;
    if (!buf) return;
    w = rd32(buf); /* as the library: bit 0 of mode picks 24-bit (depth 2) over 15-bit (3), bit 1 sets STP */
    w = (mode & 1) ? (w & ~0x08000000u) : (w | 0x08000000u);
    w = (mode & 2) ? (w | 0x02000000u) : (w & ~0x02000000u);
    wr32(buf, w);
    command(buf);
    if (M.cb_in && !M.in_in_cb) {
        M.in_in_cb = 1;
        call_ps1(M.cb_in);
        M.in_in_cb = 0;
    }
}

PSX_SDK_WEAK void DecDCTout(uint32_t buf, int size) {
    uint8_t *dst = (uint8_t *)psx_host(buf);
    size_t want = size > 0 ? (size_t)size * 4 : 0;
    while (want && dst) {
        size_t n;
        if (M.pend_pos == M.pend_len) {
            int b = M.active ? mdec_macroblock(&T, M.rl, M.n, &M.pos, M.depth, M.signed_out, M.set15, M.pend) : 0;
            if (!b) { M.active = 0; break; }
            M.pend_len = b;
            M.pend_pos = 0;
            S.macroblocks++;
        }
        n = (size_t)(M.pend_len - M.pend_pos);
        if (n > want) n = want;
        memcpy(dst, M.pend + M.pend_pos, n);
        M.pend_pos += (int)n;
        dst += n;
        want -= n;
    }
    if (want && S.short_outs++ < 4)
        psx_log("press: DecDCTout(0x%08X, %d words): the MDEC had %lu bytes fewer to give (no DecDCTin, or its codes "
                "ran out)\n", buf, size, (unsigned long)want);
    if (!S.outs++) {
        S.first_out_vblank = psx_frames();
        PLOG("press: first DecDCTout completion (%d words to 0x%08X) at vblank %d\n", size, buf, S.first_out_vblank);
    }
    M.out_queued++;
    if (M.in_out_cb) return; /* a callback's transfer: the outer loop below runs its callback */
    while (M.out_queued) {
        M.out_queued--;
        if (M.cb_out) {
            M.in_out_cb = 1;
            call_ps1(M.cb_out);
            M.in_out_cb = 0;
        }
    }
}

PSX_SDK_WEAK int DecDCTinSync(int mode) { (void)mode; return 0; }
PSX_SDK_WEAK int DecDCToutSync(int mode) { (void)mode; return 0; }

/* A callback the game hands over: a PS1 function address, or, when the game's C calls the setter without a prototype
 * (SLUS-01252's func_800DDB0C: `DecDCToutCallback(func_800DDF20)`), the host function itself, all 64 bits of it
 * (tools/cc_ptr32.py makes a function's address its PS1 address only where it becomes a 32-bit value): that one is
 * found in the function table. */
static uint32_t ps1_callback(uintptr_t f, const char *who) {
    size_t i;
    if (f < 0x100000000ull) return (uint32_t)f;
    if (psx_in_window((const void *)f)) return (uint32_t)(f - PSX_HOST_BASE);
    for (i = 0; i < psx_function_count; i++)
        if ((uintptr_t)psx_functions[i].fn == f) return psx_functions[i].vram;
    psx_log("press: %s(%p): neither a PS1 address nor a game function; ignored\n", who, (void *)f);
    return 0;
}

PSX_SDK_WEAK uint32_t DecDCTinCallback(uintptr_t func) {
    uint32_t old = M.cb_in;
    M.cb_in = ps1_callback(func, "DecDCTinCallback");
    return old;
}
PSX_SDK_WEAK uint32_t DecDCToutCallback(uintptr_t func) {
    uint32_t old = M.cb_out;
    M.cb_out = ps1_callback(func, "DecDCToutCallback");
    if (M.cb_out) PLOG("press: DecDCToutCallback(0x%08X)\n", M.cb_out);
    return old;
}

void psx_mdec_exit(void) {
    if (!S.vlc_frames && !S.ins && !S.outs) return;
    psx_log("press: %lu frames decoded (DecDCTvlc), %lu DecDCTin, %lu DecDCTout completions (%lu macroblocks)%s",
            S.vlc_frames, S.ins, S.outs, S.macroblocks, S.outs ? "" : "\n");
    if (S.outs) psx_log("; the first at vblank %d\n", S.first_out_vblank);
}
