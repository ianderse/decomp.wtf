/* A software PS1 GPU, written from psx-spx's GPU chapter: VRAM, the GP0 primitives (polygons flat/gouraud/textured
 * with 4/8/15-bit textures, rectangles, lines and polylines, the fill, VRAM transfers), the draw-mode state (texture
 * page, texture window, drawing area and offset, mask bits, the four semi-transparency modes) and the GP1 display
 * controls. Not done: dithering, interlaced fields, the "draw to display area" bit, texture cache effects. */
#include "gpu.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

uint16_t gpu_vram[GPU_VRAM_W * GPU_VRAM_H];
GpuStats gpu_stats;

static struct {
    /* E1h: texture page (x base in 64s, y base 0/256, semi-transparency mode, colour depth), texture disable */
    int tpx, tpy, abr, tpmode, tex_disable, dither;
    /* E2h: texture window */
    int tw_mx, tw_my, tw_ox, tw_oy;
    /* E3h/E4h/E5h: drawing area (inclusive) and offset */
    int x1, y1, x2, y2, ofx, ofy;
    /* E6h */
    int mask_set, mask_check;
    /* display */
    GpuDisplay disp;
    int hres, vres_lines;
    uint32_t ctl[64];
} g;

#define VRAM(x, y) gpu_vram[((y) & (GPU_VRAM_H - 1)) * GPU_VRAM_W + ((x) & (GPU_VRAM_W - 1))]

static int sext11(uint32_t v) { return (int)((v & 0x7FF) ^ 0x400) - 0x400; }

static void set_texpage(uint32_t w) {
    g.tpx = (int)(w & 0xF) * 64;
    g.tpy = (int)((w >> 4) & 1) * 256;
    g.abr = (int)(w >> 5) & 3;
    g.tpmode = (int)(w >> 7) & 3;
    g.dither = (int)(w >> 9) & 1;
    g.tex_disable = (int)(w >> 11) & 1;
}

void gpu_reset(void) {
    memset(&g, 0, sizeof g);
    g.x2 = GPU_VRAM_W - 1;
    g.y2 = GPU_VRAM_H - 1;
    g.disp.w = 320;
    g.disp.h = 240;
    g.hres = 320;
}

/* ---- pixels */
static inline uint16_t blend(uint16_t b, uint16_t f, int abr) {
    int br = b & 31, bg = (b >> 5) & 31, bb = (b >> 10) & 31;
    int fr = f & 31, fg = (f >> 5) & 31, fb = (f >> 10) & 31;
    int r, gg, bl;
    switch (abr) {
    case 0: r = (br + fr) >> 1; gg = (bg + fg) >> 1; bl = (bb + fb) >> 1; break;
    case 1: r = br + fr; gg = bg + fg; bl = bb + fb; break;
    case 2: r = br - fr; gg = bg - fg; bl = bb - fb; break;
    default: r = br + (fr >> 2); gg = bg + (fg >> 2); bl = bb + (fb >> 2); break;
    }
    r = r < 0 ? 0 : r > 31 ? 31 : r;
    gg = gg < 0 ? 0 : gg > 31 ? 31 : gg;
    bl = bl < 0 ? 0 : bl > 31 ? 31 : bl;
    return (uint16_t)(r | gg << 5 | bl << 10);
}

/* c: 15-bit colour, bit 15 the texel's STP bit (textured) or 0; semi: the primitive is semi-transparent */
static inline void plot(int x, int y, uint16_t c, int semi, int abr) {
    uint16_t *d;
    if (x < g.x1 || x > g.x2 || y < g.y1 || y > g.y2) return;
    d = &VRAM(x, y);
    if (g.mask_check && (*d & 0x8000)) return;
    if (semi) c = (uint16_t)(blend(*d, c & 0x7FFF, abr) | (c & 0x8000));
    if (g.mask_set) c |= 0x8000;
    *d = c;
    gpu_stats.pixels++;
}

