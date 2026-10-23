/* Software GTE (COP2), written from the psx-spx hardware reference
 * (https://psx-spx.consoledev.net/geometrytransformationenginegte/): register file, read/write side effects, the
 * commands the PS1 libraries and games use, the sf/lm fields and the FLAG register's saturation bits.
 * Not checked bit for bit against hardware yet; where psx-spx describes a hardware quirk it is followed. */
#include <stdint.h>
#include <stdio.h>

#include "psx_runtime.h"

typedef struct {
    int32_t d[32]; /* data registers, stored as written */
    int32_t c[32]; /* control registers */
} Gte;

static Gte g;
static uint8_t unr_table[0x101];
static int unr_ready;

/* data register names */
enum { VXY0, VZ0, VXY1, VZ1, VXY2, VZ2, RGBC, OTZ, IR0, IR1, IR2, IR3, SXY0, SXY1, SXY2, SXYP, SZ0, SZ1, SZ2, SZ3,
       RGB0, RGB1, RGB2, RES1, MAC0, MAC1, MAC2, MAC3, IRGB, ORGB, LZCS, LZCR };
/* control register names */
enum { RT11RT12, RT13RT21, RT22RT23, RT31RT32, RT33, TRX, TRY, TRZ, L11L12, L13L21, L22L23, L31L32, L33, RBK, GBK, BBK,
       LR1LR2, LR3LG1, LG2LG3, LB1LB2, LB3, RFC, GFC, BFC, OFX, OFY, H, DQA, DQB, ZSF3, ZSF4, FLAG };

/* FLAG bits */
#define F_MAC1P (1u << 30)
#define F_MAC2P (1u << 29)
#define F_MAC3P (1u << 28)
#define F_MAC1N (1u << 27)
#define F_MAC2N (1u << 26)
#define F_MAC3N (1u << 25)
#define F_IR1 (1u << 24)
#define F_IR2 (1u << 23)
#define F_IR3 (1u << 22)
#define F_R (1u << 21)
#define F_G (1u << 20)
#define F_B (1u << 19)
#define F_SZ3 (1u << 18)
#define F_DIV (1u << 17)
#define F_MAC0P (1u << 16)
#define F_MAC0N (1u << 15)
#define F_SX2 (1u << 14)
#define F_SY2 (1u << 13)
#define F_IR0 (1u << 12)

static uint32_t flag;
static int sf_shift, lm;

static int16_t lo16(int32_t v) { return (int16_t)(v & 0xFFFF); }
static int16_t hi16(int32_t v) { return (int16_t)((uint32_t)v >> 16); }

/* matrix element (row r, column c) of the 3x3 matrix starting at control register base */
static int32_t mat(int base, int r, int c) {
    int i = r * 3 + c, reg = base + i / 2;
    if (i == 8) return (int16_t)g.c[base + 4];
    return (i & 1) ? hi16(g.c[reg]) : lo16(g.c[reg]);
}

static int64_t mac_check(int i, int64_t v) {
    /* MAC1-3 hold 44-bit intermediates: flag overflow, then wrap to 44 bits */
    static const uint32_t pos[3] = {F_MAC1P, F_MAC2P, F_MAC3P}, neg[3] = {F_MAC1N, F_MAC2N, F_MAC3N};
    if (v > 0x7FFFFFFFFFFLL) flag |= pos[i - 1];
    else if (v < -0x80000000000LL) flag |= neg[i - 1];
    return (int64_t)((uint64_t)v << 20) >> 20;
}

static void set_mac(int i, int64_t v) { /* v: the unshifted sum */
    v = mac_check(i, v);
    g.d[MAC0 + i] = (int32_t)(v >> sf_shift);
}

static void set_mac0(int64_t v) {
    if (v > 0x7FFFFFFFLL) flag |= F_MAC0P;
    else if (v < -0x80000000LL) flag |= F_MAC0N;
    g.d[MAC0] = (int32_t)v;
}

static int32_t sat_ir(int i, int32_t v, int lmode) {
    static const uint32_t bit[3] = {F_IR1, F_IR2, F_IR3};
    int32_t lo = lmode ? 0 : -0x8000;
    if (v < lo) { flag |= bit[i - 1]; return lo; }
    if (v > 0x7FFF) { flag |= bit[i - 1]; return 0x7FFF; }
    return v;
}

static void mac_to_ir(int lmode) {
    int i;
    for (i = 1; i <= 3; i++) g.d[IR0 + i] = sat_ir(i, g.d[MAC0 + i], lmode);
}

