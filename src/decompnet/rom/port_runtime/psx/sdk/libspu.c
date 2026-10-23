/* libspu over the software SPU (psx/spu.c): each function does what Sony's does to the SPU's registers and RAM,
 * so the matched C that also reads or writes the registers at 0x1F801C00 directly sees the same state. The
 * function surface and the voice setter shortcuts follow PsyCross's src/psx/LIBSPU.C (MIT, Copyright (c) 2020
 * REDRIVER2 Project, psx/sdk/LICENSE.PsyCross); PsyCross plays through OpenAL with no ADSR, so the register
 * semantics, SpuMalloc and the transfer path are this port's own. Every Sony-named function is a weak definition:
 * a game that has one as matched C keeps its own. Pointers from the game are PS1 addresses (PSX_PTR); libsnd.c
 * calls the psx_spu_* forms on host pointers instead (psx_sound.h). */
#include <math.h>
#include <string.h>

#include "../psx_runtime.h"
#include "../spu.h"
#include "psx_sound.h"

#define SPU_IO 0x1F801C00u
#define MALLOC_START 0x1010u
#define MALLOC_MAX 64

static int started;
static uint32_t xfer_addr = MALLOC_START;
static int xfer_mode;
static uint32_t transfer_cb, irq_cb, data_cb, spu_cb; /* PS1 addresses of the game's callbacks */
static uint16_t sample_note[SPU_VOICES];
static int rev_mode, rev_reserved;
void *psx_spu_io;

/* ---- the SPU in the window */
void psx_spu_start(void) {
    int v;
    if (started) return;
    started = 1;
    spu_init(psx_spu_io ? psx_spu_io : psx_host(SPU_IO));
    spu_reset_regs();
    spu_write(SPU_SPUCNT, SPUCNT_ENABLE | SPUCNT_UNMUTE);
    /* as libspu does: a silent looping block at 0x1000 for voices with nothing better to play */
    {
        uint8_t blk[16] = {0, 0x07};
        spu_ram_write(0x1000, blk, 16);
    }
    for (v = 0; v < SPU_VOICES; v++) {
        spu_write(v * 16 + SPU_V_SSA, 0x1000 >> 3);
        sample_note[v] = 60 << 8;
    }
    psx_spu_init_malloc(MALLOC_MAX);
    psx_log("spu: software SPU at 0x%08X (512 KB SPU RAM, 24 voices)\n", SPU_IO);
    psx_audio_start();
}

uint16_t psx_spu_note_pitch(uint16_t center, uint16_t note) {
    double semis = (double)((note >> 8) - (center >> 8)) + ((double)(note & 0xFF) - (double)(center & 0xFF)) / 128.0;
    double p = 4096.0 * exp2(semis / 12.0);
    return (uint16_t)(p > 0x3FFF ? 0x3FFF : p < 0 ? 0 : p);
}

/* ---- voice attributes */
static uint16_t vol_reg(int16_t vol, int16_t mode) {
    /* SPU_VOICE_DIRECT16 (0): fixed; 1-7 the sweep modes (bit 15 set, then exp/decrease/phase, the rate in 6-0) */
    static const uint16_t sweep[8] = {0, 0x8000, 0x9000, 0xA000, 0xB000, 0xC000, 0xD000, 0xE000};
    if (mode <= 0 || mode > 7) return (uint16_t)vol & 0x7FFF;
    return (uint16_t)(sweep[mode] | ((uint16_t)vol & 0x7F));
}

static int sweep_mode(uint16_t r) {
    if (!(r & 0x8000)) return 0;
    return 1 + ((r >> 12) & 7);
}

