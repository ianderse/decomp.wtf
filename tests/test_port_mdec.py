"""The port's MDEC and movie stream (port_runtime/psx/mdec.c under sdk/libpress.c, the St ring in sdk/libcd.c),
compiled into small harnesses with clang: a flat macroblock whose run-length codes are written here round-trips to
its colour, the IDCT against a floating-point one, the AC Huffman codes against psx-spx's table, a BS v2/v3 frame
encoded here decodes to the codes it was made from, and an STR stream on a disc image built here comes out of
StGetNext frame by frame with its XA sectors sent to the audio side."""
from __future__ import annotations

import math
import random
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from decompnet.rom.export_port import RUNTIME
from tests.test_port_cd import _msf, make_iso, to_raw

CLANG = shutil.which("clang") or shutil.which("cc")
pytestmark = pytest.mark.skipif(CLANG is None, reason="needs clang")
PSX = RUNTIME / "psx"

MDEC_HARNESS = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "mdec.h"
/* mb DEPTH CODE... -> the macroblock's bytes in hex; idct V*64 -> 64 values; ac BITS -> value length;
 * vlc FILE -> the run-length halfwords */
int main(int argc, char **argv) {
    MdecTables t;
    mdec_default_tables(&t);
    if (!strcmp(argv[1], "mb")) {
        static uint16_t rl[4096];
        uint8_t out[768];
        int n = 0, pos = 0, i, depth = atoi(argv[2]), b;
        for (i = 3; i < argc; i++) rl[n++] = (uint16_t)strtoul(argv[i], NULL, 16);
        b = mdec_macroblock(&t, rl, n, &pos, depth, 0, 0, out);
        printf("%d %d\n", b, pos);
        for (i = 0; i < b; i++) printf("%02x", out[i]);
        printf("\n");
    } else if (!strcmp(argv[1], "idct")) {
        int32_t blk[64];
        int i;
        for (i = 0; i < 64; i++) blk[i] = atoi(argv[2 + i]);
        mdec_idct(t.scale, blk);
        for (i = 0; i < 64; i++) printf("%d ", blk[i]);
        printf("\n");
    } else if (!strcmp(argv[1], "ac")) {
        const char *s = argv[2];
        uint32_t bits = 0;
        int n = 0, len = 0, v;
        for (; *s; s++, n++) bits = bits << 1 | (uint32_t)(*s == '1');
        v = mdec_ac_lookup(bits, n, &len);
        printf("%d %d\n", v, len);
    } else if (!strcmp(argv[1], "vlc")) {
        static uint8_t bs[65536];
        static uint16_t out[65536];
        FILE *f = fopen(argv[2], "rb");
        size_t n = fread(bs, 1, sizeof bs, f);
        int i, h;
        fclose(f);
        h = mdec_vlc(bs, n, out, 65536);
        printf("%d\n", h);
        for (i = 0; i < h; i++) printf("%04x ", out[i]);
        printf("\n");
    }
    return 0;
}
"""


@pytest.fixture(scope="module")
def mdec(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("mdec")
    (d / "harness.c").write_text(MDEC_HARNESS, encoding="utf-8")
    exe = d / "harness"
    r = subprocess.run([CLANG, "-std=c11", "-O1", "-Wall", "-I", str(PSX), str(d / "harness.c"), str(PSX / "mdec.c"),
                        "-o", str(exe)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return exe


def run(exe: Path, *args) -> list[str]:
    r = subprocess.run([str(exe), *map(str, args)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


def dc(q: int, v: int) -> str:
    return f"{(q << 10) | (v & 0x3FF):04x}"


# ---- the decoder

def test_a_flat_macroblock_round_trips_to_its_colour(mdec):
    # DC only: value = dc * iq[0] (2), the IDCT spreads it as /8, so a DC of 4k is k everywhere in the block
    y, cr, cb = 50, 20, -12
    codes = [dc(1, 4 * cr), "fe00", dc(1, 4 * cb), "fe00"] + [dc(1, 4 * y), "fe00"] * 4
    size, pos = map(int, run(mdec, "mb", 2, *codes)[0].split())
    assert (size, pos) == (768, 12)
    px = bytes.fromhex(run(mdec, "mb", 2, *codes)[1])
    want = (y + 1.402 * cr + 128, y - 0.3437 * cb - 0.7143 * cr + 128, y + 1.772 * cb + 128)
    for i in range(256):
        for c in range(3):
            assert abs(px[i * 3 + c] - want[c]) <= 1, (i, c, px[i * 3:i * 3 + 3], want)
    # 15-bit: the same colour in 5 bits a channel
    out = run(mdec, "mb", 3, *codes)
    assert out[0].split()[0] == "512"
    p = int.from_bytes(bytes.fromhex(out[1])[:2], "little")
    assert [abs((p >> s & 31) - want[k] / 8) <= 1 for k, s in enumerate((0, 5, 10))] == [True] * 3
    # FE00h before a block is padding; the codes ending inside a macroblock give nothing
    assert run(mdec, "mb", 2, "fe00", "fe00", *codes)[0] == "768 14"
    assert run(mdec, "mb", 2, *codes[:-2])[0] == "0 0"


def test_ac_codes_are_dequantised_in_zigzag_order(mdec):
    # Y blocks with one AC at zigzag position 1 (raster 1: the first horizontal frequency): a left-to-right ramp
    codes = [dc(1, 0), "fe00", dc(1, 0), "fe00"] + [dc(1, 0), f"{0x0000 | 100:04x}", "fe00"] * 4
    px = bytes.fromhex(run(mdec, "mb", 2, *codes)[1])
    row = [px[x * 3] for x in range(8)]
    assert row == sorted(row, reverse=True) and row[0] - row[7] > 20      # cos falls across the block
    col = [px[(y * 16) * 3] for y in range(8)]
    assert max(col) - min(col) <= 1                                       # nothing vertical


def test_idct_matches_a_float_reference(mdec):
    rng = random.Random(1252)
    c = [math.sqrt(0.125)] + [0.5] * 7
    worst = 0
    for _ in range(20):
        blk = [rng.randint(-300, 300) if rng.random() < 0.4 else 0 for _ in range(64)]
        got = list(map(int, run(mdec, "idct", *blk)[0].split()))
        for y in range(8):
            for x in range(8):
                ref = sum(c[u] * c[v] * blk[v * 8 + u] * math.cos((2 * x + 1) * u * math.pi / 16)
                          * math.cos((2 * y + 1) * v * math.pi / 16) for u in range(8) for v in range(8))
                worst = max(worst, abs(got[y * 8 + x] - ref))
    assert worst <= 2.5, worst


# psx-spx "Huffman codes for AC values BS v1/v2/v3/iki": (code without the sign bit, MDEC value)
SPX_AC = {
    "11": 0x0001, "011": 0x0401, "0100": 0x0002, "0101": 0x0801, "00110": 0x1001, "00111": 0x0C01,
    "00101": 0x0003, "00100000": 0x3401, "00100111": 0x2801, "000100": 0x1C01, "000111": 0x1401,
    "0000100": 0x0802, "0000111": 0x2001, "0000001000": 0x4001, "0000001111": 0x1002,
    "000000010000": 0x000B, "000000011111": 0x4401, "0000000010111": 0x000F, "00000000010000": 0x001F,
    "00000000011111": 0x0010, "000000000010000": 0x0028, "000000000011111": 0x0408,
    "0000000000010000": 0x0412, "0000000000011111": 0x6C01,
}


def test_ac_huffman_codes_against_psx_spx(mdec):
    for code, value in SPX_AC.items():
        assert run(mdec, "ac", code + "0")[0] == f"{value} {len(code) + 1}", code
        neg = (value & 0xFC00) | (-(value & 0x3FF) & 0x3FF)
        assert run(mdec, "ac", code + "1" + "0101")[0] == f"{neg} {len(code) + 1}", code  # trailing bits ignored
    assert run(mdec, "ac", "10")[0] == f"{0xFE00} 2"           # end of block
    assert run(mdec, "ac", "000001")[0].startswith("-2 6")       # escape: 16 raw bits follow
    assert run(mdec, "ac", "000000000000")[0].startswith("-1")   # unused


class BitWriter:
    """BS bitstream: little-endian halfwords, each filled from bit 15 down"""
    def __init__(self):
        self.bits = ""

    def put(self, s: str):
        self.bits += s

    def num(self, v: int, n: int):
        self.bits += format(v & ((1 << n) - 1), f"0{n}b")

    def data(self) -> bytes:
        b = self.bits + "0" * (-len(self.bits) % 16) + "0" * 32
        return b"".join(struct.pack("<H", int(b[i:i + 16], 2)) for i in range(0, len(b), 16))


def ac_code(value: int) -> str:
    run_, level = value >> 10, value & 0x3FF
    lv = level - 0x400 if level & 0x200 else level
    for code, v in SPX_AC.items():
        if v == (run_ << 10 | abs(lv)):
            return code + ("1" if lv < 0 else "0")
    return "000001" + format(value, "016b")


def test_vlc_decodes_a_bs_v2_frame(mdec, tmp_path):
    blocks = [(5, [0x0001, 0x0403]), (-7, []), (100, [0x1001, 0x07FF, 0x3402]), (-200, [0x0C01]), (0, [0x7FFF]),
              (511 - 1, [0x0002, 0x0801])]
    w = BitWriter()
    want = []
    for d, acs in blocks * 2:  # two macroblocks
        w.num(d, 10)
        want.append((3 << 10) | (d & 0x3FF))
        for a in acs:
            w.put(ac_code(a))
            want.append(a)
        w.put("10")
        want.append(0xFE00)
    w.num(0x1FF, 10)  # end of frame, in place of Cr
    f = tmp_path / "v2.bs"
    f.write_bytes(struct.pack("<HHHH", (len(want) + 1) // 2, 0x3800, 3, 2) + w.data())
    out = run(mdec, "vlc", f)
    assert int(out[0]) == len(want)
    assert [int(x, 16) for x in out[1].split()] == want


def test_vlc_decodes_bs_v3_dc_differences(mdec, tmp_path):
    luma = {0: "100", 1: "00", 2: "01", 3: "101", 4: "110", 5: "1110", 6: "11110", 7: "111110", 8: "1111110"}
    chroma = {0: "00", 1: "01", 2: "10", 3: "110", 4: "1110", 5: "11110", 6: "111110", 7: "1111110", 8: "11111110"}

    def put_diff(w, diff, table):
        v = diff // 4
        size = abs(v).bit_length()
        w.put(table[size])
        if size:
            w.num(v if v > 0 else v + (1 << size) - 1, size)

    w = BitWriter()
    dcs = [[8, -20, 40, 44, 36, 36], [-4, -20, 0, 400, -400, 36]]  # per macroblock: Cr, Cb, Y1..Y4
    old = [0, 0, 0]
    want = []
    for mb in dcs:
        for i, v in enumerate(mb):
            k = min(i, 2)
            put_diff(w, v - old[k], chroma if i < 2 else luma)
            old[k] = v
            want.append((1 << 10) | (v & 0x3FF))
            w.put("10")
            want.append(0xFE00)
    w.put("1111111111")
    f = tmp_path / "v3.bs"
    f.write_bytes(struct.pack("<HHHH", len(want) // 2, 0x3800, 1, 3) + w.data())
    out = run(mdec, "vlc", f)
    assert [int(x, 16) for x in out[1].split()] == want
    f.write_bytes(struct.pack("<HHHH", 1, 0x3801, 1, 2) + w.data())
    assert run(mdec, "vlc", f)[0] == "-1"  # not a BS header


# ---- the stream: StSetRing/StSetStream/CdRead2/StGetNext/StFreeRing on a disc image

ST_HARNESS = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include "psx_runtime.h"
#include "libcd.h"
const PsxConfig psx_config;
const PsxFunction psx_functions[1];
const size_t psx_function_count = 0;
const PsxFunction *psx_function_at(uint32_t vram) { (void)vram; return NULL; }
void *psx_host_fn(uint32_t addr) { (void)addr; abort(); }
void *psx_runtime_alloc(uint32_t size) { (void)size; return psx_host(0x801FF000u); }
void psx_log(const char *fmt, ...) { (void)fmt; }
static int waits, xa;
int psx_vblank_wait(void) { waits++; psx_cd_vblank(); return 1; }
void psx_cd_xa_sector(const uint8_t *raw) { (void)raw; xa++; }
int main(int argc, char **argv) {
    int frames = atoi(argv[1]), got = 0, tries = 0;
    if (mmap((void *)(uintptr_t)(PSX_HOST_BASE + PSX_RAM_BASE), PSX_RAM_SIZE, PROT_READ | PROT_WRITE,
             MAP_PRIVATE | MAP_ANON | MAP_FIXED, -1, 0) == MAP_FAILED) return 2;
    CdInit();
    StSetRing(0x80100000u, 16);
    StSetStream(1, 1, 0x0FFFFFFF, 0, 0);
    CdIntToPos(atoi(argv[2]), 0x80001000u);
    CdControl(2 /* CdlSetloc */, 0x80001000u, 0);
    CdRead2(0x1C0); /* double speed, real time (XA), streaming */
    while (got < frames && tries++ < 100000) {
        uint32_t addr, hdr, sum = 0, i, n;
        const uint8_t *h;
        if (StGetNext(0x80002000u, 0x80002004u)) continue;
        addr = *(uint32_t *)psx_host(0x80002000u);
        hdr = *(uint32_t *)psx_host(0x80002004u);
        h = (const uint8_t *)psx_host(hdr);
        n = (uint32_t)(h[6] | h[7] << 8) * 2016;
        for (i = 0; i < n; i++) sum += ((const uint8_t *)psx_host(addr))[i];
        printf("frame %u sectors %u sum %u first %02x waits %d\n", h[8] | h[9] << 8, h[6], sum,
               ((const uint8_t *)psx_host(addr))[0], waits);
        StFreeRing(addr);
        got++;
    }
    printf("xa %d waits %d\n", xa, waits);
    return 0;
}
"""


