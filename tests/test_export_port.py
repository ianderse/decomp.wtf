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
    assert ".globl SYM(D_801AC5B8)\nSYM(D_801AC5B8) = PSX(0x801AC5B8)" in asm
    assert f"#define PSX_HOST_BASE 0x{ep.PSX_HOST_BASE:X}" in asm  # PS1 address A at host PSX_HOST_BASE + A
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
    assert s["func_8017DE00"]["bios"] == "SYS_01"
    sdk = ep.sdk_stubs_c([{"name": "func_8017DE00", "lib": "libapi", "bios": "SYS_01", "label": "syscall 1",
                           "kind": "bios", "called": True},
                          {"name": "func_8017DD60", "lib": "libapi", "bios": "B0_08", "label": "bios_B0_08_OpenEvent",
                           "kind": "bios", "called": True}])
    assert "int func_8017DE00(void) { return psx_EnterCriticalSection(); }" in sdk
    assert "return psx_bios_OpenEvent(a, b, c, d); }" in sdk


def test_handwritten_gte_setup_is_replayed():
    # SLUS-01252's func_80178F28, libgte's InitGeom body: COP2 on, then ctc2 ZSF3/ZSF4/H/DQA/DQB/OFX/OFY
    words = [0, 0x40026000, 0x3C034000, 0x00431025, 0x40826000, 0, 0x24080155, 0x48C8E800, 0, 0x24080100,
             0x48C8F000, 0, 0x240803E8, 0x48C8D000, 0, 0x2408EF9E, 0x48C8D800, 0, 0x3C080140, 0x48C8E000, 0,
             0x48C0C000, 0x48C0C800, 0, 0x03E00008, 0]
    image = bytearray(0x200)
    struct.pack_into(f"<{len(words)}I", image, 0x100, *words)
    struct.pack_into("<3I", image, 0x180, 0x8C820000, 0x03E00008, 0)  # lw: not a GTE setup
    build = {"functions": [{"name": "func_80178F28", "seg": "main", "vram": 0x80178F28, "rom": 0x100},
                           {"name": "func_80178F90", "seg": "main", "vram": 0x80178F90, "rom": 0x180}]}
    h = ep.handwritten_gte(build, bytes(image))
    assert list(h) == ["func_80178F28"] and h["func_80178F28"]["init_geom"]
    assert h["func_80178F28"]["regs"] == [(29, 0x155), (30, 0x100), (26, 1000), (27, 0xFFFFEF9E), (28, 0x1400000),
                                          (24, 0), (25, 0)]
    c = ep.unmatched_c([], None, h)
    assert "int func_80178F28(void) { psx_gte_ctc2(29, 0x155); psx_gte_ctc2(30, 0x100);" in c
    assert "psx_gte_ctc2(27, (int)0xFFFFEF9Eu);" in c and "InitGeom" in c


def test_cmakelists():
    c = ep.cmakelists("slus-01252")
    assert "project(slus_01252_port C ASM)" in c
    assert "pagezero_size" not in c and "CMAKE_OSX_ARCHITECTURES x86_64" not in c
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


def test_cmakelists_no_longer_requires_x86_64():
    c = ep.cmakelists("slus-01252")
    assert "x86_64" not in c.split("project(")[1].split("\n", 1)[1].replace("-DCMAKE_OSX_ARCHITECTURES=x86_64", "")
    assert "FATAL_ERROR" not in c and "set(CMAKE_OSX_ARCHITECTURES" not in c  # the host's architecture: arm64 on a Mac
    assert "cc_ptr32.py;--functions;${CMAKE_CURRENT_SOURCE_DIR}/gen/psx_function_addrs.txt" in c
    assert ep.host_arch_flags() == []
    assert ep.function_addrs([{"name": "func_80026000", "vram": 0x80026000}, {"name": "func_80026000", "vram": 0x80026000},
                              {"name": "memset", "vram": 0x800FFF54}], {"memset": "psx_game_memset"}).splitlines()[1:] \
        == ["func_80026000 0x80026000", "psx_game_memset 0x800FFF54"]


# ---- the IR transform (tools/cc_ptr32.py): the six cases, on hand-written IR like clang's -O0 output

BASE = ep.PSX_HOST_BASE
FNS = {"func_8017C5F4": 0x8017C5F4}


def _fn(*body):
    return "define void @f(ptr addrspace(271) noundef %0, ptr noundef %1) {\n" + "\n".join(f"  {b}" for b in body) + \
        "\n  ret void\n}\n"


def _patch(*body):
    cc = _tool("cc_ptr32")
    out = cc.patch(_fn(*body), FNS)
    assert cc.leftovers(out) == []
    return out


