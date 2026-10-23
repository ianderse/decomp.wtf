"""The port's entry trace (tools/cc_ptr32.py --trace with psx/trace.S) and the dropped-argument hazards
(export_port.call_hazards): the IR the trace puts in, the thunk keeping the argument registers (compiled and run
when clang is here), and the three detectors on hand-written files."""

import importlib.util
import platform
import shutil
import subprocess
import sys

import pytest

from decompnet.rom import export_port as ep


def _cc():
    spec = importlib.util.spec_from_file_location("port_cc_ptr32", ep.RUNTIME / "tools" / "cc_ptr32.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- the trace

IR = """target triple = "arm64-apple-macosx15.0.0"

define void @func_800F5120(ptr addrspace(271) noundef %0) #0 !dbg !10 {
  %2 = alloca ptr addrspace(271), align 4
  ret void
}

define internal void @helper() {
  ret void
}

declare void @func_80104228()
"""


def test_trace_call_first_in_each_game_function_only():
    cc = _cc()
    out = cc.patch(IR, {"func_800F5120": 0x800F5120, "func_80104228": 0x80104228}, trace=True)
    lines = out.split("\n")
    k = next(i for i, ln in enumerate(lines) if ln.startswith("define void @func_800F5120"))
    call = lines[k + 1]
    assert call.startswith('  call void asm sideeffect "bl _psx_trace_thunk", "{x16},{x17},')
    assert call.endswith("(i64 2148487456, ptr @func_800F5120)")
    # x0-x9 are the thunk's to keep, never declared clobbered; the rest of the caller-saved ones are
    for r in range(0, 10):
        assert f"~{{x{r}}}" not in call
    for r in range(10, 16):
        assert f"~{{x{r}}}" in call
    assert "~{lr}" in call and "~{x30}" not in call  # LLVM saves lr for ~{lr} only
    assert all(f"~{{q{r}}}" in call for r in range(16, 32)) and not any(f"~{{q{r}}}," in call for r in range(0, 16))
    assert out.count("psx_trace_thunk") == 1  # not in helper (no game function), not in a declare
    assert cc.patch(IR, {"func_800F5120": 0x800F5120}) == cc.patch(IR, {"func_800F5120": 0x800F5120}, trace=False)
    assert "psx_trace_thunk" not in cc.patch(IR, {"func_800F5120": 0x800F5120})


def test_trace_call_for_elf_and_x86_64():
    cc = _cc()
    assert cc.ir_arch('target triple = "aarch64-unknown-linux-gnu"\n') == ("aarch64", False)
    assert cc.ir_arch('target triple = "x86_64-apple-macosx15.0.0"\n') == ("x86_64", True)
    assert '"bl psx_trace_thunk"' in cc.trace_call(0x80010000, "f", "aarch64", False)
    x86 = cc.trace_call(0x80010000, "f", "x86_64", True)
    assert "leaq -128(%rsp), %rsp\\0Acall _psx_trace_thunk\\0Aleaq 128(%rsp), %rsp" in x86
    assert '"{r10},{r11},~{xmm8}' in x86 and "~{rdi}" not in x86 and "~{xmm0}" not in x86


def test_cmake_trace_is_opt_in():
    c = ep.cmakelists("slus-01252")
    assert "-finstrument-functions" not in c.split("elseif(PSX_TRACE)")[0]
    assert "${_psx_trace_default})" in c and "set(_psx_trace_default OFF)" in c
    assert 'string(APPEND _launcher ";--trace")' in c
    assert (ep.RUNTIME / "psx" / "trace.S").exists()


HOOK = r"""
#include <stdint.h>
#include <stdio.h>
static uint32_t got_ps1; static const void *got_fn; static int n;
__attribute__((noinline)) int clobber(int a, int b, int c, int d, int e, int f, int g, int h) {
    return a + b + c + d + e + f + g + h;
}
__attribute__((noinline)) double fclobber(double a, double b, double c, double d) { return a * b + c * d; }
volatile int sink; volatile double fsink;
void psx_trace_enter(uint32_t ps1, const void *fn) {
    got_ps1 = ps1; got_fn = fn; n++;
    sink = clobber(11, 22, 33, 44, 55, 66, 77, 88);   /* x0-x7 are the hook's to use */
    fsink = fclobber(1.5, 2.5, 3.5, 4.5);             /* and d0-d3 */
}
"""

# the case from SLUS-01252: func_80104228's matched C calls func_800F5120() (an empty, unprototyped declaration) from
# a `void` function, and func_800F5120 takes the argument: on MIPS $a0 passes through; here x0 must
CALLEE = "int callee(int a) { return a * 3; }\n"
CALLER = "int callee();\nint caller(void) { return callee() + 1; }\n"
MAIN = HOOK + r"""
int caller(void);
int main(void) {
    int r = ((int (*)(int))caller)(1234);
    printf("r=%d n=%d ps1=%08X fn_is_caller=%d\n", r, n, got_ps1, got_fn == (const void *)caller);
    return 0;
}
"""

# the thunk on its own: every argument register (x0-x7, x8, d0-d7) set to a known value, the thunk called, read back
THUNK_CALL = r"""
    .text
    .globl _call_thunk
    .p2align 2
_call_thunk:                 // x0: uint64_t out[18]
    stp x29, x30, [sp, #-32]!
    mov x29, sp
    str x19, [sp, #16]
    mov x19, x0
    mov x0, #100
    mov x1, #101
    mov x2, #102
    mov x3, #103
    mov x4, #104
    mov x5, #105
    mov x6, #106
    mov x7, #107
    mov x8, #108
    fmov d0, #1.0
    fmov d1, #2.0
    fmov d2, #3.0
    fmov d3, #4.0
    fmov d4, #5.0
    fmov d5, #6.0
    fmov d6, #7.0
    fmov d7, #8.0
    mov x16, #0x5120
    movk x16, #0x800F, lsl #16
    adrp x17, _call_thunk@PAGE
    add x17, x17, _call_thunk@PAGEOFF
    bl _psx_trace_thunk
    stp x0, x1, [x19, #0]
    stp x2, x3, [x19, #16]
    stp x4, x5, [x19, #32]
    stp x6, x7, [x19, #48]
    stp x8, x16, [x19, #64]
    stp d0, d1, [x19, #80]
    stp d2, d3, [x19, #96]
    stp d4, d5, [x19, #112]
    stp d6, d7, [x19, #128]
    ldr x19, [sp, #16]
    ldp x29, x30, [sp], #32
    ret
"""
THUNK_MAIN = HOOK + r"""
#include <string.h>
void call_thunk(uint64_t *out);
int main(void) {
    uint64_t out[18]; double d[8]; int i, ok = 1;
    call_thunk(out);
    for (i = 0; i <= 8; i++) ok &= out[i] == 100u + i;
    ok &= out[9] == 0x800F5120u;
    memcpy(d, &out[10], sizeof d);
    for (i = 0; i < 8; i++) ok &= d[i] == 1.0 + i;
    printf("ok=%d n=%d ps1=%08X fn_is_thunk_caller=%d\n", ok, n, got_ps1, got_fn == (const void *)call_thunk);
    return 0;
}
"""

_ARM_MAC = sys.platform == "darwin" and platform.machine() == "arm64" and shutil.which("clang")


def _run(tmp, *cmd):
    r = subprocess.run(list(cmd), cwd=tmp, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


@pytest.mark.skipif(not _ARM_MAC, reason="needs clang on arm64 macOS")
def test_thunk_keeps_every_argument_register(tmp_path):
    (tmp_path / "call.S").write_text(THUNK_CALL)
    (tmp_path / "main.c").write_text(THUNK_MAIN)
    _run(tmp_path, "clang", "-O1", "main.c", "call.S", str(ep.RUNTIME / "psx" / "trace.S"), "-o", "t")
    assert _run(tmp_path, "./t").strip() == "ok=1 n=1 ps1=800F5120 fn_is_thunk_caller=1"


@pytest.mark.skipif(not _ARM_MAC, reason="needs clang on arm64 macOS")
def test_trace_keeps_a_passed_through_argument(tmp_path):
    (tmp_path / "a.c").write_text(CALLEE)
    (tmp_path / "b.c").write_text(CALLER)
    (tmp_path / "main.c").write_text(MAIN)
    (tmp_path / "fns.txt").write_text("caller 0x80104228\ncallee 0x800F5120\n")
    tool = str(ep.RUNTIME / "tools" / "cc_ptr32.py")
    for f in ("a", "b"):
        _run(tmp_path, sys.executable, tool, "--functions", "fns.txt", "--trace", "clang", "-O1", "-c", f"{f}.c",
             "-o", f"{f}.o")
    _run(tmp_path, "clang", "-O1", "main.c", "a.o", "b.o", str(ep.RUNTIME / "psx" / "trace.S"), "-o", "t")
    # 1234 passes through caller's x0 into callee: 1234 * 3 + 1; the trace ran for both
    assert _run(tmp_path, "./t").strip() == "r=3703 n=2 ps1=800F5120 fn_is_caller=0"


@pytest.mark.skipif(not _ARM_MAC, reason="needs clang on arm64 macOS")
def test_finstrument_functions_loses_it(tmp_path):
    """The control: what the port did before (-finstrument-functions) hands callee the caller's own address."""
    (tmp_path / "a.c").write_text(CALLEE)
    (tmp_path / "b.c").write_text(CALLER)
    (tmp_path / "main.c").write_text(MAIN + "void __cyg_profile_func_enter(void *fn, void *site) "
                                            "{ (void)site; psx_trace_enter(0, fn); }\n"
                                            "void __cyg_profile_func_exit(void *fn, void *site) { (void)fn; (void)site; }\n")
    _run(tmp_path, "clang", "-O1", "-finstrument-functions", "-c", "a.c", "b.c")
    _run(tmp_path, "clang", "-O1", "main.c", "a.o", "b.o", "-o", "t")
    assert not _run(tmp_path, "./t").startswith("r=3703 ")


# ---- the dropped-argument detectors (export_port.call_hazards)

M2C = """#include "common.h"
#ifndef M2C_DEFINES
#define M2C_DEFINES
typedef s32 M2C_UNK;
#define M2C_FIELD(expr, type_ptr, offset) (*(type_ptr)((s8 *)(expr) + (offset)))
#endif
"""
FILES = {
    "src/main/func_A.c": M2C + "void func_A(void *arg0) {\n    M2C_FIELD(arg0, s32 *, 0) = 0;\n}\n",
    "src/main/func_B.c": M2C + "s32 func_B(s32 a, s32 b) { return a + b; }\n",
    "src/main/func_C.c": M2C + "void func_C(void) { }\n",
    "src/main/func_V.c": M2C + "void func_V(s32 fmt, ...) { }\n",
    "src/main/func_D.c": M2C + "void func_D(s32 n) { }\n",
    "src/main/caller.c": M2C + """
M2C_UNK func_A();                                   /* extern */
s32 func_B(s32);
void func_C(void);
void func_V(s32, ...);
void func_Y(void);
M2C_UNK func_Z();
extern void (*fp)(int);
static s32 table[] = { 1, 2, 3 };
typedef struct { s32 (*cb)(void); s32 x; } Rec;

void caller(void) {
    Rec *s;
    M2C_UNK (*temp_v0)();
    func_A();
    func_B(1);
    func_B(1, 2);
    func_C();
    func_V(1);
    func_D();
    func_Y();
    func_Z();
    func_Q();
    s->func_A();
    M2C_FIELD(D_801A1480, M2C_UNK (**)(), 0x2C)();
    M2C_FIELD(D_801A1480, void (**)(void), 0x30)();
    M2C_FIELD(D_801A1480, M2C_UNK (**)(), 0x34)(1, 2);
    (*((M2C_UNK (**)()) (((s8 *) s) + 0xD8)))();
    temp_v0 = s->cb;
    temp_v0();
}
""",
}
HEADERS = {"common.h": "typedef int s32;\ntypedef signed char s8;\nvoid func_D(void);\nvoid func_W(s32 a, s32 b);\n"}


def _hazards(asm_reads=None, replaced=None, runtime=None):
    functions = {"func_A", "func_B", "func_C", "func_V", "func_D", "caller", "func_Y", "func_Z", "func_Q", "func_W"}
    matched = {n: f"src/main/{n}.c" for n in ("func_A", "func_B", "func_C", "func_V", "func_D", "caller")}
    return ep.call_hazards(FILES, ["src/main/caller.c"], HEADERS, functions, matched, replaced, runtime,
                           asm_reads if asm_reads is not None else {"func_Y": [0], "func_Z": []})


def test_c_toplevel_definitions_and_prototypes():
    top = ep.c_toplevel(FILES["src/main/caller.c"])
    assert list(top["definitions"]) == ["caller"]
    p = top["prototypes"]
    assert p["func_A"]["n"] is None and p["func_B"]["n"] == 1 and p["func_C"]["n"] == 0
    assert p["func_V"]["variadic"] and p["func_V"]["n"] == 1
    assert "fp" not in p and "table" not in p and "cb" not in p  # pointers and data declare no function
    d = ep.c_toplevel(FILES["src/main/func_A.c"])["definitions"]
    assert d["func_A"][0]["n"] == 1 and d["func_A"][0]["text"] == "(void *arg0)"


def test_dropped_arguments_against_the_definition():
    h = _hazards()
    got = {(d["callee"], d["declared"], d["args"], d["params"]) for d in h["dropped"]}
    assert got == {("func_A", "()", 0, 1),        # the func_80104228 case: unprototyped, called with nothing
                   ("func_B", "fewer", 1, 2),     # a prototype with fewer parameters
                   ("func_D", "none", 0, 1)}      # only common.h's (void) in sight
    a = next(d for d in h["dropped"] if d["callee"] == "func_A")
    assert a["caller"] == "caller" and a["defined"] == "(void *arg0)" and a["definition"] == "matched C"
    d = next(d for d in h["dropped"] if d["callee"] == "func_D")
    assert d["declaration"] == "(void)"
    # matching (func_C), a variadic definition (func_V), a full call (func_B(1, 2)), a member (s->func_A()): none
    assert h["counts"]["dropped"] == 3 and h["counts"]["dropped_by_declaration"] == {"()": 1, "fewer": 1, "none": 1}


def test_dropped_arguments_against_the_runtime_that_replaces_a_function():
    h = _hazards(replaced={"func_C": "DrawSync"}, runtime={"DrawSync": ep._parse_params("int mode")})
    c = next(d for d in h["dropped"] if d["callee"] == "func_C")
    assert c["definition"] == "the runtime's DrawSync" and c["params"] == 1 and c["declared"] == "(void)"


def test_table_calls_with_no_arguments():
    calls = [t["call"] for t in _hazards()["table_calls"]]
    # (**)(void) is prototyped; the 0x34 call passes its arguments; temp_v0 is declared `(*temp_v0)()`
    assert calls == ["M2C_FIELD(D_801A1480, M2C_UNK (**)(), 0x2C)", "(*((M2C_UNK (**)()) (((s8 *) s) + 0xD8)))",
                     "temp_v0"]


def test_calls_to_assembly_with_no_arguments():
    h = _hazards()
    u = {x["callee"]: x for x in h["unmatched"]}
    assert set(u) == {"func_Y", "func_Z", "func_Q"}
    assert u["func_Y"]["evidence"] == "reads $a0" and u["func_Y"]["params"] == 1 and u["func_Y"]["declared"] == "(void)"
    assert u["func_Z"]["evidence"] == "reads no argument register" and u["func_Z"]["params"] == 0
    assert u["func_Q"]["evidence"] == "unknown" and u["func_Q"]["params"] is None and u["func_Q"]["declared"] == "none"
    c = h["counts"]
    assert (c["unmatched"], c["unmatched_takes_args"], c["unmatched_no_args"], c["unmatched_unknown"]) == (3, 1, 1, 1)


def _r(rs, rt, rd, funct):
    return (rs << 21) | (rt << 16) | (rd << 11) | funct


def _imm(op, rs, rt, imm):
    return (op << 26) | (rs << 21) | (rt << 16) | (imm & 0xFFFF)


A0, A1, A2, A3, V0, SP, RA = 4, 5, 6, 7, 2, 29, 31


def test_mips_argument_registers_read_first():
    jr_ra, nop = _r(RA, 0, 0, 8), 0
    assert ep.mips_arg_reads([_imm(0x23, A0, V0, 0), jr_ra, nop]) == [0]                       # lw v0, 0(a0)
    assert ep.mips_arg_reads([_imm(0x09, 0, A1, 5), _r(A1, A2, V0, 0x21), jr_ra, nop]) == [2]  # a1 set, a2 read
    assert ep.mips_arg_reads([jr_ra, nop, _imm(0x23, A0, V0, 0)]) == []                         # past the return
    jal = (3 << 26) | 0x40000
    # the delay slot (move a0, a1) runs before the call; after it a0-a3 are the callee's
    assert ep.mips_arg_reads([jal, _r(A1, 0, A0, 0x21), _imm(0x23, A3, V0, 0), jr_ra, nop]) == [1]
    assert ep.mips_arg_reads([_imm(0x2B, SP, A3, 16), jr_ra, nop]) == [3]                      # sw a3, 16(sp)


def test_what_the_dropped_register_holds_at_the_call():
    callee = 0x800F5120
    jal, j = (3 << 26) | (callee >> 2 & 0x3FFFFFF), (2 << 26) | (callee >> 2 & 0x3FFFFFF)
    other = (3 << 26) | 0x40000
    prologue = _imm(0x09, SP, SP, -24)  # addiu sp, sp, -24
    # func_80104228: addiu sp; sw ra; jal func_800F5120; nop: $a0 is its own argument
    assert ep.passes_through([prologue, _imm(0x2B, SP, RA, 16), jal, 0], callee, 0, 1) == "pass-through"
    assert ep.passes_through([j, 0], callee, 0, 1) == "pass-through"                       # a tail call
    assert ep.passes_through([other, 0, jal, 0], callee, 0, 1) == "leftover"               # a call came first
    assert ep.passes_through([_imm(0x09, 0, A0, 1), jal, 0], callee, 0, 1) == "leftover"   # a0 written
    assert ep.passes_through([jal, _imm(0x09, 0, A1, 1)], callee, 0, 2) == "leftover"      # in the delay slot
    assert ep.passes_through([_imm(0x09, 0, A0, 1), jal, 0], callee, 1, 2) == "pass-through"  # a0 is passed; a1 kept
    assert ep.passes_through([other, 0], callee, 0, 1) == "?"
    assert ep.passes_through([jal, _imm(0x09, 0, A0, 1), jal, 0], callee, 0, 1, nth=1) == "leftover"  # the 2nd


def test_what_the_dropped_register_holds_along_the_control_flow():
    vram, callee = 0x80010000, 0x800F5120
    jal, other, jr_ra = (3 << 26) | (callee >> 2 & 0x3FFFFFF), (3 << 26) | 0x40000, _r(RA, 0, 0, 8)
    # if (x) { g(); return; } f();  -- the call to g is on the other branch
    words = [_imm(5, 3, V0, 5), 0, other, 0, jr_ra, 0, jal, 0, jr_ra, 0]
    assert ep.passes_through(words, callee, 0, 1) == "leftover"  # in a straight line, g came first
    assert ep.passes_through(words, callee, 0, 1, vram=vram) == "pass-through"
    # do { f(); } while (v0): the second time round $a0 is what f left
    loop = [jal, 0, _imm(5, V0, 0, -3), 0, jr_ra, 0]
    assert ep.passes_through(loop, callee, 0, 1, vram=vram) == "leftover"
    assert ep.passes_through([jal, _imm(0x09, 0, A1, 1), jr_ra, 0], callee, 0, 2, vram=vram) == "leftover"
    assert ep.passes_through([other, 0, jr_ra, 0], callee, 0, 1, vram=vram) == "?"


def test_argument_registers_live_at_entry():
    jr_ra = _r(RA, 0, 0, 8)
    # if (a0 == 0) return; ... reads a1 after the first return
    words = [_imm(4, A0, 0, 3), 0, jr_ra, 0, _r(A1, 0, V0, 0x21), jr_ra, 0]
    assert ep.mips_arg_reads(words) == [0] and ep.mips_arg_live(words, 0x80010000) == [0, 1]
    # a call: what its callee reads (reads_of), then the registers are its
    g = 0x80020000
    call = [(3 << 26) | (g >> 2 & 0x3FFFFFF), 0, _r(A2, 0, V0, 0x21), jr_ra, 0]
    assert ep.mips_arg_live(call, 0x80010000, lambda v: [1] if v == g else None) == [1]
    assert ep.mips_arg_live(call, 0x80010000) == [0, 1, 2, 3]  # an unknown callee may read all four


def test_port_md_lists_the_hazards():
    h = _hazards()
    h["dropped"][0]["register"] = "pass-through"
    md = ep.call_hazards_md(h)
    assert "## Arguments the matched C drops" in md and "3 calls in 1 functions" in md
    assert "| `caller` | `func_A` | `()` | `(void *arg0)` | 0 |" in md
    assert "none (common.h `(void)`)" in md and "`caller`: `M2C_FIELD(D_801A1480, M2C_UNK (**)(), 0x2C)()`" in md
    assert "`caller` -> `func_Y` (declared `(void)`): reads $a0" in md and "`caller` -> `func_Q` (unknown)" in md
    assert ep.call_hazards_md(None) == ""


# ---- calls through a cast of a game function

CAST_FILES = {
    "src/main/func_A.c": "void func_A(void *arg0) {\n}\n",
    "src/main/func_B.c": "s32 func_B(s32 a, s32 b) {\n    return a + b;\n}\n",
    "src/main/func_R.c": "void func_R(s32 a) {\n}\n",
    "src/main/caller.c": """typedef int s32;
typedef s32 (*Fn)();

void caller(void *arg0) {
    void (*cb)(void);
    s32 (*full)(s32, s32) = func_B;
    ((void (*)()) func_A)();
    ((void (*)(void *)) func_A)(arg0);
    (*(s32 (*)(s32)) &func_B)(1);
    ((Fn) func_B)(1, 2);
    cb = (void (*)(void)) func_A;
    cb();
    full(1, 2);
    if (((s32 (*)(void)) func_R)() == 0) {
        (*cb)();
    }
}
""",
}


def test_cast_calls_found_with_their_types():
    body = ep.c_toplevel(CAST_FILES["src/main/caller.c"])["definitions"]["caller"][1]
    calls = ep.cast_calls(body, {"func_A", "func_B", "func_R"})
    got = [(c["callee"], c["nargs"], c["via"], [g["text"] for g in c["sigs"]]) for c in calls]
    assert got == [
        ("func_A", 0, "cast", ["void (*)()"]),
        ("func_A", 1, "cast", ["void (*)(void *)"]),
        ("func_B", 1, "cast", ["s32 (*)(s32)"]),
        ("func_B", 2, "cast", []),  # a typedef: no type written here to rewrite
        ("func_A", 0, "cb", ["void (*cb)(void)", "void (*)(void)"]),  # stored in a variable, then called
        ("func_B", 2, "full", ["s32 (*full)(s32, s32)"]),
        ("func_R", 0, "cast", ["s32 (*)(void)"]),  # a cast with another return type than the definition's
        ("func_A", 0, "cb", ["void (*cb)(void)", "void (*)(void)"]),
    ]
    g = calls[2]["sigs"][0]
    assert body[g["params"][0]:g["params"][1] + 1] == "(s32)" and body[g["ret"][0]:g["ret"][1]].strip() == "s32"


def test_cast_calls_dropping_arguments_are_hazards():
    matched = {n: f"src/main/{n}.c" for n in ("func_A", "func_B", "func_R", "caller")}
    h = ep.call_hazards(CAST_FILES, ["src/main/caller.c"], {}, set(matched), matched)
    got = sorted((d["callee"], d["args"], d["params"], d["declared"], d["declaration"]) for d in h["dropped"])
    assert got == [("func_A", 0, 1, "cast", "void (*)()"), ("func_B", 1, 2, "cast", "s32 (*)(s32)"),
                   ("func_R", 0, 1, "cast", "s32 (*)(void)")]  # (func_A with 0 through cb: the same as the first)
    assert h["counts"]["cast_calls"] == 8 and h["counts"]["dropped_through_cast"] == 3
    assert "8 of them, 3 dropping arguments" in ep.call_hazards_md(h)