void psx_spu_set_voice_attr(const SpuVoiceAttr *a) {
    uint32_t m = a->mask ? a->mask : 0xFFFFFFFFu; /* mask 0: every attribute */
    int v;
    spu_lock();
    for (v = 0; v < SPU_VOICES; v++) {
        uint32_t b = (uint32_t)v * 16;
        uint16_t a1, a2;
        if (!(a->voice & (1u << v))) continue;
        if (m & SPU_VOICE_VOLL)
            spu_write(b + SPU_V_VOLL, vol_reg(a->volume.left, (m & SPU_VOICE_VOLMODEL) ? a->volmode.left : 0));
        if (m & SPU_VOICE_VOLR)
            spu_write(b + SPU_V_VOLR, vol_reg(a->volume.right, (m & SPU_VOICE_VOLMODER) ? a->volmode.right : 0));
        if (m & SPU_VOICE_SAMPLE_NOTE) sample_note[v] = a->sample_note;
        if (m & SPU_VOICE_PITCH) spu_write(b + SPU_V_PITCH, a->pitch);
        if (m & SPU_VOICE_NOTE) spu_write(b + SPU_V_PITCH, psx_spu_note_pitch(sample_note[v], a->note));
        if (m & SPU_VOICE_WDSA) spu_write(b + SPU_V_SSA, (uint16_t)(a->addr >> 3));
        if (m & SPU_VOICE_LSAX) spu_write(b + SPU_V_LSA, (uint16_t)(a->loop_addr >> 3));
        a1 = spu_read(b + SPU_V_ADSR1);
        a2 = spu_read(b + SPU_V_ADSR2);
        if (m & SPU_VOICE_ADSR_AMODE)
            a1 = (uint16_t)((a1 & 0x7FFF) | (a->a_mode == SPU_VOICE_EXPIncN || a->a_mode == SPU_VOICE_EXPIncR ? 0x8000 : 0));
        if (m & SPU_VOICE_ADSR_AR) a1 = (uint16_t)((a1 & ~0x7F00) | ((a->ar & 0x7F) << 8));
        if (m & SPU_VOICE_ADSR_DR) a1 = (uint16_t)((a1 & ~0x00F0) | ((a->dr & 0xF) << 4));
        if (m & SPU_VOICE_ADSR_SL) a1 = (uint16_t)((a1 & ~0x000F) | (a->sl & 0xF));
        if (m & SPU_VOICE_ADSR_SMODE) {
            uint16_t sm;
            switch (a->s_mode) {
            case SPU_VOICE_LINEARDecN: case SPU_VOICE_LINEARDecR: sm = 0x4000; break;
            case SPU_VOICE_EXPIncN: case SPU_VOICE_EXPIncR: sm = 0x8000; break;
            case SPU_VOICE_EXPDec: sm = 0xC000; break;
            default: sm = 0; break;
            }
            a2 = (uint16_t)((a2 & 0x3FFF) | sm);
        }
        if (m & SPU_VOICE_ADSR_SR) a2 = (uint16_t)((a2 & ~0x1FC0) | ((a->sr & 0x7F) << 6));
        if (m & SPU_VOICE_ADSR_RMODE) a2 = (uint16_t)((a2 & ~0x0020) | (a->r_mode == SPU_VOICE_EXPDec ? 0x20 : 0));
        if (m & SPU_VOICE_ADSR_RR) a2 = (uint16_t)((a2 & ~0x001F) | (a->rr & 0x1F));
        if (m & SPU_VOICE_ADSR_ADSR1) a1 = a->adsr1;
        if (m & SPU_VOICE_ADSR_ADSR2) a2 = a->adsr2;
        spu_write(b + SPU_V_ADSR1, a1);
        spu_write(b + SPU_V_ADSR2, a2);
    }
    spu_unlock();
}

void psx_spu_get_voice_attr(SpuVoiceAttr *a) {
    int v;
    uint32_t b;
    uint16_t a1, a2, vl, vr;
    for (v = 0; v < SPU_VOICES && !(a->voice & (1u << v)); v++) {}
    if (v == SPU_VOICES) return;
    b = (uint32_t)v * 16;
    vl = spu_read(b + SPU_V_VOLL);
    vr = spu_read(b + SPU_V_VOLR);
    a1 = spu_read(b + SPU_V_ADSR1);
    a2 = spu_read(b + SPU_V_ADSR2);
    a->volume.left = (int16_t)(vl & 0x8000 ? vl & 0x7F : vl);
    a->volume.right = (int16_t)(vr & 0x8000 ? vr & 0x7F : vr);
    a->volmode.left = (int16_t)sweep_mode(vl);
    a->volmode.right = (int16_t)sweep_mode(vr);
    a->volumex.left = a->volume.left;
    a->volumex.right = a->volume.right;
    a->pitch = spu_read(b + SPU_V_PITCH);
    a->note = 0;
    a->sample_note = sample_note[v];
    a->envx = (int16_t)spu_read(b + SPU_V_ADSRVOL);
    a->addr = (uint32_t)spu_read(b + SPU_V_SSA) << 3;
    a->loop_addr = (uint32_t)spu_read(b + SPU_V_LSA) << 3;
    a->a_mode = (a1 & 0x8000) ? SPU_VOICE_EXPIncN : SPU_VOICE_LINEARIncN;
    switch (a2 >> 14) {
    case 0: a->s_mode = SPU_VOICE_LINEARIncN; break;
    case 1: a->s_mode = SPU_VOICE_LINEARDecN; break;
    case 2: a->s_mode = SPU_VOICE_EXPIncN; break;
    default: a->s_mode = SPU_VOICE_EXPDec; break;
    }
    a->r_mode = (a2 & 0x20) ? SPU_VOICE_EXPDec : SPU_VOICE_LINEARDecN;
    a->ar = (a1 >> 8) & 0x7F;
    a->dr = (a1 >> 4) & 0xF;
    a->sl = a1 & 0xF;
    a->sr = (a2 >> 6) & 0x7F;
    a->rr = a2 & 0x1F;
    a->adsr1 = a1;
    a->adsr2 = a2;
}

/* ---- common attributes (main, CD and external volumes; the CD/external mix and reverb switches in SPUCNT) */
static void cnt_bit(uint16_t bit, int on) {
    uint16_t c = spu_read(SPU_SPUCNT);
    spu_write(SPU_SPUCNT, (uint16_t)(on ? c | bit : c & ~bit));
}