def test_ir_loads_and_stores_through_a_32_bit_pointer_go_to_the_window():
    out = _patch("%3 = load i16, ptr addrspace(271) %0, align 2",
                 "store volatile ptr addrspace(271) %0, ptr addrspace(271) %0, align 4",
                 "%4 = load i32, ptr addrspace(271) inttoptr (i32 -2146708500 to ptr addrspace(271)), align 4",
                 "%5 = cmpxchg ptr addrspace(271) %0, i32 0, i32 1 seq_cst seq_cst",
                 "%6 = atomicrmw add ptr addrspace(271) %0, i32 1 seq_cst")
    assert "%__p32_1 = ptrtoint ptr addrspace(271) %0 to i64" in out
    assert f"%__p32_2 = or i64 %__p32_1, {BASE}" in out and "%__p32_3 = inttoptr i64 %__p32_2 to ptr" in out
    assert "%3 = load i16, ptr %__p32_3, align 2" in out
    assert "store volatile ptr addrspace(271) %0, ptr %__p32_6, align 4" in out  # the value stays 4 bytes
    assert f"load i32, ptr inttoptr (i64 {BASE + (-2146708500 & 0xFFFFFFFF)} to ptr), align 4" in out  # a raw address, folded
    assert "%5 = cmpxchg ptr %__p32_9, i32 0, i32 1" in out and "%6 = atomicrmw add ptr %__p32_12, i32 1" in out


def test_ir_memory_intrinsics_on_32_bit_pointers_become_the_p0_forms():
    cc = _tool("cc_ptr32")
    ir = _fn("call void @llvm.memcpy.p271.p271.i64(ptr addrspace(271) align 4 %0, ptr addrspace(271) align 4 "
             "addrspacecast (ptr @D_801A0FE0 to ptr addrspace(271)), i64 8, i1 false)",
             "call void @llvm.memset.p271.i32(ptr addrspace(271) align 1 %0, i8 0, i32 16, i1 false)") + \
        "declare void @llvm.memcpy.p271.p271.i64(ptr addrspace(271), ptr addrspace(271), i64, i1 immarg)\n"
    out = cc.patch(ir, FNS)
    assert "call void @llvm.memcpy.p0.p0.i64(ptr align 4 %__p32_3, ptr align 4 @D_801A0FE0, i64 8, i1 false)" in out
    assert "call void @llvm.memset.p0.i32(ptr align 1 %__p32_6, i8 0, i32 16, i1 false)" in out
    assert "declare void @llvm.memcpy.p0.p0.i64(" in out and "declare void @llvm.memset.p0.i32(" in out
    assert "p271" not in out and cc.leftovers(out) == []


def test_ir_addrspacecast_from_32_bits_is_zext_plus_the_base():
    out = _patch("%3 = addrspacecast ptr addrspace(271) %0 to ptr",
                 "call void @g(ptr addrspacecast (ptr addrspace(271) inttoptr (i32 -2147051520 to ptr addrspace(271)) "
                 "to ptr))")
    assert "%__p32_1 = ptrtoint ptr addrspace(271) %0 to i64" in out
    assert f"%__p32_2 = or i64 %__p32_1, {BASE}" in out and "%3 = inttoptr i64 %__p32_2 to ptr" in out
    assert f"call void @g(ptr inttoptr (i64 {BASE + (-2147051520 & 0xFFFFFFFF)} to ptr))" in out


def test_ir_addrspacecast_to_32_bits_is_the_low_half():
    out = _patch("%3 = alloca i32, align 4",
                 "%4 = addrspacecast ptr %3 to ptr addrspace(271)",
                 "store ptr addrspace(271) addrspacecast (ptr @.str to ptr addrspace(271)), ptr %1, align 4")
    assert "%__p32_1 = ptrtoint ptr %3 to i32" in out and "%4 = inttoptr i32 %__p32_1 to ptr addrspace(271)" in out
    assert "store ptr addrspace(271) inttoptr (i32 ptrtoint (ptr @.str to i32) to ptr addrspace(271)), ptr %1" in out


def test_ir_a_game_functions_address_is_its_ps1_address():
    out = _patch("store ptr addrspace(271) addrspacecast (ptr @func_8017C5F4 to ptr addrspace(271)), ptr %1, align 4",
                 "%3 = ptrtoint ptr @func_8017C5F4 to i32",
                 "%4 = icmp eq i32 %3, ptrtoint (ptr @func_8017C5F4 to i32)",
                 "%5 = addrspacecast ptr @func_8017C5F4 to ptr addrspace(271)",
                 "%6 = ptrtoint ptr @host_thing to i32")
    addr = 0x8017C5F4 - (1 << 32)
    assert f"store ptr addrspace(271) inttoptr (i32 {addr} to ptr addrspace(271)), ptr %1" in out
    assert f"%3 = bitcast i32 {addr} to i32" in out and f"%4 = icmp eq i32 %3, {addr}" in out
    assert f"%5 = inttoptr i32 {addr} to ptr addrspace(271)" in out
    assert "%6 = ptrtoint ptr @host_thing to i32" in out  # not a game function: its truncated host address


