/* libsnd on the host: the VAB sound banks, the SEQ sequencer and the voice manager, over libspu.c and the
 * software SPU. The port replaces the game's libsnd wholesale (its address range is an "sdk range": every function
 * in it, matched C or not, is the SDK's; docs/plans/port.md has the measurements), so the state here is the host's
 * own: nothing in RAM is libsnd's any more. Written from the formats as the public PSY-Q documentation and
 * psx-spx describe them:
 *
 * - VAB header (SsVabOpenHead): "pBAV", version, id, size, 2 reserved, ps (programs), ts (tones), vs (VAGs), master
 *   volume and pan, then 128 x 16-byte ProgAtr {tones, mvol, prior, mode, mpan, ...}, then 16 x 32-byte VagAtr
 *   {prior, mode, vol, pan, center, shift, min, max, vibW, vibT, porW, porT, pbmin, pbmax, -, -, adsr1, adsr2, prog,
 *   vag} for each program that has tones (in program order), then 256 halfwords: VAG n's size / 8. The body
 *   (SsVabTransBody) is the VAGs back to back, ADPCM, into SPU RAM (SpuMalloc).
 * - SEQ (SsSeqOpen): "pQES", version (big-endian), ticks per quarter note (2 bytes BE), tempo in us per quarter
 *   (3 bytes BE), time signature (2 bytes), then a MIDI event stream from offset 15: a variable-length delta time
 *   before each event, running status, FF 51 + 3 bytes tempo, FF 2F end. Loops: CC 99 = 20 marks a loop start
 *   (CC 6 then gives its count, 0 or 127 forever), CC 99 = 30 its end.
 *
 * The tick (SsSeqCalledTbyT) runs on the audio thread at the tick mode's rate once SsStart/SsStart2 is called,
 * under the SPU's lock; if the game calls SsSeqCalledTbyT itself (its own VSync callback), the audio thread stops
 * ticking and the game's calls drive it. Pitch: 0x1000 plays a tone's VAG at 44.1 kHz at its center note (center
 * plus shift/128 semitones); volume and pan combine the note's velocity, the tone's, the program's, the VAB's, the
 * channel's volume/expression/pan and the sequence's volume. Not done: vibrato, portamento, SEP files, ritardando,
 * the per-program priority beyond voice stealing. Every Sony-named function is weak (the game's own wins). */
#include <math.h>
#include <stdlib.h>
#include <string.h>

#include "../psx_runtime.h"
#include "../spu.h"
#include "libsnd.h"
#include "psx_sound.h"

#define N_VABS 16
#define N_SEQS 32
#define LOGS 12

typedef struct {
    uint8_t prior, mode, vol, pan, center, shift, min, max, pbmin, pbmax;
    uint16_t adsr1, adsr2;
    int16_t vag;
} Tone;

typedef struct {
    uint8_t tones, mvol, prior, mode, mpan;
    int first; /* index of its first tone in Vab.tone, or -1 */
} Prog;

typedef struct {
    int used, body;
    uint8_t mvol, pan;
    Prog prog[128];
    Tone *tone;
    int ntones;
    uint32_t vag_addr[256];
    int nvags;
    uint32_t spu_addr, size;
} Vab;

typedef struct {
    uint8_t prog, vol, pan, expr;
    int bend; /* -8192..8191 */
} Channel;

typedef struct {
    int used, playing, paused, eos;
    int vab;
    const uint8_t *data;
    uint32_t start, pos, end; /* end: a bound, the data's size isn't known */
    uint32_t tempo, wait;
    uint16_t reso;
    uint64_t acc;
    uint8_t status;
    int plays_left; /* 0: forever */
    int loop_active, loop_count, nrpn;
    uint32_t loop_pos;
    int voll, volr;
    Channel ch[16];
} Seq;

typedef struct {
    int busy;     /* given to a note (until its release is over) */
    int seq, ch;  /* -1: a sound effect (SsVoKeyOn/SsUtKeyOn) */
    int note, fine, vel, vab, prog, tone;
    int voll, volr; /* for sound effects: their own volume, 0..127 */
    uint32_t age;
} VVoice;

static Vab vabs[N_VABS];
static Seq seqs[N_SEQS];
static VVoice vv[SPU_VOICES];
static int n_seqs = N_SEQS, reserved_voices = SPU_VOICES, mono, tick_hz = 60, game_ticks;
static uint32_t clock_age;
static int logged;

#define LOG(...) do { if (logged++ < LOGS * 4) psx_log(__VA_ARGS__); } while (0)

static uint16_t rd16(const uint8_t *p) { return (uint16_t)(p[0] | p[1] << 8); }