static void push_sz(int64_t v) {
    if (v < 0) { flag |= F_SZ3; v = 0; }
    else if (v > 0xFFFF) { flag |= F_SZ3; v = 0xFFFF; }
    g.d[SZ0] = g.d[SZ1]; g.d[SZ1] = g.d[SZ2]; g.d[SZ2] = g.d[SZ3]; g.d[SZ3] = (int32_t)v;
}

static void push_sxy(int32_t x, int32_t y) {
    if (x < -0x400) { flag |= F_SX2; x = -0x400; } else if (x > 0x3FF) { flag |= F_SX2; x = 0x3FF; }
    if (y < -0x400) { flag |= F_SY2; y = -0x400; } else if (y > 0x3FF) { flag |= F_SY2; y = 0x3FF; }
    g.d[SXY0] = g.d[SXY1]; g.d[SXY1] = g.d[SXY2];
    g.d[SXY2] = (int32_t)(((uint32_t)y << 16) | ((uint32_t)x & 0xFFFF));
}

static uint32_t sat_col(int32_t v, uint32_t bit) {
    if (v < 0) { flag |= bit; return 0; }
    if (v > 0xFF) { flag |= bit; return 0xFF; }
    return (uint32_t)v;
}

static void push_color(void) {
    uint32_t r = sat_col(g.d[MAC1] >> 4, F_R), gg = sat_col(g.d[MAC2] >> 4, F_G), b = sat_col(g.d[MAC3] >> 4, F_B);
    uint32_t code = (uint32_t)g.d[RGBC] & 0xFF000000u;
    g.d[RGB0] = g.d[RGB1]; g.d[RGB1] = g.d[RGB2];
    g.d[RGB2] = (int32_t)(code | (b << 16) | (gg << 8) | r);
}

static void make_unr(void) {
    int i;
    for (i = 0; i <= 0x100; i++) {
        int v = (0x40000 / (i + 0x100) + 1) / 2 - 0x101;
        unr_table[i] = (uint8_t)(v < 0 ? 0 : v);
    }
    unr_ready = 1;
}

/* H / SZ3 as the GTE's Newton-Raphson divider computes it (psx-spx "GTE Division Inaccuracy") */
static uint32_t gte_divide(uint32_t h, uint32_t sz3) {
    if (!unr_ready) make_unr();
    if (h < sz3 * 2) {
        int z = 0;
        uint32_t n, d, u;
        while (z < 16 && !(sz3 & (0x8000u >> z))) z++;
        n = h << z;
        d = sz3 << z;
        u = unr_table[(d - 0x7FC0) >> 7] + 0x101;
        d = (0x2000080 - (d * u)) >> 8;
        d = (0x0000080 + (d * u)) >> 8;
        n = (uint32_t)((((uint64_t)n * d) + 0x8000) >> 16);
        return n > 0x1FFFF ? 0x1FFFF : n;
    }
    flag |= F_DIV;
    return 0x1FFFF;
}

static void rtp(int v, int last) {
    int32_t vx = lo16(g.d[VXY0 + v * 2]), vy = hi16(g.d[VXY0 + v * 2]), vz = (int16_t)g.d[VZ0 + v * 2];
    int i;
    int64_t m3;
    uint32_t n;
    for (i = 1; i <= 3; i++) {
        int64_t s = ((int64_t)g.c[TRX + i - 1] << 12) + (int64_t)mat(RT11RT12, i - 1, 0) * vx;
        s += (int64_t)mat(RT11RT12, i - 1, 1) * vy;
        s += (int64_t)mat(RT11RT12, i - 1, 2) * vz;
        set_mac(i, s);
        if (i == 3) m3 = mac_check(3, s);
    }
    g.d[IR1] = sat_ir(1, g.d[MAC1], lm);
    g.d[IR2] = sat_ir(2, g.d[MAC2], lm);
    /* IR3's flag follows MAC3 SAR 12 whatever sf is (psx-spx RTPS notes); the value saturates normally */
    {
        int32_t v3 = g.d[MAC3], lo = lm ? 0 : -0x8000, f3 = (int32_t)(m3 >> 12);
        g.d[IR3] = v3 < lo ? lo : v3 > 0x7FFF ? 0x7FFF : v3;
        if (f3 < -0x8000 || f3 > 0x7FFF) flag |= F_IR3;
    }
    push_sz(m3 >> 12);
    n = gte_divide((uint32_t)(uint16_t)g.c[H], (uint32_t)g.d[SZ3]);
    {
        int64_t x = (int64_t)n * g.d[IR1] + g.c[OFX], y = (int64_t)n * g.d[IR2] + g.c[OFY];
        set_mac0(x); set_mac0(y);
        push_sxy((int32_t)(x >> 16), (int32_t)(y >> 16));
    }
    if (last) {
        int64_t d = (int64_t)n * (int16_t)g.c[DQA] + g.c[DQB];
        int32_t ir0;
        set_mac0(d);
        ir0 = (int32_t)(d >> 12);
        if (ir0 < 0) { flag |= F_IR0; ir0 = 0; } else if (ir0 > 0x1000) { flag |= F_IR0; ir0 = 0x1000; }
        g.d[IR0] = ir0;
    }
}

