/* The MDEC in software and the BS Huffman stage (mdec.h). From psx-spx's MDEC and BS compression sections. */
#include <string.h>

#include "mdec.h"

const uint8_t mdec_zigzag[64] = {
    0,  1,  5,  6,  14, 15, 27, 28, 2,  4,  7,  13, 16, 26, 29, 42, 3,  8,  12, 17, 25, 30, 41, 43, 9,  11, 18, 24, 31, 40, 44, 53,
    10, 19, 23, 32, 39, 45, 52, 54, 20, 22, 33, 38, 46, 51, 55, 60, 21, 34, 37, 47, 50, 56, 59, 61, 35, 36, 48, 49, 57, 58, 62, 63,
};
const uint8_t mdec_zagzig[64] = {
    0,  1,  8,  16, 9,  2,  3,  10, 17, 24, 32, 25, 18, 11, 4,  5,  12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6,  7,  14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51, 58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63,
};

static const uint16_t std_scale[64] = {
    0x5A82, 0x5A82, 0x5A82, 0x5A82, 0x5A82, 0x5A82, 0x5A82, 0x5A82, 0x7D8A, 0x6A6D, 0x471C, 0x18F8, 0xE707, 0xB8E3, 0x9592, 0x8275,
    0x7641, 0x30FB, 0xCF04, 0x89BE, 0x89BE, 0xCF04, 0x30FB, 0x7641, 0x6A6D, 0xE707, 0x8275, 0xB8E3, 0x471C, 0x7D8A, 0x18F8, 0x9592,
    0x5A82, 0xA57D, 0xA57D, 0x5A82, 0x5A82, 0xA57D, 0xA57D, 0x5A82, 0x471C, 0x8275, 0x18F8, 0x6A6D, 0x9592, 0xE707, 0x7D8A, 0xB8E3,
    0x30FB, 0x89BE, 0x7641, 0xCF04, 0xCF04, 0x7641, 0x89BE, 0x30FB, 0x18F8, 0xB8E3, 0x6A6D, 0x8275, 0x7D8A, 0x9592, 0x471C, 0xE707,
};
static const uint8_t std_iq[64] = {
    2,  16, 16, 19, 16, 19, 22, 22, 22, 22, 22, 22, 26, 24, 26, 27, 27, 27, 26, 26, 26, 26, 27, 27, 27, 29, 29, 29, 34, 34, 34, 29,
    29, 29, 27, 27, 29, 29, 32, 32, 34, 34, 37, 38, 37, 35, 35, 34, 35, 38, 38, 40, 40, 40, 48, 48, 46, 46, 56, 56, 58, 69, 69, 83,
};

void mdec_default_tables(MdecTables *t) {
    int i;
    memcpy(t->iq_y, std_iq, 64);
    memcpy(t->iq_c, std_iq, 64);
    for (i = 0; i < 64; i++) t->scale[i] = (int16_t)std_scale[i];
}

/* ---- the IDCT: dst = src * scale, src read column by column (psx-spx's real_idct_core), twice */
void mdec_idct(const int16_t scale[64], int32_t blk[64]) {
    int32_t tmp[64], *src = blk, *dst = tmp, *t;
    int pass, x, y, z;
    for (pass = 0; pass < 2; pass++) {
        for (x = 0; x < 8; x++)
            for (y = 0; y < 8; y++) {
                int64_t sum = 0;
                for (z = 0; z < 8; z++) sum += (int64_t)src[y + z * 8] * (scale[x + z * 8] >> 3);
                dst[x + y * 8] = (int32_t)((sum + 0x1000) >> 13);
            }
        t = src; src = dst; dst = t;
    }
    /* two passes: the result is back in blk */
}

static int s10(uint16_t n) { int v = n & 0x3FF; return v & 0x200 ? v - 0x400 : v; }
static int32_t clamp(int32_t v, int32_t lo, int32_t hi) { return v < lo ? lo : v > hi ? hi : v; }