/* ---------------------------------------------------------------- VABs */
int psx_snd_vab_open_head(const uint8_t *h, int vabid) {
    Vab *b;
    int ps, vs, i, k = 0, ntones = 0;
    uint32_t total = 0, off;
    const uint8_t *sizes;
    if (!h || memcmp(h, "pBAV", 4) != 0) return -1;
    spu_lock();
    if (vabid < 0)
        for (vabid = 0; vabid < N_VABS && vabs[vabid].used; vabid++) {}
    if (vabid >= N_VABS || vabs[vabid].used) {
        spu_unlock();
        return -1;
    }
    b = &vabs[vabid];
    memset(b, 0, sizeof *b);
    ps = rd16(h + 18);
    vs = rd16(h + 22);
    b->mvol = h[24];
    b->pan = h[25];
    if (ps > 128) ps = 128;
    if (vs > 255) vs = 255;
    for (i = 0; i < 128; i++)
        if (h[32 + i * 16] && k < ps) {
            k++;
            ntones += 16;
        }
    b->tone = calloc((size_t)(ntones ? ntones : 1), sizeof(Tone));
    k = 0;
    for (i = 0; i < 128; i++) {
        const uint8_t *p = h + 32 + i * 16;
        Prog *g = &b->prog[i];
        g->tones = p[0];
        g->mvol = p[1];
        g->prior = p[2];
        g->mode = p[3];
        g->mpan = p[4];
        g->first = -1;
        if (g->tones && k < ps) {
            int t;
            g->first = k * 16;
            if (g->tones > 16) g->tones = 16;
            for (t = 0; t < 16; t++) {
                const uint8_t *a = h + 32 + 2048 + (k * 16 + t) * 32;
                Tone *x = &b->tone[k * 16 + t];
                x->prior = a[0]; x->mode = a[1]; x->vol = a[2]; x->pan = a[3];
                x->center = a[4]; x->shift = a[5]; x->min = a[6]; x->max = a[7];
                x->pbmin = a[12]; x->pbmax = a[13];
                x->adsr1 = rd16(a + 16); x->adsr2 = rd16(a + 18);
                x->vag = (int16_t)rd16(a + 22);
            }
            k++;
        }
    }
    b->ntones = ntones;
    sizes = h + 32 + 2048 + ps * 512;
    for (i = 1; i <= vs; i++) total += (uint32_t)rd16(sizes + i * 2) << 3;
    b->size = total;
    b->nvags = vs;
    b->spu_addr = total ? (uint32_t)psx_spu_malloc((int)total) : 0x1000;
    if (total && b->spu_addr == 0xFFFFFFFFu) {
        free(b->tone);
        memset(b, 0, sizeof *b);
        spu_unlock();
        return -1;
    }
    off = b->spu_addr;
    for (i = 1; i <= vs; i++) {
        b->vag_addr[i] = off;
        off += (uint32_t)rd16(sizes + i * 2) << 3;
    }
    b->used = 1;
    spu_unlock();
    return vabid;
}

int psx_snd_vab_trans_body(const uint8_t *vb, int vabid) {
    Vab *b;
    if (vabid < 0 || vabid >= N_VABS || !vabs[vabid].used || !vb) return -1;
    b = &vabs[vabid];
    spu_lock();
    psx_spu_set_transfer_addr(b->spu_addr);
    psx_spu_write(vb, b->size);
    b->body = 1;
    spu_unlock();
    return vabid;
}

static void voices_off(int (*match)(const VVoice *, int), int arg) {
    uint32_t m = 0;
    int v;
    for (v = 0; v < SPU_VOICES; v++)
        if (vv[v].busy && match(&vv[v], arg)) {
            m |= 1u << v;
            vv[v].busy = 0;
        }
    if (m) spu_key_off(m);
}
static int of_vab(const VVoice *x, int vab) { return x->vab == vab; }
static int of_seq(const VVoice *x, int seq) { return x->seq == seq; }

void psx_snd_vab_close(int vabid) {
    Vab *b;
    if (vabid < 0 || vabid >= N_VABS || !vabs[vabid].used) return;
    spu_lock();
    b = &vabs[vabid];
    voices_off(of_vab, vabid);
    if (b->size) psx_spu_free(b->spu_addr);
    free(b->tone);
    memset(b, 0, sizeof *b);
    spu_unlock();
}