def test_ir_calls_through_a_32_bit_pointer_ask_the_runtime():
    out = _patch("%3 = load ptr addrspace(271), ptr %1, align 4",
                 "call addrspace(271) void %3(ptr addrspace(271) noundef %0) #2, !dbg !7",
                 "%4 = call addrspace(271) i32 addrspacecast (ptr @g to ptr addrspace(271))(i32 noundef 1)",
                 "call addrspace(271) void inttoptr (i32 -2145926668 to ptr addrspace(271))()")
    assert "%__p32_1 = ptrtoint ptr addrspace(271) %3 to i32" in out
    assert "%__p32_2 = call ptr @psx_host_fn(i32 %__p32_1), !dbg !7" in out
    assert "call void %__p32_2(ptr addrspace(271) noundef %0) #2, !dbg !7" in out
    assert "%4 = call i32 @g(i32 noundef 1)" in out  # a direct call written through a cast
    assert "%__p32_3 = call ptr @psx_host_fn(i32 -2145926668)" in out and "call void %__p32_3()" in out
    assert out.count("declare ptr @psx_host_fn(i32)") == 1


def test_the_window_base_is_one_constant():
    import re
    cc = _tool("cc_ptr32")
    header = (ep.RUNTIME / "psx" / "psx_runtime.h").read_text()
    assert cc.PSX_HOST_BASE == ep.PSX_HOST_BASE
    assert int(re.search(r"#define PSX_HOST_BASE (0x[0-9A-Fa-f]+)ull", header).group(1), 16) == ep.PSX_HOST_BASE
    assert int(re.search(r"#define PSX_LITERAL_BASE (0x[0-9A-Fa-f]+)u", header).group(1), 16) == cc.PSX_LITERAL_BASE
    assert ep.PSX_HOST_BASE & 0xFFFFFFFF == 0 and ep.PSX_HOST_BASE >= 0x300000000  # zext | base; past dyld's cache


def test_ir_string_literals_the_game_points_at_move_into_the_window(tmp_path):
    cc = _tool("cc_ptr32")
    ir = '@.str = private unnamed_addr constant [7 x i8] c"POLYF4\\00", align 1\n' \
         '@.str.1 = private unnamed_addr constant [3 x i8] c"ok\\00", align 1\n' + \
         _fn("call void @CdInit_Print(ptr addrspace(271) noundef addrspacecast (ptr @.str to ptr addrspace(271)))",
             "call void @puts(ptr @.str.1)")
    lits = {}
    out = cc.patch(ir, FNS, lits)
    (sym, data), = lits.items()
    assert data == b"POLYF4\0" and sym.startswith("__psx_lit_")
    assert f"@{sym} = external global [7 x i8], align 1" in out and "@.str =" not in out
    # an absolute symbol in the window, so the 32-bit pointer is its low half
    assert f"inttoptr (i32 ptrtoint (ptr @{sym} to i32) to ptr addrspace(271))" in out
    assert "@.str.1 = private" in out and "call void @puts(ptr @.str.1)" in out  # host-only use: stays
    (tmp_path / f"{sym}.bin").write_bytes(data)
    (tmp_path / "__psx_lit_0000.bin").write_bytes(b"ab")
    asm = cc.collect_literals(str(tmp_path))
    assert f"SYM(__psx_lit_0000) = PSX(0x{cc.PSX_LITERAL_BASE:08X})" in asm
    assert f"SYM({sym}) = PSX(0x{cc.PSX_LITERAL_BASE + 8:08X})" in asm  # 8-aligned
    assert "SYM(psx_literal_size): .long 15" in asm and f"#define PSX(a) (0x{BASE:X} + a)" in asm
    assert "SYM(psx_literal_size): .long 0" in cc.collect_literals(str(tmp_path / "none"))
    # other data of the file whose address goes 32-bit would be host memory: refused
    bad = "@tab = internal global [4 x i32] zeroinitializer, align 4\n" + \
        _fn("store ptr addrspace(271) addrspacecast (ptr @tab to ptr addrspace(271)), ptr %1, align 4")
    try:
        cc.patch(bad, FNS)
        raise AssertionError("expected an IRError")
    except cc.IRError as e:
        assert "@tab" in str(e)


