/* A software SPU, written from psx-spx's description of the hardware (see spu.h for the model). 24 voices of
 * 4-bit ADPCM from 512 KB of SPU RAM, ADSR envelopes, the 4.12 pitch counter with linear interpolation between
 * decoded samples, fixed voice/main volumes (a sweep holds the last fixed level), the CD input, KON/KOFF/ENDX and
 * the loop flags, all at the SPU's own 44.1 kHz. Not done: reverb (its registers are kept, nothing is added),
 * noise, pitch modulation, IRQs, the gaussian interpolation table. Self-contained (no port runtime), so a test
 * harness can compile it alone. */
#include <pthread.h>
#include <string.h>

#include "spu.h"

typedef struct {
    SpuEnvelope env;
    uint32_t cur;     /* byte address of the block being played */
    uint32_t counter; /* 4.12 position within the block's 28 samples */
    int16_t hist[2];
    int16_t buf[28];
    int16_t last;     /* the sample before buf[0], for the interpolation */
    uint8_t flags;    /* the current block's loop flags */
    uint8_t keyed;
    uint8_t running;
    uint8_t ignore_loop; /* software set the repeat address after key-on: loop-start flags don't move it */
    uint16_t lsa_seen;
    int vol_cache[2];
} Voice;

static uint16_t own_regs[SPU_REG_BYTES / 2];
static volatile uint16_t *regs = own_regs;
static uint8_t ram[SPU_RAM_SIZE];
static Voice voices[SPU_VOICES];
static int mvol_cache[2];
static uint32_t xfer_ptr;
static void (*tick_fn)(void);
static int tick_hz, tick_acc;

#define CD_QUEUE 32768 /* frames, about 0.74 s */
static int16_t cd_q[CD_QUEUE][2];
static int cd_head, cd_count;

static pthread_mutex_t mtx;
static pthread_once_t mtx_once = PTHREAD_ONCE_INIT;
static void mtx_init(void) {
    pthread_mutexattr_t a;
    pthread_mutexattr_init(&a);
    pthread_mutexattr_settype(&a, PTHREAD_MUTEX_RECURSIVE);
    pthread_mutex_init(&mtx, &a);
    pthread_mutexattr_destroy(&a);
}
void spu_lock(void) {
    pthread_once(&mtx_once, mtx_init);
    pthread_mutex_lock(&mtx);
}
void spu_unlock(void) { pthread_mutex_unlock(&mtx); }

#define R(off) regs[(off) >> 1]
#define VR(v, off) regs[((v) * 16 + (off)) >> 1]

volatile uint16_t *spu_regs(void) { return regs; }
uint16_t spu_read(uint32_t off) { return off < SPU_REG_BYTES ? R(off & ~1u) : 0; }
uint8_t *spu_ram(void) { return ram; }

static uint32_t mask_bits(uint32_t lo_off) { return R(lo_off) | ((uint32_t)(R(lo_off + 2) & 0xFF) << 16); }

/* ---- ADPCM: shift and filter in byte 0, loop flags in byte 1, 28 4-bit samples (low nibble first) */
static const int pos_k[5] = {0, 60, 115, 98, 122};
static const int neg_k[5] = {0, 0, -52, -55, -60};

void spu_adpcm_decode(const uint8_t b[16], int16_t hist[2], int16_t out[28]) {
    int shift = b[0] & 15, filter = (b[0] >> 4) & 7, i;
    if (shift > 12) shift = 9; /* psx-spx: 13-15 act like 9 */
    if (filter > 4) filter = 4;
    for (i = 0; i < 28; i++) {
        int nib = (b[2 + i / 2] >> ((i & 1) * 4)) & 15;
        int32_t s = (int32_t)(int16_t)(nib << 12) >> shift;
        s += (hist[0] * pos_k[filter] + hist[1] * neg_k[filter] + 32) >> 6;
        if (s > 32767) s = 32767;
        if (s < -32768) s = -32768;
        hist[1] = hist[0];
        hist[0] = (int16_t)s;
        out[i] = (int16_t)s;
    }
}