/* ---------------------------------------------------------------- voices */
static int alloc_voice(int prior) {
    int v, best = -1;
    uint32_t oldest = 0xFFFFFFFFu;
    clock_age++;
    for (v = 0; v < reserved_voices; v++) /* free: nothing given, or a release that is over */
        if (!spu_voice_keyed(v) && spu_voice_env(v) == 0) return v;
    for (v = 0; v < reserved_voices; v++) /* releasing: the quietest */
        if (!spu_voice_keyed(v) && (best < 0 || spu_voice_env(v) < spu_voice_env(best))) best = v;
    if (best >= 0) return best;
    for (v = 0; v < reserved_voices; v++) { /* the oldest note no more important than this one */
        int p = vabs[vv[v].vab].used && vv[v].tone >= 0 ? vabs[vv[v].vab].tone[vv[v].tone].prior : 0;
        if (p <= prior && vv[v].age < oldest) {
            oldest = vv[v].age;
            best = v;
        }
    }
    return best;
}

static double clamp01(double x) { return x < 0 ? 0 : x > 1 ? 1 : x; }

static void voice_volume(int v) {
    VVoice *x = &vv[v];
    Vab *b = &vabs[x->vab];
    Tone *t = &b->tone[x->tone];
    Prog *g = &b->prog[x->prog];
    double vol = (x->vel / 127.0) * (t->vol / 127.0) * (g->mvol / 127.0) * (b->mvol / 127.0), l, r;
    int pan = (int)t->pan - 64 + (int)g->mpan - 64 + (int)b->pan - 64 + 64;
    if (x->seq >= 0) {
        Seq *s = &seqs[x->seq];
        Channel *c = &s->ch[x->ch];
        vol *= (c->vol / 127.0) * (c->expr / 127.0);
        pan += (int)c->pan - 64;
        l = vol * (s->voll / 127.0);
        r = vol * (s->volr / 127.0);
    } else {
        l = vol * (x->voll / 127.0);
        r = vol * (x->volr / 127.0);
    }
    if (pan < 0) pan = 0;
    if (pan > 127) pan = 127;
    if (!mono) {
        if (pan > 64) l *= (127 - pan) / 63.0;
        if (pan < 64) r *= pan / 64.0;
    } else {
        l = r = (l + r) / 2;
    }
    spu_write((uint32_t)v * 16 + SPU_V_VOLL, (uint16_t)(clamp01(l) * 0x3FFF));
    spu_write((uint32_t)v * 16 + SPU_V_VOLR, (uint16_t)(clamp01(r) * 0x3FFF));
}

static void voice_pitch(int v) {
    VVoice *x = &vv[v];
    Tone *t = &vabs[x->vab].tone[x->tone];
    double semis = (x->note - t->center) + (x->fine - t->shift) / 128.0, p;
    if (x->seq >= 0) {
        int bend = seqs[x->seq].ch[x->ch].bend;
        semis += bend >= 0 ? bend * t->pbmax / 8192.0 : bend * t->pbmin / 8192.0;
    }
    p = 4096.0 * exp2(semis / 12.0);
    spu_write((uint32_t)v * 16 + SPU_V_PITCH, (uint16_t)(p > 0x3FFF ? 0x3FFF : p));
}

/* key on every tone of (vab, prog) whose range holds the note (or just `only_tone`); the first voice, or -1 */
static int note_on(int vab, int prog, int only_tone, int note, int fine, int vel, int seq, int ch, int voll, int volr) {
    Vab *b;
    Prog *g;
    int t, first = -1;
    uint32_t on = 0;
    if (vab < 0 || vab >= N_VABS || !vabs[vab].used || prog < 0 || prog > 127) return -1;
    b = &vabs[vab];
    g = &b->prog[prog];
    if (g->first < 0) return -1;
    for (t = 0; t < g->tones; t++) {
        Tone *x = &b->tone[g->first + t];
        int v;
        if (only_tone >= 0 ? t != only_tone : (note < x->min || note > x->max)) continue;
        if (x->vag <= 0 || x->vag > b->nvags) continue;
        v = alloc_voice(x->prior);
        if (v < 0) break;
        if (on & (1u << v)) break;
        vv[v].busy = 1;
        vv[v].seq = seq;
        vv[v].ch = ch;
        vv[v].note = note;
        vv[v].fine = fine;
        vv[v].vel = vel;
        vv[v].vab = vab;
        vv[v].prog = prog;
        vv[v].tone = g->first + t;
        vv[v].voll = voll;
        vv[v].volr = volr;
        vv[v].age = clock_age;
        spu_write((uint32_t)v * 16 + SPU_V_SSA, (uint16_t)(b->vag_addr[x->vag] >> 3));
        spu_write((uint32_t)v * 16 + SPU_V_ADSR1, x->adsr1);
        spu_write((uint32_t)v * 16 + SPU_V_ADSR2, x->adsr2);
        voice_volume(v);
        voice_pitch(v);
        psx_spu_set_reverb_voice((x->mode & 4) ? SPU_ON : SPU_OFF, 1u << v);
        spu_key_on(1u << v); /* now, so the next tone's allocation sees this voice taken */
        on |= 1u << v;
        if (first < 0) first = v;
    }
    return first;
}