static inline uint16_t rgb15(uint32_t rgb) {
    return (uint16_t)(((rgb >> 3) & 31) | ((rgb >> 11) & 31) << 5 | ((rgb >> 19) & 31) << 10);
}

typedef struct { int tpx, tpy, mode, cx, cy; } Tex;

static inline uint16_t texel(const Tex *t, int u, int v) {
    u = (u & ~(g.tw_mx * 8)) | ((g.tw_ox & g.tw_mx) * 8);
    v = (v & ~(g.tw_my * 8)) | ((g.tw_oy & g.tw_my) * 8);
    u &= 0xFF; v &= 0xFF;
    switch (t->mode) {
    case 0: {
        uint16_t w = VRAM(t->tpx + (u >> 2), t->tpy + v);
        return VRAM(t->cx + ((w >> ((u & 3) * 4)) & 0xF), t->cy);
    }
    case 1: {
        uint16_t w = VRAM(t->tpx + (u >> 1), t->tpy + v);
        return VRAM(t->cx + ((w >> ((u & 1) * 8)) & 0xFF), t->cy);
    }
    default: return VRAM(t->tpx + u, t->tpy + v);
    }
}

/* a texel modulated by a colour (128 = as is), or raw */
static inline uint16_t shade(uint16_t tx, int r, int gg, int b, int raw) {
    int tr, tg, tb;
    if (raw) return tx;
    tr = ((tx & 31) * r) >> 7; tg = (((tx >> 5) & 31) * gg) >> 7; tb = (((tx >> 10) & 31) * b) >> 7;
    if (tr > 31) tr = 31;
    if (tg > 31) tg = 31;
    if (tb > 31) tb = 31;
    return (uint16_t)(tr | tg << 5 | tb << 10 | (tx & 0x8000));
}

/* ---- triangles: edge functions over the bounding box, the top-left rule (right and bottom edges not drawn) */
typedef struct { int x, y, r, g, b, u, v; } Vtx;

static inline long long edge(const Vtx *a, const Vtx *b, int px, int py) {
    return (long long)(b->x - a->x) * (py - a->y) - (long long)(b->y - a->y) * (px - a->x);
}
static inline int top_left(const Vtx *a, const Vtx *b) {
    int dx = b->x - a->x, dy = b->y - a->y;
    return dy < 0 || (dy == 0 && dx > 0);
}

static void triangle(Vtx v0, Vtx v1, Vtx v2, int gouraud, const Tex *tex, int raw, int semi, int abr) {
    long long area = edge(&v0, &v1, v2.x, v2.y);
    int minx, maxx, miny, maxy, x, y, b0, b1, b2;
    if (area == 0) return;
    if (area < 0) { Vtx t = v1; v1 = v2; v2 = t; area = -area; }
    minx = v0.x < v1.x ? v0.x : v1.x; if (v2.x < minx) minx = v2.x;
    maxx = v0.x > v1.x ? v0.x : v1.x; if (v2.x > maxx) maxx = v2.x;
    miny = v0.y < v1.y ? v0.y : v1.y; if (v2.y < miny) miny = v2.y;
    maxy = v0.y > v1.y ? v0.y : v1.y; if (v2.y > maxy) maxy = v2.y;
    if (maxx - minx >= 1024 || maxy - miny >= 512) return; /* the GPU skips polygons this large */
    if (minx < g.x1) minx = g.x1;
    if (miny < g.y1) miny = g.y1;
    if (maxx > g.x2) maxx = g.x2;
    if (maxy > g.y2) maxy = g.y2;
    b0 = top_left(&v1, &v2) ? 0 : 1; /* a pixel exactly on an edge counts only for top and left edges */
    b1 = top_left(&v2, &v0) ? 0 : 1;
    b2 = top_left(&v0, &v1) ? 0 : 1;
    gpu_stats.primitives++;
    for (y = miny; y <= maxy; y++) {
        for (x = minx; x <= maxx; x++) {
            long long w0 = edge(&v1, &v2, x, y), w1 = edge(&v2, &v0, x, y), w2 = edge(&v0, &v1, x, y);
            int r = v0.r, gg = v0.g, b = v0.b;
            if (w0 < b0 || w1 < b1 || w2 < b2) continue;
            if (gouraud) {
                r = (int)((w0 * v0.r + w1 * v1.r + w2 * v2.r) / area);
                gg = (int)((w0 * v0.g + w1 * v1.g + w2 * v2.g) / area);
                b = (int)((w0 * v0.b + w1 * v1.b + w2 * v2.b) / area);
            }
            if (tex) {
                int u = (int)((w0 * v0.u + w1 * v1.u + w2 * v2.u + area / 2) / area);
                int v = (int)((w0 * v0.v + w1 * v1.v + w2 * v2.v + area / 2) / area);
                uint16_t t = texel(tex, u, v);
                if (!t) continue; /* 0000h is transparent */
                plot(x, y, shade(t, r, gg, b, raw), semi && (t & 0x8000), abr);
            } else {
                plot(x, y, rgb15((uint32_t)r | (uint32_t)gg << 8 | (uint32_t)b << 16), semi, abr);
            }
        }
    }
}

