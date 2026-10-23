"""Export a PS1 project for porting: a CMake project that compiles the game's matched C for the host (clang, 64-bit)
against a PS1 runtime layer, links, and runs until the first thing the runtime doesn't do yet.

It builds on the rebuildable tree (export.py): the matched C is used from ../source/src as it is, so a re-export is
picked up. What it adds, in <project>/port/:

- PS1 memory in a 4 GB window of the host's address space: PS1 address A at PSX_HOST_BASE + A (0x4_0000_0000 + A,
  clear of the executable and the dyld shared cache), so RAM is 2 MB at 0x4_8000_0000, the game's stack and its
  string literals are mapped in the window too, and the low 32 bits of a host
  pointer into it are the PS1 address. The executable's image is copied in and its bss cleared, so raw addresses,
  32-bit pointers in the game's data and overlay images all work once the game's accesses are translated: the game's
  files are compiled to LLVM IR and every access through a 32-bit pointer becomes one through zext(p) + PSX_HOST_BASE
  (tools/cc_ptr32.py). Native arm64 (the host's architecture); docs/plans/port.md has the measurements.
- every data symbol the C uses as an absolute symbol at PSX_HOST_BASE + its PS1 address (gen/psx_symbols.S), so each
  file's `extern` lands in that RAM; common.h's tentative definitions become common symbols the absolute ones override.
- a software GTE behind the same macro names as the export's psx_gte.h; one stub per function still in assembly
  (logs, then stops unless PSX_PORT_SOFT=1); Sony's library functions as logging stubs, a few implemented (VSync,
  the environment calls, the pad, libgte's matrix arithmetic, the BIOS's C library calls).
- a table of every function at its PS1 address, so a call through a function pointer read from the game's data (a
  MIPS address) lands on the host function (psx_host_fn); the overlays that share an address are told apart by their
  code in RAM. A game function's address taken in the C is its PS1 address too (gen/psx_function_addrs.txt).

Generation is a set of pure functions (tested in tests/test_export_port.py); export_port() reads the tree and the
project's database, runs them, and optionally builds and runs the result.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import struct
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RUNTIME = Path(__file__).with_name("port_runtime")
PSX_HOST_BASE = 0x400000000  # PS1 address A lives at host PSX_HOST_BASE + A (psx/psx_runtime.h, tools/cc_ptr32.py agree)
RAM_BASE, RAM_SIZE = 0x80000000, 0x200000
SCRATCH_BASE, SCRATCH_SIZE = 0x1F800000, 0x400

# flags for the game's own files: matching code written for GCC 2.x on a 32-bit MIPS
GAME_FLAGS = ["-std=gnu89", "-fwrapv", "-fno-strict-aliasing", "-fno-builtin", "-fcommon", "-w",
              "-Wno-int-conversion", "-Wno-incompatible-pointer-types", "-Wno-pointer-to-int-cast",
              "-Wno-int-to-pointer-cast", "-Wno-implicit-function-declaration", "-Wno-implicit-int",
              "-Wno-return-type", "-Wno-incompatible-function-pointer-types", "-DDECOMP_PORT=1"]
PTR32_FLAGS = ["-fms-extensions"]  # __ptr32 __uptr (tools/ptr32.py)
GAME_OPT = "-O1"

# Sony functions the runtime implements (psx/sdk/*.c): no stub for these
IMPLEMENTED = {
    "VSync": "libetc", "ResetGraph": "libgpu", "SetGraphDebug": "libgpu", "SetDispMask": "libgpu",
    "SetDefDrawEnv": "libgpu", "SetDefDispEnv": "libgpu", "PutDrawEnv": "libgpu", "PutDispEnv": "libgpu",
    "PadInitDirect": "libpad", "PadStartCom": "libpad", "PadStopCom": "libpad",
    "SetRotMatrix": "libgte", "SetTransMatrix": "libgte", "RotMatrix": "libgte", "TransMatrix": "libgte",
    "ScaleMatrix": "libgte", "MulMatrix0": "libgte", "ApplyMatrix": "libgte", "SquareRoot0": "libgte",
    "InitGeom": "libgte", "SetGeomOffset": "libgte", "SetGeomScreen": "libgte",
    "OpenEvent": "libapi", "CloseEvent": "libapi", "EnableEvent": "libapi", "DisableEvent": "libapi",
    "EnterCriticalSection": "libapi", "ExitCriticalSection": "libapi",
}
# the BIOS's C library calls (A0 table slot -> wrapper signature) the runtime implements behind the game's stub names
BIOS_LIBC = {
    "A0_2A": ("void *", "void *a, const void *b, int n", "psx_bios_memcpy(PSX_PTR(a), PSX_PTR(b), n)"),
    "A0_2B": ("void *", "void *a, int c, int n", "psx_bios_memset(PSX_PTR(a), c, n)"),
    "A0_2C": ("void *", "void *a, const void *b, int n", "psx_bios_memmove(PSX_PTR(a), PSX_PTR(b), n)"),
    "A0_2E": ("void *", "const void *a, int c, int n", "psx_bios_memchr(PSX_PTR(a), c, n)"),
    "A0_1B": ("int", "const char *a", "psx_bios_strlen(PSX_PTR(a))"),
    "A0_19": ("char *", "char *a, const char *b", "psx_bios_strcpy(PSX_PTR(a), PSX_PTR(b))"),
    "A0_1A": ("char *", "char *a, const char *b, int n", "psx_bios_strncpy(PSX_PTR(a), PSX_PTR(b), n)"),
    "A0_15": ("char *", "char *a, const char *b", "psx_bios_strcat(PSX_PTR(a), PSX_PTR(b))"),
    "A0_2F": ("int", "void", "psx_bios_rand()"),
    "A0_30": ("int", "unsigned a", "(psx_bios_srand(a), 0)"),
    # the heap, in RAM (InitHeap's region)
    "A0_39": ("int", "void *a, unsigned n", "(psx_bios_InitHeap(PSX_PTR(a), n), 0)"),
    "A0_33": ("void *", "unsigned n", "psx_bios_malloc(n)"),
    "A0_34": ("int", "void *a", "(psx_bios_free(PSX_PTR(a)), 0)"),
    "A0_37": ("void *", "unsigned n, unsigned size", "psx_bios_calloc(n, size)"),
    "A0_38": ("void *", "void *a, unsigned n", "psx_bios_realloc(PSX_PTR(a), n)"),
    # events: a handle each, never delivered yet
    "B0_08": ("int", "unsigned a, unsigned b, unsigned c, unsigned d", "psx_bios_OpenEvent(a, b, c, d)"),
    "B0_09": ("int", "unsigned a", "psx_bios_CloseEvent(a)"),
    "B0_0C": ("int", "unsigned a", "psx_bios_EnableEvent(a)"),
    "B0_0D": ("int", "unsigned a", "psx_bios_DisableEvent(a)"),
    # libapi's syscall wrappers (syscall_stubs)
    "SYS_01": ("int", "void", "psx_EnterCriticalSection()"),
    "SYS_02": ("int", "void", "psx_ExitCriticalSection()"),
}
# names the host's C library defines: a game function called that is renamed (-Dmemset=psx_game_memset) in the game's
# files and the generated tables, so the runtime's own calls still reach the C library
RESERVED = set("""memset memcpy memmove memcmp memchr strlen strcpy strncpy strcat strncat strcmp strncmp strchr strrchr
strstr strtol strtoul atoi atol abs labs div rand srand malloc free calloc realloc printf sprintf vsprintf snprintf fprintf
puts putchar getchar exit abort atexit qsort bsearch sin cos tan atan atan2 sqrt pow floor ceil fabs time clock open close
read write lseek signal raise setjmp longjmp main toupper tolower isdigit isalpha isspace isupper islower bzero bcopy
index rindex sleep usleep wait fork kill alarm pause sync stat fstat mmap munmap getenv system remove rename""".split())
_C_NAME = re.compile(r"^[A-Za-z_]\w*$")
SDK_STRUCTS = ["RECT", "DR_ENV", "DRAWENV", "DISPENV", "MATRIX", "SVECTOR", "VECTOR", "CVECTOR"]
LIBRARIES = ["libgpu", "libgte", "libspu", "libsnd", "libcd", "libpad", "libapi", "libetc", "libcard", "libpress"]

_OBJECT_LIB = [  # PSY-Q object name (prefix) -> library
    (("GEO", "MSC", "MTX", "REG", "SMP", "CMB", "FGO", "RCOS", "RSIN", "SQRT", "RATAN"), "libgte"),
    (("EXT", "PRIM", "OTAG", "FONT", "TMD"), "libgpu"),
    (("VSYNC", "INTR", "CALLBACK", "PAD.", "PADI"), "libetc"),
    (("CDROM", "EVENT", "96VEC", "CDREAD", "C_"), "libcd"),
    (("SC", "SE", "SS", "PAUSE", "REPLAY", "SEQ", "STOP", "VMANAGER", "UT_", "VS_", "VM_"), "libsnd"),
    (("SPU", "S_"), "libspu"),
    (("PD",), "libpad"),
    (("CARD", "MCRD"), "libcard"),
    (("LIBPRESS", "VLC"), "libpress"),
    (("COUNTER", "SPRINTF", "NONE", "FILE", "KERNEL"), "libapi"),
]
_BIOS = re.compile(r"bios_([ABC]0)_([0-9A-F]{2})(?:_(\w+))?")
_HEX8 = re.compile(r"(?:^|_)([0-9A-Fa-f]{8})(?=_|$)")
_IDENT = re.compile(r"\b[A-Za-z_]\w*\b")
_C_KEYWORDS = set("""auto break case char const continue default do double else enum extern float for goto if inline int
long register restrict return short signed sizeof static struct switch typedef union unsigned void volatile while asm
__asm__ __volatile__ __attribute__ __extension__ __inline __inline__ __typeof__ typeof""".split())


class PortError(RuntimeError):
    pass


# ---------------------------------------------------------------------------------------------- parsing helpers

def strip_c(text: str) -> str:
    """C source without comments, string/char literals and preprocessor lines (kept as blank lines)."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("\n" * text.count("\n", i, j))
            i = j
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            out.append(" 0 ")
            i = j + 1
        elif c == "#" and (i == 0 or text[i - 1] == "\n"):
            j = i
            while True:  # continued lines
                k = text.find("\n", j)
                if k < 0:
                    j = n
                    break
                if text[k - 1] != "\\":
                    j = k
                    break
                j = k + 1
            line = text[i:j]
            # keep #define bodies: a macro can name a symbol
            out.append(line.split(None, 2)[2] if line.startswith("#define") and len(line.split(None, 2)) > 2 else "")
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def c_identifiers(text: str) -> set[str]:
    return {m for m in _IDENT.findall(strip_c(text)) if m not in _C_KEYWORDS}


