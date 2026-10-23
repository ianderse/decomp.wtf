/* libgpu over the software GPU (psx/gpu.c): the environments are applied, ordering tables walked in PS1 RAM and
 * rasterised, VRAM transfers done at once (DrawSync has nothing to wait for). Both halves of the library are here:
 * the calls a game makes (ClearOTag, DrawOTag, LoadImage, ...) and the driver under them (_otc, _cwc, _dws, ...) that
 * libgpu's own code calls through its table, so a game whose libgpu is partly matched C goes through the same GPU.
 * Every function is left out when the game has it as matched C (PSX_GAME_HAS_<name>, gen/psx_sdk_config.h). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../psx_runtime.h"
#include "../gpu.h"
#include "libgpu.h"
#include "libetc.h"

static DRAWENV draw_env;
static DISPENV disp_env;
static int have_draw, debug_level, reverse, queue_mode;
static psx_addr sync_callback;
static int started;

static void *H(psx_addr a) { return psx_host(a); }
static uint32_t rd32(psx_addr a) { uint32_t v; memcpy(&v, H(a), 4); return v; }
static void wr32(psx_addr a, uint32_t v) { memcpy(H(a), &v, 4); }
static void set_len(psx_addr p, int len) { ((uint8_t *)H(p))[3] = (uint8_t)len; }
static void set_code(psx_addr p, int code) { ((uint8_t *)H(p))[7] = (uint8_t)code; }

static void start(void) {
    if (started) return;
    started = 1;
    gpu_reset();
}

/* GPUSTAT as the game would read it at 0x1F801814 (plain memory here): ready, so a loop polling it doesn't hang */
static void publish_stat(void) {
    uint32_t s = gpu_read_stat();
    memcpy(psx_host(0x1F801814u), &s, 4);
}

/* ---- the driver */
#ifndef PSX_GAME_HAS__ctl
void _ctl(uint32_t word) {
    start();
    gpu_gp1(word);
    publish_stat();
}
#endif

#ifndef PSX_GAME_HAS__getctl
uint32_t _getctl(int n) { return ((uint32_t)n << 24) | (gpu_ctl(n) & 0xFFFFFF); }
#endif

#ifndef PSX_GAME_HAS__param
uint32_t _param(int n) { return gpu_info((uint32_t)n) & 0xFFFFFF; }
#endif

#ifndef PSX_GAME_HAS__status
uint32_t _status(void) { return gpu_read_stat(); }
#endif

#ifndef PSX_GAME_HAS__reset
int _reset(int mode) {
    start();
    if (mode == 0) gpu_gp1(0);
    publish_stat();
    return 0;
}
#endif

#ifndef PSX_GAME_HAS__sync
int _sync(int mode) { (void)mode; return 0; }
#endif

#ifndef PSX_GAME_HAS__exeque
int _exeque(void) { return 0; }
#endif

/* the queue: run at once. func is a PS1 function address (one of the driver's, from libgpu's table) */
#ifndef PSX_GAME_HAS__addque2
int _addque2(psx_addr func, psx_addr param, int size, int arg) {
    int (*fn)(psx_addr, int) = (int (*)(psx_addr, int))psx_host_fn(func);
    (void)size;
    return fn(param, arg);
}
#endif

#ifndef PSX_GAME_HAS__addque
int _addque(psx_addr func, psx_addr param, int arg) { return _addque2(func, param, 0, arg); }
#endif

#ifndef PSX_GAME_HAS__clr
int _clr(psx_addr rect, uint32_t rgb) {
    const RECT *r = H(rect);
    start();
    gpu_fill(r->x, r->y, r->w, r->h, rgb);
    return 0;
}
#endif

static void load(psx_addr rect, psx_addr p) {
    static int logged;
    const RECT *r = H(rect);
    start();
    if (logged++ < 8) psx_log("gpu: LoadImage %d,%d %dx%d from 0x%08X\n", r->x, r->y, r->w, r->h, p);
    if (r->w <= 0 || r->h <= 0) return;
    gpu_load_image(r->x, r->y, r->w, r->h, (const uint16_t *)H(p));
}