void psx_spu_set_common_attr(const SpuCommonAttr *a) {
    uint32_t m = a->mask ? a->mask : 0xFFFFFFFFu;
    spu_lock();
    if (m & SPU_COMMON_MVOLL)
        spu_write(SPU_MVOLL, vol_reg(a->mvol.left, (m & SPU_COMMON_MVOLMODEL) ? a->mvolmode.left : 0));
    if (m & SPU_COMMON_MVOLR)
        spu_write(SPU_MVOLR, vol_reg(a->mvol.right, (m & SPU_COMMON_MVOLMODER) ? a->mvolmode.right : 0));
    if (m & SPU_COMMON_CDVOLL) spu_write(SPU_CDVOLL, (uint16_t)a->cd.volume.left);
    if (m & SPU_COMMON_CDVOLR) spu_write(SPU_CDVOLR, (uint16_t)a->cd.volume.right);
    if (m & SPU_COMMON_CDREV) cnt_bit(SPUCNT_CD_REVERB, a->cd.reverb);
    if (m & SPU_COMMON_CDMIX) cnt_bit(SPUCNT_CD_AUDIO, a->cd.mix);
    if (m & SPU_COMMON_EXTVOLL) spu_write(SPU_EXTVOLL, (uint16_t)a->ext.volume.left);
    if (m & SPU_COMMON_EXTVOLR) spu_write(SPU_EXTVOLR, (uint16_t)a->ext.volume.right);
    if (m & SPU_COMMON_EXTREV) cnt_bit(0x0008, a->ext.reverb);
    if (m & SPU_COMMON_EXTMIX) cnt_bit(0x0002, a->ext.mix);
    spu_unlock();
}

void psx_spu_get_common_attr(SpuCommonAttr *a) {
    uint16_t ml = spu_read(SPU_MVOLL), mr = spu_read(SPU_MVOLR), c = spu_read(SPU_SPUCNT);
    a->mvol.left = (int16_t)(ml & 0x8000 ? ml & 0x7F : ml);
    a->mvol.right = (int16_t)(mr & 0x8000 ? mr & 0x7F : mr);
    a->mvolmode.left = (int16_t)sweep_mode(ml);
    a->mvolmode.right = (int16_t)sweep_mode(mr);
    a->mvolx.left = (int16_t)spu_read(SPU_CURVOLL);
    a->mvolx.right = (int16_t)spu_read(SPU_CURVOLR);
    a->cd.volume.left = (int16_t)spu_read(SPU_CDVOLL);
    a->cd.volume.right = (int16_t)spu_read(SPU_CDVOLR);
    a->cd.reverb = !!(c & SPUCNT_CD_REVERB);
    a->cd.mix = !!(c & SPUCNT_CD_AUDIO);
    a->ext.volume.left = (int16_t)spu_read(SPU_EXTVOLL);
    a->ext.volume.right = (int16_t)spu_read(SPU_EXTVOLR);
    a->ext.reverb = !!(c & 0x0008);
    a->ext.mix = !!(c & 0x0002);
}

/* ---- SPU RAM allocation: SpuInitMalloc's table is the host's (the game's table in RAM isn't read), the same
 * rules: from 0x1010 to the reverb work area (or the end of SPU RAM), 16-byte blocks, first fit */
typedef struct { uint32_t addr, size; } Block;
static Block used[MALLOC_MAX];
static int n_used, max_blocks = MALLOC_MAX;

static uint32_t rev_size(int mode) {
    static const uint32_t sz[SPU_REV_MODE_MAX] = {0x10, 0x26C0, 0x1F40, 0x4840, 0x6FE0, 0xADE0, 0xF6C0,
                                                  0x18040, 0x18040, 0x3C00};
    return mode >= 0 && mode < SPU_REV_MODE_MAX ? sz[mode] : 0x10;
}
static uint32_t malloc_end(void) { return rev_reserved ? SPU_RAM_SIZE - rev_size(rev_mode) : SPU_RAM_SIZE; }

int psx_spu_init_malloc(int num) {
    n_used = 0;
    max_blocks = num > 0 && num < MALLOC_MAX ? num : MALLOC_MAX;
    return num;
}

static int place(uint32_t addr, uint32_t size) {
    int i, at = n_used;
    if (n_used >= max_blocks) return -1;
    for (i = 0; i < n_used; i++)
        if (used[i].addr > addr) { at = i; break; }
    memmove(&used[at + 1], &used[at], (size_t)(n_used - at) * sizeof used[0]);
    used[at].addr = addr;
    used[at].size = size;
    n_used++;
    return (int)addr;
}

int psx_spu_malloc(int size) {
    uint32_t want = ((uint32_t)size + 15) & ~15u, at = MALLOC_START;
    int i;
    if (size <= 0) return -1;
    for (i = 0; i <= n_used; i++) {
        uint32_t next = i < n_used ? used[i].addr : malloc_end();
        if (next >= at + want) return place(at, want);
        if (i < n_used) at = used[i].addr + used[i].size;
    }
    return -1;
}

int psx_spu_malloc_at(uint32_t addr, int size) {
    uint32_t want = ((uint32_t)size + 15) & ~15u;
    int i;
    if (addr < MALLOC_START || addr + want > malloc_end()) return -1;
    for (i = 0; i < n_used; i++)
        if (addr < used[i].addr + used[i].size && used[i].addr < addr + want) return -1;
    return place(addr, want);
}