static void polygon(const uint32_t *w) {
    uint32_t cmd = w[0] >> 24;
    int gouraud = (cmd & 0x10) != 0, quad = (cmd & 0x08) != 0, textured = (cmd & 0x04) != 0;
    int semi = (cmd & 0x02) != 0, raw = (cmd & 0x01) != 0, n = quad ? 4 : 3, i, k = 0, abr = g.abr;
    Vtx v[4];
    Tex t, *tp = 0;
    for (i = 0; i < n; i++) {
        uint32_t c;
        if (gouraud || i == 0) c = w[k++]; /* the first colour shares the command word */
        else c = w[0];
        v[i].r = (int)(c & 0xFF); v[i].g = (int)((c >> 8) & 0xFF); v[i].b = (int)((c >> 16) & 0xFF);
        v[i].x = sext11(w[k]) + g.ofx;
        v[i].y = sext11(w[k] >> 16) + g.ofy;
        k++;
        v[i].u = v[i].v = 0;
        if (textured) {
            v[i].u = (int)(w[k] & 0xFF);
            v[i].v = (int)((w[k] >> 8) & 0xFF);
            if (i == 0) { t.cx = (int)((w[k] >> 16) & 0x3F) * 16; t.cy = (int)((w[k] >> 22) & 0x1FF); }
            if (i == 1) { /* the polygon's own texture page becomes the current one */
                uint32_t page = w[k] >> 16;
                set_texpage((page & 0x9FF) | ((uint32_t)g.dither << 9));
                abr = g.abr;
            }
            k++;
        }
    }
    if (textured && !g.tex_disable) {
        t.tpx = g.tpx; t.tpy = g.tpy; t.mode = g.tpmode;
        tp = &t;
    }
    /* flat-shaded textured polygons are modulated by the one colour; untextured gouraud interpolates */
    triangle(v[0], v[1], v[2], gouraud, tp, raw, semi, abr);
    if (quad) triangle(v[1], v[2], v[3], gouraud, tp, raw, semi, abr);
}