def str_sector(lba: int, frame: int, chunk: int, nchunks: int) -> tuple[bytes, bytes]:
    data = bytes((frame * 31 + chunk * 7 + i) & 0xFF for i in range(2016))
    hdr = struct.pack("<HHHHIIHHII", 0x0160, 0x8001, chunk, nchunks, frame, nchunks * 2016, 320, 240,
                      0x38000000, 0x00020001) + bytes(4)
    sync = b"\0" + b"\xff" * 10 + b"\0"
    raw = sync + _msf(lba) + b"\x02" + b"\x01\x01\x48\x00" * 2 + hdr + data + bytes(280)
    return raw, data


def xa_sector(lba: int) -> bytes:
    sync = b"\0" + b"\xff" * 10 + b"\0"
    return sync + _msf(lba) + b"\x02" + b"\x01\x01\x64\x01" * 2 + bytes(2324) + bytes(4)


def test_str_frames_come_out_of_the_ring_in_order(tmp_path):
    d = tmp_path
    (d / "st.c").write_text(ST_HARNESS, encoding="utf-8")
    exe = d / "st"
    r = subprocess.run([CLANG, "-std=c11", "-D_DEFAULT_SOURCE", "-O1", "-Wall", "-Wno-unused-function",
                        "-I", str(PSX), "-I", str(PSX / "sdk"), str(d / "st.c"), str(PSX / "sdk" / "libcd.c"),
                        str(PSX / "cd.c"), "-o", str(exe)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    iso, _ = make_iso()
    image = bytearray(to_raw(iso, 2))
    start = len(image) // 2352
    sums, n_xa = {}, 0
    lba = start
    for frame in range(1, 7):  # 6 frames of 3 sectors, an XA sector after every fourth one
        for chunk in range(3):
            raw, data = str_sector(lba, frame, chunk, 3)
            image += raw
            sums[frame] = sums.get(frame, 0) + sum(data)
            lba += 1
            if (lba - start) % 5 == 4:
                image += xa_sector(lba)
                lba += 1
                n_xa += 1
    for _ in range(40):  # what follows the movie: data sectors
        image += to_raw(bytes(2048), 2)[:2352]
        lba += 1
    disc = d / "movie.bin"
    disc.write_bytes(bytes(image))
    r = subprocess.run([str(exe), "6", str(start)], capture_output=True, text=True, timeout=60,
                       env={"PSX_DISC": str(disc), "PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    frames = [ln.split() for ln in lines if ln.startswith("frame")]
    assert [int(f[1]) for f in frames] == [1, 2, 3, 4, 5, 6]
    for f in frames:
        assert int(f[3]) == 3 and int(f[5]) == sums[int(f[1])], f   # three 2016-byte chunks, back to back
    xa, waits = int(lines[-1].split()[1]), int(lines[-1].split()[3])
    assert xa >= n_xa - 1  # the audio sectors went to the XA side, not the ring
    # the game waited on the drive: 23 sectors at 2.5 a vblank (double speed) take about 9 vblanks
    assert 7 <= waits <= 12, waits


def test_the_run_summary_says_what_the_mdec_decoded():
    from decompnet.rom.export_port import summarize_run
    log = ("cd: CdRead2(mode 0x1C0) from LBA 301740 (XMV/A2MV24.STR +0x0): streaming into the St ring\n"
           "psx: stopped after the time PSX_TIMEOUT gave\npsx: 1798 frames (vertical blanks)\n"
           "press: 385 frames decoded (DecDCTvlc), 385 DecDCTin, 7700 DecDCTout completions (115500 macroblocks); "
           "the first at vblank 260\n")
    r = summarize_run(log)
    assert r["cd_streams"] == ["XMV/A2MV24.STR"]
    assert r["mdec"] == {"frames": 385, "ins": 385, "outs": 7700, "macroblocks": 115500, "first_vblank": 260}
    assert ("streams XMV/A2MV24.STR (St ring); the MDEC decoded 385 movie frames (7700 DecDCTout completions, "
            "115500 macroblocks, the first at vblank 260)") in r["story"]
    assert summarize_run("psx: 3 frames (vertical blanks)\n")["mdec"] is None
