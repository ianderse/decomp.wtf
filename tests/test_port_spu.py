"""The port's software SPU (port_runtime/psx/spu.c, xa.c) and its libspu/libsnd (psx/sdk/libspu.c, libsnd.c),
compiled with clang into a harness: ADPCM blocks, envelopes, a two-voice mix, raw register key-ons, XA sectors and
a VAB + SEQ played by the host libsnd. The expected values come from psx-spx's formulas written again here in
Python, not from the C. Skipped without clang."""

import json
import shutil
import struct
import subprocess

import pytest

from decompnet.rom import export_port as ep

PSX = ep.RUNTIME / "psx"
CLANG = shutil.which("clang") or shutil.which("cc")
pytestmark = pytest.mark.skipif(not CLANG, reason="needs clang")

HARNESS = r"""
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "psx_runtime.h"
#include "spu.h"
#include "psx_sound.h"
#include "libsnd.h"

void psx_log(const char *fmt, ...) { (void)fmt; }
void *psx_host_fn(uint32_t addr) { (void)addr; return NULL; }
void psx_audio_start(void) {}
void psx_audio_stop(void) {}
static uint16_t io[SPU_REG_BYTES / 2];

static void hex(const char *s, uint8_t *out, int n) {
    int i;
    for (i = 0; i < n; i++) { unsigned v; sscanf(s + 2 * i, "%2x", &v); out[i] = (uint8_t)v; }
}
static uint8_t *slurp(const char *path, long *n) {
    FILE *f = fopen(path, "rb"); uint8_t *b;
    fseek(f, 0, SEEK_END); *n = ftell(f); fseek(f, 0, SEEK_SET);
    b = malloc((size_t)*n); fread(b, 1, (size_t)*n, f); fclose(f);
    return b;
}

int main(int argc, char **argv) {
    const char *cmd = argv[1];
    int i;
    if (!strcmp(cmd, "adpcm")) { /* block hist0 hist1 */
        uint8_t b[16]; int16_t h[2], out[28];
        hex(argv[2], b, 16); h[0] = (int16_t)atoi(argv[3]); h[1] = (int16_t)atoi(argv[4]);
        spu_adpcm_decode(b, h, out);
        for (i = 0; i < 28; i++) printf("%d ", out[i]);
        printf("\n%d %d\n", h[0], h[1]);
    } else if (!strcmp(cmd, "env")) { /* adsr1 adsr2 samples keyoff_at */
        SpuEnvelope e = {1, 0, 0};
        uint16_t a1 = (uint16_t)strtol(argv[2], 0, 0), a2 = (uint16_t)strtol(argv[3], 0, 0);
        int n = atoi(argv[4]), off = atoi(argv[5]);
        for (i = 0; i < n; i++) {
            if (i == off) { e.phase = 4; e.wait = 0; }
            printf("%d:%d ", e.phase, (int)e.level);
            spu_env_step(&e, a1, a2);
        }
        printf("\n");
    } else if (!strcmp(cmd, "mix")) { /* voices from argv: v ssa pitch voll volr adsr1 adsr2 ; then frames, raw */
        int frames = atoi(argv[2]), raw = atoi(argv[3]), k;
        long n; uint8_t *ram = slurp(argv[4], &n);
        int16_t *out = calloc((size_t)frames * 2, sizeof(int16_t));
        uint32_t on = 0;
        spu_init(io);
        spu_reset_regs();
        spu_ram_write(0, ram, (uint32_t)n);
        spu_write(SPU_SPUCNT, 0xC000);
        spu_write(SPU_MVOLL, 0x3FFF); spu_write(SPU_MVOLR, 0x3FFF);
        for (k = 5; k + 6 < argc; k += 7) {
            int v = atoi(argv[k]); uint32_t b = (uint32_t)v * 16;
            spu_write(b + SPU_V_SSA, (uint16_t)strtol(argv[k + 1], 0, 0));
            spu_write(b + SPU_V_PITCH, (uint16_t)strtol(argv[k + 2], 0, 0));
            spu_write(b + SPU_V_VOLL, (uint16_t)strtol(argv[k + 3], 0, 0));
            spu_write(b + SPU_V_VOLR, (uint16_t)strtol(argv[k + 4], 0, 0));
            spu_write(b + SPU_V_ADSR1, (uint16_t)strtol(argv[k + 5], 0, 0));
            spu_write(b + SPU_V_ADSR2, (uint16_t)strtol(argv[k + 6], 0, 0));
            on |= 1u << v;
        }
        if (raw) { io[SPU_KON >> 1] = (uint16_t)on; io[(SPU_KON >> 1) + 1] = (uint16_t)(on >> 16); }
        else spu_key_on(on);
        spu_mix(out, frames);
        printf("{\"frames\": [");
        for (i = 0; i < frames; i++) printf("%s[%d, %d]", i ? ", " : "", out[2 * i], out[2 * i + 1]);
        printf("], \"endx\": %d, \"kon\": %d", io[SPU_ENDX >> 1], io[SPU_KON >> 1]);
        printf(", \"lsa\": [%d, %d], \"env\": [%d, %d]}\n", io[SPU_V_LSA >> 1], io[(16 + SPU_V_LSA) >> 1],
               io[SPU_V_ADSRVOL >> 1], io[(16 + SPU_V_ADSRVOL) >> 1]);
    } else if (!strcmp(cmd, "xa")) { /* sector file */
        long n; uint8_t *raw = slurp(argv[2], &n);
        static int16_t out[4032 * 2];
        int ch = 0, rate = 0, m = xa_decode_sector(raw, out, &ch, &rate), q;
        spu_init(io);
        q = psx_cd_xa_sector(raw);
        printf("{\"n\": %d, \"channels\": %d, \"rate\": %d, \"queued\": %d, \"pcm\": [", m, ch, rate, q);
        for (i = 0; i < m * ch; i++) printf("%s%d", i ? ", " : "", out[i]);
        printf("]}\n");
    } else if (!strcmp(cmd, "snd")) { /* vh vb seq: open, play, tick; voice state after each step */
        long nh, nb, ns; uint8_t *vh = slurp(argv[2], &nh), *vb = slurp(argv[3], &nb), *sq = slurp(argv[4], &ns);
        int vab, seq, t, v;
        psx_spu_io = io;
        SsInit();
        SsSetTickMode(1);
        vab = psx_snd_vab_open_head(vh, -1);
        printf("{\"vab\": %d, \"body\": %d", vab, psx_snd_vab_trans_body(vb, vab));
        seq = psx_snd_seq_open(sq, vab);
        psx_snd_seq_play(seq, 1, 1);
        printf(", \"seq\": %d, \"steps\": [", seq);
        for (t = 0; t < 40; t++) {
            psx_snd_tick();
            v = psx_snd_voice_of(seq, 0, 72);
            printf("%s[%d, %d, %d, %d, %d]", t ? ", " : "", v, v >= 0 ? spu_voice_keyed(v) : 0,
                   v >= 0 ? io[(v * 16 + SPU_V_PITCH) >> 1] : 0, v >= 0 ? io[(v * 16 + SPU_V_SSA) >> 1] : 0,
                   SsIsEos(seq, 0));
        }
        printf("]}\n");
    }
    return 0;
}
"""


