"""psx/call32.S, the trampoline of every call through a game function pointer (tools/cc_ptr32.py case 5): the
argument registers the call site set (x0-x7, x8, d0-d7) reach the host function unchanged, through the C lookup and
through the cache; and LLVM's `nest` argument is the register the trampoline reads the PS1 address from."""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path

import pytest

from decompnet.rom import export_port as ep

CALL32 = ep.RUNTIME / "psx" / "call32.S"
CLANG = shutil.which("clang")
needs_arm64 = pytest.mark.skipif(not CLANG or platform.machine() not in ("arm64", "aarch64"),
                                 reason="needs clang on arm64")

# the callee's view: x0-x8 and d0-d7 stored where the harness reads them, then 0x1234 returned in x0
SHIM = r"""
#if defined(__APPLE__)
#define SYM(x) _##x
#else
#define SYM(x) x
#endif
    .text
    .globl SYM(capture)
    .p2align 2
SYM(capture):
#if defined(__APPLE__)
    adrp x16, SYM(seen)@PAGE
    add x16, x16, SYM(seen)@PAGEOFF
#else
    adrp x16, SYM(seen)
    add x16, x16, :lo12:SYM(seen)
#endif
    stp x0, x1, [x16, #0]
    stp x2, x3, [x16, #16]
    stp x4, x5, [x16, #32]
    stp x6, x7, [x16, #48]
    str x8, [x16, #64]
    stp d0, d1, [x16, #72]
    stp d2, d3, [x16, #88]
    stp d4, d5, [x16, #104]
    stp d6, d7, [x16, #120]
    mov x0, #0x1234
    ret

/* call_with(addr, regs): x0-x8 = regs[0..8], d0-d7 = regs[9..16] as raw bits, x15 = addr, bl psx_call32 */
    .globl SYM(call_with)
    .p2align 2
SYM(call_with):
    stp x29, x30, [sp, #-16]!
    mov x29, sp
    mov x15, x0
    mov x16, x1
    ldp d0, d1, [x16, #72]
    ldp d2, d3, [x16, #88]
    ldp d4, d5, [x16, #104]
    ldp d6, d7, [x16, #120]
    ldr x8, [x16, #64]
    ldp x6, x7, [x16, #48]
    ldp x4, x5, [x16, #32]
    ldp x2, x3, [x16, #16]
    ldp x0, x1, [x16, #0]
    bl SYM(psx_call32)
    ldp x29, x30, [sp], #16
    ret
"""

# the compiler's side: a call with the PS1 address as the nest argument and two arguments of its own
NEST_IR = """
define i64 @via_nest(i64 %a0, i64 %a1, i32 %addr) {
  %z = zext i32 %addr to i64
  %p = inttoptr i64 %z to ptr
  %r = call i64 @psx_call32(ptr nest %p, i64 %a0, i64 %a1)
  ret i64 %r
}
declare void @psx_call32()
"""