void psx_spu_free(uint32_t addr) {
    int i;
    for (i = 0; i < n_used; i++)
        if (used[i].addr == addr) {
            memmove(&used[i], &used[i + 1], (size_t)(n_used - i - 1) * sizeof used[0]);
            n_used--;
            return;
        }
}

/* ---- transfers: DMA and I/O both land in SPU RAM at once */
uint32_t psx_spu_set_transfer_addr(uint32_t addr) {
    if (addr < MALLOC_START || addr >= SPU_RAM_SIZE) return 0;
    xfer_addr = addr & ~7u;
    spu_write(SPU_XFER_ADDR, (uint16_t)(xfer_addr >> 3));
    return xfer_addr;
}

uint32_t psx_spu_write(const void *src, uint32_t size) {
    if (xfer_addr + size > SPU_RAM_SIZE) size = SPU_RAM_SIZE - xfer_addr;
    spu_ram_write(xfer_addr, src, size);
    return size;
}

static void transfer_done(void) {
    if (transfer_cb) ((void (*)(void))psx_host_fn(transfer_cb))();
}

/* ---- reverb: the switches, voices and depths go to the registers; the effect itself isn't mixed yet */
int psx_spu_set_reverb(int on) {
    cnt_bit(SPUCNT_REVERB, on == SPU_ON);
    return on;
}
uint32_t psx_spu_set_reverb_voice(int on, uint32_t bits) {
    uint32_t e = spu_read(SPU_EON) | ((uint32_t)spu_read(SPU_EON + 2) << 16);
    e = on == SPU_ON ? e | bits : on == SPU_OFF ? e & ~bits : on == SPU_BIT ? bits : e;
    e &= 0xFFFFFF;
    spu_write(SPU_EON, (uint16_t)e);
    spu_write(SPU_EON + 2, (uint16_t)(e >> 16));
    return e;
}
void psx_spu_set_reverb_depth(int16_t l, int16_t r) {
    spu_write(SPU_RVOLL, (uint16_t)l);
    spu_write(SPU_RVOLR, (uint16_t)r);
}
int psx_spu_set_reverb_mode(int mode) {
    mode &= ~SPU_REV_MODE_CLEAR_WA;
    if (mode < 0 || mode >= SPU_REV_MODE_MAX) return -1;
    rev_mode = mode;
    spu_write(SPU_RVB_BASE, (uint16_t)((SPU_RAM_SIZE - rev_size(mode)) >> 3));
    return 0;
}

/* ================================================================ the Sony entry points (the game's pointers) */
PSX_SDK_WEAK void SpuInit(void) { psx_spu_start(); }
PSX_SDK_WEAK void SpuInitHot(void) { psx_spu_start(); }
PSX_SDK_WEAK void SpuStart(void) { psx_spu_start(); }
PSX_SDK_WEAK void SpuQuit(void) { spu_key_off(0xFFFFFF); }

PSX_SDK_WEAK int SpuSetMute(int on_off) {
    cnt_bit(SPUCNT_UNMUTE, on_off != SPU_ON);
    return on_off;
}
PSX_SDK_WEAK int SpuGetMute(void) { return (spu_read(SPU_SPUCNT) & SPUCNT_UNMUTE) ? SPU_OFF : SPU_ON; }
PSX_SDK_WEAK void SpuSetEnv(SpuEnv *env) { (void)env; }

PSX_SDK_WEAK int SpuSetNoiseClock(int n) {
    uint16_t c = spu_read(SPU_SPUCNT);
    spu_write(SPU_SPUCNT, (uint16_t)((c & ~0x3F00) | ((n & 0x3F) << 8)));
    return n & 0x3F;
}
PSX_SDK_WEAK int SpuGetNoiseClock(void) { return (spu_read(SPU_SPUCNT) >> 8) & 0x3F; }

static uint32_t set_bits(uint32_t lo_off, int on, uint32_t bits) {
    uint32_t e = spu_read(lo_off) | ((uint32_t)spu_read(lo_off + 2) << 16);
    e = on == SPU_ON ? e | bits : on == SPU_OFF ? e & ~bits : on == SPU_BIT ? bits : e;
    e &= 0xFFFFFF;
    spu_write(lo_off, (uint16_t)e);
    spu_write(lo_off + 2, (uint16_t)(e >> 16));
    return e;
}
PSX_SDK_WEAK unsigned int SpuSetNoiseVoice(int on_off, unsigned int bits) { return set_bits(SPU_NON, on_off, bits); }
PSX_SDK_WEAK unsigned int SpuGetNoiseVoice(void) { return set_bits(SPU_NON, -1, 0); }
PSX_SDK_WEAK unsigned int SpuSetPitchLFOVoice(int on_off, unsigned int bits) { return set_bits(SPU_PMON, on_off, bits); }
PSX_SDK_WEAK unsigned int SpuGetPitchLFOVoice(void) { return set_bits(SPU_PMON, -1, 0); }

