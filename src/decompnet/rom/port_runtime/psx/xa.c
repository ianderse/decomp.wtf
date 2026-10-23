/* XA-ADPCM, from psx-spx ("CDROM XA Audio ADPCM Compression"): a Form 2 audio sector holds 18 sound groups of
 * 128 bytes at raw + 0x18. Each group: 16 header bytes (the 4 or 8 sound units' shift/filter at 4..11, copies
 * around them) and 28 words of samples; a 4-bit group has 8 units of 28 samples (in stereo even units are left,
 * odd right), an 8-bit group 4. The coding byte (subheader byte 3) gives stereo (bits 0-1), 18.9 kHz (bits 2-3)
 * and 8 bits (bits 4-5). Decoded at its own rate, then resampled (linear) to the SPU's 44.1 kHz and queued on its
 * CD input. Self-contained apart from spu.h. */
#include <string.h>

#include "spu.h"

static const int xa_pos[4] = {0, 60, 115, 98};
static const int xa_neg[4] = {0, 0, -52, -55};
static int16_t hist[2][2]; /* per channel: the decoder's last two outputs, kept across sectors */

/* resampler state: position in 16.16 between the last input frame and the next */
static uint32_t rs_pos;
static int16_t rs_prev[2];
static int rs_have_prev;

void psx_cd_xa_reset(void) {
    memset(hist, 0, sizeof hist);
    rs_pos = 0;
    rs_have_prev = 0;
    rs_prev[0] = rs_prev[1] = 0;
}

static int16_t clamp16(int32_t s) { return (int16_t)(s > 32767 ? 32767 : s < -32768 ? -32768 : s); }

/* one sound unit of 28 samples into out (stride: 1 mono, 2 stereo) */
static void unit(const uint8_t *g, int u, int eight, int ch, int16_t *out, int stride) {
    int param = g[4 + u], shift = param & 15, filter = (param >> 4) & 3, j;
    int16_t *h = hist[ch];
    if (shift > 12) shift = 9;
    for (j = 0; j < 28; j++) {
        int32_t s;
        if (eight) s = (int32_t)(int16_t)(g[16 + j * 4 + u] << 8) >> shift;
        else s = (int32_t)(int16_t)(((g[16 + j * 4 + u / 2] >> ((u & 1) * 4)) & 15) << 12) >> shift;
        s += (h[0] * xa_pos[filter] + h[1] * xa_neg[filter] + 32) >> 6;
        h[1] = h[0];
        h[0] = clamp16(s);
        out[j * stride] = h[0];
    }
}

int xa_decode_sector(const uint8_t *raw, int16_t *out, int *channels, int *rate) {
    uint8_t submode = raw[0x12], coding = raw[0x13];
    int stereo = (coding & 3) == 1, eight = ((coding >> 4) & 3) == 1, units = eight ? 4 : 8;
    int g, u, n = 0;
    if (!(submode & 0x04)) return 0; /* not audio */
    *channels = stereo ? 2 : 1;
    *rate = ((coding >> 2) & 3) == 1 ? 18900 : 37800;
    for (g = 0; g < 18; g++) {
        const uint8_t *grp = raw + 0x18 + g * 128;
        if (stereo) {
            for (u = 0; u < units; u += 2) {
                unit(grp, u, eight, 0, out + n * 2, 2);
                unit(grp, u + 1, eight, 1, out + n * 2 + 1, 2);
                n += 28;
            }
        } else {
            for (u = 0; u < units; u++) {
                unit(grp, u, eight, 0, out + n, 1);
                n += 28;
            }
        }
    }
    return n;
}

/* the most output frames one sector makes: 4032 mono samples at 18.9 kHz, at 44.1 kHz */
#define XA_OUT_MAX 9416

int psx_cd_xa_sector(const uint8_t *raw) {
    static int16_t pcm[4032 * 2], st[XA_OUT_MAX * 2];
    int ch, rate, n, m = 0;
    uint32_t step;
    n = xa_decode_sector(raw, pcm, &ch, &rate);
    if (n <= 0) return 0;
    step = (uint32_t)(((uint64_t)rate << 16) / SPU_RATE); /* input frames per output frame, 16.16 */
    if (!rs_have_prev) {
        rs_prev[0] = pcm[0];
        rs_prev[1] = ch == 2 ? pcm[1] : pcm[0];
        rs_have_prev = 1;
    }
    /* output frames between rs_prev (input -1) and pcm[0..n): position p in [0, n) counted from rs_prev */
    while (m < XA_OUT_MAX) {
        uint32_t idx = rs_pos >> 16, frac = rs_pos & 0xFFFF;
        int16_t a[2], b[2];
        int c;
        if (idx >= (uint32_t)n) break;
        for (c = 0; c < 2; c++) {
            int cc = ch == 2 ? c : 0;
            a[c] = idx ? pcm[(idx - 1) * ch + cc] : rs_prev[c];
            b[c] = pcm[idx * ch + cc];
            st[m * 2 + c] = (int16_t)(a[c] + (((b[c] - a[c]) * (int32_t)frac) >> 16));
        }
        m++;
        rs_pos += step;
    }
    rs_pos -= (uint32_t)n << 16;
    rs_prev[0] = pcm[(n - 1) * ch];
    rs_prev[1] = pcm[(n - 1) * ch + (ch == 2 ? 1 : 0)];
    return spu_cd_push(st, m);
}