/* [MAC] = (cv SHL 12 + M * v) SAR sf, IR = MAC saturated. vec: 3 components */
static void mul_matrix(int base, const int32_t *vec, const int32_t *cv) {
    int i;
    for (i = 1; i <= 3; i++) {
        int64_t s = ((int64_t)(cv ? cv[i - 1] : 0) << 12) + (int64_t)mat(base, i - 1, 0) * vec[0];
        s += (int64_t)mat(base, i - 1, 1) * vec[1];
        s += (int64_t)mat(base, i - 1, 2) * vec[2];
        set_mac(i, s);
    }
    mac_to_ir(lm);
}

static void ir_vec(int32_t *v) { v[0] = g.d[IR1]; v[1] = g.d[IR2]; v[2] = g.d[IR3]; }

static void vertex(int n, int32_t *v) {
    v[0] = lo16(g.d[VXY0 + n * 2]); v[1] = hi16(g.d[VXY0 + n * 2]); v[2] = (int16_t)g.d[VZ0 + n * 2];
}

/* light colour stage: IR = LLM * V; IR = BK + LCM * IR */
static void light(int n) {
    int32_t v[3], bk[3];
    vertex(n, v);
    mul_matrix(L11L12, v, 0);
    ir_vec(v);
    bk[0] = g.c[RBK]; bk[1] = g.c[GBK]; bk[2] = g.c[BBK];
    mul_matrix(LR1LR2, v, bk);
}

/* MAC = MAC + (FC - MAC) * IR0, then SAR sf; MAC on entry is unshifted (x 0x1000) */
static void depth_cue(int64_t m1, int64_t m2, int64_t m3) {
    int64_t m[3];
    int32_t fc[3];
    int i;
    m[0] = m1; m[1] = m2; m[2] = m3;
    fc[0] = g.c[RFC]; fc[1] = g.c[GFC]; fc[2] = g.c[BFC];
    for (i = 0; i < 3; i++) {
        int64_t diff = mac_check(i + 1, ((int64_t)fc[i] << 12) - m[i]);
        int32_t ir = sat_ir(i + 1, (int32_t)(diff >> sf_shift), 0);
        set_mac(i + 1, (int64_t)ir * (int16_t)g.d[IR0] + m[i]);
    }
    mac_to_ir(lm);
}

/* color = RGBC * IR, SHL 4; with or without depth cue */
static void color_from_ir(int cue) {
    uint32_t c = (uint32_t)g.d[RGBC];
    int64_t m1 = ((int64_t)(c & 0xFF) * g.d[IR1]) << 4;
    int64_t m2 = ((int64_t)((c >> 8) & 0xFF) * g.d[IR2]) << 4;
    int64_t m3 = ((int64_t)((c >> 16) & 0xFF) * g.d[IR3]) << 4;
    if (cue) depth_cue(m1, m2, m3);
    else { set_mac(1, m1); set_mac(2, m2); set_mac(3, m3); mac_to_ir(lm); }
    push_color();
}

static void depth_from_rgb(uint32_t c) { /* DPCS/DPCT: MAC = RGB SHL 16, then depth cue */
    depth_cue((int64_t)(c & 0xFF) << 16, (int64_t)((c >> 8) & 0xFF) << 16, (int64_t)((c >> 16) & 0xFF) << 16);
    push_color();
}

static int unimplemented_logged[64];