static void store(psx_addr rect, psx_addr p) {
    const RECT *r = H(rect);
    if (r->w <= 0 || r->h <= 0) return;
    gpu_store_image(r->x, r->y, r->w, r->h, (uint16_t *)H(p));
}

#ifndef PSX_GAME_HAS__dws
int _dws(psx_addr rect, psx_addr p) { load(rect, p); return 0; }
#endif

#ifndef PSX_GAME_HAS__drs
int _drs(psx_addr rect, psx_addr p) { store(rect, p); return 0; }
#endif

/* GP0 words straight from memory (DrawPrim) */
#ifndef PSX_GAME_HAS__cwb
int _cwb(psx_addr p, int n) {
    start();
    if (n > 0) gpu_gp0((const uint32_t *)H(p), n);
    return 0;
}
#endif

static void draw_chain(psx_addr p) {
    static int in_callback;
    start();
    gpu_draw_chain((const uint8_t *)psx_host(PSX_RAM_BASE), p);
    if (sync_callback && !in_callback) { /* drawing is done when DrawOTag returns: the callback the GPU's IRQ ran */
        in_callback = 1;
        ((void (*)(void))psx_host_fn(sync_callback))();
        in_callback = 0;
    }
}

/* DMA2 in linked-list mode: an ordering table, or one packet ending in a terminator (MoveImage's) */
#ifndef PSX_GAME_HAS__cwc
void _cwc(psx_addr p) { draw_chain(p); }
#endif

/* DMA6: an ordering table cleared in reverse (each entry points at the one before; the first ends the list) */
#ifndef PSX_GAME_HAS__otc
void _otc(psx_addr ot, int n) {
    int i;
    if (n <= 0) return;
    wr32(ot, 0x00FFFFFF);
    for (i = 1; i < n; i++) wr32(ot + 4u * (uint32_t)i, (ot + 4u * (uint32_t)(i - 1)) & 0xFFFFFF);
}
#endif

/* ---- the library */
#ifndef PSX_GAME_HAS_ResetGraph
int ResetGraph(int mode) {
    psx_log("gpu: ResetGraph(%d)\n", mode);
    start();
    if (mode == 0 || mode == 3) {
        gpu_gp1(0);
        if (mode == 0) memset(gpu_vram, 0, sizeof gpu_vram);
    }
    publish_stat();
    return 0;
}
#endif

#ifndef PSX_GAME_HAS_SetGraphDebug
int SetGraphDebug(int level) {
    int old = debug_level;
    debug_level = level;
    return old;
}
#endif

#ifndef PSX_GAME_HAS_GetGraphDebug
int GetGraphDebug(void) { return debug_level; }
#endif

#ifndef PSX_GAME_HAS_SetGraphReverse
int SetGraphReverse(int mode) {
    int old = reverse;
    reverse = mode; /* the screen flipped horizontally (arcade cabinets): not drawn flipped here */
    return old;
}
#endif

#ifndef PSX_GAME_HAS_SetGraphQueue
int SetGraphQueue(int mode) {
    int old = queue_mode;
    queue_mode = mode;
    return old;
}
#endif

#ifndef PSX_GAME_HAS_GetGraphType
int GetGraphType(void) { return 0; }
#endif

#ifndef PSX_GAME_HAS_SetDispMask
void SetDispMask(int mask) { _ctl(mask ? 0x03000000u : 0x03000001u); }
#endif

#ifndef PSX_GAME_HAS_SetDefDrawEnv
psx_addr SetDefDrawEnv(psx_addr a, int x, int y, int w, int h) {
    DRAWENV *env = H(a);
    memset(env, 0, sizeof *env);
    env->clip.x = (int16_t)x; env->clip.y = (int16_t)y; env->clip.w = (int16_t)w; env->clip.h = (int16_t)h;
    env->ofs[0] = (int16_t)x; env->ofs[1] = (int16_t)y;
    env->tpage = 10; /* GetTPage(0, 0, 640, 0) */
    env->dtd = 1;
    env->dfe = 1;
    return a;
}
#endif

