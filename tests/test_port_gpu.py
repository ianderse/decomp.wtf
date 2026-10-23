"""The port's software GPU (port_runtime/psx/gpu.c), compiled on its own into a small harness with clang: a flat
triangle (with the top-left rule), textured sprites (15-bit and 4-bit CLUT), a semi-transparent tile, the fill, a
VRAM-to-VRAM copy, a CPU-to-VRAM packet, and an ordering table walked in a fake 2 MB RAM buffer."""

import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from decompnet.rom import export_port as ep
from decompnet.rom.export_port import RUNTIME

CLANG = shutil.which("clang") or shutil.which("cc")

HARNESS = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "gpu.h"

static void gp0(const uint32_t *w, int n) { gpu_gp0(w, n); }
#define PIX(x, y) gpu_vram[(y) * GPU_VRAM_W + (x)]
#define SHOW(name, v) printf("%s %u\n", name, (unsigned)(v))

int main(void) {
    gpu_reset();
    /* a flat red triangle (10,10) (50,10) (10,50) */
    {
        uint32_t t[] = {0x200000FFu, 10u | 10u << 16, 50u | 10u << 16, 10u | 50u << 16};
        gp0(t, 4);
        SHOW("tri_inside", PIX(15, 15));
        SHOW("tri_corner", PIX(10, 10));     /* top-left vertex: drawn */
        SHOW("tri_right", PIX(50, 10));      /* right vertex: not drawn */
        SHOW("tri_hyp", PIX(30, 30));        /* on the hypotenuse x + y = 60: a right/bottom edge, not drawn */
        SHOW("tri_below", PIX(29, 30));      /* just inside */
        SHOW("tri_outside", PIX(49, 49));
    }
    /* a 15-bit texture at (512,0), drawn as a 16x16 raw sprite at (100,100) */
    {
        uint16_t tex[16 * 16];
        int i;
        for (i = 0; i < 256; i++) tex[i] = (uint16_t)(0x8000 | i);
        gpu_load_image(512, 0, 16, 16, tex);
        uint32_t mode = 0xE1000000u | 8u | 2u << 7;
        uint32_t s[] = {mode, 0x7D808080u, 100u | 100u << 16, 0};
        gp0(s, 4);
        SHOW("sprt15", PIX(103, 105));       /* texel (3,5) = 0x8000 | 5*16+3 */
        SHOW("sprt15_edge", PIX(116, 100));  /* past the 16 pixels */
    }
    /* a 4-bit texture at (640,0) with a CLUT at (0,480): an 8x8 sprite, modulated by 0x808080 (as is) */
    {
        uint16_t clut[16], row[2];
        int i;
        for (i = 0; i < 16; i++) clut[i] = (uint16_t)(i ? (i * 2) : 0); /* index 0 transparent */
        gpu_load_image(0, 480, 16, 1, clut);
        row[0] = 0x3210; row[1] = 0x7654;   /* texels 0..7 = indices 0..7 */
        for (i = 0; i < 8; i++) gpu_load_image(640, i, 2, 1, row);
        gpu_fill(192, 192, 16, 16, 0x0000F8u); /* background under the sprite: red */
        uint32_t mode = 0xE1000000u | 10u;  /* tpage x 640, 4-bit */
        uint32_t s[] = {mode, 0x74808080u, 200u | 200u << 16, 0u | (0u | 480u << 6) << 16};
        gp0(s, 4);
        SHOW("sprt4_idx3", PIX(203, 201));   /* index 3 -> clut 6 */
        SHOW("sprt4_idx0", PIX(200, 200));   /* index 0 -> 0000: transparent, the fill shows */
    }
    /* a semi-transparent tile, mode 0 (B/2 + F/2), over a white fill */
    {
        gpu_fill(304, 304, 16, 16, 0xFFFFFFu);
        uint32_t mode = 0xE1000000u;        /* abr 0 */
        uint32_t t[] = {mode, 0x62000000u, 304u | 304u << 16, 8u | 8u << 16};
        gp0(t, 4);
        SHOW("semi", PIX(306, 306));          /* (31+0)/2 = 15 each channel */
        SHOW("semi_outside", PIX(318, 318));  /* white */
    }
    /* the fill ignores the drawing area and rounds the width up to 16 */
    {
        uint32_t f[] = {0x02FF0000u, 400u | 400u << 16, 3u | 2u << 16};
        gp0(f, 3);
        SHOW("fill", PIX(415, 401));
        SHOW("fill_out", PIX(416, 401));
    }
    /* VRAM to VRAM, and a CPU-to-VRAM packet with its data inline */
    {
        uint32_t c[] = {0x80000000u, 400u | 400u << 16, 450u | 450u << 16, 4u | 4u << 16};
        gp0(c, 4);
        SHOW("copy", PIX(451, 451));
        uint32_t l[] = {0xA0000000u, 600u | 300u << 16, 2u | 1u << 16, 0x22221111u};
        gp0(l, 4);
        SHOW("load0", PIX(600, 300));
        SHOW("load1", PIX(601, 300));
    }
    /* drawing area and offset: a 4x4 tile at (-2,-2) + offset (500,100) clipped to x >= 500 */
    {
        uint32_t env[] = {0xE3000000u | 500u | 100u << 10, 0xE4000000u | 600u | 200u << 10,
                          0xE5000000u | 500u | 100u << 11};
        gp0(env, 3);
        uint32_t t[] = {0x6000FF00u, 0x7FEu | 0x7FEu << 16, 4u | 4u << 16}; /* (-2,-2) in 11 bits */
        gp0(t, 3);
        SHOW("clip_in", PIX(501, 101));
        SHOW("clip_out", PIX(499, 101));
        uint32_t reset[] = {0xE3000000u, 0xE4000000u | 1023u | 511u << 10, 0xE5000000u};
        gp0(reset, 3);
    }
    /* an ordering table in a fake 2 MB RAM: 4 entries at 0x1000 cleared in reverse (each points at the one before,
     * the first ends the list), a TILE at 0x2000 added to entry 2 and a gouraud line at 0x2100 to entry 1; the walk
     * starts at the last entry */
    {
        uint8_t *ram = calloc(1, 0x200000);
        uint32_t w;
        int i;
        for (i = 0; i < 4; i++) {
            w = i ? (0x1000u + 4u * (uint32_t)(i - 1)) : 0x00FFFFFFu;
            memcpy(ram + 0x1000 + 4 * i, &w, 4);
        }
        uint32_t tile[] = {0, 0x60FF0000u, 700u | 10u << 16, 5u | 5u << 16};
        uint32_t ln[] = {0, 0x500000FFu, 700u | 40u << 16, 0x00FF0000u, 710u | 40u << 16};
        /* AddPrim(ot + 2, tile): tile.next = ot[2].next; ot[2].next = tile; lengths 3 and 4 */
        memcpy(&w, ram + 0x1000 + 8, 4);
        tile[0] = 3u << 24 | (w & 0xFFFFFF);
        w = (w & 0xFF000000u) | 0x2000u;
        memcpy(ram + 0x1000 + 8, &w, 4);
        memcpy(ram + 0x2000, tile, sizeof tile);
        memcpy(&w, ram + 0x1000 + 4, 4);
        ln[0] = 4u << 24 | (w & 0xFFFFFF);
        w = (w & 0xFF000000u) | 0x2100u;
        memcpy(ram + 0x1000 + 4, &w, 4);
        memcpy(ram + 0x2100, ln, sizeof ln);
        SHOW("ot_nodes", gpu_draw_chain(ram, 0x80001000u + 12));
        SHOW("ot_tile", PIX(702, 12));
        SHOW("ot_line_start", PIX(700, 40));
        SHOW("ot_line_end", PIX(710, 40));
        free(ram);
    }
    /* the display: GP1(05h) start, GP1(08h) 320x240, GP1(03h) on */
    {
        GpuDisplay d;
        uint32_t px;
        gpu_gp1(0x05000000u | 0u | 256u << 10);
        gpu_gp1(0x08000001u);
        gpu_gp1(0x03000000u);
        gpu_get_display(&d);
        printf("display %d %d %d %d %d\n", d.x, d.y, d.w, d.h, d.enabled);
        gpu_display_rgba(gpu_vram, 318, 318, 1, 1, 0, &px);
        SHOW("rgba_white", px);
    }
    return 0;
}
"""


def run_harness(tmp_path: Path) -> dict[str, str]:
    src = tmp_path / "harness.c"
    src.write_text(HARNESS)
    exe = tmp_path / "harness"
    psx = RUNTIME / "psx"
    r = subprocess.run([CLANG, "-std=gnu11", "-O1", "-Wall", "-Werror", f"-I{psx}", str(src), str(psx / "gpu.c"),
                        "-o", str(exe)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    out = subprocess.run([str(exe)], capture_output=True, text=True, check=True).stdout
    vals = {}
    for line in out.splitlines():
        k, _, v = line.partition(" ")
        vals[k] = v
    return vals


@pytest.mark.skipif(CLANG is None, reason="no C compiler")
def test_gpu_rasterises_primitives(tmp_path):
    v = {k: int(x) if x.isdigit() else x for k, x in run_harness(tmp_path).items()}
    red = 0x001F
    assert v["tri_inside"] == red and v["tri_corner"] == red and v["tri_below"] == red
    assert v["tri_right"] == 0 and v["tri_hyp"] == 0 and v["tri_outside"] == 0
    assert v["sprt15"] == 0x8000 | (5 * 16 + 3)
    assert v["sprt15_edge"] == 0
    assert v["sprt4_idx3"] == 6
    assert v["sprt4_idx0"] == 0x001F  # transparent texel: the red fill under it
    assert v["semi"] == 15 | 15 << 5 | 15 << 10 and v["semi_outside"] == 0x7FFF
    assert v["fill"] == 0x7C00 and v["fill_out"] == 0
    assert v["copy"] == 0x7C00
    assert v["load0"] == 0x1111 and v["load1"] == 0x2222
    assert v["clip_in"] == 31 << 5 and v["clip_out"] == 0
    assert v["display"] == "0 256 320 240 1"
    assert v["rgba_white"] == 0xFFFFFFFF


@pytest.mark.skipif(CLANG is None, reason="no C compiler")
def test_gpu_walks_an_ordering_table_in_ram(tmp_path):
    v = run_harness(tmp_path)
    assert int(v["ot_nodes"]) == 6  # 4 entries + the tile + the line
    assert int(v["ot_tile"]) == 0x7C00  # blue
    assert int(v["ot_line_start"]) == 0x001F  # red to blue, gouraud
    assert int(v["ot_line_end"]) == 0x7C00


def test_sony_functions_known_by_their_code():
    """SLUS-01252's DrawSync (func_80176240), matched C whose m2c draft drops its argument: the same words wherever
    libgpu's table lands (the lui/%lo pair masked) are recognised, the C left out, and a forwarder emitted."""
    def words(table_hi, table_lo):
        return [0x3C020000 | table_hi, 0x8C420000 | table_lo, 0x27BDFFE8, 0xAFBF0010, 0x8C42003C, 0, 0x0040F809, 0,
                0x8FBF0010, 0, 0x03E00008, 0x27BD0018]
    image = bytearray(0x200)
    struct.pack_into("<12I", image, 0x100, *words(0x801A, 0x1480))
    struct.pack_into("<12I", image, 0x140, *words(0x8008, 0x1234))  # an overlay's copy, its table elsewhere
    build = {"functions": [
        {"name": "func_80176240", "seg": "main", "vram": 0x80176240, "rom": 0x100, "c": "src/main/func_80176240.c"},
        {"name": "func_80070000", "seg": "ovl", "vram": 0x80070000, "rom": 0x140},
        {"name": "func_80070030", "seg": "ovl", "vram": 0x80070030, "rom": 0x170},
        {"name": "func_80176270", "seg": "main", "vram": 0x80176270, "rom": 0x130}]}
    fp = ep.shape_fingerprint(bytes(image), 0x100, 0x30)
    assert fp == ep.shape_fingerprint(bytes(image), 0x140, 0x30) and ep.SONY_SHAPES[fp][0] == "DrawSync"
    s = ep.sony_by_shape(build, bytes(image), {})
    assert s["func_80176240"]["alias"] == "DrawSync" and s["func_80176240"]["replace"]
    assert s["func_80070000"]["alias"] == "DrawSync" and not s["func_80070000"]["replace"]
    sdk = ep.sdk_stubs_c([{"name": "func_80176240", "lib": "libgpu", "bios": None, "label": s["func_80176240"]["label"],
                           "kind": "alias", "alias": "DrawSync", "called": True}])
    assert 'extern void psx_impl_DrawSync(void) __asm__(PSX_ASM_NAME("DrawSync"));' in sdk
    assert "long func_80176240(long a0, long a1, long a2, long a3, long a4, long a5, long a6, long a7)" in sdk
    # a function still in assembly named like one the runtime implements (SLUS-01252's LoadImage) is the runtime's
    named = ep.sony_by_name({"functions": [{"name": "LoadImage", "seg": "main", "vram": 0x801762C0}]}, {})
    assert named["LoadImage"]["lib"] == "libgpu"