/* ---- rectangles: TILE / SPRT, variable size or 1x1, 8x8, 16x16 */
static void rectangle(const uint32_t *w) {
    uint32_t cmd = w[0] >> 24;
    int textured = (cmd & 0x04) != 0, semi = (cmd & 0x02) != 0, raw = (cmd & 0x01) != 0, size = (int)(cmd >> 3) & 3;
    int x0 = sext11(w[1]) + g.ofx, y0 = sext11(w[1] >> 16) + g.ofy, k = 2, wd, ht, x, y, u0 = 0, v0 = 0;
    int r = (int)(w[0] & 0xFF), gg = (int)((w[0] >> 8) & 0xFF), b = (int)((w[0] >> 16) & 0xFF);
    uint16_t flat = rgb15(w[0]);
    Tex t;
    if (textured) {
        u0 = (int)(w[k] & 0xFF); v0 = (int)((w[k] >> 8) & 0xFF);
        t.cx = (int)((w[k] >> 16) & 0x3F) * 16; t.cy = (int)((w[k] >> 22) & 0x1FF);
        t.tpx = g.tpx; t.tpy = g.tpy; t.mode = g.tpmode;
        k++;
    }
    switch (size) {
    case 0: wd = (int)(w[k] & 0x3FF); ht = (int)((w[k] >> 16) & 0x1FF); break;
    case 1: wd = ht = 1; break;
    case 2: wd = ht = 8; break;
    default: wd = ht = 16; break;
    }
    gpu_stats.primitives++;
    for (y = 0; y < ht; y++) {
        int py = y0 + y;
        if (py < g.y1 || py > g.y2) continue;
        for (x = 0; x < wd; x++) {
            int px = x0 + x;
            if (px < g.x1 || px > g.x2) continue;
            if (textured && !g.tex_disable) {
                uint16_t tx = texel(&t, u0 + x, v0 + y);
                if (!tx) continue;
                plot(px, py, shade(tx, r, gg, b, raw), semi && (tx & 0x8000), g.abr);
            } else {
                plot(px, py, flat, semi, g.abr);
            }
        }
    }
}

/* ---- lines */
static void line(int x0, int y0, uint32_t c0, int x1, int y1, uint32_t c1, int gouraud, int semi) {
    int dx = abs(x1 - x0), dy = abs(y1 - y0), sx = x0 < x1 ? 1 : -1, sy = y0 < y1 ? 1 : -1, err, steps, i;
    if (dx >= 1024 || dy >= 512) return;
    steps = dx > dy ? dx : dy;
    err = dx - dy;
    gpu_stats.primitives++;
    for (i = 0;; i++) {
        uint32_t c = c0;
        if (gouraud && steps) {
            int r = (int)(c0 & 0xFF) + ((int)(c1 & 0xFF) - (int)(c0 & 0xFF)) * i / steps;
            int gg = (int)((c0 >> 8) & 0xFF) + ((int)((c1 >> 8) & 0xFF) - (int)((c0 >> 8) & 0xFF)) * i / steps;
            int b = (int)((c0 >> 16) & 0xFF) + ((int)((c1 >> 16) & 0xFF) - (int)((c0 >> 16) & 0xFF)) * i / steps;
            c = (uint32_t)r | (uint32_t)gg << 8 | (uint32_t)b << 16;
        }
        plot(x0, y0, rgb15(c), semi, g.abr);
        if (x0 == x1 && y0 == y1) break;
        {
            int e2 = 2 * err;
            if (e2 > -dy) { err -= dy; x0 += sx; }
            if (e2 < dx) { err += dx; y0 += sy; }
        }
    }
}

static int is_term(uint32_t w) { return (w & 0xF000F000u) == 0x50005000u; }

/* words a line command takes (polylines: up to and including the terminator, which follows two vertices at least;
 * a gouraud polyline's comes where a colour would) */
static int line_size(const uint32_t *w, int n) {
    uint32_t cmd = w[0] >> 24;
    int gouraud = (cmd & 0x10) != 0, i;
    if (!(cmd & 0x08)) return gouraud ? 4 : 3;
    for (i = gouraud ? 4 : 3; i < n; i += gouraud ? 2 : 1)
        if (is_term(w[i])) return i + 1;
    return n;
}

static void lines(const uint32_t *w, int n) {
    uint32_t cmd = w[0] >> 24;
    int gouraud = (cmd & 0x10) != 0, semi = (cmd & 0x02) != 0, poly = (cmd & 0x08) != 0, k = 1;
    uint32_t c0 = w[0] & 0xFFFFFF, c1;
    int x0 = sext11(w[k]) + g.ofx, y0 = sext11(w[k] >> 16) + g.ofy;
    k++;
    for (;;) {
        int x1, y1;
        if (k >= n) break;
        if (gouraud) {
            if (poly && is_term(w[k])) break;
            c1 = w[k++] & 0xFFFFFF;
        } else {
            c1 = c0;
        }
        if (k >= n || (poly && is_term(w[k]))) break;
        x1 = sext11(w[k]) + g.ofx; y1 = sext11(w[k] >> 16) + g.ofy;
        k++;
        line(x0, y0, c0, x1, y1, c1, gouraud, semi);
        x0 = x1; y0 = y1; c0 = c1;
        if (!poly) break;
    }
}