#ifndef PSX_GAME_HAS_SetDefDispEnv
psx_addr SetDefDispEnv(psx_addr a, int x, int y, int w, int h) {
    DISPENV *env = H(a);
    memset(env, 0, sizeof *env);
    env->disp.x = (int16_t)x; env->disp.y = (int16_t)y; env->disp.w = (int16_t)w; env->disp.h = (int16_t)h;
    return a;
}
#endif

/* the draw-mode words (what libgpu's get_* helpers build) */
#ifndef PSX_GAME_HAS_get_mode
uint32_t get_mode(int dfe, int dtd, int tpage) {
    return 0xE1000000u | (dtd ? 0x200u : 0) | (dfe ? 0x400u : 0) | ((uint32_t)tpage & 0x9FF);
}
#endif
#ifndef PSX_GAME_HAS_get_cs
uint32_t get_cs(int x, int y) {
    x = x < 0 ? 0 : x > 1023 ? 1023 : x;
    y = y < 0 ? 0 : y > 1023 ? 1023 : y;
    return 0xE3000000u | (uint32_t)(y & 0x3FF) << 10 | (uint32_t)(x & 0x3FF);
}
#endif
#ifndef PSX_GAME_HAS_get_ce
uint32_t get_ce(int x, int y) {
    x = x < 0 ? 0 : x > 1023 ? 1023 : x;
    y = y < 0 ? 0 : y > 1023 ? 1023 : y;
    return 0xE4000000u | (uint32_t)(y & 0x3FF) << 10 | (uint32_t)(x & 0x3FF);
}
#endif
#ifndef PSX_GAME_HAS_get_ofs
uint32_t get_ofs(int x, int y) { return 0xE5000000u | ((uint32_t)y & 0x7FF) << 11 | ((uint32_t)x & 0x7FF); }
#endif
#ifndef PSX_GAME_HAS_get_tw
uint32_t get_tw(psx_addr a) {
    const RECT *tw = H(a);
    uint32_t mx, my, ox, oy;
    if (!a) return 0;
    ox = (uint32_t)(uint8_t)tw->x >> 3;
    oy = (uint32_t)(uint8_t)tw->y >> 3;
    mx = (uint32_t)((-tw->w) & 0xFF) >> 3;
    my = (uint32_t)((-tw->h) & 0xFF) >> 3;
    return 0xE2000000u | oy << 15 | ox << 10 | my << 5 | mx;
}
#endif
#ifndef PSX_GAME_HAS_get_dx
int get_dx(psx_addr a) { return ((const DISPENV *)H(a))->disp.x; }
#endif

/* a DR_ENV: the drawing area, offset, mode, texture window and mask, and the background fill */
#ifndef PSX_GAME_HAS_SetDrawEnv
void SetDrawEnv(psx_addr dr, psx_addr a) {
    const DRAWENV *env = H(a);
    uint32_t code[16];
    int n = 0, i;
    code[n++] = get_cs(env->clip.x, env->clip.y);
    code[n++] = get_ce(env->clip.x + env->clip.w - 1, env->clip.y + env->clip.h - 1);
    code[n++] = get_ofs(env->ofs[0], env->ofs[1]);
    code[n++] = get_mode(env->dfe, env->dtd, env->tpage);
    code[n++] = get_tw(a + 12); /* &env->tw */
    code[n++] = 0xE6000000u;
    if (env->isbg) {
        int w = env->clip.w, h = env->clip.h;
        if (w > 1024) w = 1024;
        if (h > 512) h = 512;
        code[n++] = 0x02000000u | env->b0 << 16 | env->g0 << 8 | env->r0;
        code[n++] = (uint32_t)(uint16_t)env->clip.y << 16 | (uint16_t)env->clip.x;
        code[n++] = (uint32_t)(uint16_t)h << 16 | (uint16_t)w;
    }
    wr32(dr, 0x00FFFFFFu | (uint32_t)n << 24);
    for (i = 0; i < n; i++) wr32(dr + 4u + 4u * (uint32_t)i, code[i]);
}
#endif