void psx_gte_op(unsigned int word) {
    int op = word & 0x3F, i;
    sf_shift = (word & (1u << 19)) ? 12 : 0;
    lm = (word >> 10) & 1;
    flag = 0;
    switch (op) {
    case 0x01: rtp(0, 1); break; /* RTPS */
    case 0x30: rtp(0, 0); rtp(1, 0); rtp(2, 1); break; /* RTPT */
    case 0x06: { /* NCLIP */
        int32_t x0 = lo16(g.d[SXY0]), y0 = hi16(g.d[SXY0]), x1 = lo16(g.d[SXY1]), y1 = hi16(g.d[SXY1]);
        int32_t x2 = lo16(g.d[SXY2]), y2 = hi16(g.d[SXY2]);
        set_mac0((int64_t)x0 * y1 + (int64_t)x1 * y2 + (int64_t)x2 * y0 - (int64_t)x0 * y2 - (int64_t)x1 * y0
                 - (int64_t)x2 * y1);
        break;
    }
    case 0x2D: case 0x2E: { /* AVSZ3, AVSZ4 */
        int64_t s = op == 0x2D ? (int64_t)(int16_t)g.c[ZSF3] * (g.d[SZ1] + g.d[SZ2] + g.d[SZ3])
                               : (int64_t)(int16_t)g.c[ZSF4] * (g.d[SZ0] + g.d[SZ1] + g.d[SZ2] + g.d[SZ3]);
        int64_t z;
        set_mac0(s);
        z = s >> 12;
        if (z < 0) { flag |= F_SZ3; z = 0; } else if (z > 0xFFFF) { flag |= F_SZ3; z = 0xFFFF; }
        g.d[OTZ] = (int32_t)z;
        break;
    }
    case 0x12: { /* MVMVA */
        int mx = (word >> 17) & 3, v = (word >> 15) & 3, cvsel = (word >> 13) & 3;
        int32_t vec[3], cv[3] = {0, 0, 0};
        static const int bases[3] = {RT11RT12, L11L12, LR1LR2};
        if (v == 3) ir_vec(vec); else vertex(v, vec);
        if (cvsel == 0) { cv[0] = g.c[TRX]; cv[1] = g.c[TRY]; cv[2] = g.c[TRZ]; }
        else if (cvsel == 1) { cv[0] = g.c[RBK]; cv[1] = g.c[GBK]; cv[2] = g.c[BBK]; }
        else if (cvsel == 2) { cv[0] = g.c[RFC]; cv[1] = g.c[GFC]; cv[2] = g.c[BFC]; }
        if (mx == 3) { /* psx-spx: the "garbage" matrix; rows -R*0x10, R*0x10, IR0 / RT13 RT13 RT13 / RT22 RT22 RT22 */
            int64_t r = ((int64_t)((uint32_t)g.d[RGBC] & 0xFF)) << 4;
            int32_t m[3][3];
            m[0][0] = (int32_t)-r; m[0][1] = (int32_t)r; m[0][2] = (int16_t)g.d[IR0];
            m[1][0] = m[1][1] = m[1][2] = mat(RT11RT12, 0, 2);
            m[2][0] = m[2][1] = m[2][2] = mat(RT11RT12, 1, 1);
            for (i = 1; i <= 3; i++)
                set_mac(i, ((int64_t)cv[i - 1] << 12) + (int64_t)m[i - 1][0] * vec[0] + (int64_t)m[i - 1][1] * vec[1]
                               + (int64_t)m[i - 1][2] * vec[2]);
            mac_to_ir(lm);
        } else if (cvsel == 2) { /* the far-colour bug: the first column's product is flagged but not kept */
            for (i = 1; i <= 3; i++) {
                int32_t ir = sat_ir(i, (int32_t)(mac_check(i, ((int64_t)cv[i - 1] << 12)
                                                              + (int64_t)mat(bases[mx], i - 1, 0) * vec[0]) >> sf_shift), 0);
                (void)ir;
                set_mac(i, (int64_t)mat(bases[mx], i - 1, 1) * vec[1] + (int64_t)mat(bases[mx], i - 1, 2) * vec[2]);
            }
            mac_to_ir(lm);
        } else {
            mul_matrix(bases[mx], vec, cvsel == 3 ? 0 : cv);
        }
        break;
    }
    case 0x28: /* SQR */
        for (i = 1; i <= 3; i++) set_mac(i, (int64_t)g.d[IR0 + i] * g.d[IR0 + i]);
        mac_to_ir(lm);
        break;
    case 0x0C: { /* OP: cross product of (RT11, RT22, RT33) and IR */
        int32_t d1 = mat(RT11RT12, 0, 0), d2 = mat(RT11RT12, 1, 1), d3 = mat(RT11RT12, 2, 2);
        set_mac(1, (int64_t)g.d[IR3] * d2 - (int64_t)g.d[IR2] * d3);
        set_mac(2, (int64_t)g.d[IR1] * d3 - (int64_t)g.d[IR3] * d1);
        set_mac(3, (int64_t)g.d[IR2] * d1 - (int64_t)g.d[IR1] * d2);
        mac_to_ir(lm);
        break;
    }
    case 0x3D: /* GPF */
        for (i = 1; i <= 3; i++) set_mac(i, (int64_t)(int16_t)g.d[IR0] * g.d[IR0 + i]);
        mac_to_ir(lm);
        push_color();
        break;
    case 0x3E: /* GPL */
        for (i = 1; i <= 3; i++)
            set_mac(i, ((int64_t)g.d[MAC0 + i] << sf_shift) + (int64_t)(int16_t)g.d[IR0] * g.d[IR0 + i]);
        mac_to_ir(lm);
        push_color();
        break;
    case 0x10: depth_from_rgb((uint32_t)g.d[RGBC]); break; /* DPCS */
    case 0x2A: for (i = 0; i < 3; i++) depth_from_rgb((uint32_t)g.d[RGB0]); break; /* DPCT */
    case 0x11: /* INTPL */
        depth_cue((int64_t)g.d[IR1] << 12, (int64_t)g.d[IR2] << 12, (int64_t)g.d[IR3] << 12);
        push_color();
        break;
    case 0x29: color_from_ir(1); break; /* DCPL */
    case 0x1E: light(0); push_color(); break; /* NCS */
    case 0x20: for (i = 0; i < 3; i++) { light(i); push_color(); } break; /* NCT */
    case 0x1B: light(0); color_from_ir(0); break; /* NCCS */
    case 0x3F: for (i = 0; i < 3; i++) { light(i); color_from_ir(0); } break; /* NCCT */
    case 0x13: light(0); color_from_ir(1); break; /* NCDS */
    case 0x16: for (i = 0; i < 3; i++) { light(i); color_from_ir(1); } break; /* NCDT */
    case 0x1C: case 0x14: { /* CC, CDP: start from IR */
        int32_t v[3], bk[3];
        ir_vec(v);
        bk[0] = g.c[RBK]; bk[1] = g.c[GBK]; bk[2] = g.c[BBK];
        mul_matrix(LR1LR2, v, bk);
        color_from_ir(op == 0x14);
        break;
    }
    default:
        if (!unimplemented_logged[op]) {
            unimplemented_logged[op] = 1;
            psx_log("gte: command 0x%02X (word 0x%08X) not implemented; registers left alone\n", op, word);
        }
        return;
    }
    if (flag & 0x7F87E000u) flag |= 0x80000000u;
    g.c[FLAG] = (int32_t)flag;
}