/* ---- VRAM transfers */
void gpu_fill(int x, int y, int w, int h, uint32_t rgb24) {
    uint16_t c = rgb15(rgb24);
    int i, j;
    x &= 0x3F0;
    y &= 0x1FF;
    w = ((w & 0x3FF) + 15) & ~15;
    h &= 0x1FF;
    gpu_stats.fills++;
    for (j = 0; j < h; j++)
        for (i = 0; i < w; i++) VRAM(x + i, y + j) = c;
}

void gpu_load_image(int x, int y, int w, int h, const uint16_t *src) {
    int i, j;
    gpu_stats.loads++;
    for (j = 0; j < h; j++)
        for (i = 0; i < w; i++) VRAM(x + i, y + j) = (uint16_t)(*src++ | (g.mask_set ? 0x8000 : 0));
}

void gpu_store_image(int x, int y, int w, int h, uint16_t *dst) {
    int i, j;
    for (j = 0; j < h; j++)
        for (i = 0; i < w; i++) *dst++ = VRAM(x + i, y + j);
}

void gpu_move_image(int sx, int sy, int dx, int dy, int w, int h) {
    int i, j;
    uint16_t *tmp;
    if (w <= 0 || h <= 0) return;
    tmp = (uint16_t *)malloc((size_t)w * (size_t)h * 2);
    if (!tmp) return;
    gpu_store_image(sx, sy, w, h, tmp);
    for (j = 0; j < h; j++)
        for (i = 0; i < w; i++) VRAM(dx + i, dy + j) = tmp[j * w + i];
    free(tmp);
}

static void xfer_size(uint32_t wh, int *w, int *h) {
    *w = (int)(((wh & 0xFFFF) - 1) & 0x3FF) + 1;
    *h = (int)((((wh >> 16) & 0xFFFF) - 1) & 0x1FF) + 1;
}

/* ---- GP0 */
static int command_size(const uint32_t *w, int n) {
    uint32_t cmd = w[0] >> 24;
    switch (cmd >> 5) {
    case 1: { /* polygon */
        int nv = (cmd & 0x08) ? 4 : 3, tex = (cmd & 0x04) ? 1 : 0, gour = (cmd & 0x10) ? 1 : 0;
        return 1 + nv * (1 + tex) + gour * (nv - 1);
    }
    case 2: return line_size(w, n);
    case 3: return 2 + ((cmd & 0x04) ? 1 : 0) + (((cmd >> 3) & 3) == 0 ? 1 : 0);
    case 4: return 4;
    case 5: {
        int ww, hh;
        if (n < 3) return 3;
        xfer_size(w[2], &ww, &hh);
        return 3 + (ww * hh + 1) / 2;
    }
    case 6: return 3;
    default: return cmd == 0x02 ? 3 : 1;
    }
}

static void env_command(uint32_t w) {
    switch (w >> 24) {
    case 0xE1: set_texpage(w); break;
    case 0xE2:
        g.tw_mx = (int)(w & 0x1F); g.tw_my = (int)((w >> 5) & 0x1F);
        g.tw_ox = (int)((w >> 10) & 0x1F); g.tw_oy = (int)((w >> 15) & 0x1F);
        break;
    case 0xE3: g.x1 = (int)(w & 0x3FF); g.y1 = (int)((w >> 10) & 0x1FF); break;
    case 0xE4: g.x2 = (int)(w & 0x3FF); g.y2 = (int)((w >> 10) & 0x1FF); break;
    case 0xE5: g.ofx = sext11(w); g.ofy = sext11(w >> 11); break;
    case 0xE6: g.mask_set = (int)(w & 1); g.mask_check = (int)((w >> 1) & 1); break;
    default: break;
    }
}