#ifndef PSX_GAME_HAS_PutDrawEnv
psx_addr PutDrawEnv(psx_addr a) {
    static int logged;
    DRAWENV *env = H(a);
    psx_addr dr = a + 28; /* &env->dr_env */
    start();
    SetDrawEnv(dr, a);
    gpu_gp0((const uint32_t *)H(dr + 4), (int)(rd32(dr) >> 24));
    draw_env = *env;
    have_draw = 1;
    if (logged++ < 2)
        psx_log("gpu: PutDrawEnv clip %d,%d %dx%d, offset %d,%d, clear %s to %d,%d,%d\n", env->clip.x, env->clip.y,
                env->clip.w, env->clip.h, env->ofs[0], env->ofs[1], env->isbg ? "on" : "off", env->r0, env->g0,
                env->b0);
    return a;
}
#endif

#ifndef PSX_GAME_HAS_GetDrawEnv
psx_addr GetDrawEnv(psx_addr a) {
    *(DRAWENV *)H(a) = draw_env;
    return a;
}
#endif

#ifndef PSX_GAME_HAS_PutDispEnv
psx_addr PutDispEnv(psx_addr a) {
    static int logged;
    const DISPENV *env = H(a);
    int w = env->disp.w, h = env->disp.h;
    uint32_t mode = w >= 560 ? 3 : w >= 400 ? 2 : w >= 352 ? 0x40 : w >= 280 ? 1 : 0;
    start();
    if (h > 256) mode |= 0x24; /* 480 lines, interlaced */
    if (env->isinter) mode |= 0x20;
    if (env->isrgb24) mode |= 0x10;
    _ctl(0x05000000u | (uint32_t)(env->disp.y & 0x3FF) << 10 | (uint32_t)(env->disp.x & 0x3FF));
    _ctl(0x08000000u | mode);
    gpu_set_display(env->disp.x, env->disp.y, w, h, env->isrgb24);
    disp_env = *env;
    if (logged++ < 2)
        psx_log("gpu: PutDispEnv display %d,%d %dx%d%s\n", env->disp.x, env->disp.y, w, h,
                env->isrgb24 ? ", 24-bit" : "");
    return a;
}
#endif

#ifndef PSX_GAME_HAS_GetDispEnv
psx_addr GetDispEnv(psx_addr a) {
    *(DISPENV *)H(a) = disp_env;
    return a;
}
#endif

#ifndef PSX_GAME_HAS_DrawSync
int DrawSync(int mode) { (void)mode; return 0; }
#endif

#ifndef PSX_GAME_HAS_DrawSyncCallback
psx_addr DrawSyncCallback(psx_addr func) {
    psx_addr old = sync_callback;
    sync_callback = func;
    return old;
}
#endif

/* ---- ordering tables: a word each (next address in bits 0-23, length in 24-31), the end 0xFFFFFF */
#ifndef PSX_GAME_HAS_ClearOTag
psx_addr ClearOTag(psx_addr ot, int n) {
    int i;
    for (i = 0; i < n - 1; i++) wr32(ot + 4u * (uint32_t)i, (ot + 4u * (uint32_t)(i + 1)) & 0xFFFFFF);
    if (n > 0) wr32(ot + 4u * (uint32_t)(n - 1), 0x00FFFFFF);
    return ot;
}
#endif

#ifndef PSX_GAME_HAS_ClearOTagR
psx_addr ClearOTagR(psx_addr ot, int n) {
    _otc(ot, n);
    return ot;
}
#endif

static void set_addr(psx_addr p, uint32_t next) { wr32(p, (rd32(p) & 0xFF000000u) | (next & 0xFFFFFF)); }