static void note_off(int seq, int ch, int vab, int prog, int note) {
    uint32_t m = 0;
    int v;
    for (v = 0; v < SPU_VOICES; v++)
        if (vv[v].busy && vv[v].seq == seq && vv[v].ch == ch && vv[v].note == note &&
            (seq >= 0 || (vv[v].vab == vab && vv[v].prog == prog))) {
            m |= 1u << v;
            vv[v].busy = 0;
        }
    if (m) spu_key_off(m);
}

int psx_snd_voice_of(int seq, int ch, int note) {
    int v;
    for (v = 0; v < SPU_VOICES; v++)
        if (vv[v].busy && vv[v].seq == seq && vv[v].ch == ch && vv[v].note == note) return v;
    return -1;
}

/* ---------------------------------------------------------------- SEQ */
static void seq_reset_channels(Seq *s) {
    int c;
    for (c = 0; c < 16; c++) {
        s->ch[c].prog = (uint8_t)c;
        s->ch[c].vol = 127;
        s->ch[c].pan = 64;
        s->ch[c].expr = 127;
        s->ch[c].bend = 0;
    }
}

static uint32_t vlq(Seq *s) {
    uint32_t v = 0;
    int i;
    for (i = 0; i < 4 && s->pos < s->end; i++) {
        uint8_t b = s->data[s->pos++];
        v = (v << 7) | (b & 0x7F);
        if (!(b & 0x80)) break;
    }
    return v;
}

int psx_snd_seq_open(const uint8_t *d, int vabid) {
    int i;
    Seq *s;
    if (!d || memcmp(d, "pQES", 4) != 0) return -1;
    spu_lock();
    for (i = 0; i < n_seqs && seqs[i].used; i++) {}
    if (i >= n_seqs) {
        spu_unlock();
        return -1;
    }
    s = &seqs[i];
    memset(s, 0, sizeof *s);
    s->used = 1;
    s->vab = vabid;
    s->data = d;
    s->reso = (uint16_t)(d[8] << 8 | d[9]);
    s->tempo = (uint32_t)(d[10] << 16 | d[11] << 8 | d[12]);
    if (!s->tempo) s->tempo = 500000;
    if (!s->reso) s->reso = 480;
    s->start = 15;
    s->end = 0x200000; /* SEQs carry no length: the end event ends them */
    s->voll = s->volr = 127;
    seq_reset_channels(s);
    spu_unlock();
    return i;
}

static void seq_rewind(Seq *s) {
    s->pos = s->start;
    s->status = 0;
    s->loop_active = 0;
    s->nrpn = 0;
    s->acc = 0;
    s->wait = vlq(s);
}

void psx_snd_seq_play(int i, int mode, int l_count) {
    Seq *s;
    if (i < 0 || i >= N_SEQS || !seqs[i].used) return;
    spu_lock();
    s = &seqs[i];
    seq_rewind(s);
    s->plays_left = l_count;
    s->eos = 0;
    s->playing = 1;
    s->paused = mode == 0; /* SSPLAY_PAUSE */
    spu_unlock();
}

void psx_snd_seq_stop(int i) {
    if (i < 0 || i >= N_SEQS || !seqs[i].used) return;
    spu_lock();
    seqs[i].playing = 0;
    voices_off(of_seq, i);
    spu_unlock();
}

void psx_snd_seq_set_vol(int i, int l, int r) {
    int v;
    if (i < 0 || i >= N_SEQS || !seqs[i].used) return;
    spu_lock();
    seqs[i].voll = l & 0x7F;
    seqs[i].volr = r & 0x7F;
    for (v = 0; v < SPU_VOICES; v++)
        if (vv[v].busy && vv[v].seq == i) voice_volume(v);
    spu_unlock();
}

static void channel_changed(int i, int ch, int pitch) {
    int v;
    for (v = 0; v < SPU_VOICES; v++)
        if (vv[v].busy && vv[v].seq == i && vv[v].ch == ch) {
            if (pitch) voice_pitch(v);
            else voice_volume(v);
        }
}