int gpu_gp0(const uint32_t *w, int n) {
    int i = 0;
    while (i < n) {
        uint32_t cmd = w[i] >> 24;
        int size = command_size(w + i, n - i);
        int have = n - i;
        if (cmd >= 0xA0 && cmd < 0xC0) { /* CPU to VRAM: the data follows (DR_LOAD); a short packet loads what it has */
            int ww, hh, k, px;
            if (have < 3) break;
            xfer_size(w[i + 2], &ww, &hh);
            {
                int x = (int)(w[i + 1] & 0x3FF), y = (int)((w[i + 1] >> 16) & 0x1FF);
                int words = have - 3 < size - 3 ? have - 3 : size - 3;
                gpu_stats.loads++;
                for (k = 0, px = 0; k < words; k++) {
                    uint32_t d = w[i + 3 + k];
                    if (px < ww * hh) { VRAM(x + px % ww, y + px / ww) = (uint16_t)d; px++; }
                    if (px < ww * hh) { VRAM(x + px % ww, y + px / ww) = (uint16_t)(d >> 16); px++; }
                }
            }
            i += size < have ? size : have;
            continue;
        }
        if (size > have) break; /* a primitive cut short: nothing to draw */
        switch (cmd >> 5) {
        case 1: polygon(w + i); break;
        case 2: lines(w + i, size); break;
        case 3: rectangle(w + i); break;
        case 4: { /* VRAM to VRAM */
            int ww, hh;
            xfer_size(w[i + 3], &ww, &hh);
            gpu_move_image((int)(w[i + 1] & 0x3FF), (int)((w[i + 1] >> 16) & 0x1FF), (int)(w[i + 2] & 0x3FF),
                           (int)((w[i + 2] >> 16) & 0x1FF), ww, hh);
            break;
        }
        case 6: break; /* VRAM to CPU: StoreImage reads VRAM directly */
        case 7: env_command(w[i]); break;
        default:
            if (cmd == 0x02)
                gpu_fill((int)(w[i + 1] & 0xFFFF), (int)(w[i + 1] >> 16), (int)(w[i + 2] & 0xFFFF),
                         (int)(w[i + 2] >> 16), w[i] & 0xFFFFFF);
            break;
        }
        i += size;
    }
    return i;
}

long gpu_draw_chain(const uint8_t *ram, uint32_t addr) {
    long nodes = 0;
    addr &= 0x1FFFFC;
    while (nodes < 1000000) {
        uint32_t tag;
        int len;
        memcpy(&tag, ram + addr, 4);
        len = (int)(tag >> 24);
        nodes++;
        if (len) {
            uint32_t buf[256];
            int k;
            for (k = 0; k < len; k++) memcpy(&buf[k], ram + ((addr + 4 + 4 * (uint32_t)k) & 0x1FFFFC), 4);
            gpu_stats.packets++;
            gpu_gp0(buf, len);
        }
        if ((tag & 0xFFFFFF) == 0xFFFFFF) break;
        addr = tag & 0x1FFFFC;
    }
    return nodes;
}

/* ---- GP1 and the display */
static void apply_mode(uint32_t w) {
    static const int widths[4] = {256, 320, 512, 640};
    g.hres = (w & 0x40) ? 368 : widths[w & 3];
    g.disp.w = g.hres;
    g.disp.h = ((w & 0x04) && (w & 0x20)) ? 480 : 240;
    g.disp.rgb24 = (int)((w >> 4) & 1);
}