#ifndef PSX_GAME_HAS_AddPrim
void AddPrim(psx_addr ot, psx_addr p) {
    set_addr(p, rd32(ot));
    set_addr(ot, p);
}
#endif

#ifndef PSX_GAME_HAS_AddPrims
void AddPrims(psx_addr ot, psx_addr p0, psx_addr p1) {
    set_addr(p1, rd32(ot));
    set_addr(ot, p0);
}
#endif

#ifndef PSX_GAME_HAS_CatPrim
void CatPrim(psx_addr p0, psx_addr p1) { set_addr(p0, p1); }
#endif

#ifndef PSX_GAME_HAS_TermPrim
void TermPrim(psx_addr p) { set_addr(p, 0xFFFFFF); }
#endif

#ifndef PSX_GAME_HAS_DrawOTag
void DrawOTag(psx_addr p) { draw_chain(p); }
#endif

#ifndef PSX_GAME_HAS_DrawOTagIO
void DrawOTagIO(psx_addr p) { draw_chain(p); }
#endif

#ifndef PSX_GAME_HAS_DrawOTagEnv
void DrawOTagEnv(psx_addr p, psx_addr env) {
    PutDrawEnv(env);
    draw_chain(p);
}
#endif

#ifndef PSX_GAME_HAS_DrawPrim
void DrawPrim(psx_addr p) {
    start();
    gpu_gp0((const uint32_t *)H(p + 4), (int)(rd32(p) >> 24));
}
#endif

/* ---- VRAM */
#ifndef PSX_GAME_HAS_LoadImage
int LoadImage(psx_addr rect, psx_addr p) { load(rect, p); return 0; }
#endif
#ifndef PSX_GAME_HAS_LoadImage2
int LoadImage2(psx_addr rect, psx_addr p) { load(rect, p); return 0; }
#endif
#ifndef PSX_GAME_HAS_StoreImage
int StoreImage(psx_addr rect, psx_addr p) { store(rect, p); return 0; }
#endif
#ifndef PSX_GAME_HAS_StoreImage2
int StoreImage2(psx_addr rect, psx_addr p) { store(rect, p); return 0; }
#endif

#ifndef PSX_GAME_HAS_MoveImage
int MoveImage(psx_addr rect, int x, int y) {
    const RECT *r = H(rect);
    start();
    gpu_move_image(r->x, r->y, x, y, r->w, r->h);
    return 0;
}
#endif

#ifndef PSX_GAME_HAS_ClearImage
int ClearImage(psx_addr rect, int r, int g, int b) {
    return _clr(rect, (uint32_t)(b & 0xFF) << 16 | (uint32_t)(g & 0xFF) << 8 | (uint32_t)(r & 0xFF));
}
#endif

#ifndef PSX_GAME_HAS_ClearImage2
int ClearImage2(psx_addr rect, int r, int g, int b) { return ClearImage(rect, r, g, b); }
#endif

#ifndef PSX_GAME_HAS_GetTPage
int GetTPage(int tp, int abr, int x, int y) {
    return ((tp & 3) << 7) | ((abr & 3) << 5) | ((y & 0x100) >> 4) | ((x & 0x3FF) >> 6) | ((y & 0x200) << 2);
}
#endif

#ifndef PSX_GAME_HAS_GetClut
int GetClut(int x, int y) { return (y << 6) | ((x >> 4) & 0x3F); }
#endif

#ifndef PSX_GAME_HAS_LoadTPage
int LoadTPage(psx_addr pix, int tp, int abr, int x, int y, int w, int h) {
    int vw = tp == 0 ? w / 4 : tp == 1 ? w / 2 : w;
    start();
    gpu_load_image(x, y, vw, h, (const uint16_t *)H(pix));
    return GetTPage(tp, abr, x, y);
}
#endif

#ifndef PSX_GAME_HAS_LoadClut
int LoadClut(psx_addr clut, int x, int y) {
    start();
    gpu_load_image(x, y, 256, 1, (const uint16_t *)H(clut));
    return GetClut(x, y);
}
#endif