PSX_SDK_WEAK int SpuSetReverb(int on_off) { return psx_spu_set_reverb(on_off); }
PSX_SDK_WEAK int SpuGetReverb(void) { return (spu_read(SPU_SPUCNT) & SPUCNT_REVERB) ? SPU_ON : SPU_OFF; }
PSX_SDK_WEAK int SpuSetReverbModeParam(SpuReverbAttr *p) {
    SpuReverbAttr a;
    memcpy(&a, PSX_PTR(p), sizeof a);
    if (!a.mask || (a.mask & SPU_REV_MODE)) psx_spu_set_reverb_mode(a.mode);
    if (!a.mask || (a.mask & SPU_REV_DEPTHL) || (a.mask & SPU_REV_DEPTHR))
        psx_spu_set_reverb_depth(a.depth.left, a.depth.right);
    return 0;
}
PSX_SDK_WEAK void SpuGetReverbModeParam(SpuReverbAttr *p) {
    SpuReverbAttr a;
    memset(&a, 0, sizeof a);
    a.mode = rev_mode;
    a.depth.left = (int16_t)spu_read(SPU_RVOLL);
    a.depth.right = (int16_t)spu_read(SPU_RVOLR);
    memcpy(PSX_PTR(p), &a, sizeof a);
}
PSX_SDK_WEAK int SpuSetReverbDepth(SpuReverbAttr *p) {
    SpuReverbAttr a;
    memcpy(&a, PSX_PTR(p), sizeof a);
    psx_spu_set_reverb_depth(a.depth.left, a.depth.right);
    return 0;
}
PSX_SDK_WEAK int SpuReserveReverbWorkArea(int on_off) {
    rev_reserved = on_off == SPU_ON;
    return on_off;
}
PSX_SDK_WEAK int SpuIsReverbWorkAreaReserved(int on_off) { (void)on_off; return rev_reserved ? SPU_ON : SPU_OFF; }
PSX_SDK_WEAK unsigned int SpuSetReverbVoice(int on_off, unsigned int bits) { return psx_spu_set_reverb_voice(on_off, bits); }
PSX_SDK_WEAK unsigned int SpuGetReverbVoice(void) { return set_bits(SPU_EON, -1, 0); }
PSX_SDK_WEAK int SpuClearReverbWorkArea(int mode) {
    uint32_t n = rev_size(mode), at = SPU_RAM_SIZE - n;
    static const uint8_t zero[256];
    while (n) {
        uint32_t k = n < sizeof zero ? n : (uint32_t)sizeof zero;
        spu_ram_write(at, zero, k);
        at += k;
        n -= k;
    }
    return 0;
}
PSX_SDK_WEAK int SpuSetReverbModeType(int mode) { return psx_spu_set_reverb_mode(mode); }
PSX_SDK_WEAK void SpuSetReverbModeDepth(short l, short r) { psx_spu_set_reverb_depth(l, r); }
PSX_SDK_WEAK void SpuSetReverbModeDelayTime(int delay) { (void)delay; }
PSX_SDK_WEAK void SpuSetReverbModeFeedback(int feedback) { (void)feedback; }

PSX_SDK_WEAK unsigned int SpuWrite(unsigned char *addr, unsigned int size) {
    size = psx_spu_write(PSX_PTR(addr), size);
    transfer_done();
    return size;
}
PSX_SDK_WEAK unsigned int SpuWrite0(unsigned int size) {
    static const uint8_t zero[256];
    uint32_t n = size, at = xfer_addr;
    while (n && at < SPU_RAM_SIZE) {
        uint32_t k = n < sizeof zero ? n : (uint32_t)sizeof zero;
        spu_ram_write(at, zero, k);
        at += k;
        n -= k;
    }
    transfer_done();
    return size;
}
PSX_SDK_WEAK unsigned int SpuWritePartly(unsigned char *addr, unsigned int size) {
    size = psx_spu_write(PSX_PTR(addr), size);
    xfer_addr += size;
    transfer_done();
    return size;
}
PSX_SDK_WEAK unsigned int SpuRead(unsigned char *addr, unsigned int size) {
    if (xfer_addr + size > SPU_RAM_SIZE) size = SPU_RAM_SIZE - xfer_addr;
    spu_ram_read(xfer_addr, PSX_PTR(addr), size);
    transfer_done();
    return size;
}
PSX_SDK_WEAK int SpuSetTransferMode(int mode) {
    xfer_mode = mode == SPU_TRANSFER_BY_IO ? SPU_TRANSFER_BY_IO : SPU_TRANSFER_BY_DMA;
    return xfer_mode;
}
PSX_SDK_WEAK int SpuGetTransferMode(void) { return xfer_mode; }
PSX_SDK_WEAK unsigned int SpuSetTransferStartAddr(unsigned int addr) { return psx_spu_set_transfer_addr(addr); }
PSX_SDK_WEAK unsigned int SpuGetTransferStartAddr(void) { return xfer_addr; }
PSX_SDK_WEAK int SpuIsTransferCompleted(int flag) { (void)flag; return 1; }
PSX_SDK_WEAK SpuTransferCallbackProc SpuSetTransferCallback(SpuTransferCallbackProc func) {
    uint32_t old = transfer_cb;
    transfer_cb = (uint32_t)(uintptr_t)func;
    return (SpuTransferCallbackProc)(uintptr_t)old;
}
PSX_SDK_WEAK int SpuReadDecodedData(SpuDecodedData *d, int flag) {
    (void)flag;
    memset(PSX_PTR(d), 0, sizeof(SpuDecodedData));
    return SPU_DECODED_FIRSTHALF;
}