static int32_t ir_to_5(int32_t v) { v >>= 7; return v < 0 ? 0 : v > 0x1F ? 0x1F : v; }

int psx_gte_mfc2(int reg) {
    switch (reg & 31) {
    case VZ0: case VZ1: case VZ2: case IR0: case IR1: case IR2: case IR3: return (int16_t)g.d[reg];
    case OTZ: case SZ0: case SZ1: case SZ2: case SZ3: return (uint16_t)g.d[reg];
    case SXYP: return g.d[SXY2];
    case IRGB: case ORGB:
        return ir_to_5(g.d[IR1]) | (ir_to_5(g.d[IR2]) << 5) | (ir_to_5(g.d[IR3]) << 10);
    case LZCR: {
        int32_t v = g.d[LZCS];
        uint32_t x = v < 0 ? ~(uint32_t)v : (uint32_t)v;
        int n = 0;
        while (n < 32 && !(x & (0x80000000u >> n))) n++;
        return n;
    }
    default: return g.d[reg & 31];
    }
}

void psx_gte_mtc2(int reg, int value) {
    reg &= 31;
    switch (reg) {
    case SXYP: g.d[SXY0] = g.d[SXY1]; g.d[SXY1] = g.d[SXY2]; g.d[SXY2] = value; break;
    case IRGB:
        g.d[IRGB] = value & 0x7FFF;
        g.d[IR1] = (value & 0x1F) << 7; g.d[IR2] = ((value >> 5) & 0x1F) << 7; g.d[IR3] = ((value >> 10) & 0x1F) << 7;
        break;
    case ORGB: case LZCR: break; /* read-only */
    default: g.d[reg] = value;
    }
}

int psx_gte_cfc2(int reg) {
    reg &= 31;
    switch (reg) {
    case RT33: case L33: case LB3: case H: case DQA: case ZSF3: case ZSF4: return (int16_t)g.c[reg];
    default: return g.c[reg];
    }
}

void psx_gte_ctc2(int reg, int value) {
    reg &= 31;
    if (reg == FLAG) {
        uint32_t f = (uint32_t)value & 0x7FFFF000u;
        if (f & 0x7F87E000u) f |= 0x80000000u;
        g.c[FLAG] = (int32_t)f;
        return;
    }
    g.c[reg] = value;
}