int mdec_rl_block(const MdecTables *t, const uint8_t qt[64], const uint16_t *src, int n, int *pos, int32_t blk[64]) {
    int p = *pos, k = 0, q;
    uint16_t c;
    int32_t val;
    memset(blk, 0, 64 * sizeof blk[0]);
    do { /* FE00h before a block is padding */
        if (p >= n) return 0;
        c = src[p++];
    } while (c == 0xFE00);
    q = (c >> 10) & 0x3F;
    val = s10(c) * qt[0];
    for (;;) {
        if (q == 0) val = s10(c) * 2;
        val = clamp(val, -0x400, 0x3FF);
        if (q > 0) blk[mdec_zagzig[k]] = val;
        else blk[k] = val;
        if (p >= n) break; /* the data ended inside the block: what there is of it */
        c = src[p++];
        k += ((c >> 10) & 0x3F) + 1;
        if (k > 63) break; /* FE00h (or a run past the end) */
        val = (s10(c) * qt[k] * q + 4) / 8;
    }
    *pos = p;
    mdec_idct(t->scale, blk);
    return 1;
}

int mdec_macroblock_bytes(int depth) {
    switch (depth) {
    case MDEC_DEPTH24: return 16 * 16 * 3;
    case MDEC_DEPTH15: return 16 * 16 * 2;
    case MDEC_DEPTH8: return 64;
    default: return 32;
    }
}

static int8_t mono9(int32_t y) { /* y_to_mono: signed 9 bits, saturated to 8 */
    int v = y & 0x1FF;
    if (v & 0x100) v -= 0x200;
    return (int8_t)clamp(v, -128, 127);
}

int mdec_macroblock(const MdecTables *t, const uint16_t *src, int n, int *pos, int depth, int signed_out, int set15,
                    uint8_t *out) {
    int32_t cr[64], cb[64], yb[64];
    int p = *pos, b, x, y;
    if (depth == MDEC_DEPTH4 || depth == MDEC_DEPTH8) {
        if (!mdec_rl_block(t, t->iq_y, src, n, &p, yb)) return 0;
        *pos = p;
        for (x = 0; x < 64; x++) {
            int v = mono9(yb[x]);
            uint8_t u = (uint8_t)(signed_out ? v : v + 128);
            if (depth == MDEC_DEPTH8) out[x] = u;
            else if (x & 1) out[x >> 1] |= (uint8_t)((u >> 4) << 4);
            else out[x >> 1] = (uint8_t)(u >> 4);
        }
        return mdec_macroblock_bytes(depth);
    }
    if (!mdec_rl_block(t, t->iq_c, src, n, &p, cr) || !mdec_rl_block(t, t->iq_c, src, n, &p, cb)) return 0;
    for (b = 0; b < 4; b++) {
        int xx = (b & 1) * 8, yy = (b >> 1) * 8;
        if (!mdec_rl_block(t, t->iq_y, src, n, &p, yb)) return 0;
        for (y = 0; y < 8; y++)
            for (x = 0; x < 8; x++) {
                int ci = ((x + xx) >> 1) + ((y + yy) >> 1) * 8, o = (x + xx) + (y + yy) * 16;
                int32_t R = cr[ci], B = cb[ci], Y = yb[x + y * 8], G;
                /* yuv_to_rgb: 1.402, -0.3437, -0.7143, 1.772 at 12 fraction bits */
                G = (-1408 * B - 2926 * R + 2048) >> 12;
                R = (5743 * R + 2048) >> 12;
                B = (7258 * B + 2048) >> 12;
                R = clamp(Y + R, -128, 127);
                G = clamp(Y + G, -128, 127);
                B = clamp(Y + B, -128, 127);
                if (!signed_out) { R += 128; G += 128; B += 128; }
                if (depth == MDEC_DEPTH24) {
                    out[o * 3] = (uint8_t)R;
                    out[o * 3 + 1] = (uint8_t)G;
                    out[o * 3 + 2] = (uint8_t)B;
                } else {
                    uint16_t px = (uint16_t)(((uint8_t)R >> 3) | ((uint8_t)G >> 3) << 5 | ((uint8_t)B >> 3) << 10 |
                                             (set15 ? 0x8000 : 0));
                    out[o * 2] = (uint8_t)px;
                    out[o * 2 + 1] = (uint8_t)(px >> 8);
                }
            }
    }
    *pos = p;
    return mdec_macroblock_bytes(depth);
}