HARNESS = r"""
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>

typedef struct { uint32_t ps1, word0, word1, pad; const void *fn; } PsxCall32Slot;
PsxCall32Slot psx_call32_cache[1024];
uint64_t seen[17];
static uint32_t looked_up[8];
static int n_lookups;
void capture(void);
uint64_t call_with(uint32_t addr, const uint64_t *regs);
uint64_t via_nest(uint64_t a0, uint64_t a1, uint32_t addr);

/* the C lookup behind the trampoline, as psx/dispatch.c's; it clobbers what it can */
void *psx_call32_lookup(uint32_t addr) {
    volatile double junk[8] = {9, 9, 9, 9, 9, 9, 9, 9};
    char buf[256];
    memset(buf, 0x5A, sizeof buf);
    snprintf(buf, sizeof buf, "%f %u", junk[3], addr);
    if (n_lookups < 8) looked_up[n_lookups] = addr;
    n_lookups++;
    return (void *)capture;
}

static int check(const char *what, const uint64_t *regs, uint64_t r) {
    int i, bad = 0;
    for (i = 0; i < 17; i++)
        if (seen[i] != regs[i]) {
            printf("%s: register %d is 0x%llx, the call site set 0x%llx\n", what, i, (unsigned long long)seen[i],
                   (unsigned long long)regs[i]);
            bad = 1;
        }
    if (r != 0x1234) { printf("%s: returned 0x%llx\n", what, (unsigned long long)r); bad = 1; }
    return bad;
}

int main(void) {
    uint64_t regs[17];
    int i, bad = 0;
    uint32_t addr = 0x800C58ECu;
    uint32_t *ram;
    for (i = 0; i < 17; i++) regs[i] = 0x1111111111111111ull * (uint64_t)(i % 15 + 1) + (uint64_t)i;
    /* the slow path: an empty cache */
    bad |= check("lookup", regs, call_with(addr, regs));
    if (n_lookups != 1 || looked_up[0] != addr) { printf("lookup: asked %d times, 0x%08X\n", n_lookups, looked_up[0]); bad = 1; }
    /* the fast path: a slot for the address with the code words that are in RAM there */
    ram = mmap((void *)0x480000000ull, 0x200000, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON | MAP_FIXED, -1, 0);
    if (ram == MAP_FAILED) { printf("mmap failed\n"); return 2; }
    ram[(addr - 0x80000000u) / 4] = 0x27BDFFE8u;
    ram[(addr - 0x80000000u) / 4 + 1] = 0xAFBF0010u;
    {
        PsxCall32Slot *s = &psx_call32_cache[(addr >> 2) & 1023];
        s->ps1 = addr; s->word0 = 0x27BDFFE8u; s->word1 = 0xAFBF0010u; s->fn = (const void *)capture;
    }
    for (i = 0; i < 17; i++) regs[i] ^= 0xA5A5A5A5A5A5A5A5ull;
    memset(seen, 0, sizeof seen);
    bad |= check("cache", regs, call_with(addr, regs));
    if (n_lookups != 1) { printf("cache: the lookup ran on a hit\n"); bad = 1; }
    /* other code in RAM at the address (another overlay): a miss */
    ram[(addr - 0x80000000u) / 4 + 1] = 0;
    memset(seen, 0, sizeof seen);
    bad |= check("overlay", regs, call_with(addr, regs));
    if (n_lookups != 2) { printf("overlay: the cache answered for other code\n"); bad = 1; }
    /* the compiler's nest argument is the register the trampoline reads */
    memset(seen, 0, sizeof seen);
    if (via_nest(0xAAAA0000BBBB0001ull, 0xCCCC0000DDDD0002ull, 0x80123450u) != 0x1234
        || seen[0] != 0xAAAA0000BBBB0001ull || seen[1] != 0xCCCC0000DDDD0002ull
        || n_lookups != 3 || looked_up[2] != 0x80123450u) {
        printf("nest: x0 0x%llx x1 0x%llx, lookup of 0x%08X\n", (unsigned long long)seen[0],
               (unsigned long long)seen[1], looked_up[2]);
        bad = 1;
    }
    if (!bad) printf("ok\n");
    return bad;
}
"""


@needs_arm64
def test_call32_keeps_the_argument_registers(tmp_path: Path):
    (tmp_path / "shim.S").write_text(SHIM)
    (tmp_path / "nest.ll").write_text(NEST_IR)
    (tmp_path / "harness.c").write_text(HARNESS)
    exe = tmp_path / "harness"
    r = subprocess.run([CLANG, "-O2", "-Wno-override-module", str(CALL32), str(tmp_path / "shim.S"),
                        str(tmp_path / "nest.ll"), str(tmp_path / "harness.c"), "-o", str(exe)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stdout + r.stderr


@pytest.mark.skipif(not CLANG, reason="needs clang")
def test_call32_assembles_for_x86_64(tmp_path: Path):
    r = subprocess.run([CLANG, "-target", "x86_64-apple-macos11", "-c", str(CALL32), "-o", str(tmp_path / "c.o")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