void gpu_gp1(uint32_t w) {
    uint32_t cmd = (w >> 24) & 0x3F;
    g.ctl[cmd] = w;
    switch (cmd) {
    case 0x00: {
        GpuDisplay keep = g.disp;
        gpu_reset();
        g.disp.x = keep.x; g.disp.y = keep.y;
        g.ctl[0] = w;
        break;
    }
    case 0x03: g.disp.enabled = !(w & 1); break;
    case 0x05: g.disp.x = (int)(w & 0x3FE); g.disp.y = (int)((w >> 10) & 0x1FF); break;
    case 0x08: apply_mode(w); break;
    default: break;
    }
}

uint32_t gpu_ctl(int cmd) { return g.ctl[cmd & 0x3F]; }

uint32_t gpu_read_stat(void) {
    /* ready for commands, for VRAM-to-CPU and for DMA; the texture page and the display bits */
    uint32_t s = 0x1C000000u;
    s |= (uint32_t)(g.tpx / 64) | (uint32_t)(g.tpy / 256) << 4 | (uint32_t)g.abr << 5 | (uint32_t)g.tpmode << 7;
    s |= (uint32_t)g.dither << 9 | (uint32_t)g.tex_disable << 15;
    s |= (uint32_t)g.mask_set << 11 | (uint32_t)g.mask_check << 12;
    s |= (uint32_t)g.disp.rgb24 << 21 | (uint32_t)(!g.disp.enabled) << 23;
    return s;
}

uint32_t gpu_info(uint32_t which) {
    switch (which & 7) {
    case 2: return (uint32_t)g.tw_mx | (uint32_t)g.tw_my << 5 | (uint32_t)g.tw_ox << 10 | (uint32_t)g.tw_oy << 15;
    case 3: return (uint32_t)g.x1 | (uint32_t)g.y1 << 10;
    case 4: return (uint32_t)g.x2 | (uint32_t)g.y2 << 10;
    case 5: return ((uint32_t)g.ofx & 0x7FF) | ((uint32_t)g.ofy & 0x7FF) << 11;
    case 7: return 2; /* GPU version */
    default: return 0;
    }
}

void gpu_get_display(GpuDisplay *d) { *d = g.disp; }

void gpu_set_display(int x, int y, int w, int h, int rgb24) {
    g.disp.x = x & 0x3FF;
    g.disp.y = y & 0x1FF;
    if (w > 0) g.disp.w = w > 1024 ? 1024 : w;
    if (h > 0) g.disp.h = h > 512 ? 512 : h;
    g.disp.rgb24 = rgb24;
}

static inline uint32_t to_rgba(uint16_t c) {
    uint32_t r = c & 31, gg = (c >> 5) & 31, b = (c >> 10) & 31;
    r = r << 3 | r >> 2; gg = gg << 3 | gg >> 2; b = b << 3 | b >> 2;
    return 0xFF000000u | r << 16 | gg << 8 | b;
}

void gpu_display_rgba(const uint16_t *vram, int x, int y, int w, int h, int rgb24, uint32_t *out) {
    int i, j;
    for (j = 0; j < h; j++) {
        const uint16_t *row = vram + ((y + j) & (GPU_VRAM_H - 1)) * GPU_VRAM_W;
        if (!rgb24) {
            for (i = 0; i < w; i++) out[j * w + i] = to_rgba(row[(x + i) & (GPU_VRAM_W - 1)]);
        } else { /* 24-bit: three bytes a pixel, packed across the halfwords */
            for (i = 0; i < w; i++) {
                int byte = i * 3, hw = x + byte / 2;
                uint8_t b[4];
                int k;
                for (k = 0; k < 2; k++) {
                    uint16_t v = row[(hw + k) & (GPU_VRAM_W - 1)];
                    b[k * 2] = (uint8_t)v; b[k * 2 + 1] = (uint8_t)(v >> 8);
                }
                {
                    int o = byte & 1;
                    out[j * w + i] = 0xFF000000u | (uint32_t)b[o] << 16 | (uint32_t)b[o + 1] << 8 | b[o + 2];
                }
            }
        }
    }
}