static void control(Seq *s, int i, int ch, int cc, int val) {
    Channel *c = &s->ch[ch];
    switch (cc) {
    case 6: /* data entry: the loop count after a loop start */
        if (s->nrpn == 20 && !s->loop_active) {
            s->loop_active = 1;
            s->loop_count = val;
        }
        s->nrpn = 0;
        break;
    case 7: c->vol = (uint8_t)val; channel_changed(i, ch, 0); break;
    case 10: c->pan = (uint8_t)val; channel_changed(i, ch, 0); break;
    case 11: c->expr = (uint8_t)val; channel_changed(i, ch, 0); break;
    case 99:
        if (val == 20) {
            s->loop_pos = s->pos;
            s->nrpn = 20;
        } else if (val == 30 && s->loop_active) {
            if (s->loop_count == 0 || s->loop_count == 127 || --s->loop_count > 0) s->pos = s->loop_pos;
            else s->loop_active = 0;
        }
        break;
    default: break;
    }
}

static void end_of_track(Seq *s, int i) {
    if (s->plays_left == 0 || --s->plays_left > 0) {
        seq_rewind(s); /* the first delta is read: the tick loop goes on from it */
        return;
    }
    s->playing = 0;
    s->eos = 1;
    voices_off(of_seq, i);
}

/* one event at s->pos; 0 when the sequence ended */
static int event(Seq *s, int i) {
    uint8_t b = s->data[s->pos], st, d1 = 0, d2 = 0;
    int ch, type;
    if (b == 0xFF) {
        uint8_t meta = s->data[s->pos + 1];
        s->pos += 2;
        if (meta == 0x51) {
            s->tempo = (uint32_t)(s->data[s->pos] << 16 | s->data[s->pos + 1] << 8 | s->data[s->pos + 2]);
            if (!s->tempo) s->tempo = 500000;
            s->pos += 3;
            return 1;
        }
        end_of_track(s, i); /* 2F, and anything else: no length to skip it by */
        return s->playing ? 2 : 0;
    }
    if (b & 0x80) {
        st = b;
        s->status = b;
        s->pos++;
    } else {
        st = s->status;
    }
    if (!(st & 0x80)) { /* data with no status before it: a broken stream */
        end_of_track(s, i);
        return 0;
    }
    type = st & 0xF0;
    ch = st & 0x0F;
    d1 = s->data[s->pos++];
    if (type != 0xC0 && type != 0xD0) d2 = s->data[s->pos++];
    switch (type) {
    case 0x90:
        if (d2) {
            note_off(i, ch, 0, 0, d1); /* the same note again: retrigger */
            note_on(s->vab, s->ch[ch].prog, -1, d1, 0, d2, i, ch, 127, 127);
            break;
        }
        /* fall through: velocity 0 is a note off */
    case 0x80: note_off(i, ch, 0, 0, d1); break;
    case 0xB0: control(s, i, ch, d1, d2); break;
    case 0xC0: s->ch[ch].prog = d1 & 0x7F; break;
    case 0xE0:
        s->ch[ch].bend = ((d2 << 7) | d1) - 8192;
        channel_changed(i, ch, 1);
        break;
    default: break; /* A0 key pressure, D0 channel pressure */
    }
    return 1;
}

static void midi_tick(Seq *s, int i) {
    int guard = 0;
    while (s->playing && s->wait == 0 && guard++ < 4096) {
        int r = event(s, i);
        if (r == 0) return;
        if (r == 2) continue; /* rewound: the first delta is in s->wait already */
        s->wait = vlq(s);
    }
    if (s->wait) s->wait--;
}

void psx_snd_tick(void) {
    int i;
    spu_lock();
    for (i = 0; i < N_SEQS; i++) {
        Seq *s = &seqs[i];
        uint64_t per = (uint64_t)s->tempo * (uint64_t)tick_hz;
        if (!s->used || !s->playing || s->paused) continue;
        s->acc += (uint64_t)s->reso * 1000000u;
        while (s->acc >= per && s->playing) {
            s->acc -= per;
            midi_tick(s, i);
        }
    }
    spu_unlock();
}

static void auto_tick(void) {
    if (!game_ticks) psx_snd_tick();
}

/* ================================================================ the Sony entry points */
static int tick_rate(int mode) {
    static const int hz[6] = {60, 60, 240, 120, 50, 60}; /* NOTICK0, TICK60, TICK240, TICK120, TICK50, TICKVSYNC */
    mode &= 0xFF;
    return mode >= 0 && mode < 6 ? hz[mode] : 60;
}