#ifndef PSX_GAME_HAS_LoadClut2
int LoadClut2(psx_addr clut, int x, int y) {
    start();
    gpu_load_image(x, y, 16, 1, (const uint16_t *)H(clut));
    return GetClut(x, y);
}
#endif

/* ---- the drawing-environment primitives */
#ifndef PSX_GAME_HAS_SetDrawMode
void SetDrawMode(psx_addr p, int dfe, int dtd, int tpage, psx_addr tw) {
    set_len(p, 2);
    wr32(p + 4, get_mode(dfe, dtd, tpage));
    wr32(p + 8, get_tw(tw));
}
#endif

#ifndef PSX_GAME_HAS_SetDrawTPage
void SetDrawTPage(psx_addr p, int dfe, int dtd, int tpage) {
    set_len(p, 1);
    wr32(p + 4, get_mode(dfe, dtd, tpage));
}
#endif

#ifndef PSX_GAME_HAS_SetDrawArea
void SetDrawArea(psx_addr p, psx_addr rect) {
    const RECT *r = H(rect);
    set_len(p, 2);
    wr32(p + 4, get_cs(r->x, r->y));
    wr32(p + 8, get_ce(r->x + r->w - 1, r->y + r->h - 1));
}
#endif

#ifndef PSX_GAME_HAS_SetDrawOffset
void SetDrawOffset(psx_addr p, psx_addr ofs) {
    const uint16_t *o = H(ofs);
    set_len(p, 1);
    wr32(p + 4, get_ofs((int16_t)o[0], (int16_t)o[1]));
}
#endif

#ifndef PSX_GAME_HAS_SetDrawStp
void SetDrawStp(psx_addr p, int pbw) {
    set_len(p, 1);
    wr32(p + 4, 0xE6000000u | (pbw ? 1u : 0u));
}
#endif

#ifndef PSX_GAME_HAS_SetPriority
void SetPriority(psx_addr p, int pbc, int pbw) {
    set_len(p, 1);
    wr32(p + 4, 0xE6000000u | (pbc ? 2u : 0u) | (pbw ? 1u : 0u));
}
#endif

#ifndef PSX_GAME_HAS_SetTexWindow
void SetTexWindow(psx_addr p, psx_addr tw) {
    set_len(p, 2);
    wr32(p + 4, get_tw(tw));
    wr32(p + 8, 0);
}
#endif

#ifndef PSX_GAME_HAS_SetDrawMove
void SetDrawMove(psx_addr p, psx_addr rect, int x, int y) {
    const RECT *r = H(rect);
    set_len(p, (r->w && r->h) ? 5 : 0);
    wr32(p + 4, 0x01000000u);
    wr32(p + 8, 0x80000000u);
    wr32(p + 12, (uint32_t)(uint16_t)r->y << 16 | (uint16_t)r->x);
    wr32(p + 16, (uint32_t)(y & 0xFFFF) << 16 | (uint32_t)(x & 0xFFFF));
    wr32(p + 20, (uint32_t)(uint16_t)r->h << 16 | (uint16_t)r->w);
}
#endif

/* ---- primitive setters (the game's headers may have them as macros; these are for its calls) */
#define SETTER(name, len, code)                                                                                        \
    void name(psx_addr p) { set_len(p, len); set_code(p, code); }