def address_from_name(name: str) -> int | None:
    """The PS1 address an auto-generated name carries (D_801A18E0, jtbl_80012340, aPolyf4_801A12E0): the first
    8-hex-digit part that is in RAM or the scratchpad."""
    for m in _HEX8.finditer(name):
        v = int(m.group(1), 16)
        if RAM_BASE <= v < RAM_BASE + RAM_SIZE or SCRATCH_BASE <= v < SCRATCH_BASE + SCRATCH_SIZE:
            return v
    return None


def parse_symbol_addrs(text: str) -> list[tuple[str, int, str]]:
    """symbol_addrs.txt lines: (name, address, the comment after //)."""
    out = []
    for m in re.finditer(r"^\s*(\w+)\s*=\s*(0x[0-9A-Fa-f]+)\s*;[ \t]*(?://(.*))?$", text, re.M):
        out.append((m.group(1), int(m.group(2), 16), (m.group(3) or "").strip()))
    return out


def sony_library(name: str, objects: list[str] | None = None, bios: str | None = None) -> str:
    """Which PSY-Q library a Sony function belongs to: by its name first, then its object file."""
    if bios:
        return "libapi"
    if re.match(r"(Cd|CD_|_96_|St[A-Z]|cb_read|def_cb|DsC)", name):
        return "libcd"
    if re.match(r"(Ss|_Ss|Snd_|SpuVm|GetMetaEvent|_Snd|_SsVm)", name):
        return "libsnd"
    if re.match(r"(Spu|_spu|_Spu|_gcSPU|_waitfs|_print$)", name):
        return "libspu"
    if re.match(r"(Pad|_pad)", name):
        return "libpad"
    if re.match(r"(MDEC|DecDCT|DecDCTin|ivlc)", name):
        return "libpress"
    if re.match(r"(_card|InitCARD|StartCARD|_new_card|MemCard)", name):
        return "libcard"
    if re.match(r"(VSync|v_wait|ResetCallback|.*Callback$|intInit|setIntCallback|stopInit|VBLNK|intVBLNK|DMA|intDMA)",
                name):
        return "libetc"
    for obj in objects or []:
        up = obj.upper()
        if up == "SYS.OBJ":  # libgpu's SYS.OBJ and libcd's
            return "libgpu"
        if up == "BIOS.OBJ":
            return "libcd"
        for prefixes, lib in _OBJECT_LIB:
            if up.startswith(prefixes):
                return lib
    return "libapi"


def sony_from_rows(rows) -> dict[str, dict]:
    """The database's Sony rows (status 'asm', meta with "library" or a BIOS call stub) as
    {name: {"lib", "bios", "label", "vram"}}."""
    out = {}
    for r in rows:
        meta = json.loads(r["meta"] or "{}")
        lib = meta.get("library") or {}
        m = _BIOS.search(meta.get("asm_code") or "")
        bios = f"{m.group(1)}_{m.group(2)}" if m and not lib else None
        label = (m.group(0) if bios else lib.get("name")) or r["name"]
        out[r["name"]] = {"lib": sony_library(r["name"], lib.get("objects"), bios), "bios": bios, "label": label,
                          "objects": lib.get("objects") or [], "vram": r["vram"]}
    return out


SYSCALLS = {0: "Exception", 1: "EnterCriticalSection", 2: "ExitCriticalSection", 3: "ChangeThreadSubFunction"}


def syscall_stubs(build: dict, image: bytes, known: dict[str, dict]) -> dict[str, dict]:
    """libapi's syscall wrappers (`li $a0, N; syscall; jr $ra; nop`) still in assembly and not marked as Sony's in the
    database (SLUS-01252's EnterCriticalSection and ExitCriticalSection at 0x8017DE00/10): Sony stubs too."""
    out = {}
    for f in build["functions"]:
        if f.get("c") or f["name"] in known or f.get("rom") is None or f["rom"] + 16 > len(image):
            continue
        w = struct.unpack_from("<4I", image, f["rom"])
        if (w[0] & 0xFFFF0000) == 0x24040000 and w[1] == 0x0000000C and w[2] == 0x03E00008 and w[3] == 0:
            n = w[0] & 0xFFFF
            out[f["name"]] = {"lib": "libapi", "bios": f"SYS_{n:02X}", "label": f"syscall {n}: {SYSCALLS.get(n, '?')}",
                              "objects": [], "vram": f["vram"]}
    return out


GTE_CONTROL = {24: "OFX", 25: "OFY", 26: "H", 27: "DQA", 28: "DQB", 29: "ZSF3", 30: "ZSF4"}