/* ---- the envelope (psx-spx "SPU ADSR"): every AdsrCycles samples, level += AdsrStep, saturated */
void spu_env_step(SpuEnvelope *e, uint16_t a1, uint16_t a2) {
    int exp, dec, shift, step, cycles;
    int32_t s;
    switch (e->phase) {
    case 1: exp = a1 >> 15; dec = 0; shift = (a1 >> 10) & 31; step = 7 - ((a1 >> 8) & 3); break;
    case 2: exp = 1; dec = 1; shift = (a1 >> 4) & 15; step = -8; break;
    case 3:
        exp = a2 >> 15;
        dec = (a2 >> 14) & 1;
        shift = (a2 >> 8) & 31;
        step = dec ? -8 + ((a2 >> 6) & 3) : 7 - ((a2 >> 6) & 3);
        break;
    case 4: exp = (a2 >> 5) & 1; dec = 1; shift = a2 & 31; step = -8; break;
    default: return;
    }
    if (e->wait > 1) {
        e->wait--;
        return;
    }
    cycles = 1 << (shift > 11 ? shift - 11 : 0);
    s = (int32_t)step << (shift < 11 ? 11 - shift : 0);
    if (exp && !dec && e->level > 0x6000) cycles *= 4;
    if (exp && dec) s = (s * e->level) >> 15;
    e->level += s;
    if (e->level > 0x7FFF) e->level = 0x7FFF;
    if (e->level < 0) e->level = 0;
    e->wait = cycles;
    if (e->phase == 1 && e->level >= 0x7FFF) {
        e->phase = 2;
        e->wait = 0;
    } else if (e->phase == 2 && e->level <= (int32_t)(((a1 & 15) + 1) * 0x800)) {
        e->phase = 3;
        e->wait = 0;
    } else if (e->phase == 4 && e->level == 0) {
        e->phase = 0;
    }
}

/* ---- voices */
static void load_block(int v) {
    Voice *x = &voices[v];
    x->cur &= (SPU_RAM_SIZE - 1) & ~7u;
    if (x->cur + 16 > SPU_RAM_SIZE) x->cur = 0;
    spu_adpcm_decode(ram + x->cur, x->hist, x->buf);
    x->flags = ram[x->cur + 1];
    if ((x->flags & 4) && !x->ignore_loop) {
        x->lsa_seen = (uint16_t)(x->cur >> 3);
        VR(v, SPU_V_LSA) = x->lsa_seen;
    }
}

static void key_on(int v) {
    Voice *x = &voices[v];
    x->cur = (uint32_t)VR(v, SPU_V_SSA) << 3;
    x->counter = 0;
    x->hist[0] = x->hist[1] = 0;
    x->last = 0;
    x->ignore_loop = 0;
    x->lsa_seen = VR(v, SPU_V_LSA);
    x->env.phase = 1;
    x->env.level = 0;
    x->env.wait = 0;
    x->keyed = 1;
    x->running = 1;
    VR(v, SPU_V_ADSRVOL) = 0;
    if (v < 16) R(SPU_ENDX) &= (uint16_t)~(1u << v);
    else R(SPU_ENDX + 2) &= (uint16_t)~(1u << (v - 16));
    load_block(v);
}

static void key_off(int v) {
    voices[v].keyed = 0;
    if (voices[v].env.phase) {
        voices[v].env.phase = 4;
        voices[v].env.wait = 0;
    }
}

void spu_key_on(uint32_t mask) {
    int v;
    spu_lock();
    for (v = 0; v < SPU_VOICES; v++)
        if (mask & (1u << v)) key_on(v);
    spu_unlock();
}

void spu_key_off(uint32_t mask) {
    int v;
    spu_lock();
    for (v = 0; v < SPU_VOICES; v++)
        if (mask & (1u << v)) key_off(v);
    spu_unlock();
}

int spu_voice_keyed(int v) { return v >= 0 && v < SPU_VOICES && voices[v].keyed; }
int spu_voice_env(int v) { return v >= 0 && v < SPU_VOICES ? (int)voices[v].env.level : 0; }