PSX_SDK_WEAK void SsInit(void) {
    int i;
    psx_spu_start();
    spu_lock();
    for (i = 0; i < N_VABS; i++)
        if (vabs[i].used) psx_snd_vab_close(i);
    memset(seqs, 0, sizeof seqs);
    memset(vv, 0, sizeof vv);
    spu_unlock();
    LOG("snd: SsInit (libsnd on the host: psx/sdk/libsnd.c)\n");
}
PSX_SDK_WEAK void SsInitHot(void) { SsInit(); }
PSX_SDK_WEAK void SsSetTableSize(char *table, short s_max, short t_max) {
    (void)table; (void)t_max;
    n_seqs = s_max > 0 && s_max <= N_SEQS ? s_max : N_SEQS;
    LOG("snd: SsSetTableSize(%d sequences, %d tracks)\n", s_max, t_max);
}
PSX_SDK_WEAK void SsSetTickMode(int mode) {
    tick_hz = tick_rate(mode);
    LOG("snd: SsSetTickMode(%d): %d Hz\n", mode, tick_hz);
}
PSX_SDK_WEAK void SsStart(void) { spu_set_tick(auto_tick, tick_hz); }
PSX_SDK_WEAK void SsStart2(void) { spu_set_tick(auto_tick, tick_hz); }
PSX_SDK_WEAK void SsEnd(void) { spu_set_tick(NULL, 0); }
PSX_SDK_WEAK void SsQuit(void) {
    spu_set_tick(NULL, 0);
    spu_key_off(0xFFFFFF);
}
PSX_SDK_WEAK void SsSeqCalledTbyT(void) {
    if (!game_ticks) LOG("snd: the game calls SsSeqCalledTbyT itself: its calls drive the sequencer from now on\n");
    game_ticks = 1;
    psx_snd_tick();
}
PSX_SDK_WEAK void SsSetMono(void) { mono = 1; }
PSX_SDK_WEAK void SsSetStereo(void) { mono = 0; }
PSX_SDK_WEAK void SsSetReservedVoice(char n) { reserved_voices = n > 0 && n <= SPU_VOICES ? n : SPU_VOICES; }

PSX_SDK_WEAK void SsSetMVol(short l, short r) {
    spu_write(SPU_MVOLL, (uint16_t)((l & 0x7F) * 0x3FFF / 127));
    spu_write(SPU_MVOLR, (uint16_t)((r & 0x7F) * 0x3FFF / 127));
}
PSX_SDK_WEAK void SsSetRVol(short l, short r) {
    psx_spu_set_reverb_depth((int16_t)((l & 0x7F) * 0x7FFF / 127), (int16_t)((r & 0x7F) * 0x7FFF / 127));
}
/* s_num 0: the CD (SS_SERIAL_A), 1: external; attr 0: mix (SS_MIX), 1: reverb (SS_REV); mode 1 on */
PSX_SDK_WEAK void SsSetSerialAttr(char s_num, char attr, char mode) {
    SpuCommonAttr a;
    memset(&a, 0, sizeof a);
    if (s_num == 0) {
        a.mask = attr ? SPU_COMMON_CDREV : SPU_COMMON_CDMIX;
        a.cd.reverb = a.cd.mix = mode != 0;
    } else {
        a.mask = attr ? SPU_COMMON_EXTREV : SPU_COMMON_EXTMIX;
        a.ext.reverb = a.ext.mix = mode != 0;
    }
    psx_spu_set_common_attr(&a);
}
PSX_SDK_WEAK void SsSetSerialVol(char s_num, short l, short r) {
    SpuCommonAttr a;
    memset(&a, 0, sizeof a);
    if (s_num == 0) {
        a.mask = SPU_COMMON_CDVOLL | SPU_COMMON_CDVOLR;
        a.cd.volume.left = (int16_t)((l & 0x7F) * 0x7FFF / 127);
        a.cd.volume.right = (int16_t)((r & 0x7F) * 0x7FFF / 127);
    } else {
        a.mask = SPU_COMMON_EXTVOLL | SPU_COMMON_EXTVOLR;
        a.ext.volume.left = (int16_t)((l & 0x7F) * 0x7FFF / 127);
        a.ext.volume.right = (int16_t)((r & 0x7F) * 0x7FFF / 127);
    }
    psx_spu_set_common_attr(&a);
}