PSX_SDK_WEAK int SpuSetIRQ(int on_off) {
    cnt_bit(0x0040, on_off == SPU_ON);
    return on_off;
}
PSX_SDK_WEAK int SpuGetIRQ(void) { return (spu_read(SPU_SPUCNT) & 0x40) ? SPU_ON : SPU_OFF; }
PSX_SDK_WEAK unsigned int SpuSetIRQAddr(unsigned int addr) {
    spu_write(SPU_IRQ_ADDR, (uint16_t)(addr >> 3));
    return addr & ~7u;
}
PSX_SDK_WEAK unsigned int SpuGetIRQAddr(void) { return (uint32_t)spu_read(SPU_IRQ_ADDR) << 3; }
PSX_SDK_WEAK SpuIRQCallbackProc SpuSetIRQCallback(SpuIRQCallbackProc f) {
    uint32_t old = irq_cb;
    irq_cb = (uint32_t)(uintptr_t)f;
    return (SpuIRQCallbackProc)(uintptr_t)old;
}
/* libetc's CALLBACK.OBJ: the SPU's DMA and IRQ callbacks, kept (nothing raises them: transfers finish at once) */
PSX_SDK_WEAK void *SpuDataCallback(void *f) {
    uint32_t old = data_cb;
    data_cb = (uint32_t)(uintptr_t)f;
    return (void *)(uintptr_t)old;
}
PSX_SDK_WEAK void *SpuCallback(void *f) {
    uint32_t old = spu_cb;
    spu_cb = (uint32_t)(uintptr_t)f;
    return (void *)(uintptr_t)old;
}

PSX_SDK_WEAK void SpuSetVoiceAttr(SpuVoiceAttr *arg) {
    SpuVoiceAttr a;
    memcpy(&a, PSX_PTR(arg), sizeof a);
    psx_spu_set_voice_attr(&a);
}
PSX_SDK_WEAK void SpuGetVoiceAttr(SpuVoiceAttr *arg) {
    SpuVoiceAttr a;
    memcpy(&a, PSX_PTR(arg), sizeof a);
    psx_spu_get_voice_attr(&a);
    memcpy(PSX_PTR(arg), &a, sizeof a);
}
PSX_SDK_WEAK void SpuNSetVoiceAttr(int vNum, SpuVoiceAttr *arg) {
    SpuVoiceAttr a;
    memcpy(&a, PSX_PTR(arg), sizeof a);
    a.voice = SPU_VOICECH(vNum);
    psx_spu_set_voice_attr(&a);
}
PSX_SDK_WEAK void SpuNGetVoiceAttr(int vNum, SpuVoiceAttr *arg) {
    SpuVoiceAttr a;
    memset(&a, 0, sizeof a);
    a.voice = SPU_VOICECH(vNum);
    psx_spu_get_voice_attr(&a);
    memcpy(PSX_PTR(arg), &a, sizeof a);
}
PSX_SDK_WEAK int SpuRSetVoiceAttr(int min_, int max_, SpuVoiceAttr *arg) {
    SpuVoiceAttr a;
    int v;
    memcpy(&a, PSX_PTR(arg), sizeof a);
    for (v = min_; v <= max_ && v < SPU_VOICES; v++)
        if (v >= 0 && (a.voice & SPU_VOICECH(v))) {
            SpuVoiceAttr one = a;
            one.voice = SPU_VOICECH(v);
            psx_spu_set_voice_attr(&one);
        }
    return SPU_SUCCESS;
}
PSX_SDK_WEAK void SpuLSetVoiceAttr(int num, SpuLVoiceAttr *list) {
    SpuLVoiceAttr *l = PSX_PTR(list);
    int i;
    for (i = 0; i < num; i++) {
        SpuLVoiceAttr e;
        memcpy(&e, &l[i], sizeof e);
        e.attr.voice = SPU_VOICECH(e.voiceNum);
        psx_spu_set_voice_attr(&e.attr);
    }
}

PSX_SDK_WEAK void SpuSetKey(int on_off, unsigned int voice_bit) {
    if (on_off == SPU_ON) spu_key_on(voice_bit & 0xFFFFFF);
    else spu_key_off(voice_bit & 0xFFFFFF);
}
PSX_SDK_WEAK void SpuSetKeyOnWithAttr(SpuVoiceAttr *attr) {
    SpuVoiceAttr a;
    memcpy(&a, PSX_PTR(attr), sizeof a);
    psx_spu_set_voice_attr(&a);
    spu_key_on(a.voice & 0xFFFFFF);
}
static int key_status(int v) {
    int on = spu_voice_keyed(v), env = spu_voice_env(v) > 0;
    return on ? (env ? SPU_ON : SPU_ON_ENV_OFF) : (env ? SPU_OFF_ENV_ON : SPU_OFF);
}
PSX_SDK_WEAK int SpuGetKeyStatus(unsigned int voice_bit) {
    int v;
    for (v = 0; v < SPU_VOICES; v++)
        if (voice_bit == SPU_VOICECH(v)) return key_status(v);
    return -1;
}
PSX_SDK_WEAK void SpuGetAllKeysStatus(char *status) {
    char *s = PSX_PTR(status);
    int v;
    for (v = 0; v < SPU_VOICES; v++) s[v] = (char)key_status(v);
}
PSX_SDK_WEAK int SpuRGetAllKeysStatus(int min_, int max_, char *status) {
    char *s = PSX_PTR(status);
    int v;
    for (v = min_; v <= max_ && v < SPU_VOICES; v++)
        if (v >= 0) s[v - min_] = (char)key_status(v);
    return SPU_SUCCESS;
}
PSX_SDK_WEAK unsigned int SpuFlush(unsigned int ev) { (void)ev; return 0; }