@pytest.fixture(scope="module")
def harness(tmp_path_factory):
    d = tmp_path_factory.mktemp("spu")
    (d / "harness.c").write_text(HARNESS)
    exe = d / "harness"
    srcs = [PSX / "spu.c", PSX / "xa.c", PSX / "sdk" / "libspu.c", PSX / "sdk" / "libsnd.c", d / "harness.c"]
    r = subprocess.run([CLANG, "-std=gnu11", "-O1", "-w", f"-I{PSX}", f"-I{PSX / 'sdk'}", *map(str, srcs), "-lm",
                        "-lpthread", "-o", str(exe)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return d, exe


def run(h, *args):
    d, exe = h
    r = subprocess.run([str(exe), *map(str, args)], capture_output=True, text=True, cwd=d, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


# ---------------------------------------------------------------------------------------------- reference models

POS, NEG = [0, 60, 115, 98, 122], [0, 0, -52, -55, -60]


def ref_adpcm(block: bytes, h0=0, h1=0):
    shift, filt = block[0] & 15, min((block[0] >> 4) & 7, 4)
    shift = 9 if shift > 12 else shift
    out = []
    for i in range(28):
        nib = (block[2 + i // 2] >> ((i & 1) * 4)) & 15
        raw = ((nib << 12) - 0x10000 if nib & 8 else nib << 12) >> shift
        s = raw + ((h0 * POS[filt] + h1 * NEG[filt] + 32) >> 6)
        s = max(-32768, min(32767, s))
        h1, h0 = h0, s
        out.append(s)
    return out, (h0, h1)


def ref_env(a1, a2, n, off):
    """psx-spx's envelope: every AdsrCycles samples, level += AdsrStep."""
    phase, level, wait, out = 1, 0, 0, []
    for i in range(n):
        if i == off:
            phase, wait = 4, 0
        out.append((phase, level))
        if phase == 0:
            continue
        if phase == 1:
            exp, dec, shift, step = a1 >> 15, 0, (a1 >> 10) & 31, 7 - ((a1 >> 8) & 3)
        elif phase == 2:
            exp, dec, shift, step = 1, 1, (a1 >> 4) & 15, -8
        elif phase == 3:
            exp, dec, shift = a2 >> 15, (a2 >> 14) & 1, (a2 >> 8) & 31
            step = -8 + ((a2 >> 6) & 3) if dec else 7 - ((a2 >> 6) & 3)
        else:
            exp, dec, shift, step = (a2 >> 5) & 1, 1, a2 & 31, -8
        if wait > 1:
            wait -= 1
            continue
        cycles = 1 << max(0, shift - 11)
        s = step << max(0, 11 - shift)
        if exp and not dec and level > 0x6000:
            cycles *= 4
        if exp and dec:
            s = (s * level) >> 15
        level = max(0, min(0x7FFF, level + s))
        wait = cycles
        if phase == 1 and level >= 0x7FFF:
            phase, wait = 2, 0
        elif phase == 2 and level <= ((a1 & 15) + 1) * 0x800:
            phase, wait = 3, 0
        elif phase == 4 and level == 0:
            phase = 0
    return out


def block(shift, filt, flags, nibbles):
    b = bytearray(16)
    b[0], b[1] = (filt << 4) | shift, flags
    for i, n in enumerate(nibbles):
        b[2 + i // 2] |= (n & 15) << ((i & 1) * 4)
    return bytes(b)


# ---------------------------------------------------------------------------------------------- ADPCM

def test_adpcm_block_by_hand(harness):
    # shift 12, filter 0: each nibble is its own value (1 -> 1, 0xF -> -1, 7 -> 7, 8 -> -8)
    b = block(12, 0, 0, [1, 0xF, 7, 8] * 7)
    out = run(harness, "adpcm", b.hex(), 0, 0).splitlines()[0]
    assert [int(x) for x in out.split()][:4] == [1, -1, 7, -8]
    # shift 0: nibble << 12 (1 -> 4096, 8 -> -32768); filter 1 adds 60/64 of the last output
    b = block(0, 1, 0, [1, 0, 0] + [0] * 25)
    out = [int(x) for x in run(harness, "adpcm", b.hex(), 0, 0).split()[:3]]
    assert out == [4096, (4096 * 60 + 32) >> 6, (((4096 * 60 + 32) >> 6) * 60 + 32) >> 6] == [4096, 3840, 3600]


@pytest.mark.parametrize("shift,filt", [(0, 0), (4, 1), (6, 2), (9, 3), (11, 4), (13, 2), (2, 7)])
def test_adpcm_matches_psx_spx(harness, shift, filt):
    nib = [(i * 7 + shift * 3 + filt) % 16 for i in range(28)]
    b = block(shift, filt, 0, nib)
    lines = run(harness, "adpcm", b.hex(), 1234, -2000).splitlines()
    want, hist = ref_adpcm(b, 1234, -2000)
    assert [int(x) for x in lines[0].split()] == want
    assert tuple(int(x) for x in lines[1].split()) == hist


# ---------------------------------------------------------------------------------------------- ADSR

@pytest.mark.parametrize("a1,a2,off", [
    (0x000F, 0x0000, 40),      # fastest linear attack (+7 << 11 per sample), sustain level 15, linear release
    (0x3F7A, 0x5FC5, 900),     # linear attack shift 15 (every 16 samples), decay 7 to level 0xB, sustain decrease
    (0xC0F3, 0xA087, 1500),    # exponential attack (slower above 0x6000), exponential sustain increase
    (0x00F0, 0xC1E9, 300),     # exponential decay to sustain level 1, exponential release shift 9
])
def test_envelope_matches_psx_spx(harness, a1, a2, off):
    n = 2000
    got = [tuple(int(v) for v in t.split(":")) for t in run(harness, "env", a1, a2, n, off).split()]
    assert got == ref_env(a1, a2, n, off)


def test_envelope_by_hand(harness):
    got = [tuple(int(v) for v in t.split(":")) for t in run(harness, "env", 0x000F, 0x0000, 8, 4).split()]
    # attack +7 << 11 a sample, saturating at 0x7FFF; decay's one exponential step (-8 << 11 scaled by the level)
    # takes it to 16383, below sustain level 16 * 0x800, so sustain; key off at sample 4: a linear release of
    # -8 << 11 a sample reaches 0 and the voice is off
    assert got == [(1, 0), (1, 14336), (1, 28672), (2, 32767), (4, 16383), (0, 0), (0, 0), (0, 0)]


# ---------------------------------------------------------------------------------------------- the mixer

def _ram(tmp, blocks: dict[int, bytes]):
    ram = bytearray(0x2000)
    for at, b in blocks.items():
        ram[at:at + len(b)] = b
    p = tmp / "ram.bin"
    p.write_bytes(bytes(ram))
    return p


def test_two_voices_mix_to_the_expected_samples(harness):
    d, _ = harness
    # voice 0: a looping block of 4096s (nibble 1, shift 0); voice 1: -8192s (nibble 0xE); flags 7 = loop start,
    # loop end, repeat
    ram = _ram(d, {0x1000: block(0, 0, 7, [1] * 28), 0x1010: block(0, 0, 7, [0xE] * 28)})
    voices = [0, 0x200, 0x1000, 0x3FFF, 0x0000, 0x000F, 0x0000,   # full left
              1, 0x202, 0x1000, 0x2000, 0x3FFF, 0x000F, 0x0000]   # half left, full right
    r = json.loads(run(harness, "mix", 64, 0, ram, *voices))
    f = r["frames"]
    assert f[0] == [0, 0]  # the interpolation's one-sample delay, and the envelope starts at 0
    # steady state: v0 4096 * env 0x7FFF >> 15 = 4095, * vol 0x7FFE >> 15 = 4094 left; v1 -8192 -> -8192,
    # -4096 left, -8192 right; left -2, right -8192, through main volume 0x7FFE: -2, -8192
    s0 = (4096 * 0x7FFF) >> 15
    s1 = (-8192 * 0x7FFF) >> 15
    left = ((s0 * 0x7FFE) >> 15) + ((s1 * 0x4000) >> 15)
    right = (s1 * 0x7FFE) >> 15
    want = [(left * 0x7FFE) >> 15, (right * 0x7FFE) >> 15]
    assert want == [-2, -8192]
    # from frame 7: attack (3 samples), decay's one step down to 16383, sustain climbing back to 0x7FFF
    assert all(x == want for x in f[7:]) and f[4] != want
    assert r["endx"] == 0b11              # both blocks ended (and looped) after 28 samples
    assert r["lsa"] == [0x200, 0x202]     # the loop-start flag set each repeat address
    assert r["env"] == [0x7FFF, 0x7FFF]   # ADSRVOL written back


def test_key_on_by_a_raw_register_store_is_polled(harness):
    d, _ = harness
    ram = _ram(d, {0x1000: block(0, 0, 7, [1] * 28)})
    r = json.loads(run(harness, "mix", 16, 1, ram, 0, 0x200, 0x1000, 0x3FFF, 0x3FFF, 0x000F, 0x0000))
    assert r["kon"] == 0                  # consumed, as the hardware's trigger
    assert r["frames"][8][0] > 4000


def test_pitch_interpolates_between_samples(harness):
    d, _ = harness
    # a ramp 0, 4096, 8192, ... (shift 0, nibbles 0..7 then repeated), played at half speed: every other frame
    # is the midpoint
    ram = _ram(d, {0x1000: block(0, 0, 7, [i % 8 for i in range(28)])})
    r = json.loads(run(harness, "mix", 16, 0, ram, 0, 0x200, 0x0800, 0x3FFF, 0x3FFF, 0x000F, 0x0000))
    f = [x[0] for x in r["frames"]]
    raw = [((i % 8) << 12) for i in range(28)]

    def out(s):  # through a saturated envelope, voice volume and main volume
        return ((((s * 0x7FFF) >> 15) * 0x7FFE >> 15) * 0x7FFE) >> 15
    # frame 2k+1 sits halfway between raw[k-1] and raw[k] (k >= 4: the envelope has settled at 0x7FFF)
    for k in range(4, 6):
        mid = raw[k - 1] + ((raw[k] - raw[k - 1]) * 0x800 >> 12)
        assert f[2 * k + 1] == out(mid)
        assert f[2 * k] == out(raw[k - 1])


def test_key_off_releases(harness):
    d, _ = harness
    ram = _ram(d, {0x1000: block(0, 0, 3, [1] * 28)})   # loop end + repeat, no loop start: repeat address 0 stays
    r = json.loads(run(harness, "mix", 64, 0, ram, 0, 0x200, 0x1000, 0x3FFF, 0x3FFF, 0x000F, 0x0000))
    assert r["endx"] & 1 and r["lsa"][0] == 0


# ---------------------------------------------------------------------------------------------- XA

XA_POS, XA_NEG = [0, 60, 115, 98], [0, 0, -52, -55]


def _xa_sector(coding, seed=3):
    raw = bytearray(2352)
    raw[0x12], raw[0x13] = 0x64, coding    # submode: audio, form 2, real time
    for g in range(18):
        grp = 0x18 + g * 128
        for u in range(8):
            raw[grp + 4 + u] = (((u + g) % 4) << 4) | ((u * 3 + g + seed) % 13)
        raw[grp:grp + 4] = raw[grp + 4:grp + 8]
        raw[grp + 12:grp + 16] = raw[grp + 8:grp + 12]
        for j in range(112):
            raw[grp + 16 + j] = (j * 37 + g * 11 + seed) & 0xFF
    return bytes(raw)


def ref_xa(raw):
    coding = raw[0x13]
    stereo, eight = (coding & 3) == 1, ((coding >> 4) & 3) == 1
    units = 4 if eight else 8
    hist = [[0, 0], [0, 0]]
    chans = [[], []]
    for g in range(18):
        grp = raw[0x18 + g * 128:0x18 + (g + 1) * 128]
        for u in range(units):
            p = grp[4 + u]
            shift, f = p & 15, (p >> 4) & 3
            shift = 9 if shift > 12 else shift
            c = (u & 1) if stereo else 0
            h = hist[c]
            for j in range(28):
                if eight:
                    v = grp[16 + j * 4 + u]
                    raw16 = (v << 8) - 0x10000 if v & 0x80 else v << 8
                else:
                    n = (grp[16 + j * 4 + u // 2] >> ((u & 1) * 4)) & 15
                    raw16 = (n << 12) - 0x10000 if n & 8 else n << 12
                s = (raw16 >> shift) + ((h[0] * XA_POS[f] + h[1] * XA_NEG[f] + 32) >> 6)
                s = max(-32768, min(32767, s))
                h[1], h[0] = h[0], s
                chans[c].append(s)
    if stereo:
        return [x for pair in zip(*chans) for x in pair], 2
    return chans[0], 1


@pytest.mark.parametrize("coding,rate,ch,n", [(0x00, 37800, 1, 4032), (0x01, 37800, 2, 2016), (0x04, 18900, 1, 4032),
                                              (0x11, 37800, 2, 1008)])
def test_xa_sector_decodes_as_psx_spx_says(harness, coding, rate, ch, n):
    d, _ = harness
    raw = _xa_sector(coding)
    (d / "xa.bin").write_bytes(raw)
    r = json.loads(run(harness, "xa", d / "xa.bin"))
    want, wch = ref_xa(raw)
    assert (r["rate"], r["channels"], r["n"]) == (rate, ch, n) and wch == ch
    assert r["pcm"] == want
    assert abs(r["queued"] - n * 44100 // rate) <= 2   # resampled to 44.1 kHz onto the CD input


def test_xa_ignores_data_sectors(harness):
    d, _ = harness
    raw = bytearray(_xa_sector(0x00))
    raw[0x12] = 0x08  # data, not audio
    (d / "xa.bin").write_bytes(bytes(raw))
    assert json.loads(run(harness, "xa", d / "xa.bin"))["n"] == 0


# ---------------------------------------------------------------------------------------------- libsnd

def _vab():
    """One program (0) with one tone: center note 60, VAG 1 (one looping block), notes 0-127."""
    h = bytearray(32 + 2048 + 512 + 512)
    h[0:4] = b"pBAV"
    struct.pack_into("<IIIHHHH", h, 4, 7, 0, len(h) + 16, 0, 1, 1, 1)  # ver, id, size, -, ps, ts, vs
    h[24], h[25] = 127, 64                                               # master volume, pan
    h[32:32 + 5] = bytes([1, 127, 0, 0, 64])                             # program 0: 1 tone, mvol, prior, mode, mpan
    a = 32 + 2048
    h[a:a + 16] = bytes([0, 0, 127, 64, 60, 0, 0, 127, 0, 0, 0, 0, 2, 2, 0, 0])
    struct.pack_into("<HHhh", h, a + 16, 0x000F, 0x0000, 0, 1)            # adsr1, adsr2, prog, vag 1
    struct.pack_into("<H", h, a + 512 + 2, 16 >> 3)                      # VAG 1: 16 bytes
    return bytes(h), block(0, 0, 7, [1] * 28)


def _seq(reso=480, tempo=500000):
    head = b"pQES" + struct.pack(">I", 1) + struct.pack(">H", reso) + tempo.to_bytes(3, "big") + bytes([4, 2])
    # delta 0: note on ch 0, note 72 vel 100; delta 480 (0x83 0x60): note off by velocity 0 (running status);
    # delta 0: end of track
    return head + bytes([0x00, 0x90, 72, 100, 0x83, 0x60, 72, 0, 0x00, 0xFF, 0x2F, 0x00])


def test_vab_and_seq_play_through_the_host_libsnd(harness):
    d, _ = harness
    vh, vb = _vab()
    (d / "vh.bin").write_bytes(vh)
    (d / "vb.bin").write_bytes(vb)
    (d / "seq.bin").write_bytes(_seq())
    r = json.loads(run(harness, "snd", d / "vh.bin", d / "vb.bin", d / "seq.bin"))
    assert r["vab"] == 0 and r["body"] == 0 and r["seq"] == 0
    steps = r["steps"]
    v, keyed, pitch, ssa, _ = steps[0]
    assert v >= 0 and keyed == 1
    assert pitch == 0x2000            # note 72 from a tone centred on 60: an octave up
    assert ssa == 0x1010 >> 3         # its VAG where SpuMalloc put the body
    # 120 bpm at 480 ticks a quarter, 60 ticks a second: 16 MIDI ticks a call; 480 ticks = 30 calls
    off_at = next(i for i, s in enumerate(steps) if s[0] < 0)
    assert 29 <= off_at <= 31
    assert steps[-1][4] == 0          # played once (l_count 1): ended


# ---------------------------------------------------------------------------------------------- the generator

def test_sdk_ranges_from_the_regions():
    regions = [{"from": 0x8016A000, "to": 0x80170DC8, "toolchain": "a"}, {"from": 0x80170DC8, "to": 0x80172000}]
    sony = {"SsSetMVol": {"lib": "libsnd", "vram": 0x8016A05C}, "VSync": {"lib": "libetc", "vram": 0x8017C368}}
    # merged where they touch, starting at the first Sony function inside (0x8016A028 before it is the game's)
    assert ep.sdk_ranges(regions, sony) == [(0x8016A05C, 0x80172000)]
    assert ep.sdk_ranges([{"from": 0x80010000, "to": 0x80020000}], sony) == []  # no Sony function: not the SDK's
    assert ep.sdk_ranges([], sony) == []
    assert ep.parse_sdk_range("8016A000-80172000") == ep.parse_sdk_range("0x8016A000 - 0x80172000") == \
        (0x8016A000, 0x80172000)
    with pytest.raises(ep.PortError):
        ep.parse_sdk_range("80172000-8016A000")


BUILD = {
    "image": "baserom.exe",
    "functions": [
        {"name": "func_800DA380", "seg": "main", "vram": 0x800DA380, "rom": 0x800, "c": "src/main/func_800DA380.c"},
        {"name": "func_8016A028", "seg": "main", "vram": 0x8016A028, "rom": 0x880, "c": "src/main/func_8016A028.c"},
        {"name": "SsSetMVol", "seg": "main", "vram": 0x8016A05C, "rom": 0x900},
        {"name": "func_8016A860", "seg": "main", "vram": 0x8016A860, "rom": 0x980, "c": "src/main/func_8016A860.c"},
        {"name": "func_8016A880", "seg": "main", "vram": 0x8016A880, "rom": 0xA00, "c": "src/main/func_8016A880.c"},
        {"name": "func_8016B0B8", "seg": "main", "vram": 0x8016B0B8, "rom": 0xA80},
        {"name": "func_80175434", "seg": "main", "vram": 0x80175434, "rom": 0xB00},
        {"name": "func_8017295C", "seg": "main", "vram": 0x8017295C, "rom": 0xB80},
    ],
}
SOURCES = {
    "src/main/func_800DA380.c": "void func_800DA380(void) { func_8016A860(); SsSetMVol(127, 127); func_8016A028(); "
                                "func_80175434(0x1010); }\n",
    "src/main/func_8016A028.c": "int func_8016A028(void) { return CdRead(); }\n",
    "src/main/func_8016A860.c": "void func_8016A860(void) { _SsInit(0); }\n",
    "src/main/func_8016A880.c": "void func_8016A880(void) { _SsInit(1); }\n",
}
SONY = {"SsSetMVol": {"lib": "libsnd", "bios": None, "label": "SsSetMVol", "vram": 0x8016A05C},
        "func_80175434": {"lib": "libspu", "bios": None, "label": "SpuSetTransferStartAddr", "vram": 0x80175434}}


def test_sdk_range_functions_are_the_runtimes():
    ranges = ep.sdk_ranges([{"from": 0x8016A000, "to": 0x80172000}], SONY)
    ident = {"func_8016A860": ("SsInit", "_SsInit(0)"), "func_8017295C": ("_spu_t", "libspu's")}
    sony = ep.sdk_functions(BUILD, SONY, ranges, ident)
    assert "func_8016A028" not in sony                                  # before the range's first Sony function
    assert sony["func_8016A860"]["sdk_range"] and sony["func_8016A860"]["label"] == "SsInit"
    assert sony["func_8016A880"]["lib"] == "libsnd" and "replaced" in sony["func_8016A880"]["why"]
    assert sony["func_8016B0B8"]["sdk_range"] and sony["SsSetMVol"]["sdk_range"]
    assert sony["func_8017295C"]["label"] == "_spu_t" and not sony["func_8017295C"].get("sdk_range")
    p = ep.plan(BUILD, SOURCES, {}, sony)
    assert p["compiled"] == ["src/main/func_800DA380.c", "src/main/func_8016A028.c"]  # the range's C isn't
    by = {s["name"]: s for s in p["sdk"]}
    assert by["SsSetMVol"]["kind"] == "implemented"                    # libsnd.c has it
    assert by["func_8016A860"]["kind"] == "alias" and by["func_8016A860"]["alias"] == "SsInit"
    assert by["func_80175434"]["alias"] == "SpuSetTransferStartAddr"    # a Sony row named by address
    assert by["func_8016A880"]["kind"] == "stub" and by["func_8016B0B8"]["kind"] == "stub"  # no identification
    assert by["func_8017295C"]["kind"] == "stub" and by["func_8017295C"]["lib"] == "libspu"
    c = ep.sdk_stubs_c(p["sdk"])
    assert "extern int SsInit();" in c
    assert "int func_8016A860(long a0, long a1, long a2, long a3, long a4, long a5, long a6, long a7) { return ((int (*)(" \
           "long, long, long, long, long, long, long, long))SsInit)(a0, a1, a2, a3, a4, a5, a6, a7); }" in c
    assert "int SsSetMVol" not in c
    kinds = {d["name"]: d["kind"] for d in p["dispatch"]}
    assert kinds["func_8016A860"] == 3 and kinds["func_8016A880"] == 2 and kinds["func_800DA380"] == 0


def test_the_sound_functions_are_implemented():
    for n in ("SpuInit", "SpuSetVoiceAttr", "SpuSetKey", "SpuMalloc", "SpuWrite", "SpuSetTransferStartAddr",
              "SpuIsTransferCompleted", "SpuSetCommonAttr", "SpuSetReverb"):
        assert ep.IMPLEMENTED[n] == "libspu"
    for n in ("SsInit", "SsSetTableSize", "SsSetTickMode", "SsStart", "SsSeqOpen", "SsSeqPlay", "SsSeqStop",
              "SsSeqSetVol", "SsVabOpenHead", "SsVabTransBody", "SsVabClose", "SsSetMVol", "SsSetSerialVol",
              "SsUtKeyOn", "SsUtKeyOff", "SsVoKeyOn", "SsVoKeyOff", "SsSeqCalledTbyT"):
        assert ep.IMPLEMENTED[n] == "libsnd"
    assert "PSX_AUDIO_SDL=1" in ep.cmakelists("x")