#ifndef PSX_GAME_HAS_SetPolyF3
SETTER(SetPolyF3, 4, 0x20)
#endif
#ifndef PSX_GAME_HAS_SetPolyFT3
SETTER(SetPolyFT3, 7, 0x24)
#endif
#ifndef PSX_GAME_HAS_SetPolyG3
SETTER(SetPolyG3, 6, 0x30)
#endif
#ifndef PSX_GAME_HAS_SetPolyGT3
SETTER(SetPolyGT3, 9, 0x34)
#endif
#ifndef PSX_GAME_HAS_SetPolyF4
SETTER(SetPolyF4, 5, 0x28)
#endif
#ifndef PSX_GAME_HAS_SetPolyFT4
SETTER(SetPolyFT4, 9, 0x2C)
#endif
#ifndef PSX_GAME_HAS_SetPolyG4
SETTER(SetPolyG4, 8, 0x38)
#endif
#ifndef PSX_GAME_HAS_SetPolyGT4
SETTER(SetPolyGT4, 12, 0x3C)
#endif
#ifndef PSX_GAME_HAS_SetSprt
SETTER(SetSprt, 4, 0x64)
#endif
#ifndef PSX_GAME_HAS_SetSprt8
SETTER(SetSprt8, 3, 0x74)
#endif
#ifndef PSX_GAME_HAS_SetSprt16
SETTER(SetSprt16, 3, 0x7C)
#endif
#ifndef PSX_GAME_HAS_SetTile
SETTER(SetTile, 3, 0x60)
#endif
#ifndef PSX_GAME_HAS_SetTile1
SETTER(SetTile1, 2, 0x68)
#endif
#ifndef PSX_GAME_HAS_SetTile8
SETTER(SetTile8, 2, 0x70)
#endif
#ifndef PSX_GAME_HAS_SetTile16
SETTER(SetTile16, 2, 0x78)
#endif
#ifndef PSX_GAME_HAS_SetLineF2
SETTER(SetLineF2, 3, 0x40)
#endif
#ifndef PSX_GAME_HAS_SetLineG2
SETTER(SetLineG2, 4, 0x50)
#endif
/* polylines end in a terminator word */
#ifndef PSX_GAME_HAS_SetLineF3
void SetLineF3(psx_addr p) { set_len(p, 5); set_code(p, 0x48); wr32(p + 20, 0x55555555u); }
#endif
#ifndef PSX_GAME_HAS_SetLineG3
void SetLineG3(psx_addr p) { set_len(p, 7); set_code(p, 0x58); wr32(p + 28, 0x55555555u); }
#endif
#ifndef PSX_GAME_HAS_SetLineF4
void SetLineF4(psx_addr p) { set_len(p, 6); set_code(p, 0x4C); wr32(p + 24, 0x55555555u); }
#endif
#ifndef PSX_GAME_HAS_SetLineG4
void SetLineG4(psx_addr p) { set_len(p, 9); set_code(p, 0x5C); wr32(p + 36, 0x55555555u); }
#endif

#ifndef PSX_GAME_HAS_SetSemiTrans
void SetSemiTrans(psx_addr p, int abe) {
    uint8_t *c = (uint8_t *)H(p) + 7;
    *c = (uint8_t)(abe ? (*c | 2) : (*c & ~2));
}
#endif

#ifndef PSX_GAME_HAS_SetShadeTex
void SetShadeTex(psx_addr p, int tge) {
    uint8_t *c = (uint8_t *)H(p) + 7;
    *c = (uint8_t)(tge ? (*c | 1) : (*c & ~1));
}
#endif

/* ---- PSX_GPU_SELFTEST=1: before the game's main, a frame drawn through libgpu as a game would (environments,
 * an ordering table and primitives in PS1 RAM, a texture and CLUT loaded), shown for PSX_GPU_SELFTEST frames */
static psx_addr put_words(psx_addr at, const uint32_t *w, int n) {
    int i;
    for (i = 0; i < n; i++) wr32(at + 4u * (uint32_t)i, w[i]);
    return at;
}