static void end_block(int v) {
    Voice *x = &voices[v];
    x->last = x->buf[27];
    if (x->flags & 1) {
        if (v < 16) R(SPU_ENDX) |= (uint16_t)(1u << v);
        else R(SPU_ENDX + 2) |= (uint16_t)(1u << (v - 16));
        x->cur = (uint32_t)VR(v, SPU_V_LSA) << 3;
        if (!(x->flags & 2)) { /* loop end + mute */
            x->env.phase = 4;
            x->env.level = 0;
            x->keyed = 0;
        }
    } else {
        x->cur += 16;
    }
    load_block(v);
}

/* a fixed volume (bit 15 clear: bits 14-0 are volume/2); a sweep holds the last fixed level */
static int vol_level(uint16_t r, int *cache) {
    if (!(r & 0x8000)) *cache = (int16_t)(uint16_t)(r << 1);
    return *cache;
}

/* raw stores into the register file: KON/KOFF consumed (KOFF first), a repeat address software wrote noticed */
static void poll(void) {
    uint32_t kon, koff;
    int v;
    koff = mask_bits(SPU_KOFF);
    if (koff) {
        R(SPU_KOFF) = 0;
        R(SPU_KOFF + 2) = 0;
        for (v = 0; v < SPU_VOICES; v++)
            if (koff & (1u << v)) key_off(v);
    }
    kon = mask_bits(SPU_KON);
    if (kon) {
        R(SPU_KON) = 0;
        R(SPU_KON + 2) = 0;
        for (v = 0; v < SPU_VOICES; v++)
            if (kon & (1u << v)) key_on(v);
    }
    for (v = 0; v < SPU_VOICES; v++) {
        uint16_t lsa = VR(v, SPU_V_LSA);
        if (lsa != voices[v].lsa_seen) {
            voices[v].lsa_seen = lsa;
            voices[v].ignore_loop = 1;
        }
    }
    R(SPU_SPUSTAT) = (uint16_t)((R(SPU_SPUSTAT) & ~0x3Fu) | (R(SPU_SPUCNT) & 0x3F));
}

void spu_write(uint32_t off, uint16_t val) {
    if (off >= SPU_REG_BYTES) return;
    off &= ~1u;
    spu_lock();
    R(off) = val;
    if (off < SPU_VOICES * 16 && (off & 15) == SPU_V_LSA) {
        voices[off >> 4].lsa_seen = val;
        voices[off >> 4].ignore_loop = 1;
    } else if (off == SPU_KON || off == SPU_KON + 2 || off == SPU_KOFF || off == SPU_KOFF + 2) {
        poll();
    } else if (off == SPU_XFER_ADDR) {
        xfer_ptr = (uint32_t)val << 3;
    } else if (off == SPU_XFER_FIFO) {
        ram[xfer_ptr & (SPU_RAM_SIZE - 1)] = (uint8_t)val;
        ram[(xfer_ptr + 1) & (SPU_RAM_SIZE - 1)] = (uint8_t)(val >> 8);
        xfer_ptr += 2;
    } else if (off == SPU_SPUCNT) {
        R(SPU_SPUSTAT) = (uint16_t)((R(SPU_SPUSTAT) & ~0x3Fu) | (val & 0x3F));
    }
    spu_unlock();
}

void spu_ram_write(uint32_t addr, const void *src, uint32_t n) {
    const uint8_t *s = src;
    spu_lock();
    while (n--) ram[addr++ & (SPU_RAM_SIZE - 1)] = *s++;
    spu_unlock();
}

void spu_ram_read(uint32_t addr, void *dst, uint32_t n) {
    uint8_t *d = dst;
    spu_lock();
    while (n--) *d++ = ram[addr++ & (SPU_RAM_SIZE - 1)];
    spu_unlock();
}

void spu_init(void *r) {
    spu_lock();
    regs = r ? (volatile uint16_t *)r : own_regs;
    memset(voices, 0, sizeof voices);
    memset(ram, 0, sizeof ram);
    mvol_cache[0] = mvol_cache[1] = 0;
    cd_head = cd_count = 0;
    spu_unlock();
}

void spu_reset_regs(void) {
    unsigned i;
    spu_lock();
    for (i = 0; i < SPU_REG_BYTES / 2; i++) regs[i] = 0;
    memset(voices, 0, sizeof voices);
    spu_unlock();
}