/* ---- the VRAM as a PNG (stored deflate blocks: no zlib needed) */
static uint32_t crc_table[256];
static uint32_t crc(uint32_t c, const uint8_t *p, size_t n) {
    size_t i;
    if (!crc_table[1]) {
        uint32_t k, j, v;
        for (k = 0; k < 256; k++) {
            for (v = k, j = 0; j < 8; j++) v = (v & 1) ? 0xEDB88320u ^ (v >> 1) : v >> 1;
            crc_table[k] = v;
        }
    }
    c = ~c;
    for (i = 0; i < n; i++) c = crc_table[(c ^ p[i]) & 0xFF] ^ (c >> 8);
    return ~c;
}
static void be32(uint8_t *p, uint32_t v) { p[0] = (uint8_t)(v >> 24); p[1] = (uint8_t)(v >> 16); p[2] = (uint8_t)(v >> 8); p[3] = (uint8_t)v; }
static void chunk(FILE *f, const char *type, const uint8_t *data, uint32_t n) {
    uint8_t h[8];
    uint32_t c;
    be32(h, n);
    memcpy(h + 4, type, 4);
    fwrite(h, 1, 8, f);
    if (n) fwrite(data, 1, n, f);
    c = crc(0, h + 4, 4);
    c = crc(c, data, n);
    be32(h, c);
    fwrite(h, 1, 4, f);
}

int gpu_dump_vram_png(const char *path) {
    uint32_t *px = malloc((size_t)GPU_VRAM_W * GPU_VRAM_H * 4);
    int r;
    if (!px) return -1;
    gpu_display_rgba(gpu_vram, 0, 0, GPU_VRAM_W, GPU_VRAM_H, 0, px);
    r = gpu_write_png(path, px, GPU_VRAM_W, GPU_VRAM_H);
    free(px);
    return r;
}

int gpu_write_png(const char *path, const uint32_t *px, int w, int h) {
    size_t row = (size_t)w * 3 + 1, raw_n = row * (size_t)h, blocks = (raw_n + 65534) / 65535, z_n, i, o;
    uint8_t *raw = malloc(raw_n), *z, ihdr[13];
    uint32_t a = 1, bsum = 0;
    FILE *f;
    int x, y;
    if (!raw) return -1;
    for (y = 0; y < h; y++) {
        uint8_t *p = raw + row * (size_t)y;
        *p++ = 0;
        for (x = 0; x < w; x++) {
            uint32_t c = px[(size_t)y * (size_t)w + (size_t)x];
            *p++ = (uint8_t)(c >> 16); *p++ = (uint8_t)(c >> 8); *p++ = (uint8_t)c;
        }
    }
    z_n = 2 + raw_n + blocks * 5 + 4;
    z = malloc(z_n);
    if (!z) { free(raw); return -1; }
    o = 0;
    z[o++] = 0x78; z[o++] = 0x01;
    for (i = 0; i < raw_n; i += 65535) {
        size_t n = raw_n - i < 65535 ? raw_n - i : 65535;
        z[o++] = (uint8_t)(i + n >= raw_n);
        z[o++] = (uint8_t)n; z[o++] = (uint8_t)(n >> 8);
        z[o++] = (uint8_t)~n; z[o++] = (uint8_t)(~n >> 8);
        memcpy(z + o, raw + i, n);
        o += n;
    }
    for (i = 0; i < raw_n; i++) { a = (a + raw[i]) % 65521; bsum = (bsum + a) % 65521; }
    be32(z + o, bsum << 16 | a);
    o += 4;
    f = fopen(path, "wb");
    if (!f) { free(raw); free(z); return -1; }
    fwrite("\x89PNG\r\n\x1a\n", 1, 8, f);
    be32(ihdr, (uint32_t)w); be32(ihdr + 4, (uint32_t)h);
    ihdr[8] = 8; ihdr[9] = 2; ihdr[10] = ihdr[11] = ihdr[12] = 0;
    chunk(f, "IHDR", ihdr, 13);
    chunk(f, "IDAT", z, (uint32_t)o);
    chunk(f, "IEND", NULL, 0);
    fclose(f);
    free(raw);
    free(z);
    return 0;
}