void psx_gpu_selftest(int frames) {
    const psx_addr base = 0x80010000u; /* overlay space, unused before the game runs */
    psx_addr draw = base, disp = base + 0x100, ot = base + 0x200, prims = base + 0x400, tex = base + 0x1000;
    DRAWENV *de;
    uint16_t *t;
    int i, x, y, clut, tpage;
    ResetGraph(0);
    SetDefDrawEnv(draw, 0, 0, 320, 240);
    SetDefDispEnv(disp, 0, 0, 320, 240);
    de = H(draw);
    de->isbg = 1; de->r0 = 16; de->g0 = 24; de->b0 = 64;
    /* a 4-bit 16x16 checker with a 16-colour ramp CLUT */
    t = H(tex);
    for (i = 0; i < 16; i++) t[i] = (uint16_t)((i * 2) | (31 - i * 2) << 5 | 16 << 10);
    t[0] = 0; /* transparent */
    clut = LoadClut2(tex, 0, 480);
    for (y = 0; y < 16; y++)
        for (x = 0; x < 4; x++) {
            uint16_t w = 0;
            int k;
            for (k = 0; k < 4; k++) w |= (uint16_t)((((x * 4 + k) ^ y) & 15) << (k * 4));
            t[64 + y * 4 + x] = w;
        }
    tpage = LoadTPage(tex + 128, 0, 0, 640, 0, 16, 16);
    for (i = 0; i < frames; i++) {
        psx_addr p = prims;
        int s = i % 64;
        ClearOTagR(ot, 8);
        { /* gouraud triangle */
            uint32_t w[] = {0x06FFFFFFu, 0x300000FFu, 40u | 200u << 16, 0x0000FF00u, 160u | 30u << 16, 0x00FF0000u,
                            280u | 200u << 16};
            AddPrim(ot + 4 * 6, put_words(p, w, 7)); p += 32;
        }
        { /* semi-transparent flat quad (mode 1: additive) moving across */
            uint32_t w[] = {0x06FFFFFFu, 0xE1000020u, 0x2A404040u, (uint32_t)(20 + s * 3) | 60u << 16,
                            (uint32_t)(120 + s * 3) | 60u << 16, (uint32_t)(20 + s * 3) | 160u << 16,
                            (uint32_t)(120 + s * 3) | 160u << 16};
            AddPrim(ot + 4 * 4, put_words(p, w, 7)); p += 32;
        }
        { /* textured sprite, 4-bit CLUT, drawn 4 times */
            int k;
            for (k = 0; k < 4; k++) {
                uint32_t w[] = {0x04FFFFFFu, 0xE1000000u | (uint32_t)tpage, 0x7C808080u,
                                (uint32_t)(200 + k * 20) | (uint32_t)(120 + k * 10) << 16, (uint32_t)clut << 16};
                AddPrim(ot + 4 * 2, put_words(p, w, 5)); p += 20;
            }
        }
        { /* a white polyline frame */
            uint32_t w[] = {0x06FFFFFFu, 0x48FFFFFFu, 4u | 4u << 16, 315u | 4u << 16, 315u | 235u << 16,
                            4u | 235u << 16, 0x55555555u};
            AddPrim(ot + 4 * 1, put_words(p, w, 7)); p += 28;
        }
        DrawSync(0);
        VSync(0);
        PutDrawEnv(draw);
        PutDispEnv(disp);
        SetDispMask(1);
        DrawOTag(ot + 4 * 7);
    }
    psx_log("gpu: self-test drew %d frames\n", frames);
}

/* ---- the frame loop's side */
void psx_gpu_present(void) {
    GpuDisplay d;
    start();
    gpu_get_display(&d);
    publish_stat();
    if (!d.enabled) host_present(NULL, 0, 0, 0, 0, 0); /* display off: black */
    else host_present(gpu_vram, d.x, d.y, d.w, d.h, d.rgb24);
}

void psx_gpu_exit(void) {
    const char *path = getenv("PSX_PORT_DUMP_VRAM");
    GpuDisplay d;
    gpu_get_display(&d);
    psx_log("gpu: %lu packets, %lu primitives, %lu pixels, %lu fills, %lu loads; display %s at %d,%d %dx%d\n",
            gpu_stats.packets, gpu_stats.primitives, gpu_stats.pixels, gpu_stats.fills, gpu_stats.loads,
            d.enabled ? "on" : "off", d.x, d.y, d.w, d.h);
    if (path && *path) {
        if (gpu_dump_vram_png(path) == 0) psx_log("gpu: VRAM written to %s\n", path);
        else psx_log("gpu: couldn't write %s\n", path);
    }
}