def test_ir_32_bit_address_arithmetic_stays():
    body = ["%3 = ptrtoint ptr addrspace(271) %0 to i32", "%4 = add i32 %3, 16",
            "%5 = inttoptr i32 %4 to ptr addrspace(271)",
            "%6 = getelementptr inbounds i8, ptr addrspace(271) %5, i32 4",
            "%7 = icmp eq ptr addrspace(271) %6, null"]
    out = _patch(*body)
    for b in body:
        assert f"  {b}\n" in out
    assert "__p32" not in out
    # but a 32-bit integer made into an ordinary pointer is a PS1 address
    out = _patch("%3 = inttoptr i32 %4 to ptr")
    assert "%__p32_1 = zext i32 %4 to i64" in out and "%3 = inttoptr i64 %__p32_2 to ptr" in out


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


# SLUS-01252's libcd wrappers: CdControl (func_80168B1C) and CdSyncCallback (func_801689B4), relocated elsewhere
CD_CONTROL = [0x27BDFFE8, 0xAFBF0010, 0x308400FF, 0x0C05A27F, 0x00003821, 0x8FBF0010, 0x2C420001, 0x03E00008,
              0x27BD0018]
CD_SWAP = [0x3C02801F, 0x8C424EB0, 0x3C01801F, 0xAC244EB0, 0x03E00008, 0x00000000]


def _libcd_build():
    """CdRead (named) at 0x80100400; CdControl's code 0xB8 before it; CdSyncCallback's 0x220 before it with other
    relocations (masked out); the same swap code at CdReadyCallback's offset but a word longer"""
    image = bytearray(0x800)
    base = 0x80100000

    def put(vram, words):
        struct.pack_into(f"<{len(words)}I", image, vram - base, *words)

    put(0x80100400 - 0x220, [0x3C028020, 0x8C421234, 0x3C018020, 0xAC241234, 0x03E00008, 0x00000000])
    put(0x80100400 - 0x208, CD_SWAP + [0])
    put(0x80100400 - 0xB8, CD_CONTROL)
    fns = [("func_801001E0", 0x80100400 - 0x220, None), ("func_801001F8", 0x80100400 - 0x208, None),
           ("func_80100348", 0x80100400 - 0xB8, "src/main/func_80100348.c"),
           ("func_8010036C", 0x80100400 - 0x94, None), ("CdRead", 0x80100400, None),
           ("func_80100500", 0x80100500, None)]
    build = {"functions": [dict({"name": n, "seg": "main", "vram": v, "rom": v - base}, **({"c": c} if c else {}))
                           for n, v, c in fns]}
    return build, bytes(image)


def test_libcd_functions_found_by_offset_and_masked_code():
    build, image = _libcd_build()
    sony = {"func_8006DAD0": {"lib": "libetc", "label": "CdDataCallback"}}
    build["functions"].append({"name": "func_8006DAD0", "seg": "ovl_sj00", "vram": 0x8006DAD0, "rom": 0x700})
    a = ep.libcd_aliases(build, image, sony)
    assert a == {"CdRead": "CdRead", "func_80100348": "CdControl", "func_801001E0": "CdSyncCallback",
                 "func_8006DAD0": "CdDataCallback"}  # CdReadyCallback's slot is a word too long: not taken


def test_libcd_replaces_the_games_matched_copy_with_a_thunk():
    build, image = _libcd_build()
    a = ep.libcd_aliases(build, image, {})
    sources = {"src/main/func_80100348.c": "int func_80100348(int a, int *b, int c) { return CD_cw(a, b, c, 0); }"}
    p = ep.plan(build, sources, {}, {}, "", "", {}, {}, a)
    assert p["compiled"] == [] and p["replaced"] == {"src/main/func_80100348.c": "func_80100348"}
    assert "CD_cw" not in p["calls"] and not any(n == "func_80100348" for n, _ in p["unmatched"])
    sdk = ep.sdk_stubs_c(p["sdk"], p["renames"])
    assert ("int func_80100348(int com, unsigned param, unsigned result) { return CdControl(com, param, result); }"
            in sdk)
    assert "unsigned func_801001E0(unsigned func) { return CdSyncCallback(func); }" in sdk
    assert "int CdRead(" not in sdk  # the runtime's own, under its name
    assert '#include "libcd.h"' in sdk
    assert {d["name"]: d["kind"] for d in p["dispatch"]}["func_80100348"] == 3


def test_config_carries_the_disc():
    entry = {"load": 0x800C1400, "size": 0xE0800, "offset": 0x800, "bss": [0x801A1570, 0x801FFA20],
             "bss_detected": True, "pc": 0x801676E8, "sp": 0x801FFFF0, "gp": 0x801A0F68}
    c = ep.config_c("SLUS-01252", "baserom.exe", entry, "func_800C27B8", '/discs/Arc "II".bin')
    assert '"/discs/Arc \\"II\\".bin", /* the disc image' in c
    assert '"", /* the disc image' in ep.config_c("SLUS-01252", "baserom.exe", entry, "func_800C27B8")
