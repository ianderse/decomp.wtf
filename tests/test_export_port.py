"""The port export's generation (export_port.py) and its build tools (port_runtime/tools): pure functions on small
inputs. No compiler, no game."""

import importlib.util
import struct

from decompnet.rom import export_port as ep


def _tool(name):
    spec = importlib.util.spec_from_file_location(f"port_{name}", ep.RUNTIME / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


REG = {"zero": 0, "at": 1, "v0": 2, "v1": 3, "a0": 4, "gp": 28, "sp": 29, "ra": 31}


def _i(op, rs, rt, imm):
    return (op << 26) | (REG[rs] << 21) | (REG[rt] << 16) | (imm & 0xFFFF)


def _exe(code: list[int], load=0x800C1400, pc=0x801676E8) -> bytes:
    """A PS-X EXE whose start-up code is `code`, at pc."""
    size = 0xE0800
    img = bytearray(0x800 + size)
    img[:8] = b"PS-X EXE"
    struct.pack_into("<4I", img, 0x10, pc, 0, load, size)
    struct.pack_into("<I", img, 0x30, 0x801FFFF0)
    off = pc - load + 0x800
    for k, w in enumerate(code):
        struct.pack_into("<I", img, off + 4 * k, w)
    return bytes(img)


def _crt0():
    jal = lambda a: (3 << 26) | ((a >> 2) & 0x3FFFFFF)  # noqa: E731
    return [
        _i(0x0F, "zero", "v0", 0x801A), _i(0x09, "v0", "v0", 0x1570),        # lui/addiu v0 = bss start
        _i(0x0F, "zero", "v1", 0x8020), _i(0x09, "v1", "v1", -0x5E0),        # v1 = bss end
        _i(0x2B, "v0", "zero", 0), _i(0x09, "v0", "v0", 4),                  # loop: sw zero,0(v0); addiu
        (REG["v0"] << 21) | (REG["v1"] << 16) | (REG["at"] << 11) | 0x2B,    # sltu at, v0, v1
        _i(0x05, "at", "zero", -4), 0,                                       # bnez at, loop; nop
        _i(0x0F, "zero", "gp", 0x801A), _i(0x09, "gp", "gp", 0x0F68),        # gp
        jal(0x8017DEA0), 0, jal(0x800C27B8), 0,                              # InitHeap-like, then main
        0x0000000D,                                                          # break
    ]


def test_entry_point_and_bss_from_the_start_up_code():
    e = ep.detect_entry(_exe(_crt0()))
    assert e["load"] == 0x800C1400 and e["size"] == 0xE0800 and e["pc"] == 0x801676E8
    assert e["bss"] == (0x801A1570, 0x801FFA20) and e["bss_detected"]
    assert e["gp"] == 0x801A0F68
    assert e["calls"] == [0x8017DEA0, 0x800C27B8] and e["main"] == 0x800C27B8


def test_entry_without_a_clear_loop_falls_back_to_image_end():
    jal = (3 << 26) | ((0x800C27B8 >> 2) & 0x3FFFFFF)
    e = ep.detect_entry(_exe([jal, 0, 0x0000000D]))
    assert not e["bss_detected"] and e["bss"] == (0x800C1400 + 0xE0800, 0x80200000)
    assert e["main"] == 0x800C27B8


BUILD = {
    "image": "baserom.exe",
    "functions": [
        {"name": "func_800C27B8", "seg": "main", "vram": 0x800C27B8, "rom": 0x800, "c": "src/main/func_800C27B8.c"},
        {"name": "func_800C2838", "seg": "main", "vram": 0x800C2838, "rom": 0x880},
        {"name": "VSync", "seg": "main", "vram": 0x80170000, "rom": 0x900},
        {"name": "SsSeqPlay", "seg": "main", "vram": 0x80170100, "rom": 0x980},
        {"name": "func_8017CF60", "seg": "main", "vram": 0x8017CF60, "rom": 0xA00},
        {"name": "func_80029D58", "seg": "ovl_a", "vram": 0x80029D58, "rom": 0xB00},
        {"name": ".L800294FC", "seg": "ovl_a", "vram": 0x800294FC, "rom": 0xB80},
        {"name": "memset", "seg": "main", "vram": 0x800FFF54, "rom": 0xC00, "c": "src/main/memset.c"},
        {"name": "func_800C3000", "seg": "main", "vram": 0x800C3000, "rom": 0xC80, "c": "src/main/func_800C3000.c"},
    ],
    "symbols": {"D_801A1920": 0x801A1920},
}
SOURCES = {
    "src/main/func_800C27B8.c": '#include "common.h"\nextern s32 D_801AC5B8;\nextern u8 func_80029D58[];\n'
                                'extern s32 g_primPtr;\nvoid func_800C27B8(void) { D_801A1920 = &D_801AC5B8; '
                                'func_800C2838(); VSync(0); SsSeqPlay(1, 2, 3); func_8017CF60(0, 0, 4); '
                                'memset(0, 0, 1); g_primPtr = func_80029D58[2]; /* D_80123456 in a comment */ }\n',
    "src/main/memset.c": '#include "common.h"\nvoid memset(u8 *p, s32 c, s32 n) { while (n--) *p++ = c; }\n',
    "src/main/func_800C3000.c": '#include "common.h"\nvoid func_800C3000(void) { broken }\n',
}
HEADERS = {"common.h": "typedef int s32;\ntypedef unsigned char u8;\ns32 *D_801A1920;\n"
                       "typedef struct { short vx, vy, vz, pad; } SVECTOR;\n"}
SONY = {"VSync": {"lib": "libetc", "bios": None, "label": "VSync"},
        "SsSeqPlay": {"lib": "libsnd", "bios": None, "label": "SsSeqPlay"},
        "func_8017CF60": {"lib": "libapi", "bios": "A0_2A", "label": "bios_A0_2A_memcpy"}}
SYMBOLS = "func_800C27B8 = 0x800C27B8; // type:func rom:0x800\ng_primPtr = 0x801A18C0;\n"


def _plan():
    return ep.plan(BUILD, SOURCES, HEADERS, SONY, SYMBOLS, "", {"src/main/func_800C3000.c": "expected ';'"})


def test_symbols_at_their_ps1_addresses():
    p = _plan()
    s = p["symbols"]
    assert s["D_801AC5B8"] == 0x801AC5B8 and s["D_801A1920"] == 0x801A1920  # from its name; from build.json
    assert s["g_primPtr"] == 0x801A18C0                                        # from symbol_addrs.txt
    assert s["func_80029D58"] == 0x80029D58     # unmatched, used only as data: an absolute symbol, no stub
    assert "D_80123456" not in s                # comments don't count
    assert "func_800C27B8" not in s and "VSync" not in s
    asm = ep.symbols_asm(s, p["renames"])
    assert ".globl SYM(D_801AC5B8)\nSYM(D_801AC5B8) = 0x801AC5B8" in asm
    assert "#define SYM(x) _##x" in asm


def test_unmatched_and_sdk_stubs():
    p = _plan()
    names = [n for n, _ in p["unmatched"]]
    assert "func_800C2838" in names and "func_800C3000" in names  # in assembly; its C doesn't compile
    assert ".L800294FC" not in names and "func_80029D58" not in names and "VSync" not in names
    c = ep.unmatched_c(p["unmatched"], p["renames"])
    assert 'int func_800C2838(void) { return psx_unmatched("func_800C2838"); }' in c
    assert "its C is excluded: expected ';'" in c
    by = {s["name"]: s for s in p["sdk"]}
    assert by["VSync"]["kind"] == "implemented" and by["SsSeqPlay"]["kind"] == "stub"
    assert by["func_8017CF60"]["kind"] == "bios" and all(s["called"] for s in p["sdk"])
    sdk = ep.sdk_stubs_c(p["sdk"], p["renames"])
    assert "VSync" not in sdk                                       # the runtime's own
    assert 'int SsSeqPlay(void) { return psx_sdk_stub("SsSeqPlay", &calls_SsSeqPlay); }' in sdk
    assert "void * func_8017CF60(void *a, const void *b, int n) { return psx_bios_memcpy(PSX_PTR(a), PSX_PTR(b), n); }" in sdk
    assert "/* ---- libsnd */" in sdk


def test_c_library_names_are_renamed_and_the_dispatch_table():
    p = _plan()
    assert p["renames"] == {"memset": "psx_game_memset"}
    assert "    memset=psx_game_memset\n" in ep.game_sources_cmake(p["compiled"], p["renames"])
    image = bytearray(0x1000)
    struct.pack_into("<2I", image, 0x800, 0x27BDFFE8, 0xAFBF0010)
    fn = ep.functions_c(p["dispatch"], bytes(image), p["renames"])
    assert '{0x800C27B8u, 0x27BDFFE8u, 0xAFBF0010u, func_800C27B8, "func_800C27B8", "main", 0},' in fn
    assert 'psx_game_memset, "memset", "main", 0}' in fn
    assert "func_80029D58," not in fn  # data only: no host function


def test_sdk_config_and_library_classification():
    h = ep.sdk_config_h(HEADERS, ["PadStartCom"])
    assert "#define PSX_COMMON_HAS_SVECTOR 1" in h and "PSX_COMMON_HAS_MATRIX" not in h
    assert "#define PSX_GAME_HAS_PadStartCom 1" in h
    assert ep.sony_library("SsSeqPlay") == "libsnd" and ep.sony_library("SpuSetReverb") == "libspu"
    assert ep.sony_library("GetTPage", ["PRIM.OBJ"]) == "libgpu"
    assert ep.sony_library("RotTransPers4", ["CMB.OBJ"]) == "libgte"
    assert ep.sony_library("CdRead", ["SYS.OBJ"]) == "libcd" and ep.sony_library("ResetGraph", ["SYS.OBJ"]) == "libgpu"
    assert ep.sony_library("func_8017CF60", bios="A0_2A") == "libapi"
    assert ep.sony_library("text_A4", ["PDCMD2.OBJ"]) == "libpad"


def test_syscall_wrappers_become_sony_stubs():
    image = bytearray(0x40)
    struct.pack_into("<4I", image, 0x10, 0x24040001, 0x0000000C, 0x03E00008, 0)
    build = {"functions": [{"name": "func_8017DE00", "seg": "main", "vram": 0x8017DE00, "rom": 0x10},
                           {"name": "func_8017DE20", "seg": "main", "vram": 0x8017DE20, "rom": 0x20}]}
    s = ep.syscall_stubs(build, bytes(image), {})
    assert list(s) == ["func_8017DE00"] and "EnterCriticalSection" in s["func_8017DE00"]["label"]


def test_cmakelists():
    c = ep.cmakelists("slus-01252")
    assert "project(slus_01252_port C ASM)" in c
    assert "-Wl,-pagezero_size,0x1000" in c and "set(CMAKE_OSX_ARCHITECTURES x86_64" in c
    for flag in ("-std=gnu89", "-fwrapv", "-fno-strict-aliasing", "-fno-builtin", "-DDECOMP_PORT=1",
                 "-Wno-int-conversion", "-fcommon"):
        assert flag in c
    assert '"${PSX_SOURCE_DIR}/include"' not in c and "${PSX_SOURCE_DIR}/include" in c
    assert "add_executable(game" in c and "tools/ptr32.py" in c and "tools/cc_ptr32.py" in c
    assert "-include ${CMAKE_CURRENT_SOURCE_DIR}/include/psx_port_game.h" in c
    assert c.index("CMAKE_CURRENT_SOURCE_DIR}/include ${PTR32_DIR}/include") > 0  # host overrides first


def test_ptr32_rewrite():
    ptr32 = _tool("ptr32")
    P = "* __ptr32 __uptr "
    types = ptr32.typedef_names("typedef int s32;\ntypedef void (*FnVoid)(void);\ntypedef struct { int a; } T, *PT;\n")
    assert types == {"s32", "FnVoid", "T", "PT"}
    r = ptr32.rewrite
    assert r("void *p, *q;", types) == f"void {P}p, {P}q;"
    assert r("s32 x = a * b, *y;", types) == f"s32 x = a * b, {P}y;"
    assert r("x = *p * 2;", types) == "x = *p * 2;"
    assert r("s32 n = (s32)*p;", types) == "s32 n = (s32)*p;"
    assert r("void (*fp)(s32 *);", types) == f"void ({P}fp)(s32 {P});"
    assert r("M((u8 (**)())x);", types | {"u8"}) == f"M((u8 ({P}{P})())x);"
    assert r("struct S { char *a; } *s, *t;", types) == f"struct S {{ char {P}a; }} {P}s, {P}t;"
    assert r("long a; unsigned long b; long long c; long int d;", types) == "int a; unsigned int b; long long c;  int d;"
    assert r('#define NULL ((void *)0)\n#include "x.h"\nchar *s = "a * b";\n', types) == \
        f'#define NULL ((void {P})0)\n#include "x.h"\nchar {P}s = "a * b";\n'
    assert r("/* T *p */ T *p;", types) == f"/* T *p */ T {P}p;"


def test_ir_patch_for_calls_and_copies_through_32_bit_pointers():
    cc = _tool("cc_ptr32")
    ir = """define void @f(ptr addrspace(271) noundef %0) #0 {
  %3 = load ptr addrspace(271), ptr %2, align 4
  call addrspace(271) void %3(ptr addrspace(271) noundef %0) #2
  %4 = call addrspace(271) i32 addrspacecast (ptr @g to ptr addrspace(271))(i32 noundef 1)
  call void @llvm.memcpy.p271.p271.i64(ptr addrspace(271) align 4 %0, ptr addrspace(271) align 4 inttoptr (i32 -2147051008 to ptr addrspace(271)), i64 8, i1 false)
  ret void
}
declare void @llvm.memcpy.p271.p271.i64(ptr addrspace(271) noalias writeonly, ptr addrspace(271) noalias readonly, i64, i1 immarg)
"""
    out = cc.patch(ir)
    assert "%__p32c1 = addrspacecast ptr addrspace(271) %3 to ptr" in out
    assert "call void %__p32c1(ptr addrspace(271) noundef %0)" in out
    assert "%4 = call i32 @g(i32 noundef 1)" in out
    assert "addrspacecast ptr addrspace(271) inttoptr (i32 -2147051008 to ptr addrspace(271)) to ptr" in out
    assert "call void @llvm.memcpy.p0.p0.i64(ptr align 4 %__p32c2, ptr align 4 %__p32c3, i64 8, i1 false)" in out
    assert "declare void @llvm.memcpy.p0.p0.i64(" in out and "p271.p271" not in out


def test_run_log_summary():
    log = """psx: calling the game's main, func_800C27B8
trace: enters func_800C27B8
trace: enters func_801676E0
sdk: intInit (call 1): not implemented yet, returns 0
sdk: intInit (call 2): not implemented yet, returns 0
unmatched: func_80178F28 (1 so far)
psx: func_80178F28 is not decompiled yet (it is still assembly in the export): stopping here.
"""
    s = ep.summarize_run(log)
    assert s["entered"] == ["func_800C27B8", "func_801676E0"] and s["stopped_at"] == "func_80178F28"
    assert s["story"] == ("calls func_800C27B8, func_801676E0; first Sony stub reached: intInit; first function still "
                          "in assembly: func_80178F28; stops at func_80178F28 (not decompiled)")
    assert "(call 2)" not in s["log_head"]