void spu_set_tick(void (*fn)(void), int hz) {
    spu_lock();
    tick_fn = fn;
    tick_hz = hz > 0 ? hz : 0;
    tick_acc = 0;
    spu_unlock();
}

/* ---- the CD input */
int spu_cd_push(const int16_t *st, int frames) {
    int n = 0;
    spu_lock();
    while (n < frames && cd_count < CD_QUEUE) {
        int at = (cd_head + cd_count) % CD_QUEUE;
        cd_q[at][0] = st[2 * n];
        cd_q[at][1] = st[2 * n + 1];
        cd_count++;
        n++;
    }
    spu_unlock();
    return n;
}
int spu_cd_queued(void) { return cd_count; }
void spu_cd_clear(void) {
    spu_lock();
    cd_head = cd_count = 0;
    spu_unlock();
}

static int16_t clamp16(int32_t s) { return (int16_t)(s > 32767 ? 32767 : s < -32768 ? -32768 : s); }

/* ---- the mixer */
static void mix_frame(int16_t *out) {
    int32_t l = 0, r = 0;
    uint16_t cnt = R(SPU_SPUCNT);
    int v, ml, mr;
    for (v = 0; v < SPU_VOICES; v++) {
        Voice *x = &voices[v];
        uint32_t step;
        int i, frac, s0, s1, s;
        if (!x->running) continue;
        i = (int)(x->counter >> 12);
        frac = (int)(x->counter & 0xFFF);
        s0 = i ? x->buf[i - 1] : x->last;
        s1 = x->buf[i];
        s = s0 + (((s1 - s0) * frac) >> 12);
        s = (s * x->env.level) >> 15;
        l += (s * vol_level(VR(v, SPU_V_VOLL), &x->vol_cache[0])) >> 15;
        r += (s * vol_level(VR(v, SPU_V_VOLR), &x->vol_cache[1])) >> 15;
        spu_env_step(&x->env, VR(v, SPU_V_ADSR1), VR(v, SPU_V_ADSR2));
        VR(v, SPU_V_ADSRVOL) = (uint16_t)x->env.level;
        step = VR(v, SPU_V_PITCH);
        if (step > 0x4000) step = 0x4000;
        x->counter += step;
        while (x->counter >= (28u << 12)) {
            x->counter -= 28u << 12;
            end_block(v);
        }
    }
    if (cnt & SPUCNT_CD_AUDIO) {
        if (cd_count) {
            l += (cd_q[cd_head][0] * (int16_t)R(SPU_CDVOLL)) >> 15;
            r += (cd_q[cd_head][1] * (int16_t)R(SPU_CDVOLR)) >> 15;
        }
    }
    if (cd_count) { /* the CD plays on whether or not it is mixed */
        cd_head = (cd_head + 1) % CD_QUEUE;
        cd_count--;
    }
    ml = vol_level(R(SPU_MVOLL), &mvol_cache[0]);
    mr = vol_level(R(SPU_MVOLR), &mvol_cache[1]);
    R(SPU_CURVOLL) = (uint16_t)ml;
    R(SPU_CURVOLR) = (uint16_t)mr;
    if (!(cnt & SPUCNT_UNMUTE)) {
        out[0] = out[1] = 0;
        return;
    }
    out[0] = clamp16((clamp16(l) * ml) >> 15);
    out[1] = clamp16((clamp16(r) * mr) >> 15);
}

void spu_mix(int16_t *out, int frames) {
    int f;
    spu_lock();
    for (f = 0; f < frames; f++) {
        if ((f & 31) == 0) poll();
        if (tick_fn && tick_hz) {
            tick_acc += tick_hz;
            if (tick_acc >= SPU_RATE) {
                tick_acc -= SPU_RATE;
                tick_fn();
            }
        }
        if (!(R(SPU_SPUCNT) & SPUCNT_ENABLE)) {
            out[2 * f] = out[2 * f + 1] = 0;
            continue;
        }
        mix_frame(out + 2 * f);
    }
    spu_unlock();
}