PSX_SDK_WEAK void SpuSetCommonAttr(SpuCommonAttr *attr) {
    SpuCommonAttr a;
    memcpy(&a, PSX_PTR(attr), sizeof a);
    psx_spu_set_common_attr(&a);
}
PSX_SDK_WEAK void SpuGetCommonAttr(SpuCommonAttr *attr) {
    SpuCommonAttr a;
    psx_spu_get_common_attr(&a);
    memcpy(PSX_PTR(attr), &a, sizeof a);
}
PSX_SDK_WEAK void SpuSetCommonMasterVolume(short l, short r) {
    spu_write(SPU_MVOLL, (uint16_t)l & 0x7FFF);
    spu_write(SPU_MVOLR, (uint16_t)r & 0x7FFF);
}
PSX_SDK_WEAK void SpuSetCommonMasterVolumeAttr(short l, short r, short ml, short mr) {
    spu_write(SPU_MVOLL, vol_reg(l, ml));
    spu_write(SPU_MVOLR, vol_reg(r, mr));
}
PSX_SDK_WEAK void SpuSetCommonCDMix(int on) { cnt_bit(SPUCNT_CD_AUDIO, on == SPU_ON); }
PSX_SDK_WEAK void SpuSetCommonCDVolume(short l, short r) {
    spu_write(SPU_CDVOLL, (uint16_t)l);
    spu_write(SPU_CDVOLR, (uint16_t)r);
}
PSX_SDK_WEAK void SpuSetCommonCDReverb(int on) { cnt_bit(SPUCNT_CD_REVERB, on == SPU_ON); }
PSX_SDK_WEAK void SpuGetCommonMasterVolume(short *l, short *r) {
    int16_t a = (int16_t)spu_read(SPU_MVOLL), b = (int16_t)spu_read(SPU_MVOLR);
    memcpy(PSX_PTR(l), &a, 2);
    memcpy(PSX_PTR(r), &b, 2);
}

PSX_SDK_WEAK int SpuInitMalloc(int num, char *top) { (void)top; return psx_spu_init_malloc(num); }
PSX_SDK_WEAK int SpuMalloc(int size) { return psx_spu_malloc(size); }
PSX_SDK_WEAK int SpuMallocWithStartAddr(unsigned int addr, int size) { return psx_spu_malloc_at(addr, size); }
PSX_SDK_WEAK void SpuFree(unsigned int addr) { psx_spu_free(addr); }