def handwritten_gte(build: dict, image: bytes, skip=frozenset(), max_insns: int = 64) -> dict[str, dict]:
    """Functions still in assembly whose whole body sets GTE control registers from constants: COP2 enabled in the
    status register (mfc0/or/mtc0 $12), li + ctc2, jr $ra. libgte's InitGeom ends in one (SLUS-01252's
    func_80178F28: ZSF3/ZSF4 = 0x155/0x100, H = 1000, DQA/DQB, OFX = OFY = 0), handwritten so it never matches. The
    runtime replays its ctc2s on the software GTE. Decoded from the words (capstone stops at COP2 instructions).
    {name: {"regs": [(reg, value)], "label", "init_geom", "vram"}}"""
    out = {}
    for f in build["functions"]:
        if f.get("c") or f["name"] in skip or f.get("rom") is None or f["rom"] + 8 > len(image):
            continue
        regs: dict[int, int] = {0: 0}
        sets: list[tuple[int, int]] = []
        ok, ended, after_jr = True, False, False
        n = min(max_insns, (len(image) - f["rom"]) // 4)
        for w in struct.unpack_from(f"<{n}I", image, f["rom"]):
            op, rs, rt, rd, funct = w >> 26, (w >> 21) & 31, (w >> 16) & 31, (w >> 11) & 31, w & 63
            imm = w & 0xFFFF
            simm = imm - 0x10000 if imm & 0x8000 else imm
            if w == 0:
                pass
            elif op == 0x10 and rs in (0, 4) and rd == 12:   # mfc0/mtc0 $12 (status: COP2 on)
                if rs == 0:
                    regs.pop(rt, None)
            elif op == 0x0F:                                # lui
                regs[rt] = (imm << 16) & 0xFFFFFFFF
            elif op in (0x08, 0x09) and rs in regs:         # addi/addiu
                regs[rt] = (regs[rs] + simm) & 0xFFFFFFFF
            elif op == 0x0D and rs in regs:                 # ori
                regs[rt] = regs[rs] | imm
            elif op == 0 and funct in (0x21, 0x25) and rd:  # addu/or
                if rs in regs and (w >> 16) & 31 in regs:
                    regs[rd] = (regs[rs] + regs[rt]) & 0xFFFFFFFF if funct == 0x21 else regs[rs] | regs[rt]
                else:
                    regs.pop(rd, None)
            elif op == 0x12 and rs == 6 and rt in regs:     # ctc2
                sets.append((rd, regs[rt]))
            elif op == 0 and funct == 0x08 and rs == 31 and not after_jr:  # jr $ra, then its delay slot
                after_jr = True
                continue
            else:
                ok = False
                break
            if after_jr:
                ended = True
                break
        if ok and ended and sets:
            is_init = {29, 30} <= {r for r, _ in sets}
            label = ("InitGeom's handwritten body" if is_init else "a handwritten GTE setup") + ": " + ", ".join(
                f"{GTE_CONTROL.get(r, f'c{r}')} = 0x{v & 0xFFFFFFFF:X}" for r, v in sets)
            out[f["name"]] = {"regs": sets, "label": label, "init_geom": is_init, "vram": f["vram"]}
    return out


# ---------------------------------------------------------------------------------------------- the entry point

def _mips_ops(image: bytes, offset: int, pc: int, count: int):
    import capstone
    md = capstone.Cs(capstone.CS_ARCH_MIPS, capstone.CS_MODE_MIPS32 + capstone.CS_MODE_LITTLE_ENDIAN)
    md.detail = False
    return list(md.disasm(image[offset:offset + count * 4], pc))


def exe_header(image: bytes) -> dict:
    if image[:8] != b"PS-X EXE":
        raise PortError("not a PS-X EXE")
    pc, gp, load, size = struct.unpack_from("<4I", image, 0x10)
    sp, sp_size = struct.unpack_from("<2I", image, 0x30)
    return {"pc": pc, "gp": gp, "load": load, "size": size, "sp": sp or 0x801FFFF0, "offset": 0x800}


def detect_entry(image: bytes, max_insns: int = 256) -> dict:
    """From the PS-X EXE's initial pc, follow the start-up code (Sony's crt0) with constant tracking: the range its
    first loop clears (bss), $gp, every jal, and the game's main (the last jal before the code ends with break,
    jr or j). Only straight-line code and one backward loop are understood, which is what crt0 is."""
    h = exe_header(image)
    off = h["pc"] - h["load"] + h["offset"]
    regs: dict[str, int] = {"$zero": 0}
    calls, bss, at_addr = [], None, {}
    end = None

    def imm(s: str) -> int:
        return int(s, 0)

    for ins in _mips_ops(image, off, h["pc"], max_insns):
        at_addr.setdefault(ins.address, dict(regs))
        ops = [o.strip() for o in ins.op_str.split(",")] if ins.op_str else []
        mn = ins.mnemonic
        try:
            if mn == "lui":
                regs[ops[0]] = (imm(ops[1]) << 16) & 0xFFFFFFFF
            elif mn in ("addiu", "addi") and ops[1] in regs:
                regs[ops[0]] = (regs[ops[1]] + imm(ops[2])) & 0xFFFFFFFF
            elif mn == "ori" and ops[1] in regs:
                regs[ops[0]] = regs[ops[1]] | (imm(ops[2]) & 0xFFFF)
            elif mn == "move" and ops[1] in regs:
                regs[ops[0]] = regs[ops[1]]
            elif mn in ("or", "addu") and ops[1] in regs and ops[2] in regs:
                v = regs[ops[1]] | regs[ops[2]] if mn == "or" else regs[ops[1]] + regs[ops[2]]
                regs[ops[0]] = v & 0xFFFFFFFF
            elif mn in ("jal",):
                calls.append(imm(ops[0]))
            elif mn in ("bnez", "beqz", "bne", "beq", "bgtz", "blez", "bltz", "bgez") and ops:
                target = imm(ops[-1])
                if target < ins.address and bss is None:
                    entry = at_addr.get(target, {})
                    loop_regs = set()
                    for prior in _mips_ops(image, off + (target - h["pc"]), target, (ins.address - target) // 4 + 1):
                        loop_regs.update(r.strip().split("(")[-1].rstrip(")") for r in prior.op_str.split(","))
                    vals = sorted({entry[r] for r in loop_regs if r in entry and r not in ("$gp", "$sp", "$zero")
                                   and RAM_BASE <= entry[r] <= RAM_BASE + RAM_SIZE})
                    if len(vals) >= 2:
                        bss = (vals[0], vals[-1])
            elif mn in ("break", "jr", "j", "b"):
                end = ins.address
                break
            elif ops and ops[0].startswith("$") and mn not in ("sw", "sh", "sb", "nop", "mtc0", "mtc2", "ctc2"):
                regs.pop(ops[0], None)  # anything else writing its first operand: no longer known
        except (ValueError, IndexError):
            continue
    image_end = h["load"] + h["size"]
    out = {**h, "calls": calls, "end": end, "gp": regs.get("$gp", h["gp"]),
           "bss": bss or (image_end, RAM_BASE + RAM_SIZE), "bss_detected": bss is not None,
           "main": calls[-1] if calls else None}
    return out


# ---------------------------------------------------------------------------------------------- the plan

def plan(build: dict, sources: dict[str, str], headers: dict[str, str], sony: dict[str, dict],
         symbol_text: str = "", auto_text: str = "", excluded: dict[str, str] | None = None,
         handwritten: dict[str, dict] | None = None) -> dict:
    """Decide what the port is made of. sources: {path in the tree: text} of the matched C files; headers:
    {name: text} of include/*.h; excluded: {path: why} matched files left out (they don't compile for the host);
    handwritten: handwritten_gte()'s functions, which the runtime replays instead of stubbing."""
    excluded = excluded or {}
    handwritten = {k: v for k, v in (handwritten or {}).items() if k not in sony}
    fns = build["functions"]
    compiled = [f for f in fns if f.get("c") and f["c"] not in excluded]
    host_c = {f["name"] for f in compiled}
    texts = {p: t for p, t in sources.items() if p not in excluded}
    idents: set[str] = set()
    data_use: set[str] = set()
    for t in list(texts.values()) + list(headers.values()):
        flat = strip_c(t)
        idents.update(m for m in _IDENT.findall(flat) if m not in _C_KEYWORDS)
        data_use.update(m.group(1) for m in re.finditer(r"\b([A-Za-z_]\w*)\s*(?:\+(?!\+)|\[)", flat))
    calls: set[str] = set()
    for t in texts.values():
        calls.update(re.findall(r"\b([A-Za-z_]\w*)\s*\(", strip_c(t)))

    asm_names = []
    excluded_names = {f["name"] for f in fns if f.get("c") in excluded}
    seen = host_c | excluded_names
    for f in fns:
        if f["name"] not in seen and _C_NAME.match(f["name"]):
            seen.add(f["name"])
            asm_names.append(f["name"])
    # functions used as data (func_X + N, func_X[i]): unmatched ones become absolute symbols if never called by name
    as_data_unmatched = sorted(n for n in asm_names if n in data_use and n not in calls and n not in sony)
    as_data_matched = sorted(n for n in host_c if n in data_use)
    stubbed = [n for n in asm_names if n not in as_data_unmatched]
    unmatched = [(n, None) for n in stubbed if n not in sony and n not in handwritten]
    replayed = {n: handwritten[n] for n in stubbed if n in handwritten}
    by_name = {f["name"]: f for f in fns}
    for path, why in sorted(excluded.items()):
        f = next((g for g in fns if g.get("c") == path), None)
        if f:
            unmatched.append((f["name"], why))
    sdk = []
    for n in stubbed:
        if n in sony:
            s = sony[n]
            kind = "implemented" if n in IMPLEMENTED else ("bios" if s.get("bios") in BIOS_LIBC else "stub")
            sdk.append({"name": n, "lib": IMPLEMENTED.get(n, s["lib"]), "bios": s.get("bios"), "label": s["label"],
                        "kind": kind, "called": n in calls})
    host_fn = host_c | {n for n, _ in unmatched} | {s["name"] for s in sdk} | set(replayed)

    explicit: dict[str, int] = {}
    for name, addr, comment in parse_symbol_addrs(symbol_text):
        if "type:func" not in comment:
            explicit.setdefault(name, addr)
    for name, addr, _ in parse_symbol_addrs(auto_text):
        explicit.setdefault(name, addr)
    for name, addr in (build.get("symbols") or {}).items():
        explicit.setdefault(name, addr)
    for n in as_data_unmatched:
        explicit[n] = by_name[n]["vram"]
    symbols, unresolved = {}, []
    for name in sorted(idents - host_fn):
        addr = explicit.get(name)
        if addr is None:
            addr = address_from_name(name)
        if addr is not None:
            symbols[name] = addr
    for name in sorted((calls - host_fn - set(symbols)) - _C_KEYWORDS):
        if name in by_name or re.match(r"func_[0-9A-F]{8}", name):
            unresolved.append(name)

    dispatch = []
    for f in fns:
        n = f["name"]
        if n not in host_fn:
            continue
        kind = 0 if n in host_c else 3 if n in replayed else 1 if n not in sony else (3 if n in IMPLEMENTED else 2)
        dispatch.append({"vram": f["vram"], "rom": f.get("rom"), "name": n, "seg": f["seg"], "kind": kind})
    dispatch.sort(key=lambda d: (d["vram"], d["seg"]))
    renames = {n: f"psx_game_{n}" for n in sorted(host_fn | set(symbols)) if n in RESERVED}
    return {"renames": renames, "game_implements": sorted(n for n in IMPLEMENTED if n in host_c),"compiled": [f["c"] for f in compiled], "unmatched": unmatched, "sdk": sdk, "symbols": symbols,
            "as_data_matched": as_data_matched, "as_data_unmatched": as_data_unmatched, "unresolved": unresolved,
            "dispatch": dispatch, "excluded": dict(excluded), "calls": calls, "replayed": replayed}


# ---------------------------------------------------------------------------------------------- emitters

def symbols_asm(symbols: dict[str, int], renames: dict[str, str] | None = None) -> str:
    lines = ["/* every data symbol the game's C uses, at its PS1 address in the host's window: PS1 address A at",
             " * PSX_HOST_BASE + A (psx/psx_runtime.h). Generated by decomp.wtf's port export. */",
             "#if defined(__APPLE__)", "#define SYM(x) _##x", "#else", "#define SYM(x) x", "#endif",
             f"#define PSX_HOST_BASE 0x{PSX_HOST_BASE:X}", "#define PSX(a) (PSX_HOST_BASE + a)"]
    for name, addr in sorted(symbols.items(), key=lambda kv: (kv[1], kv[0])):
        name = (renames or {}).get(name, name)
        lines += [f".globl SYM({name})", f"SYM({name}) = PSX(0x{addr:08X})"]
    return "\n".join(lines) + "\n"


def unmatched_c(unmatched: list[tuple[str, str | None]], renames: dict[str, str] | None = None,
                replayed: dict[str, dict] | None = None) -> str:
    out = ['/* one stub per function still in assembly (or whose C does not compile for the host): generated */',
           '#include "psx_runtime.h"', ""]
    if replayed:
        out += ["void psx_gte_ctc2(int reg, int value);",
                "/* handwritten functions that only set GTE control registers: replayed on the software GTE */"]
        for name, h in replayed.items():
            c = (renames or {}).get(name, name)
            body = " ".join(f"psx_gte_ctc2({r}, {_c_int(v)});" for r, v in h["regs"])
            out.append(f"/* {h['label']} */")
            out.append(f"int {c}(void) {{ {body} return 0; }}")
        out.append("")
    for name, why in unmatched:
        c = (renames or {}).get(name, name)
        if why:
            out.append(f"/* excluded from the build: {why} */")
            out.append(f'int {c}(void) {{ return psx_unmatched("{name} (its C is excluded: '
                       f'{_c_string(why)})"); }}')
        else:
            out.append(f'int {c}(void) {{ return psx_unmatched("{name}"); }}')
    return "\n".join(out) + "\n"


def _hx(v: int) -> str:
    """0x4_8000_0000: a host address, grouped as the docs write them."""
    h = f"{v:X}"
    return "0x" + (h[:-8] + "_" + h[-8:-4] + "_" + h[-4:] if len(h) > 8 else h)


def _c_int(v: int) -> str:
    v &= 0xFFFFFFFF
    return f"(int)0x{v:X}u" if v >= 0x80000000 else f"0x{v:X}"


def function_addrs(dispatch: list[dict], renames: dict[str, str] | None = None) -> str:
    """name 0xADDRESS for every host function at a PS1 address: tools/cc_ptr32.py turns a game function's address
    taken in the C into this, so it is the MIPS address the game's data holds. Overlays sharing an address and a name
    give one line."""
    seen, out = set(), ["# every game function's PS1 address, for tools/cc_ptr32.py (generated)"]
    for d in dispatch:
        n = (renames or {}).get(d["name"], d["name"])
        if n not in seen:
            seen.add(n)
            out.append(f"{n} 0x{d['vram']:08X}")
    return "\n".join(out) + "\n"


def _c_string(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")[:160]


def sdk_stubs_c(sdk: list[dict], renames: dict[str, str] | None = None) -> str:
    renames = renames or {}
    out = ["/* Sony's library functions and BIOS call stubs: a logging stub each (the first 20 calls of each), or the",
           " * runtime's implementation of the BIOS's C library calls. The implemented ones (psx/sdk) are not here. */",
           '#include "psx_runtime.h"', '#include "libapi.h"', ""]
    for lib in LIBRARIES:
        group = [s for s in sdk if s["lib"] == lib and s["kind"] != "implemented"]
        if not group:
            continue
        out.append(f"/* ---- {lib} */")
        for s in group:
            note = f" /* {s['label']} */" if s["label"] != s["name"] else ""
            c = renames.get(s["name"], s["name"])
            if s["kind"] == "bios":
                ret, params, call = BIOS_LIBC[s["bios"]]
                out.append(f"{ret} {c}({params}) {{ return {call}; }}{note}")
            else:
                out.append(f"static int calls_{s['name']};")
                label = s["name"] if s["label"] == s["name"] else f"{s['name']} ({s['label']})"
                out.append(f'int {c}(void) {{ return psx_sdk_stub("{label}", &calls_{s["name"]}); }}{note}')
        out.append("")
    return "\n".join(out)


def functions_c(dispatch: list[dict], image: bytes | None, renames: dict[str, str] | None = None) -> str:
    renames = renames or {}
    out = ["/* every host function at its PS1 address, for calls through the game's own function pointers */",
           '#include "psx_runtime.h"', ""]
    names = sorted({renames.get(d["name"], d["name"]) for d in dispatch})
    out += [f"extern void {n}(void);" for n in names]
    out.append("")
    out.append("const PsxFunction psx_functions[] = {")
    for d in dispatch:
        w0 = w1 = 0
        if image is not None and d.get("rom") is not None and d["rom"] + 8 <= len(image):
            w0, w1 = struct.unpack_from("<2I", image, d["rom"])
        out.append(f'    {{0x{d["vram"]:08X}u, 0x{w0:08X}u, 0x{w1:08X}u, {renames.get(d["name"], d["name"])}, '
                   f'"{d["name"]}", "{d["seg"]}", '
                   f'{d["kind"]}}},')
    if not dispatch:
        out.append("    {0, 0, 0, 0, 0, 0, 0},")
    out.append("};")
    out.append(f"const size_t psx_function_count = {len(dispatch)};")
    return "\n".join(out) + "\n"


def config_c(title: str, image_name: str, entry: dict, main_name: str) -> str:
    lo, hi = entry["bss"]
    return f"""/* where the game loads and what its start-up code does (generated from its executable's header and crt0) */
#include "psx_runtime.h"

#ifndef PSX_SOURCE_DIR_DEFAULT
#define PSX_SOURCE_DIR_DEFAULT "../source"
#endif

extern void {main_name}(void);

const PsxConfig psx_config = {{
    "{_c_string(title)}", "{image_name}", PSX_SOURCE_DIR_DEFAULT,
    0x{entry['load']:08X}u, 0x{entry['size']:X}u, 0x{entry['offset']:X}u,
    0x{lo:08X}u, 0x{hi:08X}u, /* bss{'' if entry.get('bss_detected') else ': not found in the start-up code, so image end to top of RAM'} */
    0x{entry['pc']:08X}u, 0x{entry['sp']:08X}u, 0x{entry['gp']:08X}u,
    "{main_name}", {main_name},
}};
"""


def sdk_config_h(headers: dict[str, str], game_implements: list[str] = ()) -> str:
    """Which PSY-Q structs the game's own headers already define (so psx/sdk's headers don't define them again in a
    file that includes common.h too), and which Sony functions the runtime implements are matched C in this game (so
    the runtime leaves those out)."""
    out = ["/* the PSY-Q structs the game's common.h defines itself, and the Sony functions it has as matched C",
           " * (generated) */", "#ifndef PSX_SDK_CONFIG_H", "#define PSX_SDK_CONFIG_H"]
    out += [f"#define PSX_GAME_HAS_{n} 1" for n in game_implements]
    flat = "\n".join(strip_c(t) for t in headers.values())
    for s in SDK_STRUCTS:
        if re.search(r"}\s*" + s + r"\s*;", flat) or re.search(r"typedef\s+[^;]*\b" + s + r"\s*;", flat):
            out.append(f"#define PSX_COMMON_HAS_{s} 1")
    out.append("#endif")
    return "\n".join(out) + "\n"


def game_sources_cmake(paths: list[str], renames: dict[str, str] | None = None) -> str:
    out = "# the game's matched C, relative to the exported tree (generated)\nset(GAME_SOURCES\n" + \
        "".join(f"    {p}\n" for p in paths) + ")\n"
    out += "# game functions named like the host's C library, renamed in the game's files\nset(GAME_RENAMES\n" + \
        "".join(f"    {a}={b}\n" for a, b in sorted((renames or {}).items())) + ")\n"
    return out


def cmakelists(slug: str) -> str:
    flags = " ".join(GAME_FLAGS)
    return f"""# {slug} for the host: the game's matched C against a PS1 runtime (generated by decomp.wtf's port export).
#   cmake -S port -B port/build && cmake --build port/build && port/build/game
cmake_minimum_required(VERSION 3.20)

# Native, for the host's architecture (arm64 on Apple silicon). PS1 memory lives in a 4 GB window of the host's
# address space (PS1 address A at {_hx(PSX_HOST_BASE)} + A, RAM at {_hx(PSX_HOST_BASE + RAM_BASE)}), the game's files translated to it by
# tools/cc_ptr32.py; -DCMAKE_OSX_ARCHITECTURES=x86_64 builds the same thing for Rosetta 2. See PORT.md.
project({re.sub(r'[^A-Za-z0-9_]', '_', slug)}_port C ASM)

set(PSX_SOURCE_DIR "${{CMAKE_CURRENT_SOURCE_DIR}}/../source" CACHE PATH "the exported tree (decomp.wtf's export)")
set(PSX_HOST "auto" CACHE STRING "window: auto, sdl, cocoa or none")
get_filename_component(PSX_SOURCE_DIR "${{PSX_SOURCE_DIR}}" ABSOLUTE)

option(PSX_PTR32 "the PS1's data layout: 4-byte pointers (__ptr32) and 32-bit long, through tools/ptr32.py" ON)

include(${{CMAKE_CURRENT_SOURCE_DIR}}/gen/game_sources.cmake)
set(GAME_INPUTS ${{GAME_SOURCES}})
list(TRANSFORM GAME_INPUTS PREPEND "${{PSX_SOURCE_DIR}}/")
if(PSX_PTR32)
  # the game's files and headers rewritten into build/ptr32 (every pointer 4 bytes, long 32 bits) whenever the
  # export changes; the export itself stays as it is
  find_package(Python3 COMPONENTS Interpreter REQUIRED)
  set(PTR32_DIR ${{CMAKE_BINARY_DIR}}/ptr32)
  file(GLOB PSX_GAME_HEADERS ${{PSX_SOURCE_DIR}}/include/*.h)
  set(GAME_SOURCES_PORTED ${{GAME_SOURCES}})
  list(TRANSFORM GAME_SOURCES_PORTED PREPEND "${{PTR32_DIR}}/")
  add_custom_command(
    OUTPUT ${{PTR32_DIR}}/stamp ${{GAME_SOURCES_PORTED}}
    COMMAND ${{Python3_EXECUTABLE}} ${{CMAKE_CURRENT_SOURCE_DIR}}/tools/ptr32.py --src ${{PSX_SOURCE_DIR}} --out ${{PTR32_DIR}}
            --list ${{CMAKE_CURRENT_SOURCE_DIR}}/gen/game_sources.txt --headers ${{PSX_SOURCE_DIR}}/include
            --stamp ${{PTR32_DIR}}/stamp
    DEPENDS ${{GAME_INPUTS}} ${{PSX_GAME_HEADERS}} ${{CMAKE_CURRENT_SOURCE_DIR}}/tools/ptr32.py
            ${{CMAKE_CURRENT_SOURCE_DIR}}/gen/game_sources.txt
    COMMENT "ptr32: the game's C with the PS1's pointer size")
  set(GAME_COMPILE ${{GAME_SOURCES_PORTED}})
  set(GAME_INCLUDES ${{CMAKE_CURRENT_SOURCE_DIR}}/include ${{PTR32_DIR}}/include ${{PSX_SOURCE_DIR}}/include)
  set(GAME_PTR32_FLAGS {" ".join(PTR32_FLAGS)})
else()
  set(GAME_COMPILE ${{GAME_INPUTS}})
  set(GAME_INCLUDES ${{CMAKE_CURRENT_SOURCE_DIR}}/include ${{PSX_SOURCE_DIR}}/include)
  set(GAME_PTR32_FLAGS)
endif()

# the game's own files: written for GCC 2.x on 32-bit MIPS. port/include comes first (host overrides), and
# psx_port_game.h is force-included (inline as GCC did, and the host's psx_gte.h: common.h's own #include "psx_gte.h"
# would find the export's copy next to it first)
add_library(game_code OBJECT ${{GAME_COMPILE}})
if(PSX_PTR32)  # every use of a __ptr32 pointer translated to the window (tools/cc_ptr32.py): IR, patched, compiled
  set_target_properties(game_code PROPERTIES
                        C_COMPILER_LAUNCHER "${{Python3_EXECUTABLE}};${{CMAKE_CURRENT_SOURCE_DIR}}/tools/cc_ptr32.py;--functions;${{CMAKE_CURRENT_SOURCE_DIR}}/gen/psx_function_addrs.txt;--literals;${{CMAKE_BINARY_DIR}}/literals")
  set_property(SOURCE ${{GAME_COMPILE}} APPEND PROPERTY OBJECT_DEPENDS
               ${{CMAKE_CURRENT_SOURCE_DIR}}/tools/cc_ptr32.py ${{CMAKE_CURRENT_SOURCE_DIR}}/gen/psx_function_addrs.txt)
endif()
target_include_directories(game_code BEFORE PRIVATE ${{GAME_INCLUDES}})
target_compile_definitions(game_code PRIVATE ${{GAME_RENAMES}})
target_compile_options(game_code PRIVATE {flags} ${{GAME_PTR32_FLAGS}} {GAME_OPT} -g -finstrument-functions
                       -include ${{CMAKE_CURRENT_SOURCE_DIR}}/include/psx_port_game.h)

file(GLOB PSX_RUNTIME ${{CMAKE_CURRENT_SOURCE_DIR}}/psx/*.c ${{CMAKE_CURRENT_SOURCE_DIR}}/psx/*.S
     ${{CMAKE_CURRENT_SOURCE_DIR}}/psx/sdk/*.c)
list(FILTER PSX_RUNTIME EXCLUDE REGEX "/host_[a-z]+\\\\.(c|m)$")
file(GLOB PSX_GEN ${{CMAKE_CURRENT_SOURCE_DIR}}/gen/*.c ${{CMAKE_CURRENT_SOURCE_DIR}}/gen/*.S)
# the game's string literals it holds as 32-bit pointers: tools/cc_ptr32.py moves each to an absolute symbol in the
# window and leaves its bytes in build/literals; this lays them out (and the runtime copies them in at start-up)
find_package(Python3 COMPONENTS Interpreter REQUIRED)
add_custom_command(
  OUTPUT ${{CMAKE_BINARY_DIR}}/psx_literals.S
  COMMAND ${{Python3_EXECUTABLE}} ${{CMAKE_CURRENT_SOURCE_DIR}}/tools/cc_ptr32.py --collect-literals
          ${{CMAKE_BINARY_DIR}}/literals ${{CMAKE_BINARY_DIR}}/psx_literals.S
  DEPENDS game_code $<TARGET_OBJECTS:game_code> ${{CMAKE_CURRENT_SOURCE_DIR}}/tools/cc_ptr32.py
  COMMENT "the game's string literals, into the window")
list(APPEND PSX_GEN ${{CMAKE_BINARY_DIR}}/psx_literals.S)

# the window: SDL2 when it links for this architecture (Homebrew's sdl2-config on Apple silicon: arm64), else AppKit
if(PSX_HOST STREQUAL "auto")
  set(PSX_HOST none)
  find_package(PkgConfig QUIET)
  if(PkgConfig_FOUND)
    pkg_check_modules(SDL2 QUIET sdl2)
  endif()
  if(NOT SDL2_FOUND)
    find_program(SDL2_CONFIG sdl2-config)
    if(SDL2_CONFIG)
      execute_process(COMMAND ${{SDL2_CONFIG}} --cflags OUTPUT_VARIABLE _cf OUTPUT_STRIP_TRAILING_WHITESPACE)
      execute_process(COMMAND ${{SDL2_CONFIG}} --libs OUTPUT_VARIABLE _lf OUTPUT_STRIP_TRAILING_WHITESPACE)
      separate_arguments(SDL2_CFLAGS UNIX_COMMAND "${{_cf}}")
      separate_arguments(SDL2_LDFLAGS UNIX_COMMAND "${{_lf}}")
      set(SDL2_FOUND 1)
    endif()
  endif()
  if(SDL2_FOUND)
    include(CheckCSourceCompiles)
    set(CMAKE_REQUIRED_FLAGS "${{SDL2_CFLAGS}}")
    string(REPLACE ";" " " CMAKE_REQUIRED_FLAGS "${{CMAKE_REQUIRED_FLAGS}} ${{SDL2_CFLAGS_OTHER}}")
    foreach(_d ${{SDL2_INCLUDE_DIRS}})
      string(APPEND CMAKE_REQUIRED_FLAGS " -I${{_d}}")
    endforeach()
    set(CMAKE_REQUIRED_LINK_OPTIONS ${{SDL2_LDFLAGS}})
    check_c_source_compiles("#define SDL_MAIN_HANDLED\\n#include <SDL.h>\\nint main(void) {{ return SDL_Init(0); }}"
                            PSX_SDL2_LINKS)
    unset(CMAKE_REQUIRED_FLAGS)
    unset(CMAKE_REQUIRED_LINK_OPTIONS)
    if(PSX_SDL2_LINKS)
      set(PSX_HOST sdl)
    endif()
  endif()
  if(PSX_HOST STREQUAL "none" AND APPLE)
    set(PSX_HOST cocoa)
  endif()
endif()
message(STATUS "PS1 port: window through ${{PSX_HOST}}, ${{CMAKE_OSX_ARCHITECTURES}}${{CMAKE_SYSTEM_PROCESSOR}}")

if(PSX_HOST STREQUAL "sdl")
  set(PSX_HOST_SRC ${{CMAKE_CURRENT_SOURCE_DIR}}/psx/host_sdl.c)
elseif(PSX_HOST STREQUAL "cocoa")
  enable_language(OBJC)
  set(PSX_HOST_SRC ${{CMAKE_CURRENT_SOURCE_DIR}}/psx/host_cocoa.m)
else()
  set(PSX_HOST_SRC ${{CMAKE_CURRENT_SOURCE_DIR}}/psx/host_null.c)
endif()

add_executable(game $<TARGET_OBJECTS:game_code> ${{PSX_RUNTIME}} ${{PSX_GEN}} ${{PSX_HOST_SRC}})
target_include_directories(game PRIVATE ${{CMAKE_CURRENT_SOURCE_DIR}}/psx ${{CMAKE_CURRENT_SOURCE_DIR}}/psx/sdk
                           ${{CMAKE_CURRENT_SOURCE_DIR}}/gen)
target_compile_definitions(game PRIVATE PSX_SOURCE_DIR_DEFAULT="${{PSX_SOURCE_DIR}}" SDL_MAIN_HANDLED)
set_source_files_properties(${{PSX_RUNTIME}} ${{PSX_GEN}} PROPERTIES COMPILE_OPTIONS "-std=gnu11;-O1;-g")
if(PSX_HOST STREQUAL "sdl")
  target_compile_options(game PRIVATE ${{SDL2_CFLAGS}} ${{SDL2_CFLAGS_OTHER}})
  target_include_directories(game PRIVATE ${{SDL2_INCLUDE_DIRS}})
  target_link_options(game PRIVATE ${{SDL2_LDFLAGS}})
elseif(PSX_HOST STREQUAL "cocoa")
  target_link_options(game PRIVATE "SHELL:-framework Cocoa")
endif()
target_link_libraries(game PRIVATE m)
"""


# ---------------------------------------------------------------------------------------------- hazards

def _struct_bodies(text: str):
    """(struct name, body) for each struct/union definition in a header."""
    flat = strip_c(text)
    for m in re.finditer(r"\b(?:struct|union)\s*(\w*)\s*\{", flat):
        depth, i = 1, m.end()
        while i < len(flat) and depth:
            depth += {"{": 1, "}": -1}.get(flat[i], 0)
            i += 1
        body = flat[m.end():i - 1]
        tail = re.match(r"\s*(\w+)", flat[i:])
        name = m.group(1) or (tail.group(1) if tail else "?")
        yield name, body


def hazards(headers: dict[str, str], sources: dict[str, str]) -> dict:
    """What stands between this C and a 64-bit host: pointer-typed and long-typed struct members (8 bytes on the
    host, 4 in the game's RAM), pointer-typed globals (an 8-byte read of a 4-byte slot), raw address dereferences."""
    ptr_members: dict[str, int] = {}
    long_members: dict[str, int] = {}
    for text in headers.values():
        for name, body in _struct_bodies(text):
            inner = re.sub(r"\{[^{}]*\}", "", body)
            for decl in inner.split(";"):
                if "*" in decl:
                    ptr_members[name] = ptr_members.get(name, 0) + 1
                if re.search(r"\blong\b", decl) and not re.search(r"\blong\s+long\b", decl):
                    long_members[name] = long_members.get(name, 0) + 1
    ptr_globals: set[str] = set()
    raw_derefs, raw_files = 0, 0
    for text in list(headers.values()) + list(sources.values()):
        flat = strip_c(text)
        top, depth = [], 0
        for ch in flat:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            elif depth == 0:
                top.append(ch)
        for decl in "".join(top).split(";"):
            d = decl.strip()
            if not d or d.startswith("typedef") or "(" in d.split("=")[0] and not re.search(r"\(\s*\*", d):
                continue
            for m in re.finditer(r"\*+\s*(?:const\s+)?([A-Za-z_]\w*)\s*(?:\[|,|=|$)", d):
                ptr_globals.add(m.group(1))
        n = len(re.findall(r"\(\s*(?:volatile\s+)?\w+(?:\s+\w+)*\s*\*+\s*\)\s*\(?\s*0x[0-9A-Fa-f]{6,8}", flat))
        raw_derefs += n
        raw_files += 1 if n else 0
    return {"pointer_members": ptr_members, "long_members": long_members, "pointer_globals": sorted(ptr_globals),
            "raw_address_casts": raw_derefs, "raw_address_files": raw_files}


# ---------------------------------------------------------------------------------------------- compile check

def _error_class(stderr: str) -> str:
    m = re.search(r"error: (.*)", stderr)
    if not m:
        return (stderr.strip().splitlines() or ["failed"])[-1][:120]
    msg = re.sub(r"'[^']*'", "'…'", m.group(1))
    return re.sub(r"\[-W[\w-]+\]", "", msg).strip()


def load_ptr32(port: Path):
    """The port's own copy of tools/ptr32.py (what its CMake build runs), as a module."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("psx_port_ptr32", port / "tools" / "ptr32.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check_sources(src_root: Path, paths: list[str], include_dirs: list[Path], force_include: Path,
                  arch_flags: list[str], jobs: int | None = None, extra: list[str] = (),
                  launcher: list[str] = (), scratch: Path | None = None) -> dict[str, str]:
    """Compile each matched file for the host the way the build will; {path: error class} for the ones that fail."""
    cc = shutil.which("clang") or shutil.which("cc")
    if not cc:
        return {}
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="port-check-", dir=scratch))
    base = [*launcher, cc, *arch_flags, *GAME_FLAGS, *extra, GAME_OPT, "-include", str(force_include)]
    for d in include_dirs:
        base += ["-I", str(d)]

    def one(p: str):
        obj = tmp / (re.sub(r"\W", "_", p) + ".o")
        try:
            r = subprocess.run(base + ["-o", str(obj), "-c", str(src_root / p)], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=180)
        except subprocess.TimeoutExpired:
            return p, "the compiler took over 2 minutes"
        if r.returncode != 0 and "error in backend" in r.stderr:
            return p, "clang's backend fails on it (" + _error_class(r.stderr)[:60] + ")"
        return p, (None if r.returncode == 0 else _error_class(r.stderr))

    out = {}
    try:
        with ThreadPoolExecutor(max_workers=jobs or max(2, (os.cpu_count() or 4))) as pool:
            for p, err in pool.map(one, paths):
                if err:
                    out[p] = err
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def host_arch_flags() -> list[str]:
    """The compile check builds for the host's own architecture, as the CMake build does."""
    return []


# ---------------------------------------------------------------------------------------------- the export

def put(path: Path, text: str) -> bool:
    """Write a file only if its content changes: an unchanged file keeps its time, so the build doesn't redo it."""
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def write_runtime(port: Path) -> None:
    """The runtime (port_runtime/) into port/: include/, psx/, psx/sdk/, tools/; files no longer in it are removed."""
    for sub in ("include", "psx", "tools"):
        want = {p.relative_to(RUNTIME / sub) for p in (RUNTIME / sub).rglob("*")
                if p.is_file() and "__pycache__" not in p.parts and p.name != ".DS_Store"}
        dst = port / sub
        if dst.exists():
            for old in [p for p in dst.rglob("*") if p.is_file() and p.relative_to(dst) not in want]:
                old.unlink()
        for rel in sorted(want):
            put(dst / rel, (RUNTIME / sub / rel).read_text(encoding="utf-8"))


def export_port(net, slug: str, out: Path | None = None, log=print, build: bool = False, main: str | None = None,
                check: bool = True, run_seconds: int = 5) -> dict:
    """Write <project root>/port/ from the project's export (source/), optionally build and run it; returns the
    counts PORT.md reports."""
    proj = net._project(slug)
    cfg = net.cfg(proj)
    if cfg.get("platform") != "psx":
        raise PortError("the port export is for PS1 projects")
    root = Path(proj["root"])
    source = root / "source"
    if not (source / "build.json").exists():
        log("no export yet: exporting the rebuildable tree first")
        net.export_rom(slug, log=log)
    port = out or root / "port"
    rows = net.store.all("SELECT name, vram, meta FROM functions WHERE project_id=? AND status='asm' AND "
                         "(meta LIKE '%\"library\"%' OR meta LIKE '%BIOS call stub%')", proj["id"])
    return generate(source, port, sony_from_rows(rows), title=proj["name"], slug=slug, log=log, build=build,
                    main=main, check=check, run_seconds=run_seconds)


def generate(source: Path, port: Path, sony: dict[str, dict], title: str, slug: str, log=print, build: bool = False,
             main: str | None = None, check: bool = True, run_seconds: int = 5, ptr32: bool = True) -> dict:
    t0 = time.time()
    b = json.loads((source / "build.json").read_text(encoding="utf-8"))
    image = (source / b["image"]).read_bytes()
    entry = detect_entry(image)
    by_vram = {}
    for f in b["functions"]:
        by_vram.setdefault(f["vram"], f["name"])
    detected = by_vram.get(entry["main"]) if entry["main"] is not None else None
    main_name = main or detected
    if not main_name:
        raise PortError("couldn't find the game's main in the start-up code: pass --main NAME")
    matched_names = {f["name"] for f in b["functions"] if f.get("c")}

    headers = {p.name: p.read_text(encoding="utf-8", errors="replace") for p in sorted((source / "include").glob("*.h"))
               if p.name != "psx_gte.h"}
    paths = [f["c"] for f in b["functions"] if f.get("c")]
    sources = {p: (source / p).read_text(encoding="utf-8", errors="replace") for p in paths}

    port.mkdir(parents=True, exist_ok=True)
    write_runtime(port)
    gen = port / "gen"
    gen.mkdir(exist_ok=True)
    excluded = {}
    check_list = port / "build" / "check_sources.txt"
    put(check_list, "".join(f"{x}\n" for x in paths))
    if check:
        log(f"compiling the {len(paths)} matched files for the host once, to leave out the ones that don't compile")
        if ptr32:  # the sources the build compiles: rewritten into build/ptr32, as its CMake rule does
            pdir = port / "build" / "ptr32"
            load_ptr32(port).main(["--src", str(source), "--out", str(pdir), "--list", str(check_list),
                                   "--headers", str(source / "include")])
            import sys
            excluded = check_sources(pdir, paths, [port / "include", pdir / "include", source / "include"],
                                     port / "include" / "psx_port_game.h", host_arch_flags(), extra=PTR32_FLAGS,
                                     launcher=[sys.executable, str(port / "tools" / "cc_ptr32.py")],
                                     scratch=port / "build")
        else:
            excluded = check_sources(source, paths, [port / "include", source / "include"],
                                     port / "include" / "psx_port_game.h", host_arch_flags())
        log(f"  {len(paths) - len(excluded)} compile, {len(excluded)} don't")
    sym_text = (source / "symbol_addrs.txt").read_text(encoding="utf-8") if (source / "symbol_addrs.txt").exists() else ""
    auto_text = "".join((source / n).read_text(encoding="utf-8") for n in ("undefined_syms_auto.txt",)
                        if (source / n).exists())
    sony = {**sony, **syscall_stubs(b, image, sony)}
    p = plan(b, sources, headers, sony, sym_text, auto_text, excluded, handwritten_gte(b, image, set(sony)))
    if main_name not in {d["name"] for d in p["dispatch"]}:
        raise PortError(f"{main_name} isn't a function of this game")
    rn = p["renames"]
    files = {
        "psx_symbols.S": symbols_asm(p["symbols"], rn),
        "psx_unmatched.c": unmatched_c(p["unmatched"], rn, p["replayed"]),
        "psx_sdk_stubs.c": sdk_stubs_c(p["sdk"], rn),
        "psx_functions.c": functions_c(p["dispatch"], image, rn),
        "psx_function_addrs.txt": function_addrs(p["dispatch"], rn),
        "psx_config.c": config_c(title, b["image"], entry, rn.get(main_name, main_name)),
        "psx_sdk_config.h": sdk_config_h(headers, p["game_implements"]),
        "game_sources.cmake": game_sources_cmake(p["compiled"], rn),
        "game_sources.txt": "".join(f"{x}\n" for x in p["compiled"]),
    }
    for old in gen.iterdir():
        if old.name not in files:
            old.unlink()
    for name, text in files.items():
        put(gen / name, text)
    put(port / "CMakeLists.txt", cmakelists(slug))
    put(port / ".gitignore", "build/\n")
    hz = hazards(headers, {k: v for k, v in sources.items() if k not in excluded})
    report = {
        "path": str(port), "main": main_name, "main_detected": detected, "main_matched": main_name in matched_names,
        "entry": {k: entry[k] for k in ("pc", "load", "size", "sp", "gp", "bss", "bss_detected", "calls")},
        "compiled": len(p["compiled"]), "excluded": p["excluded"], "unmatched": len(p["unmatched"]),
        "sdk_stubs": sum(1 for s in p["sdk"] if s["kind"] == "stub"),
        "sdk_bios": sum(1 for s in p["sdk"] if s["kind"] == "bios"),
        "sdk_implemented": sorted(s["name"] for s in p["sdk"] if s["kind"] == "implemented"),
        "sdk_called": sorted(s["name"] for s in p["sdk"] if s["called"]),
        "replayed": {n: h["label"] for n, h in p["replayed"].items()},
        "sdk_by_lib": {lib: sum(1 for s in p["sdk"] if s["lib"] == lib) for lib in LIBRARIES},
        "symbols": len(p["symbols"]), "renamed": rn, "game_implements": p["game_implements"], "as_data_matched": p["as_data_matched"],
        "as_data_unmatched": p["as_data_unmatched"], "unresolved": p["unresolved"], "hazards": hz,
        "generated_in": round(time.time() - t0, 1),
    }
    log(f"wrote {port}: {report['compiled']} matched files, {report['unmatched']} unmatched stubs, "
        f"{report['sdk_stubs']} Sony stubs, {len(report['sdk_implemented'])} Sony functions implemented, "
        f"{report['symbols']} data symbols")
    if build:
        report["build"] = build_and_run(port, log=log, seconds=run_seconds)
    (port / "port.json").write_text(json.dumps(report, indent=1, default=list), encoding="utf-8")
    (port / "PORT.md").write_text(port_md(title, report), encoding="utf-8")
    return report


def build_and_run(port: Path, log=print, seconds: int = 5) -> dict:
    """cmake configure + build, then the game with PSX_PORT_SOFT=1 for `seconds`, its log in build/run.log."""
    cmake = shutil.which("cmake") or "/opt/homebrew/bin/cmake"
    bdir = port / "build"
    res = {"configured": False, "built": False, "ran": False}
    cache = bdir / "CMakeCache.txt"
    if cache.exists() and "arm64 macOS can't map memory below 4 GB" in cache.read_text(encoding="utf-8", errors="replace"):
        log("cmake: the build directory was configured for x86_64 by an older generator: configuring it afresh")
        cache.unlink()
        shutil.rmtree(bdir / "CMakeFiles", ignore_errors=True)
    log("cmake: configuring")
    r = subprocess.run([cmake, "-S", str(port), "-B", str(bdir), "-DCMAKE_BUILD_TYPE=RelWithDebInfo"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    res["configure_log"] = (r.stdout + r.stderr)[-3000:]
    if r.returncode != 0:
        log(res["configure_log"])
        from .export import commit_tree
    commit_tree(port, f"Port: {report['compiled']} matched files, {report['unmatched']} unmatched stubs, "
                      f"{len(report['sdk_implemented'])} Sony functions implemented", log)
    return res
    res["configured"] = True
    res["host"] = (re.search(r"PS1 port: window through (\w+)", r.stdout) or [None, "?"])[1]
    log("cmake: building")
    r = subprocess.run([cmake, "--build", str(bdir), "-j", str(os.cpu_count() or 4)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    (bdir / "build.log").write_text(r.stdout + r.stderr, encoding="utf-8")
    res["build_log_tail"] = (r.stdout + r.stderr)[-4000:]
    if r.returncode != 0:
        log(res["build_log_tail"])
        return res
    res["built"] = True
    try:
        res["executable"] = subprocess.run(["file", "-b", str(bdir / "game")], capture_output=True, text=True,
                                           timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        res["executable"] = "?"
    # strict: stops at the first function that is still assembly; soft: goes on past them for `seconds`
    for mode, soft in (("strict", False), ("soft", True)):
        log(f"running build/game ({mode}{', PSX_PORT_SOFT=1' if soft else ''}, at most {seconds} s)")
        log_path = bdir / ("run.log" if soft else "run-strict.log")
        exit_code, took = run_game(bdir / "game", log_path, seconds, soft)
        text = log_path.read_text(encoding="utf-8", errors="replace")
        res[mode] = {"exit": exit_code, "seconds": took, **summarize_run(text)}
        log(f"  {took} s, exit {exit_code}: {res[mode]['story']}")
    res["ran"] = True
    return res


def run_game(exe: Path, log_path: Path, seconds: int, soft: bool) -> tuple[int, float]:
    env = dict(os.environ, PSX_TIMEOUT=str(seconds))
    env.pop("PSX_PORT_SOFT", None)
    if soft:
        env["PSX_PORT_SOFT"] = "1"
    started = time.time()
    with open(log_path, "w", encoding="utf-8") as fh:
        proc = subprocess.Popen([str(exe)], cwd=exe.parent, stdout=fh, stderr=subprocess.STDOUT, env=env)
        try:
            proc.wait(timeout=seconds + 5)
        except subprocess.TimeoutExpired:  # PSX_TIMEOUT didn't stop it (a hang in a signal-blocked state)
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    return proc.returncode, round(time.time() - started, 1)


def summarize_run(text: str) -> dict:
    """What a run log says: the game functions entered (in order), the first function still in assembly and the
    first Sony stub it reached, how it ended."""
    entered = re.findall(r"^trace: enters (\S+)", text, re.M)
    unmatched = re.findall(r"^unmatched: (\S+)", text, re.M)
    sdk = re.findall(r"^sdk: (.+?) \(call 1\)", text, re.M)
    fault = re.search(r"^psx: ((?:segmentation fault|bus error|call into PS1 code)[^\n]*)", text, re.M)
    stopped = re.search(r"^psx: (\S+) is not decompiled yet", text, re.M)
    timed = re.search(r"^psx: (stopped|terminated) after the time", text, re.M)
    story = "calls " + ", ".join(entered[:12]) + (f" (+{len(entered) - 12} more)" if len(entered) > 12 else "")
    if sdk:
        story += f"; first Sony stub reached: {sdk[0]}"
    if unmatched:
        story += f"; first function still in assembly: {unmatched[0]}"
    if stopped:
        story += f"; stops at {stopped.group(1)} (not decompiled)"
    elif fault:
        story += f"; ends with a {fault.group(1)}"
    elif timed:
        story += "; still running when the time was up"
    keep = [ln for ln in text.splitlines() if not re.match(r"sdk: .* \(call ([2-9]|1\d|20)\)", ln)]
    return {"entered": entered, "unmatched_hit": unmatched, "sdk_hit": sdk, "fault": fault.group(1) if fault else None,
            "stopped_at": stopped.group(1) if stopped else None, "story": story, "log_head": "\n".join(keep[:70])}


def port_md(title: str, r: dict) -> str:
    hz = r["hazards"]
    ex = r["excluded"]
    classes: dict[str, list[str]] = {}
    for path, why in ex.items():
        classes.setdefault(why, []).append(path)
    ex_lines = "".join(f"\n  - {len(v)} x {k}: " + ", ".join(f"`{Path(x).stem}`" for x in sorted(v)[:12])
                       + (" ..." if len(v) > 12 else "") for k, v in sorted(classes.items(), key=lambda kv: -len(kv[1])))
    ptr = sorted(hz["pointer_members"].items(), key=lambda kv: -kv[1])
    ptr_lines = ", ".join(f"`{n}` ({k})" for n, k in ptr)
    longs = ", ".join(f"`{n}` ({k})" for n, k in sorted(hz["long_members"].items()))
    bld = r.get("build") or {}
    if bld.get("ran"):
        s, f = bld["strict"], bld["soft"]
        built = (f"Built and run here: `build/game` is a {bld.get('executable', '?')}, window through "
                 f"{bld.get('host')}.\n\n"
                 f"- **Strict** (`port/build/game`, {s['seconds']} s, exit {s['exit']}): runs: {s['story']}.\n"
                 f"- **Soft** (`PSX_PORT_SOFT=1`, {f['seconds']} s, exit {f['exit']}): runs: {f['story']}. "
                 f"{len(f['entered'])} distinct game functions entered, {len(f['unmatched_hit'])} functions still in "
                 f"assembly and {len(f['sdk_hit'])} Sony stubs reached.\n\n"
                 f"The strict run's log (`build/run-strict.log`; the soft one is `build/run.log`):\n\n"
                 f"```\n{s.get('log_head', '')}\n```\n")
    elif bld:
        built = "The last build failed; see build/build.log.\n"
    else:
        built = "Not built by the generator (`decomp port <project> --build` does it); the commands are above.\n"
    e = r["entry"]
    sdk_lib = ", ".join(f"{k} {v}" for k, v in r["sdk_by_lib"].items() if v)
    main_note = "" if r["main_matched"] else (f"\n\n**{r['main']} is not matched**: the port calls its stub, so it "
                                              "stops at once until it is decompiled.")
    renamed = ", ".join(f"`{a}`" for a in r.get("renamed") or {}) or "none"
    return f"""# {title}: port to the host

Generated by decomp.wtf's port export (`decomp port <project>`) from the exported tree in `../source`. It compiles
the game's matched C with clang for the host against a PS1 runtime layer, links, and runs the game's main until the
first thing the runtime doesn't do yet. No rendering or audio yet: the window exists and is cleared each frame.

## Build and run

```
cmake -S port -B port/build && cmake --build port/build && port/build/game
```

Needs cmake, clang and python3 (the build rewrites the game's C, below). Environment: `PSX_PORT_SOFT=1` goes on past
functions that are still assembly (they return 0) instead of stopping at the first; `PSX_TRACE=N` logs the first N
distinct game functions entered (default 200); `PSX_HEADLESS=1` no window; `PSX_TIMEOUT=S` stops after S seconds;
`PSX_SOURCE_DIR` the exported tree if it moved. Keys: arrows (d-pad), Z/X/A/S (cross/circle/square/triangle),
Return (start), right shift (select), Q/W (L1/R1), 1/2 (L2/R2). CMake options: `-DPSX_PTR32=OFF` (the C as it is,
8-byte pointers), `-DPSX_HOST=sdl|cocoa|none`, `-DCMAKE_OSX_ARCHITECTURES=x86_64` (the same build for Rosetta 2;
the default is the host's architecture, arm64 on Apple silicon).

{built}
## What was generated

| | |
| :-- | :-- |
| Matched C files compiled (from `../source/src`, rewritten into `build/ptr32` at build time) | {r['compiled']} |
| Matched files left out (don't compile for the host; stubbed) | {len(ex)} |
| Unmatched functions (stubs in `gen/psx_unmatched.c`, the left-out ones included) | {r['unmatched']} |
| Sony functions as logging stubs (`gen/psx_sdk_stubs.c`) | {r['sdk_stubs']} ({sdk_lib}) |
| BIOS C library calls (memcpy, strlen, rand, ...) implemented behind the game's stub names | {r['sdk_bios']} |
| Sony functions implemented (`psx/sdk/*.c`) | {len(r['sdk_implemented'])}: {', '.join(r['sdk_implemented'])} |
| Handwritten GTE setups replayed on the software GTE (`gen/psx_unmatched.c`) | {len(r.get('replayed') or {})}{': ' + '; '.join(f"`{n}` ({v})" for n, v in (r.get('replayed') or {}).items()) if r.get('replayed') else ''} |
| Sony functions the matched C calls by name | {len(r['sdk_called'])} |
| Data symbols at their PS1 address (`gen/psx_symbols.S`) | {r['symbols']} |
| Game functions renamed because the C library has the name | {renamed} |

The game's main: **{r['main']}** ({'found' if r['main'] == r['main_detected'] else 'given with --main; found: ' + str(r['main_detected'])}
in the start-up code at 0x{e['pc']:08X}: it clears bss 0x{e['bss'][0]:08X}-0x{e['bss'][1]:08X}{'' if e['bss_detected'] else ' (not found: image end to top of RAM)'},
sets gp = 0x{e['gp']:08X} and calls {', '.join(f'0x{c:08X}' for c in e['calls'])}; the last call is main).{main_note}

Layout: `include/` (force-included into the game's files: the host's GTE macros, inline as GCC did), `psx/` (RAM,
main, the software GTE, calls through PS1 addresses, the window), `psx/sdk/` (PSY-Q headers with the PS1's struct
layout, and the implemented functions), `tools/` (the pointer rewrite and its compiler launcher), `gen/` (everything
generated from this game). `port.json` has these numbers.

## How it works, and what was measured (2026-10-23, Apple clang 21, ld64, macOS 26 on Apple silicon)

- **Native arm64, PS1 memory in a window.** PS1 address A lives at host address {_hx(PSX_HOST_BASE)} + A
  (`PSX_HOST_BASE`, `psx_host()`/`psx_ps1()` in `psx/psx_runtime.h`): RAM is 2 MB at {_hx(PSX_HOST_BASE + RAM_BASE)}, the
  scratchpad at {_hx(PSX_HOST_BASE + SCRATCH_BASE)}, the game's stack 8 MB at {_hx(PSX_HOST_BASE + 0x60000000)} (the game sees sp as
  0x6000xxxx), and the low 32 bits of a host pointer into the window are the PS1 address again. The executable's
  image is copied to 0x{e['load']:08X} (host {_hx(PSX_HOST_BASE + e['load'])}) and bss cleared as crt0 does. The window is clear of
  everything macOS maps: the executable (0x1_0000_0000 plus a slide) and the dyld shared cache (slid into
  0x1_8000_0000 and up at boot, ending near 0x2_2B00_0000). An earlier convention put the window at 0x1_0000_0000,
  inside the shared region (RAM fitted in the no-access space before the cache only because of the boot's slide);
  ram.c still maps with MAP_FIXED only after checking every region it covers is unmapped or no-access, as a guard.
  The first pass mapped RAM at 0x80000000 itself, which needs a __PAGEZERO under 4 GB: arm64 macOS kills such
  executables at exec, so that build was x86_64 under Rosetta 2.
- **Null pointers fault.** Nothing is mapped at the window's base, so a null PS1 pointer reads
  {_hx(PSX_HOST_BASE)} + offset and faults every time; the fault handler reports it as "a null PS1 pointer + 0xN".
- **Data symbols are absolute symbols** at {_hx(PSX_HOST_BASE)} + their PS1 address (`.globl _D_801A18E0` /
  `_D_801A18E0 = PSX(0x801A18E0)`), so each file's own `extern` lands in that RAM. ld accepts them in an arm64 PIE
  and reaches them through the GOT; a tentative definition (`s32 D_801A1A40;`, the way common.h declares small
  globals) with `-fcommon` is overridden by the absolute symbol (ld warns "tentative definition ... replaced by real
  definition").
- **4-byte pointers (`PSX_PTR32`, on by default).** A host pointer is 8 bytes; one stored in the game's RAM is 4.
  Without the rewrite the run dies at the third function: func_8017C4F4 reads a function pointer from a table in RAM
  as 8 bytes. `tools/ptr32.py` rewrites the game's files and headers into `build/ptr32` (rerun when the export
  changes): every pointer declarator gets clang's `__ptr32 __uptr` (`-fms-extensions`; clang's AArch64 target has the
  same 32-bit address space 271 as x86), `long` becomes `int`.
- **The translation (`tools/cc_ptr32.py`).** clang lowers an access through a `__ptr32` pointer with a plain
  zero-extension, below 4 GB, and can't call through one at all. So each game file goes to LLVM IR first and every
  use of a 32-bit pointer is rewritten: loads, stores and memcpy/memset through one go to `zext(p) | {_hx(PSX_HOST_BASE)}`
  (two instructions on arm64: `mov w8, w0; orr x8, x8, #0x{PSX_HOST_BASE:x}`); a host pointer given to the game (a local's
  address) is its low 32 bits; a string literal of the game's whose address it takes is moved into the window (an
  absolute symbol `__psx_lit_<sha1>` at 0x70000000 and up, its bytes copied in at start-up: `build/psx_literals.S`); a game function's address taken in the C is its PS1 address (`gen/psx_function_addrs.txt`),
  so function pointers in RAM are MIPS addresses and compare equal to the game's data; a call through a 32-bit
  pointer asks the runtime (`psx_host_fn`: the function table, overlays told apart by their code in RAM; any other
  address ends the run with it). 32-bit
  address arithmetic stays as it is. All 4000 files' IR goes through it; the same 3 fail as before (their C).
- **The runtime's side**: a pointer the game passes is a PS1 address (`PSX_PTR`/`psx_host`), and a pointer the
  runtime hands the game must be in the window: RAM, the game's stack, or the PS1 heap (`InitHeap`/`malloc`, in RAM).
- **Calls through the game's function pointers** go through `psx_host_fn`; the SIGSEGV handler that sent a jump
  into RAM to the host function stays as a safety net for jumps from anywhere else.
- **GTE**: `psx/gte.c` is a software GTE written from psx-spx (registers and their side effects, RTPS/RTPT, NCLIP,
  AVSZ3/4, MVMVA, SQR, OP, GPF/GPL, DPCS/DPCT, INTPL, DCPL, NCS/NCT, NCCS/NCCT, NCDS/NCDT, CC, CDP, the FLAG bits);
  not compared with hardware yet.
- Flags beyond the plan: `-fcommon`, `-fms-extensions`, `-Wno-implicit-function-declaration -Wno-implicit-int
  -Wno-return-type -Wno-incompatible-function-pointer-types`, and every `inline` made always_inline (gnu89 `extern
  inline` has no out-of-line copy: GCC inlined it, clang at -O1 didn't, and the link failed).

## What stands between this tree and a native port

- **Pointer-sized fields**, handled by the rewrite (they break with `-DPSX_PTR32=OFF`): {len(hz['pointer_globals'])} globals
  declared as pointers; {sum(hz['pointer_members'].values())} pointer members in {len(ptr)} structs of common.h: {ptr_lines or 'none'};
  members declared `long`: {longs or 'none'}. The rewrite is token-level: a macro that takes a type as a parameter
  (`CAST(T, x)`) keeps 8-byte pointers in its casts (harmless in a cast, wrong in a declaration).
- **Host memory the game could hold**: only the string literals whose address the game takes are moved into the
  window (2 in SLUS-01252); any other data defined in a game file whose address goes 32-bit fails the compile
  (none here: every other global is a tentative definition an absolute symbol replaces). An `int` compared with a
  game function's address in an 8-byte-pointer context (`func_X` declared as data in 2 files) still compares with the
  host address.
- **Mirrors**: KUSEG (0x00000000) and KSEG1 (0xA0000000) views of RAM aren't mapped; a game that uses them faults.
- **Functions used as data** that are matched (their name is a host function, so `func_X + N` is host code, not the
  game's): {', '.join(f'`{n}`' for n in r['as_data_matched']) or 'none'}. Unmatched ones are absolute symbols at
  their PS1 address: {', '.join(f'`{n}`' for n in r['as_data_unmatched']) or 'none'}.
- **Raw addresses**: {hz['raw_address_casts']} casts of a constant address to a pointer in {hz['raw_address_files']} files; translated like
  any other 32-bit pointer.
- **Overlays** are compiled into the one executable and share addresses; the game loads their images from the disc,
  and no CD is emulated yet. **Hardware registers** (0x1F801000 on) are plain memory.
- **Calls nothing answers** (named in the C, but neither matched, a stub nor a symbol):
  {', '.join(f'`{n}`' for n in r['unresolved'][:40]) or 'none'}.
- **Matched files that don't compile for the host**:{ex_lines or ' none.'}

## PsyCross

[PsyCross](https://github.com/OpenDriver2/PsyCross) (OpenDriver2) reimplements PSY-Q's libraries on SDL2 (2.0.16+),
OpenAL-soft and OpenGL: a renderer that emulates VRAM, an SPU, a software GTE with PGXP. REDRIVER2 and the Silent Hill
PC port use it. Its repository says MIT, so linking it is fine. Its README names no operating system (macOS isn't
mentioned; Emscripten is) and lists the CMake build scripts as a TODO. With the port native on arm64, Homebrew's
SDL2 links; its PSY-Q headers declare `long`-based structs, which need the 32-bit layout this port's headers keep,
and its pointers would have to follow the window rule. A later `-DPSX_SDK=psycross` replacing
`psx/sdk` is worth it for the renderer and the SPU, which are most of the work left, but not before the CPU side
(this port's RAM, pointers and calls) runs a frame.
"""