/* ---- the Huffman stage */
/* psx-spx's AC codes for BS v1/v2/v3: a prefix, then x index bits into the values, then the sign bit */
static const struct { const char *prefix; int xbits; uint16_t v[16]; } ac_codes[] = {
    {"11", 0, {0x0001}},
    {"011", 0, {0x0401}},
    {"010", 1, {0x0002, 0x0801}},
    {"0011", 1, {0x1001, 0x0C01}},
    {"00101", 0, {0x0003}},
    {"00100", 3, {0x3401, 0x0006, 0x3001, 0x2C01, 0x0C02, 0x0403, 0x0005, 0x2801}},
    {"0001", 2, {0x1C01, 0x1801, 0x0402, 0x1401}},
    {"00001", 2, {0x0802, 0x2401, 0x0004, 0x2001}},
    {"0000001", 3, {0x4001, 0x1402, 0x0007, 0x0803, 0x0404, 0x3C01, 0x3801, 0x1002}},
    {"00000001", 4, {0x000B, 0x2002, 0x1003, 0x000A, 0x0804, 0x1C02, 0x5401, 0x5001, 0x0009, 0x4C01, 0x4801, 0x0405,
                     0x0C03, 0x0008, 0x1802, 0x4401}},
    {"000000001", 4, {0x2802, 0x2402, 0x1403, 0x0C04, 0x0805, 0x0407, 0x0406, 0x000F, 0x000E, 0x000D, 0x000C, 0x6801,
                      0x6401, 0x6001, 0x5C01, 0x5801}},
    {"0000000001", 4, {0x001F, 0x001E, 0x001D, 0x001C, 0x001B, 0x001A, 0x0019, 0x0018, 0x0017, 0x0016, 0x0015, 0x0014,
                       0x0013, 0x0012, 0x0011, 0x0010}},
    {"00000000001", 4, {0x0028, 0x0027, 0x0026, 0x0025, 0x0024, 0x0023, 0x0022, 0x0021, 0x0020, 0x040E, 0x040D, 0x040C,
                        0x040B, 0x040A, 0x0409, 0x0408}},
    {"000000000001", 4, {0x0412, 0x0411, 0x0410, 0x040F, 0x1803, 0x4002, 0x3C02, 0x3802, 0x3402, 0x3002, 0x2C02, 0x7C01,
                         0x7801, 0x7401, 0x7001, 0x6C01}},
};
#define AC_BITS 17
#define AC_EOB 0x10000u
#define AC_ESC 0x20000u
/* entry: (length << 24) | value, value AC_EOB / AC_ESC for the two special codes; 0 = no code */
static uint32_t ac_table[1u << AC_BITS];
static int ac_built;

static void ac_put(uint32_t code, int len, uint32_t value) {
    uint32_t first = code << (AC_BITS - len), count = 1u << (AC_BITS - len), i;
    for (i = 0; i < count; i++) ac_table[first + i] = (uint32_t)len << 24 | value;
}

static uint32_t bits_of(const char *s, int *len) {
    uint32_t v = 0;
    *len = 0;
    for (; *s; s++, ++*len) v = v << 1 | (uint32_t)(*s == '1');
    return v;
}

static void ac_build(void) {
    size_t i;
    int len;
    uint32_t code;
    if (ac_built) return;
    for (i = 0; i < sizeof ac_codes / sizeof ac_codes[0]; i++) {
        uint32_t x, pre = bits_of(ac_codes[i].prefix, &len);
        for (x = 0; x < (1u << ac_codes[i].xbits); x++) {
            uint16_t v = ac_codes[i].v[x];
            int l = len + ac_codes[i].xbits + 1;
            code = (pre << ac_codes[i].xbits | x) << 1;
            ac_put(code, l, v);                                                           /* s = 0 */
            ac_put(code | 1, l, (uint32_t)((v & 0xFC00) | ((-(v & 0x3FF)) & 0x3FF))); /* s = 1: level negated */
        }
    }
    code = bits_of("10", &len);
    ac_put(code, len, AC_EOB);
    code = bits_of("000001", &len);
    ac_put(code, len, AC_ESC);
    ac_built = 1;
}