/* ---- the voice shortcuts (as PsyCross's LIBSPU.C builds them: one SpuVoiceAttr with one mask bit) */
static void one(int v, uint32_t mask, SpuVoiceAttr *a) {
    a->voice = SPU_VOICECH(v);
    a->mask = mask;
    psx_spu_set_voice_attr(a);
}
#define ATTR SpuVoiceAttr a; memset(&a, 0, sizeof a)
PSX_SDK_WEAK void SpuSetVoiceVolume(int v, short l, short r) {
    ATTR; a.volume.left = l; a.volume.right = r;
    one(v, SPU_VOICE_VOLL | SPU_VOICE_VOLR, &a);
}
PSX_SDK_WEAK void SpuSetVoiceVolumeAttr(int v, short l, short r, short ml, short mr) {
    ATTR; a.volume.left = l; a.volume.right = r; a.volmode.left = ml; a.volmode.right = mr;
    one(v, SPU_VOICE_VOLL | SPU_VOICE_VOLR | SPU_VOICE_VOLMODEL | SPU_VOICE_VOLMODER, &a);
}
PSX_SDK_WEAK void SpuSetVoicePitch(int v, unsigned short pitch) { ATTR; a.pitch = pitch; one(v, SPU_VOICE_PITCH, &a); }
PSX_SDK_WEAK void SpuSetVoiceNote(int v, unsigned short note) { ATTR; a.note = note; one(v, SPU_VOICE_NOTE, &a); }
PSX_SDK_WEAK void SpuSetVoiceSampleNote(int v, unsigned short n) { ATTR; a.sample_note = n; one(v, SPU_VOICE_SAMPLE_NOTE, &a); }
PSX_SDK_WEAK void SpuSetVoiceStartAddr(int v, unsigned int addr) { ATTR; a.addr = addr; one(v, SPU_VOICE_WDSA, &a); }
PSX_SDK_WEAK void SpuSetVoiceLoopStartAddr(int v, unsigned int lsa) { ATTR; a.loop_addr = lsa; one(v, SPU_VOICE_LSAX, &a); }
PSX_SDK_WEAK void SpuSetVoiceAR(int v, unsigned short x) { ATTR; a.ar = x; one(v, SPU_VOICE_ADSR_AR, &a); }
PSX_SDK_WEAK void SpuSetVoiceDR(int v, unsigned short x) { ATTR; a.dr = x; one(v, SPU_VOICE_ADSR_DR, &a); }
PSX_SDK_WEAK void SpuSetVoiceSR(int v, unsigned short x) { ATTR; a.sr = x; one(v, SPU_VOICE_ADSR_SR, &a); }
PSX_SDK_WEAK void SpuSetVoiceRR(int v, unsigned short x) { ATTR; a.rr = x; one(v, SPU_VOICE_ADSR_RR, &a); }
PSX_SDK_WEAK void SpuSetVoiceSL(int v, unsigned short x) { ATTR; a.sl = x; one(v, SPU_VOICE_ADSR_SL, &a); }
PSX_SDK_WEAK void SpuSetVoiceARAttr(int v, unsigned short x, int mode) {
    ATTR; a.ar = x; a.a_mode = mode; one(v, SPU_VOICE_ADSR_AR | SPU_VOICE_ADSR_AMODE, &a);
}
PSX_SDK_WEAK void SpuSetVoiceSRAttr(int v, unsigned short x, int mode) {
    ATTR; a.sr = x; a.s_mode = mode; one(v, SPU_VOICE_ADSR_SR | SPU_VOICE_ADSR_SMODE, &a);
}
PSX_SDK_WEAK void SpuSetVoiceRRAttr(int v, unsigned short x, int mode) {
    ATTR; a.rr = x; a.r_mode = mode; one(v, SPU_VOICE_ADSR_RR | SPU_VOICE_ADSR_RMODE, &a);
}
PSX_SDK_WEAK void SpuSetVoiceADSR(int v, unsigned short ar, unsigned short dr, unsigned short sr, unsigned short rr,
                                  unsigned short sl) {
    ATTR; a.ar = ar; a.dr = dr; a.sr = sr; a.rr = rr; a.sl = sl;
    one(v, SPU_VOICE_ADSR_AR | SPU_VOICE_ADSR_DR | SPU_VOICE_ADSR_SR | SPU_VOICE_ADSR_RR | SPU_VOICE_ADSR_SL, &a);
}
PSX_SDK_WEAK void SpuSetVoiceADSRAttr(int v, unsigned short ar, unsigned short dr, unsigned short sr,
                                      unsigned short rr, unsigned short sl, int am, int sm, int rm) {
    ATTR; a.ar = ar; a.dr = dr; a.sr = sr; a.rr = rr; a.sl = sl; a.a_mode = am; a.s_mode = sm; a.r_mode = rm;
    one(v, SPU_VOICE_ADSR_AR | SPU_VOICE_ADSR_DR | SPU_VOICE_ADSR_SR | SPU_VOICE_ADSR_RR | SPU_VOICE_ADSR_SL |
               SPU_VOICE_ADSR_AMODE | SPU_VOICE_ADSR_SMODE | SPU_VOICE_ADSR_RMODE, &a);
}
static void get_one(int v, SpuVoiceAttr *a) {
    memset(a, 0, sizeof *a);
    a->voice = SPU_VOICECH(v);
    psx_spu_get_voice_attr(a);
}
#define PUT(p, val) do { __typeof__(*(p)) t_ = (val); memcpy(PSX_PTR(p), &t_, sizeof t_); } while (0)
PSX_SDK_WEAK void SpuGetVoiceVolume(int v, short *l, short *r) {
    SpuVoiceAttr a; get_one(v, &a); PUT(l, a.volume.left); PUT(r, a.volume.right);
}
PSX_SDK_WEAK void SpuGetVoiceVolumeX(int v, short *l, short *r) {
    SpuVoiceAttr a; get_one(v, &a); PUT(l, a.volumex.left); PUT(r, a.volumex.right);
}
PSX_SDK_WEAK void SpuGetVoicePitch(int v, unsigned short *p) { SpuVoiceAttr a; get_one(v, &a); PUT(p, a.pitch); }
PSX_SDK_WEAK void SpuGetVoiceEnvelope(int v, short *e) { SpuVoiceAttr a; get_one(v, &a); PUT(e, a.envx); }
PSX_SDK_WEAK void SpuGetVoiceStartAddr(int v, unsigned int *x) { SpuVoiceAttr a; get_one(v, &a); PUT(x, a.addr); }
PSX_SDK_WEAK void SpuGetVoiceLoopStartAddr(int v, unsigned int *x) {
    SpuVoiceAttr a; get_one(v, &a); PUT(x, a.loop_addr);
}
PSX_SDK_WEAK void SpuGetVoiceEnvelopeAttr(int v, int *key, short *e) {
    SpuVoiceAttr a; get_one(v, &a); PUT(key, key_status(v)); PUT(e, a.envx);
}