PSX_SDK_WEAK short SsVabOpenHead(unsigned char *addr, short vabid) {
    int r = psx_snd_vab_open_head(PSX_PTR(addr), vabid);
    LOG("snd: SsVabOpenHead(0x%08X, %d) -> %d\n", psx_ps1(PSX_PTR(addr)), vabid, r);
    return (short)r;
}
PSX_SDK_WEAK short SsVabOpenHeadSticky(unsigned char *addr, short vabid, unsigned int sbaddr) {
    int r = psx_snd_vab_open_head(PSX_PTR(addr), vabid);
    if (r >= 0) { /* the body is in SPU RAM already, at sbaddr */
        int i;
        uint32_t old = vabs[r].spu_addr;
        if (vabs[r].size) psx_spu_free(old);
        for (i = 1; i <= vabs[r].nvags; i++) vabs[r].vag_addr[i] = vabs[r].vag_addr[i] - old + sbaddr;
        vabs[r].spu_addr = sbaddr;
        vabs[r].size = 0;
        vabs[r].body = 1;
    }
    return (short)r;
}
PSX_SDK_WEAK short SsVabTransBody(unsigned char *addr, short vabid) {
    int r = psx_snd_vab_trans_body(PSX_PTR(addr), vabid);
    LOG("snd: SsVabTransBody(0x%08X, %d) -> %d (%u bytes to SPU RAM)\n", psx_ps1(PSX_PTR(addr)), vabid, r,
        vabid >= 0 && vabid < N_VABS ? vabs[vabid].size : 0);
    return (short)r;
}
PSX_SDK_WEAK short SsVabTransBodyPartly(unsigned char *addr, unsigned int bufsize, short vabid) {
    static uint32_t done[N_VABS];
    Vab *b;
    if (vabid < 0 || vabid >= N_VABS || !vabs[vabid].used) return -1;
    b = &vabs[vabid];
    if (done[vabid] + bufsize > b->size) bufsize = b->size - done[vabid];
    psx_spu_set_transfer_addr(b->spu_addr + done[vabid]);
    psx_spu_write(PSX_PTR(addr), bufsize);
    done[vabid] += bufsize;
    if (done[vabid] >= b->size) {
        done[vabid] = 0;
        b->body = 1;
        return vabid;
    }
    return -2;
}
PSX_SDK_WEAK short SsVabTransCompleted(short flag) { (void)flag; return 1; }
PSX_SDK_WEAK short SsVabTransfer(unsigned char *vh, unsigned char *vb, short vabid, short flag) {
    int r = psx_snd_vab_open_head(PSX_PTR(vh), vabid);
    (void)flag;
    if (r < 0) return -1;
    return (short)psx_snd_vab_trans_body(PSX_PTR(vb), r);
}
PSX_SDK_WEAK void SsVabClose(short vabid) { psx_snd_vab_close(vabid); }

PSX_SDK_WEAK short SsSeqOpen(unsigned int *addr, short vabid) {
    int r = psx_snd_seq_open(PSX_PTR(addr), vabid);
    LOG("snd: SsSeqOpen(0x%08X, vab %d) -> %d\n", psx_ps1(PSX_PTR(addr)), vabid, r);
    return (short)r;
}
PSX_SDK_WEAK void SsSeqClose(short seq) {
    if (seq < 0 || seq >= N_SEQS) return;
    psx_snd_seq_stop(seq);
    spu_lock();
    seqs[seq].used = 0;
    spu_unlock();
}
PSX_SDK_WEAK void SsSeqPlay(short seq, char mode, short l_count) {
    LOG("snd: SsSeqPlay(%d, %s, %d)\n", seq, mode ? "play" : "pause", l_count);
    psx_snd_seq_play(seq, mode, l_count);
}
PSX_SDK_WEAK void SsSeqStop(short seq) { psx_snd_seq_stop(seq); }
PSX_SDK_WEAK void SsSeqPause(short seq) {
    if (seq >= 0 && seq < N_SEQS) {
        spu_lock();
        seqs[seq].paused = 1;
        voices_off(of_seq, seq);
        spu_unlock();
    }
}
PSX_SDK_WEAK void SsSeqReplay(short seq) {
    if (seq >= 0 && seq < N_SEQS) seqs[seq].paused = 0;
}
PSX_SDK_WEAK void SsSeqSetVol(short seq, short l, short r) { psx_snd_seq_set_vol(seq, l, r); }
PSX_SDK_WEAK void SsSeqGetVol(short seq, short tr, short *l, short *r) {
    int16_t a = 0, b = 0;
    (void)tr;
    if (seq >= 0 && seq < N_SEQS) {
        a = (int16_t)seqs[seq].voll;
        b = (int16_t)seqs[seq].volr;
    }
    memcpy(PSX_PTR(l), &a, 2);
    memcpy(PSX_PTR(r), &b, 2);
}
PSX_SDK_WEAK short SsIsEos(short seq, short tr) {
    (void)tr;
    return seq >= 0 && seq < N_SEQS && seqs[seq].used && seqs[seq].playing;
}
PSX_SDK_WEAK void SsPlayBack(short seq, short tr, short l_count) {
    (void)tr;
    psx_snd_seq_play(seq, 1, l_count);
}