int mdec_ac_lookup(uint32_t bits, int avail, int *len) {
    uint32_t peek = avail >= AC_BITS ? bits >> (avail - AC_BITS) : bits << (AC_BITS - avail), e;
    ac_build();
    e = ac_table[peek & ((1u << AC_BITS) - 1)];
    *len = (int)(e >> 24);
    if (!e || *len > avail) return -1;
    if ((e & 0xFFFFFF) == AC_EOB) return 0xFE00;
    if ((e & 0xFFFFFF) == AC_ESC) return -2;
    return (int)(e & 0xFFFF);
}

/* the bitstream: little-endian halfwords, each read from bit 15 down */
typedef struct {
    const uint8_t *p;
    size_t n, pos;
    uint32_t buf; /* the next bits, left-aligned */
    int cnt;
} Bits;

static void fill(Bits *b) {
    while (b->cnt <= 16) {
        uint32_t hw = b->pos + 1 < b->n ? (uint32_t)b->p[b->pos] | (uint32_t)b->p[b->pos + 1] << 8 : 0;
        b->pos += 2;
        b->buf |= hw << (16 - b->cnt);
        b->cnt += 16;
    }
}
static uint32_t peek(Bits *b, int n) { fill(b); return b->buf >> (32 - n); }
static void skip(Bits *b, int n) { b->buf <<= n; b->cnt -= n; }
static uint32_t get(Bits *b, int n) { uint32_t v = peek(b, n); skip(b, n); return v; }
/* past the end of the data (the 2 halfwords fill reads ahead aside) */
static int ended(const Bits *b) { return b->pos > b->n + 4; }

/* BS v3's DC: Huffman-coded size (luminance or colour table, the MPEG-1 DC sizes), then size bits of difference
 * (s = 0 for negative), times 4 */
static int dc_v3(Bits *b, int chroma, int *diff) {
    static const char *luma[9] = {"100", "00", "01", "101", "110", "1110", "11110", "111110", "1111110"};
    static const char *chro[9] = {"00", "01", "10", "110", "1110", "11110", "111110", "1111110", "11111110"};
    const char **tab = chroma ? chro : luma;
    uint32_t p = peek(b, 8);
    int size, len;
    for (size = 0; size < 9; size++) {
        uint32_t code = bits_of(tab[size], &len);
        if ((p >> (8 - len)) == code) break;
    }
    if (size == 9) return 0;
    skip(b, len);
    if (size == 0) { *diff = 0; return 1; }
    {
        int v = (int)get(b, size);
        if (!(v >> (size - 1))) v = v - (1 << size) + 1;
        *diff = v * 4;
    }
    return 1;
}

int mdec_vlc(const uint8_t *bs, size_t bs_len, uint16_t *out, int max) {
    Bits b;
    int n = 0, q, version, blk, dc_old[3] = {0, 0, 0};
    if (bs_len < 8 || (bs[2] | bs[3] << 8) != 0x3800) return -1;
    q = (bs[4] | bs[5] << 8) & 0x3F;
    version = bs[6] | bs[7] << 8;
    if (version < 1 || version > 3) return -1;
    ac_build();
    memset(&b, 0, sizeof b);
    b.p = bs;
    b.n = bs_len;
    b.pos = 8;
    for (;;) { /* a macroblock: Cr, Cb, Y1..Y4 */
        for (blk = 0; blk < 6; blk++) {
            int dc;
            if (n + 2 > max || ended(&b)) return n;
            if (version == 3) {
                int diff, c = blk < 2 ? blk : 2;
                if (peek(&b, 10) == 0x3FF) return n; /* end of frame */
                if (!dc_v3(&b, blk < 2, &diff)) return n;
                dc = dc_old[c] += diff;
            } else {
                uint32_t raw = get(&b, 10);
                if (blk == 0 && raw == 0x1FF) return n; /* end of frame, in place of Cr */
                dc = (int)raw;
            }
            out[n++] = (uint16_t)(q << 10 | (dc & 0x3FF));
            for (;;) { /* AC to end of block */
                int len, v;
                fill(&b);
                v = mdec_ac_lookup(b.buf >> (32 - AC_BITS), AC_BITS, &len);
                if (v == -1 || ended(&b)) return n; /* a bit string that isn't a code: the frame is over */
                skip(&b, len);
                if (v == -2) v = (int)get(&b, 16);
                if (n >= max) return n;
                out[n++] = (uint16_t)v;
                if (v == 0xFE00) break;
            }
        }
    }
}