/* vab_pro: VAB id << 8 | program; pitch: note << 8 | fine */
PSX_SDK_WEAK short SsVoKeyOn(int vab_pro, int pitch, unsigned short voll, unsigned short volr) {
    int v;
    spu_lock();
    v = note_on((vab_pro >> 8) & 0xFF, vab_pro & 0x7F, -1, (pitch >> 8) & 0x7F, pitch & 0xFF, 127, -1, -1,
                voll & 0x7F, volr & 0x7F);
    spu_unlock();
    return (short)v;
}
PSX_SDK_WEAK short SsVoKeyOff(int vab_pro, int pitch) {
    spu_lock();
    note_off(-1, -1, (vab_pro >> 8) & 0xFF, vab_pro & 0x7F, (pitch >> 8) & 0x7F);
    spu_unlock();
    return 0;
}
PSX_SDK_WEAK short SsUtKeyOn(short vab, short prog, short tone, short note, short fine, short voll, short volr) {
    int v;
    spu_lock();
    v = note_on(vab, prog, tone, note, fine, 127, -1, -1, voll & 0x7F, volr & 0x7F);
    spu_unlock();
    return (short)v;
}
PSX_SDK_WEAK short SsUtKeyOff(short voice, short vab, short prog, short tone, short note) {
    (void)vab; (void)prog; (void)tone; (void)note;
    if (voice < 0 || voice >= SPU_VOICES) return -1;
    spu_lock();
    vv[voice].busy = 0;
    spu_key_off(1u << voice);
    spu_unlock();
    return 0;
}
PSX_SDK_WEAK short SsUtKeyOffV(short voice) { return SsUtKeyOff(voice, 0, 0, 0, 0); }
PSX_SDK_WEAK void SsUtAllKeyOff(short mode) {
    (void)mode;
    spu_lock();
    memset(vv, 0, sizeof vv);
    spu_key_off(0xFFFFFF);
    spu_unlock();
}
PSX_SDK_WEAK short SsUtSetVVol(short voice, short l, short r) {
    if (voice < 0 || voice >= SPU_VOICES || !vv[voice].busy) return -1;
    spu_lock();
    vv[voice].voll = l & 0x7F;
    vv[voice].volr = r & 0x7F;
    voice_volume(voice);
    spu_unlock();
    return 0;
}
PSX_SDK_WEAK void SsUtReverbOn(void) { psx_spu_set_reverb(SPU_ON); }
PSX_SDK_WEAK void SsUtReverbOff(void) { psx_spu_set_reverb(SPU_OFF); }
PSX_SDK_WEAK short SsUtSetReverbType(short type) { return (short)psx_spu_set_reverb_mode(type); }
PSX_SDK_WEAK void SsUtSetReverbDepth(short l, short r) {
    /* 0..127 as documented; larger values (SLUS-01252 passes 0x2000) are taken as the register's own */
    psx_spu_set_reverb_depth((int16_t)(l > 127 ? l : l * 0x7FFF / 127), (int16_t)(r > 127 ? r : r * 0x7FFF / 127));
}
PSX_SDK_WEAK void SsUtSetReverbFeedback(short feedback) { (void)feedback; }
PSX_SDK_WEAK void SsUtSetReverbDelay(short delay) { (void)delay; }

PSX_SDK_WEAK short SpuVmSetProgVol(short vab, short prog, unsigned char vol) {
    if (vab < 0 || vab >= N_VABS || !vabs[vab].used || prog < 0 || prog > 127) return -1;
    vabs[vab].prog[prog].mvol = vol;
    return 0;
}
PSX_SDK_WEAK short SpuVmGetProgVol(short vab, short prog) {
    if (vab < 0 || vab >= N_VABS || !vabs[vab].used || prog < 0 || prog > 127) return -1;
    return vabs[vab].prog[prog].mvol;
}
PSX_SDK_WEAK short SpuVmSetProgPan(short vab, short prog, unsigned char pan) {
    if (vab < 0 || vab >= N_VABS || !vabs[vab].used || prog < 0 || prog > 127) return -1;
    vabs[vab].prog[prog].mpan = pan;
    return 0;
}
PSX_SDK_WEAK short SpuVmGetProgPan(short vab, short prog) {
    if (vab < 0 || vab >= N_VABS || !vabs[vab].used || prog < 0 || prog > 127) return -1;
    return vabs[vab].prog[prog].mpan;
}
