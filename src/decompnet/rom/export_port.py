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
  MIPS address) lands on the host function (psx/call32.S's trampoline, then psx_host_fn); the overlays that share an address are told apart by their
  code in RAM. A game function's address taken in the C is its PS1 address too (gen/psx_function_addrs.txt).

Generation is a set of pure functions (tested in tests/test_export_port.py); export_port() reads the tree and the
project's database, runs them, and optionally builds and runs the result.
"""

from __future__ import annotations

import bisect
import hashlib
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
    "PadInitDirect": "libpad", "PadStartCom": "libpad", "PadStopCom": "libpad", "PadGetState": "libpad",
    "PadInfoMode": "libpad", "PadSetMainMode": "libpad", "PadSetAct": "libpad", "PadSetActAlign": "libpad",
    "SetRotMatrix": "libgte", "SetTransMatrix": "libgte", "RotMatrix": "libgte", "TransMatrix": "libgte",
    "ScaleMatrix": "libgte", "MulMatrix0": "libgte", "ApplyMatrix": "libgte", "SquareRoot0": "libgte",
    "InitGeom": "libgte", "SetGeomOffset": "libgte", "SetGeomScreen": "libgte",
    "OpenEvent": "libapi", "CloseEvent": "libapi", "EnableEvent": "libapi", "DisableEvent": "libapi",
    "DeliverEvent": "libapi", "UnDeliverEvent": "libapi", "TestEvent": "libapi", "WaitEvent": "libapi",
    # libcard over the card image (psx/sdk/libcard.c): PSY-Q's names, for a game that has them named
    **{n: "libcard" for n in """InitCARD StartCARD StopCARD _bu_init _card_info _card_load _card_auto _card_read
       _card_write _new_card _card_status _card_wait _card_chan _card_clear _card_format""".split()},
    "EnterCriticalSection": "libapi", "ExitCriticalSection": "libapi",
    # libgpu over the software GPU (psx/gpu.c, psx/sdk/libgpu.c): the library and the driver under it
    **{n: "libgpu" for n in """SetGraphReverse SetGraphQueue GetGraphType GetGraphDebug GetDrawEnv GetDispEnv DrawSync
       DrawSyncCallback ClearOTag ClearOTagR AddPrim AddPrims CatPrim TermPrim DrawOTag DrawOTagIO DrawOTagEnv DrawPrim
       LoadImage LoadImage2 StoreImage StoreImage2 MoveImage ClearImage ClearImage2 LoadTPage LoadClut LoadClut2
       GetTPage GetClut SetDrawEnv SetDrawMode SetDrawTPage SetDrawArea SetDrawOffset SetDrawStp SetTexWindow
       SetDrawMove SetPriority get_mode get_cs get_ce get_ofs get_tw get_dx SetPolyF3 SetPolyFT3 SetPolyG3
       SetPolyGT3 SetPolyF4 SetPolyFT4 SetPolyG4 SetPolyGT4 SetSprt SetSprt8 SetSprt16 SetTile SetTile1 SetTile8
       SetTile16 SetLineF2 SetLineG2 SetLineF3 SetLineG3 SetLineF4 SetLineG4 SetSemiTrans SetShadeTex _addque
       _addque2 _exeque _clr _dws _drs _ctl _cwb _cwc _getctl _otc _param _reset _status _sync""".split()},
    # libetc: the callbacks (called from VSync, on the main thread), the video mode, the old pad interface
    **{n: "libetc" for n in """VSyncCallback VSyncCallbacks InterruptCallback DMACallback ResetCallback StopCallback
       RestartCallback CheckCallback SetVideoMode GetVideoMode PadInit PadRead PadStop""".split()},
    # the root counters (libapi's COUNTER.OBJ)
    **{n: "libapi" for n in "SetRCnt GetRCnt StartRCnt StopRCnt ResetRCnt".split()},
}


def _weak_sdk(path: Path) -> list[str]:
    """The Sony functions a runtime file defines (`PSX_SDK_WEAK <type> Name(`): libspu.c and libsnd.c."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return re.findall(r"^PSX_SDK_WEAK\s+[\w\s\*]*?\b(\w+)\s*\(", text, re.M)


# the sound stack (psx/sdk/libspu.c over the software SPU in psx/spu.c, psx/sdk/libsnd.c's sequencer) and libpress
# (psx/sdk/libpress.c over the software MDEC in psx/mdec.c): every Sony function those files define
for _lib in ("libspu", "libsnd", "libpress"):
    for _n in _weak_sdk(Path(__file__).with_name("port_runtime") / "psx" / "sdk" / f"{_lib}.c"):
        IMPLEMENTED.setdefault(_n, _lib)

# Functions of a game's SDK that the database doesn't name, identified from their code (per project slug): the port
# treats each as the named Sony function (forwarding to the runtime's when it has one, a labelled stub otherwise).
# SLUS-01252's libsnd range (8016A000-80172000) is Sony's libsnd decompiled as if it were the game's (34 of its 125
# functions matched); its unnamed entry points the game calls, by their bodies and neighbours:
SDK_IDENTIFIED = {
    "slus-01252": {
        "func_8016A860": ("SsInit", "matched C: `_SsInit(0)`, SSINIT.OBJ's SsInit; the game's sound set-up calls it"),
        "func_8016A880": ("SsInitHot", "matched C: `_SsInit(1)`"),
        "func_8016ADB8": ("SsStart", "matched C: `_SsStart(1)` (the tick on the root counter's interrupt)"),
        "func_8016ADD8": ("SsStart2", "matched C: `_SsStart(0)`; the game's sound set-up calls it after "
                                      "SsSetTickMode(1)"),
        "func_8016ADF8": ("SsEnd", "matched C: disables and closes the tick's event unless no tick was started"),
        "func_8016AE60": ("SsQuit", "matched C: `SpuQuit()`"),
        "func_8016A10C": ("SsSetSerialAttr", "matched C: (s_num, attr, mode) with 0/1 tests on the first two, one "
                                             "common-attribute call; between SCSRVOL.OBJ's SsSetRVol and SCSSVOL.OBJ's "
                                             "SsSetSerialVol, where SCSSATTR.OBJ sorts; the game calls it (0, 0, 1): "
                                             "CD audio mixed in"),
        "func_8016A5E4": ("SsSeqClose", "open: clears the score record's 0x3C-0xA4 fields and the sequence's bit in "
                                        "the open mask D_801A19F0; the game calls it after SsSeqStop"),
        "func_80171200": ("SsVabClose", "SpuFree(the VAB's SPU address, D_801FF990[vab]), clears its slot in "
                                        "D_801F4CE8; load_vab_bank calls it before opening the slot again"),
        "func_801712D8": ("SsVabOpenHead", "matched C: `_SsVabOpenHeadWithMode(addr, vabid, 0, 0)` "
                                           "(func_80171308); load_vab_bank's first call, failing below 0"),
        "func_8017170C": ("SsVabTransBody", "load_vab_bank's second call (addr, vabid), compared with vabid"),
        "func_801717D0": ("SsVabTransCompleted", "matched C: `return SpuIsTransferCompleted(flag)`"),
        "func_801711DC": ("SsSetMono", "matched C: D_801A1928 = 1, the flag the key-on paths (func_8016E650, "
                                       "func_8016EFF0) read"),
        "func_801711F0": ("SsSetStereo", "matched C: D_801A1928 = 0; the game's sound set-up calls it"),
        "func_80171C1C": ("SsUtReverbOn", "matched C: `SpuSetReverb(1)`"),
        "func_80171C3C": ("SsUtReverbOff", "matched C: `SpuSetReverb(0)`"),
        # libspu's, outside the range: in assembly, set aside as library code, not in the database's Sony set
        "func_8017295C": ("_spu_t", "libspu's transfer/ioctl dispatcher: the SPU registers through D_801A144C, its "
                                    "strings \"SPU:T/O [%s]\" and \"wait (ioctl/IRQ)\""),
        "func_8017233C": ("_spu_FsetRXX", "libspu: a read-modify-write of SPUCNT through D_801A144C (26 insns)"),
        "func_8017CD70": ("DecDCTinCallback", "`func_8017C554(0, func)`, next to DecDCToutCallback at 0x8017CD94: "
                                              "`func_8017C554(1, func)` (libetc's DMA-callback setter: channel 0 is "
                                              "the MDEC's input, 1 its output)"),
        # libpress, linked into the movie overlay (ovl_sj00, SJ00.STG at 0x8006D800) around the named DecDCTin and
        # MDEC_* internals: the API's wrappers are matched C (m2c drafts that drop the arguments they pass on) over
        # the MDEC's registers and DMA, so each is the runtime's function (psx/sdk/libpress.c over psx/mdec.c)
        "func_8006E4B4": ("DecDCTReset", "matched C: `if (mode == 0) <the DMA callbacks reset>; MDEC_reset(mode)`"),
        "func_8006E4EC": ("DecDCTBufSize", "matched C: returns the BS header's first halfword (the run-length size)"),
        "func_8006E4F8": ("DecDCTvlc", "matched C: `if (MDEC_vlc(bs, buf) == 0) MDEC_vlc2(bs, buf)` (the Huffman "
                                       "decode); the game calls it at 0x8006E4F8 with (frame, buffer)"),
        "func_8006E5B4": ("DecDCTout", "matched C: `MDEC_out(buf, size)`; the game calls it at 0x8006E5B4 with (strip "
                                       "buffer, words)"),
        "func_8006E5D4": ("DecDCTinSync", "matched C: `MDEC_in_sync(mode)`"),
        "func_8006E5F4": ("DecDCToutSync", "matched C: `MDEC_out_sync(mode)`"),
        "func_8006E614": ("MDEC_reset", "mode 0: MDEC1 = 0x80000000, the DMA channels stopped, MDEC_in of the quant "
                                        "table (0x40000001 + 128 bytes at D_8006F360) and the scale matrix "
                                        "(0x60000000 + 64 halfwords at D_8006F3E4); 1: abort; else the time-out path"),
        # libpad's state machine, matched as the game's: each reads libpad's port object through D_801A0ED4 (which
        # libpad's PadInitDirect sets up, not the runtime's); the game's pad reader func_800C305C calls them
        "func_8017E360": ("PadGetState", "matched C: the port object's state byte (+0x49), 2/3 -> 1 and 6 -> 4 "
                                         "while a request is pending; the pad reader steps its own state on 0/1/2/6"),
        "func_8017E430": ("PadInfoMode", "matched C: switch on term 1 (current id, +0xE8), 2 (current extended id, "
                                         "+0xE6), 3 (offset, +0xE4), 4 (the id list, offs < count +0xE3), 100"),
        "func_8017E050": ("PadSetAct", "matched C: the port object's actuator data pointer (+0x28) and length "
                                       "(+0x34); the pad reader calls it (port, &D_801A0F7C, 2)"),
        "func_8017E090": ("PadSetActAlign", "matched C: when the port is idle, the align request (+0x46 = 1, the "
                                            "callbacks at +0x14/+0x18, the data at +0x20), 1; else 0"),
    },
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
    # events (psx/sdk/libapi.c): opened, delivered (by the game, and by the card driver from the vblank), tested
    "B0_07": ("int", "unsigned a, unsigned b", "psx_bios_DeliverEvent(a, b)"),
    "B0_08": ("int", "unsigned a, unsigned b, unsigned c, unsigned d", "psx_bios_OpenEvent(a, b, c, d)"),
    "B0_09": ("int", "unsigned a", "psx_bios_CloseEvent(a)"),
    "B0_0A": ("int", "unsigned a", "psx_bios_WaitEvent(a)"),
    "B0_0B": ("int", "unsigned a", "psx_bios_TestEvent(a)"),
    "B0_0C": ("int", "unsigned a", "psx_bios_EnableEvent(a)"),
    "B0_0D": ("int", "unsigned a", "psx_bios_DisableEvent(a)"),
    "B0_20": ("int", "unsigned a, unsigned b", "psx_bios_UnDeliverEvent(a, b)"),
    # the memory card (psx/sdk/libcard.c): slot 1 is PSX_MEMCARD's card image, the events a vblank later
    "B0_4A": ("int", "int a", "psx_bios_InitCARD(a)"),
    "B0_4B": ("int", "void", "psx_bios_StartCARD()"),
    "B0_4C": ("int", "void", "psx_bios_StopCARD()"),
    "B0_4E": ("int", "int a, int b, const void *c", "psx_bios_card_write(a, b, PSX_PTR(c))"),
    "B0_4F": ("int", "int a, int b, void *c", "psx_bios_card_read(a, b, PSX_PTR(c))"),
    "B0_50": ("int", "void", "psx_bios_new_card()"),
    "B0_5C": ("int", "int a", "psx_bios_card_status(a)"),
    "B0_5D": ("int", "int a", "psx_bios_card_wait(a)"),
    "A0_70": ("int", "void", "psx_bios_bu_init()"),
    "A0_AB": ("int", "int a", "psx_bios_card_info(a)"),
    "A0_AC": ("int", "int a", "psx_bios_card_load(a)"),
    "A0_AD": ("int", "int a", "psx_bios_card_auto(a)"),
    # the BIOS's file calls, on the card's "bu00:" (a direntry returned as the game's own pointer)
    "B0_32": ("int", "const char *a, int b", "psx_bios_open(PSX_PTR(a), b)"),
    "B0_33": ("int", "int a, int b, int c", "psx_bios_lseek(a, b, c)"),
    "B0_34": ("int", "int a, void *b, int n", "psx_bios_read(a, PSX_PTR(b), n)"),
    "B0_35": ("int", "int a, const void *b, int n", "psx_bios_write(a, PSX_PTR(b), n)"),
    "B0_36": ("int", "int a", "psx_bios_close(a)"),
    "B0_41": ("int", "const char *a", "psx_bios_format(PSX_PTR(a))"),
    "B0_42": ("unsigned", "const char *a, void *b",
              "psx_bios_firstfile(PSX_PTR(a), PSX_PTR(b)) ? psx_ps1(PSX_PTR(b)) : 0u"),
    "B0_43": ("unsigned", "void *a", "psx_bios_nextfile(PSX_PTR(a)) ? psx_ps1(PSX_PTR(a)) : 0u"),
    "B0_45": ("int", "const char *a", "psx_bios_erase(PSX_PTR(a))"),
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


def parse_sdk_range(text: str) -> tuple[int, int]:
    """"8016A000-80172000" (hex, 0x optional) as (from, to)."""
    m = re.fullmatch(r"\s*(?:0x)?([0-9A-Fa-f]{1,8})\s*-\s*(?:0x)?([0-9A-Fa-f]{1,8})\s*", text)
    if not m or int(m.group(1), 16) >= int(m.group(2), 16):
        raise PortError(f"an SDK range is FROM-TO in hex (8016A000-80172000), not {text!r}")
    return int(m.group(1), 16), int(m.group(2), 16)


def sdk_ranges(regions: list[dict], sony: dict[str, dict]) -> list[tuple[int, int]]:
    """The address ranges whose every function is the SDK's: the project's compiler regions (an owner sets one where
    a library was compiled differently from the game, SLUS-01252's libsnd), merged where they touch, each kept only
    if it holds Sony functions the database names, and starting at the first of them (the owner's round start
    address can take in the game's own code before the library: SLUS-01252's 0x8016A028 calls CdRead)."""
    spans = sorted((int(r["from"]), int(r["to"])) for r in regions or [] if r.get("from") is not None and r.get("to"))
    merged: list[list[int]] = []
    for lo, hi in spans:
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    out = []
    for lo, hi in merged:
        inside = [s["vram"] for s in sony.values() if s.get("vram") is not None and lo <= s["vram"] < hi]
        if inside:
            out.append((min(inside), hi))
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


# ---------------------------------------------------------------------------------------------- libcd

# libcd over the disc image (psx/sdk/libcd.c, psx/cd.c): the functions the runtime has, as {name: (return type,
# parameters)} with every pointer a PS1 address (unsigned). These replace the game's own copies, matched C or not:
# a matched libcd function sits on the drive's internals (CD_cw, CD_sync, getintr: the hardware), which are stubs.
LIBCD_RUNTIME = {
    "CdInit": ("int", "void"), "CdReset": ("int", "int mode"), "CdFlush": ("void", "void"),
    "CdStatus": ("int", "void"), "CdMode": ("int", "void"), "CdLastCom": ("int", "void"),
    "CdSetDebug": ("int", "int level"), "CdSync": ("int", "int mode, unsigned result"),
    "CdReady": ("int", "int mode, unsigned result"), "CdSyncCallback": ("unsigned", "unsigned func"),
    "CdReadyCallback": ("unsigned", "unsigned func"), "CdReadCallback": ("unsigned", "unsigned func"),
    "CdDataCallback": ("unsigned", "unsigned func"),
    "CdControl": ("int", "int com, unsigned param, unsigned result"),
    "CdControlF": ("int", "int com, unsigned param"),
    "CdControlB": ("int", "int com, unsigned param, unsigned result"),
    "psx_CdControlCore": ("int", "int com, unsigned param, unsigned result, int nowait"),
    "CdMix": ("int", "unsigned vol"), "CdGetSector": ("int", "unsigned madr, int words"),
    "CdGetSector2": ("int", "unsigned madr, int words"), "CdDataSync": ("int", "int mode"),
    "CdIntToPos": ("unsigned", "int i, unsigned p"), "CdPosToInt": ("int", "unsigned p"),
    "CdSearchFile": ("unsigned", "unsigned fp, unsigned name"),
    "CdRead": ("int", "int sectors, unsigned buf, int mode"), "CdReadSync": ("int", "int mode, unsigned result"),
    "CdRead2": ("int", "int mode"), "CdReadFile": ("int", "unsigned file, unsigned addr, int nbyte"),
    "CdDiskReady": ("int", "int mode"), "CdGetDiskType": ("int", "void"),
    "StSetRing": ("void", "unsigned ring_addr, unsigned ring_size"), "StClearRing": ("void", "void"),
    "StUnSetRing": ("void", "void"),
    "StSetStream": ("void", "unsigned mode, unsigned start_frame, unsigned end_frame, unsigned func1, unsigned func2"),
    "StSetEmulate": ("void", "unsigned addr, unsigned mode, unsigned start_frame, unsigned end_frame, unsigned func1, "
                             "unsigned func2"),
    "StFreeRing": ("unsigned", "unsigned base"), "StGetNext": ("unsigned", "unsigned addr, unsigned header"),
    "StSetChannel": ("int", "unsigned channel"), "StSetMask": ("void", "unsigned mask, unsigned start, unsigned end"),
}
# libcd's entry points the database leaves unnamed, found from a named one in the same object at a fixed offset and
# checked by their code with every immediate and jump target masked out ((instructions, sha1[:12]) of the words):
# a different libcd version doesn't match and keeps its stubs. From SLUS-01252 (PSY-Q 4.x libcd): the wrappers
# CdSync/CdReady/CdControl... are matched C over the drive's internals, CdControl's core and CdRead2 still assembly.
# The three Cd*Callback setters and CdSetDebug have the same code (swap a global) and are told apart by offset.
LIBCD_LAYOUT = {
    "CdRead": [
        (-0x328, "CdReset", 8, "63ec6e923904"), (-0x308, "CdSync", 8, "63ec6e923904"),
        (-0x2E8, "CdReady", 8, "63ec6e923904"), (-0x2C8, "CdReadSync", 8, "63ec6e923904"),
        (-0x2A8, "CdSetDebug", 6, "59fd3cedf154"), (-0x220, "CdSyncCallback", 6, "59fd3cedf154"),
        (-0x208, "CdReadyCallback", 6, "59fd3cedf154"), (-0x1F0, "CdReadCallback", 6, "59fd3cedf154"),
        (-0x1D8, "psx_CdControlCore", 72, "98cdcd729719"), (-0xB8, "CdControl", 9, "24e2662caf6f"),
        (-0x94, "CdControlF", 10, "a6a294887fad"), (-0x6C, "CdControlB", 19, "3a2afaa29d3d"),
        (-0x20, "CdGetSector", 8, "98355f74c795"), (0xD4, "CdMix", 8, "6e5e9da383d0"),
        (0xF4, "CdRead2", 28, "89ae59f3616b"),
    ],
    "StClearRing": [
        (-0x58, "StSetRing", 22, "5e79946fd59d"), (0x50, "StUnSetRing", 14, "7f526a20f92b"),
        (0x380, "StSetMask", 8, "a16bd09fb3c2"),
    ],
}


def masked_code(image: bytes, rom: int, count: int) -> tuple[int, str]:
    """(instructions, sha1[:12]) of a function's words with what relocations change masked out: an I-type's
    immediate, a jump's target; R-type instructions (registers, shift, function) as they are."""
    out = bytearray()
    for w in struct.unpack_from(f"<{count}I", image, rom):
        op = w >> 26
        out += struct.pack("<I", w if op == 0 else (op << 26 if op in (2, 3) else w & 0xFFFF0000))
    return count, hashlib.sha1(bytes(out)).hexdigest()[:12]


def _sizes(build: dict, image: bytes) -> dict[str, tuple[int, int, str]]:
    """{name: (vram, instructions, segment)}: a function runs to the next one in its segment"""
    by_seg: dict[str, list[dict]] = {}
    for f in build["functions"]:
        if f.get("rom") is not None:
            by_seg.setdefault(f["seg"], []).append(f)
    out = {}
    for seg, fs in by_seg.items():
        fs.sort(key=lambda f: f["vram"])
        for i, f in enumerate(fs):
            end = fs[i + 1]["vram"] if i + 1 < len(fs) else f["vram"] + 4
            n = max(1, min((end - f["vram"]) // 4, (len(image) - f["rom"]) // 4))
            out[f["name"]] = (f["vram"], n, seg)
    return out


def libcd_aliases(build: dict, image: bytes, sony: dict[str, dict]) -> dict[str, str]:
    """{the game's function: the runtime's libcd function it is}: by name (a libcd function the database names),
    by the library's own name for it (a second copy, e.g. in an overlay: SLUS-01252's SJ00 has CdDataCallback at
    0x8006DAD0), and by LIBCD_LAYOUT."""
    names = {f["name"] for f in build["functions"]}
    out = {n: n for n in names if n in LIBCD_RUNTIME}
    for n, s in sony.items():
        if n in names and s.get("label") in LIBCD_RUNTIME and n not in out:
            out[n] = s["label"]
    sizes = _sizes(build, image)
    by_addr = {(v, seg): n for n, (v, _, seg) in sizes.items()}
    roms = {f["name"]: f["rom"] for f in build["functions"] if f.get("rom") is not None}
    for anchor, entries in LIBCD_LAYOUT.items():
        if anchor not in sizes:
            continue
        av, _, seg = sizes[anchor]
        for off, runtime, count, sig in entries:
            n = by_addr.get((av + off, seg))
            if n is None or n in out or sizes[n][1] != count:
                continue
            if masked_code(image, roms[n], count) == (count, sig):
                out[n] = runtime
    return out


_LUI_USERS = {0x08, 0x09, 0x0D, 0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x28, 0x29, 0x2A, 0x2B, 0x2E}


def shape_fingerprint(image: bytes, rom: int, count: int) -> str | None:
    """A function's code with only its relocations masked: lui immediates, the %lo half of an access or addiu
    through a register a lui set, and jal/j targets (masked_code masks every immediate, which can't tell libetc's
    table wrappers apart: they differ only in the slot they load). sha1 of the masked words (16 hex digits) + their
    number."""
    count = min(count, 256, (len(image) - rom) // 4)
    if count <= 0:
        return None
    lui: set[int] = set()
    words = []
    for w in struct.unpack_from(f"<{count}I", image, rom):
        op, rs, rt = w >> 26, (w >> 21) & 31, (w >> 16) & 31
        if op == 0x0F:
            lui.add(rt)
            w &= 0xFFFF0000
        elif op in (0x02, 0x03):
            w &= 0xFC000000
        elif op in _LUI_USERS and rs in lui:
            w &= 0xFFFF0000
            if op < 0x28:  # a load or addiu/ori writes rt: no longer a lui's
                lui.discard(rt)
        elif op == 0 and (w >> 11) & 31:
            lui.discard((w >> 11) & 31)
        elif 0x08 <= op < 0x28:
            lui.discard(rt)
        words.append(w)
    return hashlib.sha1(struct.pack(f"<{len(words)}I", *words)).hexdigest()[:16] + f":{len(words)}"


# Sony functions known by their code (shape_fingerprint) wherever they're linked: the runtime's function each one is,
# and the evidence. Measured on SLUS-01252 (Arc the Lad II), whose libgpu/libetc are partly unnamed assembly and
# partly matched C that calls libgpu's driver table (D_801A1480: {ver, addque, addque2, clr, ctl, cwb, cwc, drs, dws,
# exeque, getctl, otc, param, reset, status, sync}, every slot read from the EXE) or libetc's interrupt table
# (D_801A153C) through m2c drafts that drop the call's arguments (`M2C_FIELD(t, M2C_UNK (**)(), 0x2C)()`: right on
# the MIPS, where a0-a3 pass through untouched, wrong on the host). Each match goes into plan()'s aliases: an
# assembly one forwards to the runtime's, a matched one has its C dropped.
SONY_SHAPES: dict[str, tuple[str, str]] = {
    # libgpu, unnamed assembly
    "c0e6397316af7129:20": ("ClearImage", "addque2(clr, rect, 8, b<<16|g<<8|r) through the driver table"),
    "df741f8f33943cc7:27": ("MoveImage", "builds {0x04FFFFFF, 0x80000000, src xy, dst y<<16|x, wh} and addque2(cwc, "
                                         "it, 0x14)"),
    "89cba2e884601913:33": ("DrawPrim", "CheckPrim when debugging, sync(0), cwb(p + 4, p's length byte)"),
    "67daaeb3b1748ec9:52": ("SetGraphReverse", "ctl(getctl(8) | 0x08000000 | (reverse ? 0x80 : 0)), 0x20000501/4 on "
                                               "a type-2 GPU"),
    "817a852b9661cb20:21": ("SetDrawMode", "len 2, code[0] = get_mode(dfe, dtd, tpage), code[1] = get_tw(5th "
                                           "argument)"),
    "f216cdea5b31f948:33": ("get_tw", "0xE2000000 | y>>3 << 15 | x>>3 << 10 | (-h & 0xFF)>>3 << 5 | (-w & 0xFF)>>3"),
    "39a0a51f5f49bd8f:54": ("get_dx", "the display x by GPU type (D_801A148C) and reverse (D_801A1490)"),
    # libgpu, matched C that drops its arguments, or pokes the GPU's registers (plain memory here)
    "17874598bd8c1e51:14": ("ClearOTagR", "otc(ot, n) through the driver table, returns ot"),
    "8d76deebeba4ab66:12": ("DrawSync", "sync(mode) through the driver table"),
    "adabd31564311dad:9": ("_addque", "_addque2(a, b, 0, c): driver table slot 1"),
    "8584fbbaf56031c0:20": ("_cwc", "DMA2: GP1 = 0x04000002, MADR = p, BCR = 0, CHCR = 0x01000401 (linked list): "
                                    "slot 6"),
    "26bbd38d41f04c6f:8": ("_getctl", "n << 24 | the saved GP1 word n: slot 10"),
    "9f4018f77f5b2d17:13": ("_param", "GP1 = 0x10000000 | n, returns GPUREAD & 0xFFFFFF: slot 12"),
    "255f730c9bc1b559:6": ("_status", "returns GPUSTAT: slot 14"),
    # libetc's interrupt table (D_801A153C: {?, DMACallback, InterruptCallback, ResetCallback, StopCallback,
    # VSyncCallback}; slots 1 and 5 are null in the EXE until ResetCallback's intInit fills them). Slot 1 takes a DMA
    # channel: ResetGraph calls its wrapper with (2, the GPU's handler), CdDataCallback with (3, func),
    # DecDCToutCallback with (1, func); slot 5's wrapper passes its one argument on: _SsStart's
    # VSyncCallback(SsSeqCalledTbyT)
    "ff47d39e431c6826:12": ("ResetCallback", "calls interrupt-table slot 3 (intInit)"),
    "b1c6029eb5247457:12": ("InterruptCallback", "calls interrupt-table slot 2 (setIntCallback) with (irq, func)"),
    "ce2db528128895db:12": ("DMACallback", "calls interrupt-table slot 1 with (ch, func): ResetGraph's (2, ...), "
                                           "CdDataCallback's (3, func), DecDCToutCallback's (1, func)"),
    "6af7f4aabe2d1255:12": ("VSyncCallback", "calls interrupt-table slot 5 with (func): _SsStart's "
                                             "VSyncCallback(SsSeqCalledTbyT)"),
    "33ac02314cce677c:12": ("StopCallback", "calls interrupt-table slot 4 (stopInit)"),
}


def sony_by_shape(build: dict, image: bytes, known: dict[str, dict], aliases: dict[str, str] | None = None,
                  shapes: dict | None = None) -> dict[str, dict]:
    """Functions (assembly or matched C) whose code is a Sony function in SONY_SHAPES, as Sony rows labelled with
    the runtime's name ({name: {"lib", "bios": None, "label": target, "why", "vram"}}): plan() makes each an alias
    (forwarded, its C dropped). An overlay linked with its own copy of the library matches again. Functions the
    database already knows, or already aliased, or named as that function, are left alone."""
    shapes = SONY_SHAPES if shapes is None else shapes
    sizes = _sizes(build, image)
    out = {}
    for f in build["functions"]:
        n = f["name"]
        if f.get("rom") is None or n in known or n in (aliases or {}) or n not in sizes:
            continue
        hit = shapes.get(shape_fingerprint(image, f["rom"], sizes[n][1]))
        if not hit or hit[0] == n or hit[0] not in IMPLEMENTED:
            continue
        target, why = hit
        out[n] = {"lib": IMPLEMENTED[target], "bios": None, "label": target, "why": f"by its code: {why}",
                  "objects": [], "vram": f["vram"]}
    return out


def sony_by_name(build: dict, known: dict[str, dict]) -> dict[str, dict]:
    """Functions still in assembly that carry the name of a Sony function the runtime implements but aren't marked as
    Sony's in the database (SLUS-01252's LoadImage at 0x801762C0): the runtime's then."""
    out = {}
    for f in build["functions"]:
        n = f["name"]
        if not f.get("c") and n in IMPLEMENTED and n not in known:
            out[n] = {"lib": IMPLEMENTED[n], "bios": None, "label": n, "objects": [], "vram": f["vram"]}
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
         handwritten: dict[str, dict] | None = None, aliases: dict[str, str] | None = None) -> dict:
    """Decide what the port is made of. sources: {path in the tree: text} of the matched C files; headers:
    {name: text} of include/*.h; excluded: {path: why} matched files left out (they don't compile for the host);
    handwritten: handwritten_gte()'s functions, which the runtime replays instead of stubbing; aliases:
    libcd_aliases()'s {game function: runtime function}, which replace the game's own (matched C or not)."""
    excluded = excluded or {}
    # one mechanism for every function the runtime's stands in for: libcd_aliases() (by name, by library name, by
    # offset and masked code), and by name a Sony function the runtime implements under its label (an unnamed SDK
    # function identified in SDK_IDENTIFIED, a Sony row the database names by its address)
    aliases = {**{n: s["label"] for n, s in sony.items() if n not in IMPLEMENTED and s.get("label") != n
                  and s.get("label") in IMPLEMENTED}, **(aliases or {})}
    handwritten = {k: v for k, v in (handwritten or {}).items() if k not in sony and k not in aliases}
    fns = build["functions"]
    # replaced: matched C the runtime's functions stand in for (the libcd aliases, and every function in an SDK range)
    replaced = {f["c"]: f["name"] for f in fns if f.get("c") and f["c"] not in excluded and
                (f["name"] in aliases or sony.get(f["name"], {}).get("sdk_range"))}
    compiled = [f for f in fns if f.get("c") and f["c"] not in excluded and f["c"] not in replaced]
    host_c = {f["name"] for f in compiled}
    texts = {p: t for p, t in sources.items() if p not in excluded and p not in replaced}
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
    excluded_names = {f["name"] for f in fns if f.get("c") in excluded and f["name"] not in aliases}
    seen = host_c | excluded_names
    for f in fns:
        if f["name"] not in seen and _C_NAME.match(f["name"]):
            seen.add(f["name"])
            asm_names.append(f["name"])
    # functions used as data (func_X + N, func_X[i]): unmatched ones become absolute symbols if never called by name
    as_data_unmatched = sorted(n for n in asm_names if n in data_use and n not in calls and n not in sony
                               and n not in aliases)
    as_data_matched = sorted(n for n in host_c if n in data_use)
    stubbed = [n for n in asm_names if n not in as_data_unmatched]
    unmatched = [(n, None) for n in stubbed if n not in sony and n not in handwritten and n not in aliases]
    replayed = {n: handwritten[n] for n in stubbed if n in handwritten}
    by_name = {f["name"]: f for f in fns}
    for path, why in sorted(excluded.items()):
        f = next((g for g in fns if g.get("c") == path), None)
        if f and f["name"] not in aliases:
            unmatched.append((f["name"], why))
    sdk = []
    for n in stubbed:
        if n in aliases:  # the runtime's function: under its own name, or a thunk to it (sdk_stubs_c)
            s = sony.get(n, {})
            tgt = aliases[n]
            lib = "libcd" if tgt in LIBCD_RUNTIME else IMPLEMENTED.get(tgt) or s.get("lib") or "libapi"
            sdk.append({"name": n, "lib": lib, "bios": None, "label": s.get("label", tgt),
                        "kind": "implemented" if tgt == n else "alias", "target": tgt, "why": s.get("why"),
                        "called": n in calls, "replaced": n in set(replaced.values())})
        elif n in sony:
            s = sony[n]
            kind = "implemented" if n in IMPLEMENTED else ("bios" if s.get("bios") in BIOS_LIBC else "stub")
            sdk.append({"name": n, "lib": IMPLEMENTED.get(n, s["lib"]), "bios": s.get("bios"), "label": s["label"],
                        "kind": kind, "called": n in calls, "why": s.get("why")})
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
        kind = 0 if n in host_c else 3 if n in replayed or n in aliases else 1 if n not in sony else (
            3 if n in IMPLEMENTED else 2)
        dispatch.append({"vram": f["vram"], "rom": f.get("rom"), "name": n, "seg": f["seg"], "kind": kind})
    dispatch.sort(key=lambda d: (d["vram"], d["seg"]))
    renames = {n: f"psx_game_{n}" for n in sorted(host_fn | set(symbols)) if n in RESERVED}
    return {"renames": renames, "game_implements": sorted(n for n in IMPLEMENTED if n in host_c),"compiled": [f["c"] for f in compiled], "unmatched": unmatched, "sdk": sdk, "symbols": symbols,
            "as_data_matched": as_data_matched, "as_data_unmatched": as_data_unmatched, "unresolved": unresolved,
            "dispatch": dispatch, "excluded": dict(excluded), "calls": calls, "replayed": replayed,
            "replaced": dict(replaced)}


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
    declared: set[str] = set()
    out = ["/* Sony's library functions and BIOS call stubs: a logging stub each (psx_sdk_stub: the first 20 calls of",
           " * each, then every 10,000th with its count up to the 10,000,000th; the count saturates, never wraps), or the",
           " * runtime's implementation of the BIOS's C library calls. The implemented ones (psx/sdk) are not here. */",
           '#include "psx_runtime.h"', '#include "libapi.h"', '#include "libcard.h"', '#include "libcd.h"', ""]
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
            elif s["kind"] == "alias":  # the runtime's function stands in for the game's
                tgt = s["target"]
                if tgt in LIBCD_RUNTIME:  # its signature is known: a typed thunk
                    ret, params = LIBCD_RUNTIME[tgt]
                    args = ", ".join(x.split()[-1] for x in params.split(", ")) if params != "void" else ""
                    body = f"{tgt}({args});" if ret == "void" else f"return {tgt}({args});"
                    out.append(f"{ret} {c}({params}) {{ {body} }} /* {tgt} */")
                else:  # by its Sony name, with whatever the game passed (the 8 argument registers)
                    if tgt not in declared:
                        declared.add(tgt)
                        out.append(f"extern int {tgt}();")
                    args = ", ".join(f"long a{i}" for i in range(8))
                    fwd = ", ".join(f"a{i}" for i in range(8))
                    out.append(f"int {c}({args}) {{ return ((int (*)({', '.join(['long'] * 8)})){tgt})({fwd}); }}"
                               f" /* {tgt}{': ' + _c_string(s['why']) if s.get('why') else ''} */")
            else:
                out.append(f"static unsigned calls_{s['name']};")
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


def config_c(title: str, image_name: str, entry: dict, main_name: str, disc: str = "") -> str:
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
    "{_c_string(disc or '')}", /* the disc image (`decomp port --disc`); PSX_DISC overrides it */
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


def game_sources_cmake(paths: list[str], renames: dict[str, str] | None = None,
                       patches: list[dict] | None = None) -> str:
    out = "# the game's matched C, relative to the exported tree (generated)\nset(GAME_SOURCES\n" + \
        "".join(f"    {p}\n" for p in paths) + ")\n"
    out += "# the port's patches (patches/): compiled in place of the export's file\n" + \
        "".join(f"set(GAME_PATCH_{x['path']} ${{CMAKE_CURRENT_SOURCE_DIR}}/{x['file']})\n" for x in patches or [])
    out += "# game functions named like the host's C library, renamed in the game's files\nset(GAME_RENAMES\n" + \
        "".join(f"    {a}={b}\n" for a, b in sorted((renames or {}).items())) + ")\n"
    return out


RUN_SH = """#!/bin/sh
# Run the port the way you'd play it: soft mode (a stub returns instead of stopping the run), the disc from
# port.json, and the game's executable from the export or from the copy kept here (the export is rewritten by the
# relink every so often). PSX_PORT_STRICT=1 gives the generator's strict run; anything you set yourself wins.
cd "$(dirname "$0")" || exit 1
[ -n "$PSX_PORT_STRICT" ] || export PSX_PORT_SOFT="${PSX_PORT_SOFT:-1}"
if [ -z "$PSX_DISC" ]; then
  PSX_DISC=$(python3 -c "import json; print(json.load(open('port.json')).get('disc') or '')" 2>/dev/null)
  [ -n "$PSX_DISC" ] && export PSX_DISC
fi
if [ -z "$PSX_SOURCE_DIR" ]; then
  if [ -f "../source/@IMAGE@" ]; then
    cp -f "../source/@IMAGE@" "@IMAGE@" 2>/dev/null
  elif [ ! -f "@IMAGE@" ]; then
    echo "run.sh: no game executable: ../source/@IMAGE@ is missing (export in progress?) and no copy here yet" >&2
    exit 1
  fi
  [ -f "../source/@IMAGE@" ] || export PSX_SOURCE_DIR="$(pwd)"
fi
[ -x build/game ] || { echo "run.sh: build/game is missing: decomp port @SLUG@ --build" >&2; exit 1; }
exec ./build/game "$@"
"""


def run_sh(slug: str, image: str) -> str:
    """The launcher next to the binary: `port/run.sh`, soft mode and the disc set, the executable kept at hand."""
    return RUN_SH.replace("@SLUG@", slug).replace("@IMAGE@", image)  # not str.format: the shell's ${...} has braces


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
# the entry trace (`trace: enters ...`, the last functions entered at a crash): off by default; PSX_PORT_TRACE=1 in
# the environment at configure time turns the default on (decomp port --trace passes -DPSX_TRACE=ON)
if(DEFINED ENV{{PSX_PORT_TRACE}} AND NOT "$ENV{{PSX_PORT_TRACE}}" STREQUAL "0")
  set(_psx_trace_default ON)
else()
  set(_psx_trace_default OFF)
endif()
option(PSX_TRACE "log the game functions entered (tools/cc_ptr32.py --trace: argument registers kept)" ${{_psx_trace_default}})

include(${{CMAKE_CURRENT_SOURCE_DIR}}/gen/game_sources.cmake)
set(GAME_INPUTS)  # each file from the export, or the port's patch for it (patches/, gen/game_patches.txt)
foreach(_s IN LISTS GAME_SOURCES)
  if(DEFINED GAME_PATCH_${{_s}})
    list(APPEND GAME_INPUTS ${{GAME_PATCH_${{_s}}}})
  else()
    list(APPEND GAME_INPUTS ${{PSX_SOURCE_DIR}}/${{_s}})
  endif()
endforeach()
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
            --stamp ${{PTR32_DIR}}/stamp --patches ${{CMAKE_CURRENT_SOURCE_DIR}}/gen/game_patches.txt
    DEPENDS ${{GAME_INPUTS}} ${{PSX_GAME_HEADERS}} ${{CMAKE_CURRENT_SOURCE_DIR}}/tools/ptr32.py
            ${{CMAKE_CURRENT_SOURCE_DIR}}/gen/game_sources.txt ${{CMAKE_CURRENT_SOURCE_DIR}}/gen/game_patches.txt
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
set(GAME_TRACE_FLAGS)
if(PSX_PTR32)  # every use of a __ptr32 pointer translated to the window (tools/cc_ptr32.py): IR, patched, compiled
  set(_launcher "${{Python3_EXECUTABLE}};${{CMAKE_CURRENT_SOURCE_DIR}}/tools/cc_ptr32.py;--functions;${{CMAKE_CURRENT_SOURCE_DIR}}/gen/psx_function_addrs.txt;--literals;${{CMAKE_BINARY_DIR}}/literals")
  if(PSX_TRACE)  # a call to psx/trace.S first in each game function, the argument registers kept
    string(APPEND _launcher ";--trace")
  endif()
  set_target_properties(game_code PROPERTIES C_COMPILER_LAUNCHER "${{_launcher}}")
  # the launcher's arguments aren't part of what make checks: a file that changes with them is
  set(_trace_stamp "${{CMAKE_BINARY_DIR}}/psx_trace.txt")
  if(NOT EXISTS "${{_trace_stamp}}")
    file(WRITE "${{_trace_stamp}}" "")
  endif()
  file(READ "${{_trace_stamp}}" _was)
  if(NOT _was STREQUAL "${{PSX_TRACE}}")
    file(WRITE "${{_trace_stamp}}" "${{PSX_TRACE}}")
  endif()
  set_property(SOURCE ${{GAME_COMPILE}} APPEND PROPERTY OBJECT_DEPENDS ${{_trace_stamp}}
               ${{CMAKE_CURRENT_SOURCE_DIR}}/tools/cc_ptr32.py ${{CMAKE_CURRENT_SOURCE_DIR}}/gen/psx_function_addrs.txt)
elseif(PSX_TRACE)
  message(WARNING "PSX_TRACE without PSX_PTR32 is -finstrument-functions, which puts the function's address in x0: "
                  "matched C that passes an argument through (func_X() from a void function) breaks under it")
  set(GAME_TRACE_FLAGS -finstrument-functions)
endif()
target_include_directories(game_code BEFORE PRIVATE ${{GAME_INCLUDES}})
target_compile_definitions(game_code PRIVATE ${{GAME_RENAMES}})
target_compile_options(game_code PRIVATE {flags} ${{GAME_PTR32_FLAGS}} {GAME_OPT} -g ${{GAME_TRACE_FLAGS}}
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
message(STATUS "PS1 port: window through ${{PSX_HOST}}, ${{CMAKE_OSX_ARCHITECTURES}}${{CMAKE_SYSTEM_PROCESSOR}}, trace ${{PSX_TRACE}}")

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
  target_compile_definitions(game PRIVATE PSX_AUDIO_SDL=1)  # psx/audio.c: the SPU's mix to SDL2's audio device
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


# ---------------------------------------------------------------------------------------------- dropped arguments
#
# Matched C that calls a function with fewer arguments than it takes is byte-exact on the PS1: the argument
# registers ($a0-$a3) pass through untouched, so the callee reads whatever its caller's caller left there. On the
# host the same is true only while nothing touches x0-x7 between the caller's entry and the call; clang owes nothing
# to a value it doesn't know about. m2c drafts do this (`M2C_UNK func_X();` then `func_X()`), and so do calls
# through a library's function table (`M2C_FIELD(t, M2C_UNK (**)(), 0x2C)()`). The fix is in the C: pass the
# argument through explicitly (the caller takes it as a parameter and hands it on), which still matches on MIPS.
#
# The parser is a regex/bracket scanner over the export's one-function-per-file layout (top-level declarations and
# definitions, calls in the bodies), not clang's AST: it's fast over 4,000 files and sees the C as written, before
# the pointer rewrite. Calls through a cast of a game function (`((T (*)())f)(x)`, or a local pointer that only
# holds f) count, with the cast's type as their declaration; K&R definitions and declarations made inside a body
# aren't seen (rom/passthrough.py's scanner does both).

class CSig(dict):
    """A function's parameters as declared: n (None: `()`, unprototyped), variadic, text (the parameter list)."""


def _parse_params(text: str) -> CSig:
    t = re.sub(r"\s+", " ", text).strip()
    if t == "":
        return CSig(n=None, variadic=False, text="()")
    if t == "void":
        return CSig(n=0, variadic=False, text="(void)")
    parts = _split_commas(t)
    variadic = parts[-1].strip() == "..."
    return CSig(n=len(parts) - (1 if variadic else 0), variadic=variadic, text=f"({t})")


def _split_commas(s: str) -> list[str]:
    parts, depth, cur = [], 0, []
    for c in s:
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    parts.append("".join(cur))
    return parts


def _close(s: str, i: int, o: str = "(", c: str = ")") -> int:
    """s[i] == o: the index of its matching c (len(s) when unbalanced)."""
    depth = 0
    for j in range(i, len(s)):
        if s[j] == o:
            depth += 1
        elif s[j] == c:
            depth -= 1
            if depth == 0:
                return j
    return len(s)


def _open(s: str, j: int) -> int:
    """s[j] == ')': the index of its matching '(' (-1 when unbalanced)."""
    depth = 0
    for i in range(j, -1, -1):
        if s[i] == ")":
            depth += 1
        elif s[i] == "(":
            depth -= 1
            if depth == 0:
                return i
    return -1


_NOT_FN = _C_KEYWORDS | {"__attribute__", "__asm__", "asm", "__typeof__", "typeof", "__declspec", "defined"}
_FN_HEAD = re.compile(r"\b([A-Za-z_]\w*)\s*\(")


def _code(text: str) -> str:
    """C without comments, literals and every preprocessor line (strip_c keeps #define bodies, which would land in
    the top level here)."""
    return strip_c(re.sub(r"(?m)^[ \t]*#(?:[^\n]*\\\n)*[^\n]*", "", text))


def _declared(decl: str) -> list[tuple[str, CSig]]:
    """The functions a top-level declaration (no trailing ;) declares: name and parameters, at paren depth 0 (a
    function pointer `T (*fp)(int)` declares none)."""
    d = decl.strip()
    if not d or d.startswith("typedef") or re.match(r"(?:struct|union|enum)\b[^(]*$", d):
        return []
    out = []
    for m in _FN_HEAD.finditer(d):
        if m.group(1) in _NOT_FN:
            continue
        depth = d[:m.start()].count("(") - d[:m.start()].count(")")
        if depth != 0 or "=" in d[:m.start()]:
            continue
        end = _close(d, m.end() - 1)
        out.append((m.group(1), _parse_params(d[m.end():end])))
    return out


def c_toplevel(text: str) -> dict:
    """One C file's top level: {"prototypes": {name: CSig}, "definitions": {name: (CSig, body)}}."""
    s = _code(text)
    protos: dict[str, CSig] = {}
    defs: dict[str, tuple[CSig, str]] = {}
    i, start, n = 0, 0, len(s)
    while i < n:
        c = s[i]
        if c == ";":
            for name, sig in _declared(s[start:i]):
                protos.setdefault(name, sig)
            start = i + 1
        elif c == "{":
            end = _close(s, i, "{", "}")
            head = s[start:i].strip()
            m = None
            for m2 in _FN_HEAD.finditer(head):
                if m2.group(1) not in _NOT_FN and head[:m2.start()].count("(") == head[:m2.start()].count(")"):
                    m = m2
            if m and "=" not in head and not re.match(r"(?:typedef|struct|union|enum)\b", head):
                pend = _close(head, m.end() - 1)
                if not head[pend + 1:].strip() or head[pend + 1:].strip().startswith("__attribute__"):
                    defs[m.group(1)] = (_parse_params(head[m.end():pend]), s[i + 1:end])
                    i, start = end + 1, end + 1
                    continue
            i = end + 1  # a struct body or an initializer: the declaration goes on to its ;
            continue
        i += 1
    return {"prototypes": protos, "definitions": defs}


def body_calls(body: str, functions: set[str]) -> list[tuple[str, int]]:
    """(callee, arguments) for each direct call of a game function in a function body."""
    out = []
    for m in _FN_HEAD.finditer(body):
        name = m.group(1)
        if name not in functions:
            continue
        before = body[:m.start()].rstrip()
        if before.endswith((".", "->", "&")) or re.search(
                r"\b(?:void|s8|u8|s16|u16|s32|u32|int|char|short|long|unsigned|signed|M2C_UNK\w*|extern|static)"
                r"\s*\**$", before):
            continue  # a member, an address taken, or a declaration inside the body
        end = _close(body, m.end() - 1)
        args = body[m.end():end].strip()
        out.append((name, 0 if not args else len(_split_commas(args))))
    return out


_NOPROTO_FNPTR = re.compile(r"\(\s*\*+\s*\)\s*\(\s*\)")


def table_calls(body: str) -> list[str]:
    """Calls with no arguments through an unprototyped function pointer type (`M2C_UNK (**)()`): the callee
    expression of each, as written."""
    out = []
    locals_ = set(re.findall(r"\(\s*\*\s*([A-Za-z_]\w*)\s*\)\s*\(\s*\)", body))
    for m in re.finditer(r"\)\s*\(\s*\)", body):
        o = _open(body, m.start())
        if o < 0:
            continue
        name = re.search(r"([A-Za-z_]\w*)\s*$", body[:o])
        start = name.start(1) if name else o
        expr = body[start:m.start() + 1]
        if _NOPROTO_FNPTR.search(expr):
            out.append(re.sub(r"\s+", " ", expr))
    for v in locals_:
        for m in re.finditer(rf"(?<![\w.>])\b{re.escape(v)}\s*\(\s*\)", body):
            if not re.search(r"\(\s*\*\s*$", body[:m.start()]):
                out.append(v)
    return out


_FN_TYPE = re.compile(r"\(\s*\*+\s*([A-Za-z_]\w*)?\s*\)\s*\(")


def fn_type(s: str, a: int, b: int) -> dict | None:
    """The function-pointer type written in s[a:b] (`void (*)(s32)`, `s32 (*cb)(void *)`): {"ret": (start, end) of
    its return type, "params": (lp, rp) of its parameter list's parentheses, "n": its parameter count (None for
    `()`), "variadic", "text"}. None when s[a:b] isn't one (a typedef name)."""
    m = _FN_TYPE.search(s, a, b)
    if not m:
        return None
    lp = m.end() - 1
    rp = _close(s, lp)
    if rp >= b:
        return None
    ws = a + len(s[a:m.start()]) - len(s[a:m.start()].lstrip())
    sig = _parse_params(s[lp + 1:rp])
    return {"ret": (ws, m.start()), "params": (lp, rp), "n": sig["n"], "variadic": sig["variadic"],
            "text": re.sub(r"\s+", " ", s[a:b]).strip()}


def cast_calls(body: str, functions: set[str]) -> list[dict]:
    """Calls through a cast of a named function, in a body with comments and literals blanked (positions are
    body's): `((T (*)(...)) func_X)(args)`, `(*(T (*)()) &func_X)()`, and a local function pointer that only ever
    holds one game function (`T (*v)(...) = (T (*)(...)) func_X;` or `v = func_X;`, then `v(args)` / `(*v)(args)`).
    Each: {"callee", "nargs", "lp", "rp" (the call's parentheses), "at" (where the call starts), "via" ("cast" or
    the variable's name), "sigs": the function types written for it, as fn_type gives them (the cast's; for a
    variable its declaration's and each cast assigned to it)}."""
    out = []
    for m in re.finditer(r"\)\s*&?\s*([A-Za-z_]\w*)\s*\)\s*\(", body):
        name = m.group(1)
        if name not in functions:
            continue
        close = body.rfind(")", m.start(), m.start(1))  # the cast's closing parenthesis
        o = _open(body, close)
        if o < 0:
            continue
        sig = fn_type(body, o + 1, close)
        lp = m.end() - 1
        rp = _close(body, lp)
        args = body[lp + 1:rp].strip()
        out.append({"callee": name, "nargs": 0 if not args else len(_split_commas(args)), "lp": lp, "rp": rp,
                    "at": o, "via": "cast", "sigs": [sig] if sig else []})
    # local function pointers
    held: dict[str, set[str]] = {}
    sigs: dict[str, list[dict]] = {}
    decls: set[int] = set()
    decl = re.compile(r"\(\s*\*\s*([A-Za-z_]\w*)\s*\)\s*\([^;{}=]*?\)\s*(?:=\s*([^;,]*))?[;,]")
    for m in decl.finditer(body):
        v = m.group(1)
        start = max(body.rfind(c, 0, m.start()) for c in ";{}") + 1
        if not re.match(r"\s*(?:register\s+|const\s+|static\s+)*[A-Za-z_]\w*", body[start:m.start()]) or \
                re.search(r"\b(?:return|if|while)\b|[=(]\s*$", body[start:m.start()]):
            continue  # a call through (*v)(...), not a declaration
        decls.add(m.start())
        rp = _close(body, body.index("(", m.end(1)))
        sig = fn_type(body, start, rp + 1)
        if sig:
            sigs.setdefault(v, []).append(sig)
        if m.group(2) is not None:
            held.setdefault(v, set()).add(m.group(2).strip())
    for v in list(sigs):
        for m in re.finditer(rf"(?<![\w.>]){re.escape(v)}\s*=(?!=)\s*([^;]*);", body):
            held.setdefault(v, set()).add(m.group(1).strip())
    for v, values in held.items():
        targets, casts = set(), []
        for val in values:
            t = re.fullmatch(r"(?:\((.*)\))?\s*&?\s*([A-Za-z_]\w*)", val, re.S)
            if not t or t.group(2) not in functions:
                targets.add(None)
                continue
            targets.add(t.group(2))
            if t.group(1):
                k = body.find(val)
                sig = fn_type(body, k + 1, k + 1 + len(t.group(1)))
                if sig:
                    casts.append(sig)
        if len(targets) != 1 or None in targets:
            continue
        name = targets.pop()
        for m in re.finditer(rf"(?<![\w.>])(?:\(\s*\*\s*{re.escape(v)}\s*\)|{re.escape(v)})\s*\(", body):
            if m.start() in decls or re.search(r"\(\s*\*\s*$", body[:m.start()]):
                continue  # the declaration itself (or the inside of `(*v)(`, found as the whole)
            lp = m.end() - 1
            rp = _close(body, lp)
            args = body[lp + 1:rp].strip()
            out.append({"callee": name, "nargs": 0 if not args else len(_split_commas(args)), "lp": lp, "rp": rp,
                        "at": m.start(), "via": v, "sigs": sigs.get(v, []) + casts})
    return sorted(out, key=lambda c: c["lp"])


def mips_arg_reads(words: list[int]) -> list[int]:
    """The argument registers ($a0-$a3, as 0-3) a MIPS function reads before it writes them, scanning its code in
    order up to `jr $ra` and its delay slot (a heuristic: branches aren't followed; after a call, $a0-$a3 count as
    written). An assembly function's evidence of the parameters it takes."""
    read: set[int] = set()
    written: set[int] = set()

    def use(*regs):
        for r in regs:
            if 4 <= r <= 7 and r not in written:
                read.add(r - 4)

    def define(r):
        if 4 <= r <= 7:
            written.add(r)

    def call(k):  # jal/jalr: the delay slot runs first, then $a0-$a3 are the callee's
        if k + 1 < len(words):
            d = words[k + 1]
            dop, drs, drt = d >> 26, (d >> 21) & 31, (d >> 16) & 31
            if dop == 0:
                use(drs, drt)
            elif 8 <= dop <= 14 or 0x20 <= dop <= 0x26:
                use(drs)
            elif 0x28 <= dop <= 0x2E:
                use(drs, drt)
        written.update(range(4, 8))

    stop_after = None
    for k, w in enumerate(words):
        op, rs, rt, rd, fn = w >> 26, (w >> 21) & 31, (w >> 16) & 31, (w >> 11) & 31, w & 63
        if op == 0:
            if fn in (0, 2, 3):  # shifts by an immediate
                use(rt)
                define(rd)
            elif fn == 8:  # jr
                use(rs)
                if rs == 31:
                    stop_after = k + 1
            elif fn == 9:  # jalr
                use(rs)
                call(k)
            elif fn in (12, 13):  # syscall, break
                pass
            elif fn in (16, 18):  # mfhi, mflo
                define(rd)
            elif fn in (17, 19):  # mthi, mtlo
                use(rs)
            elif 24 <= fn <= 27:  # mult, div
                use(rs, rt)
            else:
                use(rs, rt)
                define(rd)
        elif op == 1:
            use(rs)
        elif op == 2:
            pass
        elif op == 3:
            call(k)
        elif op in (4, 5):
            use(rs, rt)
        elif op in (6, 7):
            use(rs)
        elif 8 <= op <= 14:
            use(rs)
            define(rt)
        elif op == 15:
            define(rt)
        elif 0x20 <= op <= 0x26:
            use(rs)
            define(rt)
        elif 0x28 <= op <= 0x2E:
            use(rs, rt)
        elif op in (0x10, 0x12):
            if rs in (4, 6):
                use(rt)
            elif rs in (0, 2):
                define(rt)
        elif op in (0x32, 0x3A):
            use(rs)
        if stop_after is not None and k >= stop_after:
            break
    return sorted(read)


def _mips_effect(w: int) -> tuple[set[int], set[int]]:
    """The registers one MIPS instruction reads and writes (control flow apart)."""
    op, rs, rt, rd, fn = w >> 26, (w >> 21) & 31, (w >> 16) & 31, (w >> 11) & 31, w & 63
    if op == 0:
        if fn in (0, 2, 3):
            return {rt}, {rd}
        if fn in (8, 9):
            return {rs}, ({rd} if fn == 9 else set())
        if fn in (12, 13):
            return set(), set()
        if fn in (16, 18):
            return set(), {rd}
        if fn in (17, 19):
            return {rs}, set()
        if 24 <= fn <= 27:
            return {rs, rt}, set()
        return {rs, rt}, {rd}
    if op == 1 or op in (6, 7):
        return {rs}, set()
    if op in (4, 5):
        return {rs, rt}, set()
    if 8 <= op <= 14 or 0x20 <= op <= 0x26:
        return {rs}, {rt}
    if op == 15:
        return set(), {rt}
    if 0x28 <= op <= 0x2E:
        return {rs, rt}, set()
    if op in (0x10, 0x12):
        return ({rt}, set()) if rs in (4, 6) else (set(), {rt}) if rs in (0, 2) else (set(), set())
    if op in (0x32, 0x3A):
        return {rs}, set()
    return set(), set()


def mips_arg_live(words: list[int], vram: int, reads_of=None) -> list[int]:
    """The argument registers ($a0-$a3, as 0-3) a MIPS function can read before writing them on some path from its
    entry: liveness over its control flow (branches and their delay slots followed, `jr $ra` the end, a jump table's
    `jr` counted as reading every argument register). A call reads what reads_of(target vram) says it reads (None:
    all four; default: all four) and leaves them written. A finer test than mips_arg_reads, which reads in a straight
    line to the first return."""
    n = len(words)
    args = set(range(4, 8))

    def call_reads(k: int) -> set[int]:
        w = words[k]
        if w >> 26 == 3 and reads_of is not None:
            r = reads_of(((vram + 4 * k) & 0xF0000000) | ((w & 0x3FFFFFF) << 2))
            if r is not None:
                return {4 + x for x in r}
        return set(args)

    # per instruction: (uses, defs, successors), the delay slot carrying its branch's targets
    nodes: list[tuple[set[int], set[int], list[int]]] = []
    for k, w in enumerate(words):
        u, d = _mips_effect(w)
        nodes.append((u & args, d & args, [k + 1] if k + 1 < n else []))
    for k, w in enumerate(words):
        op, rs, rt, fn = w >> 26, (w >> 21) & 31, (w >> 16) & 31, w & 63
        if k + 1 >= n:
            break
        u, d, _ = nodes[k + 1]
        if op in (1, 4, 5, 6, 7):  # conditional (beq $zero, $zero: always)
            t = k + 1 + ((w & 0xFFFF) ^ 0x8000) - 0x8000
            always = op == 4 and rs == 0 and rt == 0
            nodes[k + 1] = (u, d, [x for x in ([t] if always else [t, k + 2]) if 0 <= x < n])
        elif op == 2:  # j: within the function, or a tail call
            t = (((vram + 4 * k) & 0xF0000000) | ((w & 0x3FFFFFF) << 2)) - vram
            if 0 <= t // 4 < n:
                nodes[k + 1] = (u, d, [t // 4])
            else:  # a tail call reads its callee's arguments
                nodes[k + 1] = (u | (call_reads(k) - d), d, [])
        elif op == 0 and fn == 8:  # jr: $ra returns; another register is a jump table
            nodes[k + 1] = (u | (set() if rs == 31 else args) - d, d, [])
        elif op == 3 or (op == 0 and fn == 9):  # a call after its delay slot: reads, then clobbers
            reads = call_reads(k) if op == 3 else set(args)
            nodes[k + 1] = (u | (reads - d), d | args, [k + 2] if k + 2 < n else [])
    live_in = [set() for _ in range(n)]
    changed = True
    while changed:
        changed = False
        for k in range(n - 1, -1, -1):
            u, d, succ = nodes[k]
            out = set().union(*(live_in[s] for s in succ)) if succ else set()
            new = u | (out - d)
            if new != live_in[k]:
                live_in[k] = new
                changed = True
    return sorted(r - 4 for r in live_in[0]) if n else []


def mips_stack_arg_read(words: list[int], k: int) -> bool:
    """Whether a MIPS function loads its stack argument k (the fifth parameter is k = 4: at 16($sp) on entry, F + 16
    once its frame of F bytes is made) from $sp anywhere in its code."""
    frame, out = 0, False
    for w in words:
        op, rs, rt, imm = w >> 26, (w >> 21) & 31, (w >> 16) & 31, ((w & 0xFFFF) ^ 0x8000) - 0x8000
        if op == 9 and rs == 29 and rt == 29 and imm < 0 and not frame:
            frame = -imm
        elif 0x20 <= op <= 0x26 and rs == 29 and imm == frame + 4 * k:
            out = True
    return out


def passes_through(words: list[int], callee_vram: int, args: int, params: int, nth: int = 0,
                   vram: int | None = None) -> str:
    """What the dropped argument registers ($a<args>..$a<params-1>) hold at a caller's first call to the callee (or
    its nth, counting from 0, in the order of the code): "pass-through" (nothing wrote them and no call came before:
    the caller's own arguments, so the fix is to take them as parameters and hand them on), "leftover" (written or
    clobbered by an earlier call first: what the callee gets is whatever was there), "?" (no such jal/j to the
    callee found). With the caller's vram, along its control flow (a write or a call on another branch doesn't
    count: `if (x) { g(); return; } f();` passes $a0 through to f); without it, in a straight line."""
    if vram is not None:
        return _passes_through_flow(words, vram, callee_vram, args, params, nth)
    target = (callee_vram >> 2) & 0x3FFFFFF
    written, called, seen = set(), False, 0
    for k, w in enumerate(words):
        op, rs, rt, rd, fn = w >> 26, (w >> 21) & 31, (w >> 16) & 31, (w >> 11) & 31, w & 63
        if op in (2, 3) and (w & 0x3FFFFFF) == target and seen < nth:
            seen += 1
            called = True
            continue
        if op in (2, 3) and (w & 0x3FFFFFF) == target:
            if k + 1 < len(words):  # the delay slot runs first
                d = words[k + 1]
                dop, drt, drd = d >> 26, (d >> 16) & 31, (d >> 11) & 31
                written.add(drd if dop == 0 else drt if (8 <= dop <= 15 or 0x20 <= dop <= 0x26) else -1)
            dropped = set(range(4 + args, 4 + params))
            return "pass-through" if not called and not (written & dropped) else "leftover"
        if op == 3 or (op == 0 and fn == 9):
            called = True
        elif op == 0 and fn not in (8, 9, 12, 13, 17, 19) and not 24 <= fn <= 27:
            written.add(rd)
        elif 8 <= op <= 15 or 0x20 <= op <= 0x26 or (op in (0x10, 0x12) and rs in (0, 2)):
            written.add(rt)
    return "?"


def _mips_flow(words: list[int], vram: int) -> list[tuple[list[int], str | None]]:
    """Each instruction's successors and what follows it: a branch's or jump's delay slot carries its targets; the
    delay slot of a call is marked "call" (the call happens after it), of `jr`/a jump out of the function "end"."""
    n = len(words)
    out: list[tuple[list[int], str | None]] = [([k + 1] if k + 1 < n else [], None) for k in range(n)]
    for k, w in enumerate(words[:-1]):
        op, rs, rt, fn = w >> 26, (w >> 21) & 31, (w >> 16) & 31, w & 63
        if op in (1, 4, 5, 6, 7) and not (op == 1 and rt in (16, 17)):
            t = k + 1 + ((w & 0xFFFF) ^ 0x8000) - 0x8000
            always = op == 4 and rs == 0 and rt == 0
            out[k + 1] = ([x for x in ([t] if always else [t, k + 2]) if 0 <= x < n], None)
        elif op == 2:
            t = ((((vram + 4 * k) & 0xF0000000) | ((w & 0x3FFFFFF) << 2)) - vram) // 4
            out[k + 1] = ([t], None) if 0 <= t < n else ([], "end")
        elif op == 0 and fn == 8:
            out[k + 1] = ([], "end")
        elif op == 3 or (op == 0 and fn == 9) or (op == 1 and rt in (16, 17)):
            out[k + 1] = ([k + 2] if k + 2 < n else [], "call")
    return out


def _passes_through_flow(words: list[int], vram: int, callee_vram: int, args: int, params: int, nth: int) -> str:
    """passes_through along the control flow: the registers that may have been written (or clobbered by a call)
    on some path from the entry, at the nth jal/j to the callee."""
    target = (callee_vram >> 2) & 0x3FFFFFF
    sites = [k for k, w in enumerate(words) if w >> 26 in (2, 3) and (w & 0x3FFFFFF) == target]
    if nth >= len(sites):
        return "?"
    site = sites[nth]
    flow = _mips_flow(words, vram)
    args_regs = set(range(4, 8))
    state: list[set[int] | None] = [None] * len(words)  # None: not reached
    state[0] = set()
    work = [0]
    while work:
        k = work.pop()
        _, d = _mips_effect(words[k])
        succ, after = flow[k]
        out = state[k] | (d & args_regs) | (args_regs if after == "call" else set())
        for s in succ:
            if state[s] is None or not out <= state[s]:
                state[s] = out if state[s] is None else state[s] | out
                work.append(s)
    if site + 1 >= len(words):
        return "?"
    if state[site] is None:  # reached only through a jump table (`jr $reg`): in a straight line then
        return passes_through(words, callee_vram, args, params, nth)
    _, d = _mips_effect(words[site + 1])  # the delay slot runs first
    at_call = state[site] | (d & args_regs)
    return "leftover" if at_call & set(range(4 + args, 4 + params)) else "pass-through"


def call_hazards(sources: dict[str, str], compiled: list[str], headers: dict[str, str], functions: set[str],
                 matched: dict[str, str], replaced: dict[str, str] | None = None,
                 runtime_sigs: dict[str, CSig] | None = None, asm_reads: dict[str, list[int]] | None = None) -> dict:
    """The dropped-argument hazards of the matched C the port compiles.

    sources: {path: text} of every matched file; compiled: the paths the port compiles (callers); headers: include/
    (common.h's prototypes stand in for a caller's own); functions: every game function's name; matched: {name: path}
    of the matched ones; replaced: {game function: the runtime function that stands in for it}; runtime_sigs: the
    runtime's prototypes (psx/sdk/*.h); asm_reads: {unmatched function: the argument registers its code reads first}
    (mips_arg_reads).

    a. "dropped": a call passing fewer arguments than the callee's definition (matched C, or the runtime's prototype
       for a function it replaces) takes, with how the caller declared it: "(void)", "fewer" (a prototype with fewer
       parameters), "()" (unprototyped), "none" (only common.h's, or implicit) or "cast" (called through a cast of
       it, cast_calls: the cast's type is the declaration).
    b. "table_calls": calls with no arguments through an unprototyped function pointer (`M2C_UNK (**)()`).
    c. "unmatched": calls with no arguments to a function still in assembly, which no definition checks; with
       common.h's prototype if it has one, else the argument registers its code reads ("reads $a0, $a1"), else
       "unknown"."""
    replaced = replaced or {}
    runtime_sigs = runtime_sigs or {}
    asm_reads = asm_reads or {}
    common: dict[str, CSig] = {}
    for t in headers.values():
        for k, v in c_toplevel(t)["prototypes"].items():
            common.setdefault(k, v)
    parsed = {p: c_toplevel(t) for p, t in sources.items()}
    defined: dict[str, CSig] = {}
    for name, path in matched.items():
        d = parsed.get(path, {}).get("definitions", {}).get(name)
        if d:
            defined[name] = d[0]
    dropped, tables, unmatched = [], [], []
    n_casts, casts_checked = 0, []
    for path in compiled:
        top = parsed.get(path)
        if not top:
            continue
        for caller, (_, body) in top["definitions"].items():
            for expr in table_calls(body):
                tables.append({"caller": caller, "file": path, "call": expr[:160]})
            seen = set()
            casts = cast_calls(body, functions)
            n_casts += len(casts)
            for c in casts:
                if c["callee"] in defined:
                    casts_checked.append(c)
            direct = [(callee, nargs, None) for callee, nargs in body_calls(body, functions)]
            for callee, nargs, cast in direct + [(c["callee"], c["nargs"], c) for c in casts]:
                if (callee, nargs, cast is None) in seen:
                    continue
                seen.add((callee, nargs, cast is None))
                local = top["prototypes"].get(callee)
                decl = local or common.get(callee)
                how = "none" if local is None else local["text"] if local["n"] in (None, 0) else "fewer"
                if cast is not None:  # the cast's type is the declaration this call goes by
                    how = "cast"
                    decl = {"text": cast["sigs"][0]["text"]} if cast["sigs"] else {"text": "(a typedef)"}
                if callee in replaced and replaced[callee] in runtime_sigs:
                    sig, where = runtime_sigs[replaced[callee]], f"the runtime's {replaced[callee]}"
                elif callee in defined:
                    sig, where = defined[callee], "matched C"
                else:
                    sig, where = None, None
                if sig is not None:
                    if sig["n"] and not sig["variadic"] and nargs < sig["n"]:
                        dropped.append({"caller": caller, "file": path, "callee": callee, "declared": how,
                                        "declaration": decl["text"] if decl else None, "defined": sig["text"],
                                        "params": sig["n"], "args": nargs, "definition": where})
                    continue
                if callee in matched or nargs:
                    continue
                if callee in common and common[callee]["n"] is not None:
                    n_ev = common[callee]["n"]
                    evidence = f"common.h: {common[callee]['text']}"
                elif callee in asm_reads:
                    regs = asm_reads[callee]
                    n_ev = max(regs) + 1 if regs else 0
                    evidence = ("reads " + ", ".join(f"$a{r}" for r in regs)) if regs else "reads no argument register"
                else:
                    n_ev, evidence = None, "unknown"
                unmatched.append({"caller": caller, "file": path, "callee": callee, "declared": how,
                                  "evidence": evidence, "params": n_ev})
    unmatched_args = [u for u in unmatched if u["params"]]
    return {
        "method": "regex/bracket scan of the export's one-function-per-file C (not clang's AST); "
                  "assembly evidence: argument registers read before written, straight-line scan to jr $ra",
        "dropped": dropped, "table_calls": tables, "unmatched": unmatched,
        "counts": {"dropped": len(dropped), "dropped_callers": len({d["caller"] for d in dropped}),
                   "dropped_by_declaration": {k: sum(1 for d in dropped if d["declared"] == k)
                                              for k in sorted({d["declared"] for d in dropped})},
                   "dropped_through_cast": sum(1 for d in dropped if d["declared"] == "cast"),
                   "cast_calls": n_casts, "cast_calls_to_matched": len(casts_checked),
                   "table_calls": len(tables), "table_callers": len({t["caller"] for t in tables}),
                   "unmatched": len(unmatched), "unmatched_takes_args": len(unmatched_args),
                   "unmatched_no_args": sum(1 for u in unmatched if u["params"] == 0),
                   "unmatched_unknown": sum(1 for u in unmatched if u["params"] is None)},
    }


def port_call_hazards(build: dict, image: bytes, sources: dict[str, str], headers: dict[str, str], p: dict,
                      sony: dict[str, dict], aliases: dict[str, str]) -> dict:
    """call_hazards() for a port: plan() p's compiled files as the callers, the runtime's prototypes for the
    functions it stands in for, the assembly's argument registers for the unmatched ones."""
    fns = build["functions"]
    matched = {f["name"]: f["c"] for f in fns if f.get("c")}
    replaced = {n: s.get("label") or n for n, s in sony.items()}
    replaced.update(aliases)
    sizes = _sizes(build, image)
    reads = {}
    for f in fns:
        n = f["name"]
        if n not in matched and f.get("rom") is not None and n in sizes:
            count = min(sizes[n][1], 4096, (len(image) - f["rom"]) // 4)
            reads[n] = mips_arg_reads(list(struct.unpack_from(f"<{count}I", image, f["rom"])))
    out = call_hazards(sources, p["compiled"], headers, {f["name"] for f in fns}, matched, replaced,
                       runtime_prototypes(), reads)
    by_name = {f["name"]: f for f in fns}

    def code(f) -> list[int] | None:
        if f is None or f.get("rom") is None or f["name"] not in sizes:
            return None
        count = min(sizes[f["name"]][1], 4096, (len(image) - f["rom"]) // 4)
        return list(struct.unpack_from(f"<{count}I", image, f["rom"]))
    at: dict[int, list[dict]] = {}
    for f in fns:
        at.setdefault(f["vram"], []).append(f)
    live: dict[int, list[int] | None] = {}

    def reads_of(v: int) -> list[int] | None:  # what a function's code reads (None: unknown, or an overlay's address)
        if v not in live:
            live[v] = None
            fs = at.get(v) or []
            w = code(fs[0]) if len(fs) == 1 else None
            if w:
                live[v] = mips_arg_live(w, v, reads_of)
        return live[v]
    for d in out["dropped"]:
        f, g = by_name.get(d["caller"]), by_name.get(d["callee"])
        d["register"] = "?"
        w = code(f)
        if f and g and w:
            d["register"] = "stack argument" if d["args"] >= 4 else \
                passes_through(w, g["vram"], d["args"], min(d["params"], 4), vram=f["vram"])
        gw = code(g)
        if gw is not None:  # what the callee's code reads of what the call drops
            regs = mips_arg_live(gw, g["vram"], reads_of)
            d["callee_reads"] = [f"$a{i}" for i in range(d["args"], min(d["params"], 4)) if i in regs] + \
                [f"stack {k}" for k in range(max(4, d["args"]), d["params"]) if mips_stack_arg_read(gw, k)]
    out["counts"]["dropped_pass_through"] = sum(1 for d in out["dropped"] if d["register"] == "pass-through")
    out["counts"]["dropped_callee_ignores"] = sum(1 for d in out["dropped"] if d.get("callee_reads") == [] and
                                                  d["register"] != "pass-through")
    for u in out["unmatched"]:
        u["sony"] = u["callee"] in sony
    out["counts"]["unmatched_sony"] = sum(1 for u in out["unmatched"] if u["sony"])
    return out


def runtime_prototypes() -> dict[str, CSig]:
    """The runtime's own prototypes (psx/sdk/*.h, psx/*.h): what a function it replaces takes."""
    out: dict[str, CSig] = {}
    for h in sorted((RUNTIME / "psx").rglob("*.h")):
        for k, v in c_toplevel(h.read_text(encoding="utf-8", errors="replace"))["prototypes"].items():
            out.setdefault(k, v)
    return out


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
                  launcher: list[str] = (), scratch: Path | None = None,
                  overrides: dict[str, Path] | None = None) -> dict[str, str]:
    """Compile each matched file for the host the way the build will; {path: error class} for the ones that fail.
    overrides: {path: file} compiled in place of src_root/path (the port's patches)."""
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
            r = subprocess.run(base + ["-o", str(obj), "-c", str((overrides or {}).get(p, src_root / p))],
                               capture_output=True, text=True,
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


# ---------------------------------------------------------------------------------------------- patches

def _patch_note(text: str) -> str:
    """The first line of the comment block a patch file starts with ('' when it doesn't start with one)."""
    t = text.lstrip()
    if t.startswith("//"):
        body = t.split("\n", 1)[0][2:]
    elif t.startswith("/*"):
        body = t[2:t.find("*/")] if "*/" in t else ""
    else:
        return ""
    for line in body.splitlines():
        line = line.strip().lstrip("*/").strip()
        if line:
            return line
    return ""


def find_patches(pdir: Path, functions: list[dict]) -> list[dict]:
    """The port's patches: port/patches/<function>.c (or <segment>/<function>.c, for an overlay function whose name
    another segment's function has too) replaces the export's file for that function in the port build only; the
    export and the database stay as they are. For the original game's own bugs that matched C has to keep (it reads
    a leftover register or stack slot: deterministic on the PS1, undefined on the host). Each starts with a comment
    block saying what the PS1 does and why the port differs. A patch for a function still in assembly is that
    function's C in the port only (the port's stub for it goes; the database still has it unmatched): the movie
    player's MDEC callback in SLUS-01252, say, written from its MIPS. A patch for a function that is unknown (renamed
    since?) or in no or several segments is an error: a stale patch must not be silently ignored.
    [{name, seg, file (relative to the port), path (the export's file it replaces, or src/<seg>/<name>.c for an
    unmatched function), note (the first comment line), text, unmatched}]"""
    if not pdir.is_dir():
        return []
    by_name: dict[str, list[dict]] = {}
    for f in functions:
        by_name.setdefault(f["name"], []).append(f)
    out, errors, seen = [], [], {}
    for pf in sorted(pdir.rglob("*.c")):
        rel = pf.relative_to(pdir)
        if len(rel.parts) > 2:
            errors.append(f"{rel}: a patch is patches/<function>.c or patches/<segment>/<function>.c")
            continue
        name, seg = pf.stem, (rel.parts[0] if len(rel.parts) == 2 else None)
        cands = [f for f in by_name.get(name, []) if seg is None or f.get("seg") == seg]
        if not cands:
            errors.append(f"{rel}: no function {name}" + (f" in segment {seg}" if seg else "") +
                          " in the export (renamed or removed? the patch is stale)")
            continue
        if len(cands) > 1:
            errors.append(f"{rel}: {name} is in segments " + ", ".join(sorted(str(f.get('seg')) for f in cands)) +
                          f": put it in patches/<segment>/{name}.c")
            continue
        f = cands[0]
        text = pf.read_text(encoding="utf-8", errors="replace")
        note = _patch_note(text)
        if not note:
            errors.append(f"{rel}: doesn't start with a comment block saying what the PS1 does and why the port "
                          "differs")
            continue
        if not re.search(r"\b" + re.escape(name) + r"\s*\(", strip_c(text)):
            errors.append(f"{rel}: doesn't define {name}")
            continue
        # an unmatched function's patch is its C in the port only, under a path of its own in the tree's layout
        path = f.get("c") or f"src/{f.get('seg') or 'main'}/{name}.c"
        if path in seen:
            errors.append(f"{rel}: {seen[path]} patches {name} already")
            continue
        seen[path] = str(rel)
        out.append({"name": name, "seg": f.get("seg"), "file": "patches/" + rel.as_posix(), "path": path,
                    "note": note, "text": text, "unmatched": not f.get("c")})
    if errors:
        raise PortError("port patches (" + str(pdir) + "):\n  " + "\n  ".join(errors))
    return out


def patches_txt(patches: list[dict]) -> str:
    """gen/game_patches.txt for tools/ptr32.py --patches: the export's path, a tab, the patch relative to gen/."""
    return "# the port's patches (generated): the export's file, and the patch compiled in its place\n" + \
        "".join(f"{x['path']}\t../{x['file']}\n" for x in patches)


def patches_md(patches: list[dict]) -> str:
    """PORT.md's section on the port's patches."""
    head = ("## Port patches\n\nFiles in `patches/` replace the export's copy of a function in this build only "
            "(`patches/<function>.c`, or `patches/<segment>/<function>.c`); the export and the database keep the "
            "matched C. They are for the original game's own bugs that the matching C has to keep (it reads a "
            "leftover register or stack slot: deterministic on the PS1, undefined on the host) and for calls the "
            "port links differently from the PS1. A patch for a function still in assembly is its C in this build "
            "only (written from its MIPS; the database still has it unmatched). A patch whose function is renamed "
            "or unknown stops the generator.\n\n")
    if not patches:
        return head + "None.\n\n"
    return head + "| Function | Patch | What it does |\n| :-- | :-- | :-- |\n" + "".join(
        f"| `{x['name']}` | `{x['file']}` " + ("(its C: still assembly in the export)" if x.get("unmatched") else
                                              f"(for `{x['path']}`)") + ("" if x.get("compiled", True) else
                                                                  " (not compiled: does nothing)")
        + f" | {x['note'].replace('|', '/')} |\n" for x in patches) + "\n"


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
                check: bool = True, run_seconds: int = 5, disc: str | None = None, commit: bool | None = None,
                sdk_range: list[str] | None = None, trace: bool = False) -> dict:
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
    sony = sony_from_rows(rows)
    # SDK ranges: given (--sdk-range FROM-TO; "none" for none), else the project's compiler regions that hold Sony
    # functions (SLUS-01252's libsnd)
    if sdk_range:
        ranges = [] if sdk_range == ["none"] else [parse_sdk_range(x) for x in sdk_range]
    else:
        ranges = sdk_ranges(cfg.get("regions") or [], sony)
    return generate(source, port, sony, title=proj["name"], slug=slug, log=log, build=build,
                    main=main, check=check, run_seconds=run_seconds, disc=disc, trace=trace,
                    commit=out is None if commit is None else commit, sdk_range=ranges)


def sdk_functions(build: dict, sony: dict[str, dict], ranges: list[tuple[int, int]],
                  identified: dict[str, tuple[str, str]]) -> dict[str, dict]:
    """The Sony set with the SDK ranges' functions in it (every function in a range, matched or not: flagged
    sdk_range, so its C isn't compiled) and the identified unnamed ones (labelled with their Sony name)."""
    out = dict(sony)
    for lo, hi in ranges:
        libs = [s["lib"] for s in sony.values() if s.get("vram") is not None and lo <= s["vram"] < hi]
        lib = max(set(libs), key=libs.count) if libs else "libapi"
        for f in build["functions"]:
            if not lo <= f["vram"] < hi or not _C_NAME.match(f["name"]):
                continue
            n = f["name"]
            if n in out:
                out[n] = {**out[n], "sdk_range": True}
                continue
            label, why = identified.get(n, (n, None))
            out[n] = {"lib": lib, "bios": None, "label": label, "objects": [], "vram": f["vram"], "sdk_range": True,
                      "why": why or f"in the SDK range {lo:08X}-{hi:08X}" + (" (matched C, replaced)" if f.get("c")
                                                                             else "")}
    for f in build["functions"]:  # matched or not: an alias replaces a matched function's C too
        n = f["name"]
        if n in identified and n not in out:
            label, why = identified[n]
            out[n] = {"lib": sony_library(label), "bios": None, "label": label, "objects": [], "vram": f["vram"],
                      "why": why}
    return out


def generate(source: Path, port: Path, sony: dict[str, dict], title: str, slug: str, log=print, build: bool = False,
             main: str | None = None, check: bool = True, run_seconds: int = 5, ptr32: bool = True, trace: bool = False,
             disc: str | None = None, commit: bool = False, sdk_range: list[tuple[int, int]] | None = None) -> dict:
    """disc: the game's disc image, read in place by the port's CD (kept in port.json: None keeps the one saved
    there). commit: commit the port when it sits in a git repository (export.commit_tree). sdk_range: address ranges
    whose every function is the SDK's (sdk_ranges)."""
    t0 = time.time()
    if disc is None and (port / "port.json").exists():
        try:
            disc = json.loads((port / "port.json").read_text(encoding="utf-8")).get("disc") or None
        except (OSError, ValueError):
            disc = None
    if disc:
        disc = str(Path(disc).expanduser().resolve())
        if not Path(disc).exists():
            log(f"the disc image {disc} isn't there (saved anyway: PSX_DISC overrides it at run time)")
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
    patches = find_patches(port / "patches", b["functions"])
    for x in patches:
        sources[x["path"]] = x["text"]
        if x["unmatched"]:  # compiled as if matched, from the patch (gen/game_patches.txt): no stub for it
            for f in b["functions"]:
                if f["name"] == x["name"] and f.get("seg") == x["seg"]:
                    f["c"] = x["path"]
            paths.append(x["path"])
            log(f"patch: {x['file']} is {x['name']}'s C in the port (still assembly in the export; {x['note']})")
        else:
            log(f"patch: {x['file']} replaces {x['path']} ({x['note']})")

    port.mkdir(parents=True, exist_ok=True)
    write_runtime(port)
    gen = port / "gen"
    gen.mkdir(exist_ok=True)
    excluded = {}
    check_list = port / "build" / "check_sources.txt"
    put(check_list, "".join(f"{x}\n" for x in paths))
    put(gen / "game_patches.txt", patches_txt(patches))
    # a file patched now and not before, or the other way round: its rewrite in build/ptr32 came from the other
    # copy and may be newer than this one, so it goes (ptr32.py redoes it)
    try:
        before = {x["path"] for x in json.loads((port / "port.json").read_text(encoding="utf-8")).get("patches", [])}
    except (OSError, ValueError, AttributeError, TypeError):
        before = None
    now = {x["path"] for x in patches}
    for x in (now if before is None else before ^ now):
        (port / "build" / "ptr32" / x).unlink(missing_ok=True)
    if check:
        log(f"compiling the {len(paths)} matched files for the host once, to leave out the ones that don't compile")
        if ptr32:  # the sources the build compiles: rewritten into build/ptr32, as its CMake rule does
            pdir = port / "build" / "ptr32"
            load_ptr32(port).main(["--src", str(source), "--out", str(pdir), "--list", str(check_list),
                                   "--headers", str(source / "include"), "--patches", str(gen / "game_patches.txt")])
            import sys
            excluded = check_sources(pdir, paths, [port / "include", pdir / "include", source / "include"],
                                     port / "include" / "psx_port_game.h", host_arch_flags(), extra=PTR32_FLAGS,
                                     launcher=[sys.executable, str(port / "tools" / "cc_ptr32.py")],
                                     scratch=port / "build")
        else:
            excluded = check_sources(source, paths, [port / "include", source / "include"],
                                     port / "include" / "psx_port_game.h", host_arch_flags(),
                                     overrides={x["path"]: port / x["file"] for x in patches})
        log(f"  {len(paths) - len(excluded)} compile, {len(excluded)} don't")
    sym_text = (source / "symbol_addrs.txt").read_text(encoding="utf-8") if (source / "symbol_addrs.txt").exists() else ""
    auto_text = "".join((source / n).read_text(encoding="utf-8") for n in ("undefined_syms_auto.txt",)
                        if (source / n).exists())
    sony = {**sony, **syscall_stubs(b, image, sony)}
    ranges = list(sdk_range or [])
    sony = sdk_functions(b, sony, ranges, SDK_IDENTIFIED.get(slug, {}))
    if ranges:
        log("SDK ranges (every function in them is the SDK's): " + ", ".join(f"{lo:08X}-{hi:08X}" for lo, hi in ranges))
    aliases = libcd_aliases(b, image, sony)
    # more of the same map: an assembly function named like one the runtime implements, and Sony functions known by
    # their code (SONY_SHAPES), labelled with the runtime's name: plan() turns each label into an alias
    sony = {**sony, **sony_by_name(b, sony)}
    shaped = sony_by_shape(b, image, sony, aliases)
    sony = {**sony, **shaped}
    if shaped:
        log(f"  {len(shaped)} Sony functions known by their code: "
            + ", ".join(f"{n} = {s['label']}" for n, s in sorted(shaped.items())))
    p = plan(b, sources, headers, sony, sym_text, auto_text, excluded, handwritten_gte(b, image, set(sony)),
             aliases)
    if main_name not in {d["name"] for d in p["dispatch"]}:
        raise PortError(f"{main_name} isn't a function of this game")
    rn = p["renames"]
    files = {
        "psx_symbols.S": symbols_asm(p["symbols"], rn),
        "psx_unmatched.c": unmatched_c(p["unmatched"], rn, p["replayed"]),
        "psx_sdk_stubs.c": sdk_stubs_c(p["sdk"], rn),
        "psx_functions.c": functions_c(p["dispatch"], image, rn),
        "psx_function_addrs.txt": function_addrs(p["dispatch"], rn),
        "psx_config.c": config_c(title, b["image"], entry, rn.get(main_name, main_name), disc or ""),
        "psx_sdk_config.h": sdk_config_h(headers, p["game_implements"]),
        "game_sources.cmake": game_sources_cmake(p["compiled"], rn, patches),
        "game_patches.txt": patches_txt(patches),
        "game_sources.txt": "".join(f"{x}\n" for x in p["compiled"]),
    }
    for old in gen.iterdir():
        if old.name not in files:
            old.unlink()
    for name, text in files.items():
        put(gen / name, text)
    put(port / "CMakeLists.txt", cmakelists(slug))
    put(port / ".gitignore", "build/\n" + b["image"] + "\n")
    if put(port / "run.sh", run_sh(slug, b["image"])):
        os.chmod(port / "run.sh", 0o755)
    compiled_set = set(p["compiled"])
    for x in patches:
        if x["path"] not in compiled_set:
            log(f"patch {x['file']}: {x['name']} isn't compiled in the port ("
                + (f"it doesn't compile for the host: {excluded[x['path']]}" if x["path"] in excluded else
                   "the runtime replaces it") + "), so the patch does nothing")
    hz = hazards(headers, {k: v for k, v in sources.items() if k not in excluded})
    xo = cross_overlay_refs(b, sources)
    if (bad := [r for r in xo if r["error"]]):
        log(f"error: {len(bad)} references in overlays' matched C name another overlay's function at a shared address "
            "(PORT.md's Cross-overlay names; port.json's cross_overlay):")
    for r in bad[:20]:
        log(f"  {r['seg']}/{r['caller']} names {r['name']} ({r['name_seg']}'s function at 0x{r['vram']:08X}, "
            f"{r['use']}): the port reaches that overlay's {r['links']}, not "
            + (f"{r['seg']}'s own {r['own']}" if r["own"] else f"what {r['seg']} has there ({r['here']})"))
    ch = port_call_hazards(b, image, sources, headers, p, sony, aliases)
    put(port / "hazards.json", json.dumps(ch, indent=1) + "\n")
    log(f"dropped arguments: {ch['counts']['dropped']} calls pass fewer than the callee takes, "
        f"{ch['counts']['table_calls']} table calls with (), {ch['counts']['unmatched']} ()-calls to assembly "
        f"({ch['counts']['unmatched_takes_args']} of whose callees take arguments): port/hazards.json")
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
        "sdk_ranges": [f"{lo:08X}-{hi:08X}" for lo, hi in ranges],
        "sdk_range_functions": sum(1 for s in sony.values() if s.get("sdk_range")),
        "sdk_range_replaced": sorted(f["name"] for f in b["functions"] if f.get("c") and
                                     sony.get(f["name"], {}).get("sdk_range")),
        "sdk_aliases": {s["name"]: s["target"] for s in p["sdk"] if s["kind"] == "alias" and s["lib"] != "libcd"},
        "symbols": len(p["symbols"]), "renamed": rn, "game_implements": p["game_implements"], "as_data_matched": p["as_data_matched"],
        "as_data_unmatched": p["as_data_unmatched"], "unresolved": p["unresolved"], "hazards": hz,
        "call_hazards": ch,
        "cross_overlay": xo,
        "patches": [{"name": x["name"], "seg": x["seg"], "file": x["file"], "path": x["path"], "note": x["note"],
                     "compiled": x["path"] in compiled_set, "unmatched": x.get("unmatched", False)}
                    for x in patches],
        "generated_in": round(time.time() - t0, 1),
        "disc": disc or "",
        "libcd": {n: a for n, a in sorted(aliases.items())},
        "libcd_replaced": sorted(c for c, n in p["replaced"].items() if n in aliases),
    }
    log(f"wrote {port}: {report['compiled']} matched files, {report['unmatched']} unmatched stubs, "
        f"{report['sdk_stubs']} Sony stubs, {len(report['sdk_implemented'])} Sony functions implemented, "
        f"{report['symbols']} data symbols")
    if build:
        report["build"] = build_and_run(port, log=log, seconds=run_seconds, trace=trace)
    (port / "port.json").write_text(json.dumps(report, indent=1, default=list), encoding="utf-8")
    (port / "PORT.md").write_text(port_md(title, report), encoding="utf-8")
    if commit:
        from .export import commit_tree
        commit_tree(port, f"Port: {report['compiled']} matched files, {report['unmatched']} unmatched stubs, "
                          f"{len(report['sdk_implemented'])} Sony functions implemented", log)
    return report


def build_and_run(port: Path, log=print, seconds: int = 5, trace: bool = False) -> dict:
    """cmake configure + build, then the game with PSX_PORT_SOFT=1 for `seconds`, its log in build/run.log. trace (or
    PSX_PORT_TRACE=1 in the environment): the entry trace compiled in (-DPSX_TRACE=ON)."""
    trace = trace or os.environ.get("PSX_PORT_TRACE", "0") not in ("", "0")
    cmake = shutil.which("cmake") or "/opt/homebrew/bin/cmake"
    bdir = port / "build"
    res = {"configured": False, "built": False, "ran": False}
    cache = bdir / "CMakeCache.txt"
    if cache.exists() and "arm64 macOS can't map memory below 4 GB" in cache.read_text(encoding="utf-8", errors="replace"):
        log("cmake: the build directory was configured for x86_64 by an older generator: configuring it afresh")
        cache.unlink()
        shutil.rmtree(bdir / "CMakeFiles", ignore_errors=True)
    log("cmake: configuring")
    res["trace"] = trace
    r = subprocess.run([cmake, "-S", str(port), "-B", str(bdir), "-DCMAKE_BUILD_TYPE=RelWithDebInfo",
                        f"-DPSX_TRACE={'ON' if trace else 'OFF'}"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    res["configure_log"] = (r.stdout + r.stderr)[-3000:]
    if r.returncode != 0:
        log(res["configure_log"])
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
    first Sony stub it reached, the files it read and streamed, the movie frames the MDEC decoded, how it ended."""
    entered = re.findall(r"^trace: enters (\S+)", text, re.M)
    unmatched = re.findall(r"^unmatched: (\S+)", text, re.M)
    sdk = re.findall(r"^sdk: (.+?) \(call 1\)", text, re.M)
    fault = re.search(r"^psx: ((?:segmentation fault|bus error|call into PS1 code)[^\n]*)", text, re.M)
    stopped = re.search(r"^psx: (\S+) is not decompiled yet", text, re.M)
    timed = re.search(r"^psx: (stopped|terminated) after the time", text, re.M)
    reads = re.findall(r"^cd: CdRead \d+ sectors .*? from LBA \d+ \(([^)]*?)(?: \+0x[0-9A-F]+)?\)", text, re.M)
    story = ("calls " + ", ".join(entered[:12]) + (f" (+{len(entered) - 12} more)" if len(entered) > 12 else "")
             if entered else "no trace (built without it: decomp port --trace)")
    frames = re.search(r"^psx: (\d+) frames \(vertical blanks\)", text, re.M)
    if sdk:
        story += f"; first Sony stub reached: {sdk[0]}"
    if unmatched:
        story += f"; first function still in assembly: {unmatched[0]}"
    if reads:
        story += f"; reads {', '.join(dict.fromkeys(reads))} from the disc"
    streams = re.findall(r"^cd: CdRead2\(mode 0x[0-9A-F]+\) from LBA \d+ \(([^)]*?)(?: \+0x[0-9A-F]+)?\): streaming",
                         text, re.M)
    if streams:
        story += f"; streams {', '.join(dict.fromkeys(streams))} (St ring)"
    mdec = re.search(r"^press: (\d+) frames decoded \(DecDCTvlc\), (\d+) DecDCTin, (\d+) DecDCTout completions "
                     r"\((\d+) macroblocks\)(?:; the first at vblank (\d+))?", text, re.M)
    if mdec:
        story += (f"; the MDEC decoded {mdec.group(1)} movie frames ({mdec.group(3)} DecDCTout completions, "
                  f"{mdec.group(4)} macroblocks" + (f", the first at vblank {mdec.group(5)}" if mdec.group(5) else "")
                  + ")")
    if stopped:
        story += f"; stops at {stopped.group(1)} (not decompiled)"
    elif fault:
        story += f"; ends with a {fault.group(1)}"
    elif timed:
        story += "; still running when the time was up"
    if frames:
        story += f" ({frames.group(1)} frames)"
    gpu = re.search(r"^gpu: (\d+) packets, (\d+) primitives, (\d+) pixels, (\d+) fills, (\d+) loads; display (on|off)",
                    text, re.M)
    if gpu:
        story += (f"; the GPU drew {gpu.group(2)} primitives ({gpu.group(3)} pixels) in {gpu.group(1)} packets, "
                  f"{gpu.group(4)} fills and {gpu.group(5)} VRAM loads, display {gpu.group(6)}"
                  + ("" if re.search(r"^etc: first VSync", text, re.M) else ", no VSync reached"))
    keep = [ln for ln in text.splitlines() if not re.match(r"sdk: .* \(call ([2-9]|1\d|20)\)", ln)]
    return {"entered": entered, "unmatched_hit": unmatched, "sdk_hit": sdk, "fault": fault.group(1) if fault else None,
            "cd_reads": reads, "cd_streams": streams,
            "mdec": {"frames": int(mdec.group(1)), "ins": int(mdec.group(2)), "outs": int(mdec.group(3)),
                     "macroblocks": int(mdec.group(4)), "first_vblank": int(mdec.group(5)) if mdec.group(5) else None}
            if mdec else None,
            "stopped_at": stopped.group(1) if stopped else None, "story": story, "log_head": "\n".join(keep[:70])}


def shared_segments(build: dict) -> dict[str, tuple[int, int]]:
    """{segment: (start, end)} of the code segments whose addresses another segment shares (overlays that load at
    the same address and so are never in RAM together), from build.json's layout, else from where each segment's
    functions are."""
    span = {}
    for s in build.get("segments") or []:
        if s.get("kind") == "code" and s.get("vram") is not None and s.get("end") is not None:
            span[s["name"]] = (int(s["vram"]), int(s["vram"]) + int(s["end"]) - int(s["start"]))
    if not span:
        for f in build["functions"]:
            lo, hi = span.get(f["seg"], (f["vram"], f["vram"] + 4))
            span[f["seg"]] = (min(lo, f["vram"]), max(hi, f["vram"] + 4))
    return {n: (a, b) for n, (a, b) in span.items() if any(m != n and a < d and c < b for m, (c, d) in span.items())}


def cross_overlay_refs(build: dict, sources: dict[str, str]) -> list[dict]:
    """References in an overlay's matched C to a function of another overlay that shares its addresses: on the PS1
    a relocation to the address, so the bytes match and the code runs whatever overlay is loaded; linked by name
    (the native port, any build that links the overlays' C together), it reaches the other overlay's function.

    Each: {"caller", "seg", "file", "name" (the name the C uses), "name_seg" (whose function that is), "vram",
    "use" ("call", "address": taken or used as data, "declared": only a prototype, nothing uses it), "own" (the
    caller's overlay's function at that address, the one the C means), "here" (what the caller's overlay has at the
    address when it has no function there: "between f and g", "after f, its last function (data)"), "links" (what
    the name links to in the port: "matched C", "stub", "its address"), "error" (a call, or an address taken that
    reaches a host function: the other overlay's code, not this overlay's address)}."""
    shared = shared_segments(build)
    if not shared:
        return []
    fns = build["functions"]
    segs_of: dict[str, set[str]] = {}
    vram_of: dict[str, int] = {}
    for f in fns:
        segs_of.setdefault(f["name"], set()).add(f["seg"])
        vram_of[f["name"]] = f["vram"]
    at = {(f["seg"], f["vram"]): f["name"] for f in fns}
    starts: dict[str, list[int]] = {}
    for f in fns:
        starts.setdefault(f["seg"], []).append(f["vram"])
    for v in starts.values():
        v.sort()
    names = set(segs_of)
    has_c = {f["name"] for f in fns if f.get("c")}
    called_anywhere: set[str] = set()
    for t in sources.values():
        called_anywhere.update(re.findall(r"\b([A-Za-z_]\w*)\s*\(", _code(t)))
    out = []
    for f in fns:
        path = f.get("c")
        if not path or f["seg"] not in shared or path not in sources:
            continue
        mine = f["seg"]
        lo, hi = shared[mine]
        text = _code(sources[path])
        tl = c_toplevel(sources[path])
        bodies = " ".join(b for _, b in tl["definitions"].values())
        called = {n for n, _ in body_calls(bodies, names)}
        for name in sorted(set(_IDENT.findall(text)) & names):
            theirs = segs_of[name]
            if mine in theirs or not theirs & set(shared):
                continue
            v = vram_of[name]
            if not lo <= v < hi or not any(shared[s][0] < hi and lo < shared[s][1] for s in theirs if s in shared):
                continue
            if name in called:
                use = "call"
            elif re.search(rf"\b{re.escape(name)}\b", bodies):
                use = "address"
            else:  # outside the bodies: a prototype or an extern, or an initializer (a table of functions)
                stmts = [st for st in re.split(r"[;{}]", re.sub(r"\{[^{}]*\}", ";", text))
                         if re.search(rf"\b{re.escape(name)}\b", st)]
                use = "address" if any("=" in st for st in stmts) else "declared"
            own = at.get((mine, v))
            here = None
            if own is None:
                ss = starts.get(mine, [])
                k = bisect.bisect_right(ss, v) - 1
                prev = at[(mine, ss[k])] if k >= 0 else None
                nxt = at[(mine, ss[k + 1])] if k + 1 < len(ss) else None
                here = (f"between {prev} and {nxt} (inside {prev}, or data)" if prev and nxt else
                        f"after {prev}, its last function (data)" if prev else "before its first function")
            # what the name links to in the port (plan()): the other overlay's host function (its matched C, or its
            # stub), or, for one still in assembly that nothing calls, an absolute symbol at its PS1 address, which
            # is the right value for an address taken
            links = "matched C" if name in has_c else "stub" if name in called_anywhere else "its address"
            out.append({"caller": f["name"], "seg": mine, "file": path, "name": name, "name_seg": sorted(theirs)[0],
                        "vram": v, "use": use, "own": own, "here": here, "links": links,
                        "error": use == "call" or (use == "address" and links != "its address")})
    return out


def cross_overlay_md(refs: list[dict]) -> str:
    """PORT.md's section on cross_overlay_refs."""
    if not refs:
        return ("## Cross-overlay names\n\nNo matched C of an overlay names another overlay's function at a shared "
                "address (checked: cross_overlay_refs).\n\n")
    errors = [r for r in refs if r["error"]]
    out = [f"""## Cross-overlay names

Overlays that load at the same address share their addresses, and names are per project: where two overlays have a
function at one address, one of them carries the overlay's name in front (`ovl_yprop_func_80028724`). Matched C of
one overlay that names another's function there is byte-exact on the PS1 (a relocation to the address: whatever
overlay is loaded runs), but here it links to the other overlay's function. **{len(errors)} errors**: a call, or an
address taken (data the caller's overlay keeps at that address, named by the other overlay's function) that links
to a host function (`links`: its matched C or its stub) rather than to the PS1 address; the other
{len(refs) - len(errors)} are prototypes nothing uses, or an address taken of a function still in assembly that
nothing calls (an absolute symbol at its PS1 address: the right value). The fix is a name, through POST
/api/replace (byte-identical, so the match stays) or a port patch: the caller's overlay's own function at that
address (`own`), else a `D_` name for its data there (`here`: where that address is among its functions).

| caller | segment | names | whose | use | links | own function there | else, in the caller's overlay |
| :-- | :-- | :-- | :-- | :-- | :-- | :-- | :-- |
"""]
    for r in sorted(refs, key=lambda r: (not r["error"], r["seg"], r["caller"], r["name"])):
        out.append(f"| `{r['caller']}` | {r['seg']} | `{r['name']}` | {r['name_seg']} | {r['use']}"
                   f"{'' if r['error'] else ' (ok)'} | {r.get('links', '?')} | "
                   f"{('`' + r['own'] + '`') if r['own'] else '-'} | {r['here'] or '-'} |\n")
    return "".join(out) + "\n"


def call_hazards_md(ch: dict | None) -> str:
    """PORT.md's section on the arguments the matched C drops (call_hazards), every entry listed."""
    if not ch:
        return ""
    c = ch["counts"]
    by = ", ".join(f"{n} declared `{k}`" if k.startswith("(") else f"{n} {k}" for k, n in c["dropped_by_declaration"].items())
    out = [f"""## Arguments the matched C drops

Matched C that calls a function with fewer arguments than it takes is byte-exact on the PS1, where $a0-$a3 pass
through untouched, and works on the host only while nothing touches x0-x7 before the call: clang owes nothing to a
value it doesn't know about, so any change to the compiler, the flags or the code around the call can break it.
Nothing at run time can repair it. The fix is in the C: the caller takes the argument as a parameter and passes it
on (`void func_80104228(void *arg0) {{ func_800F5120(arg0); }}`), which still matches on MIPS because the register
is the same. Found by {ch['method']}; the lists are in `hazards.json` too.

- **a. Fewer arguments than the definition takes: {c['dropped']} calls in {c['dropped_callers']} functions** ({by};
  `none`: no prototype of the caller's own, only common.h's or an implicit declaration). In
  {c.get('dropped_pass_through', 0)} of them the registers are the caller's own arguments, untouched (pass-through:
  the fix is mechanical); in {c.get('dropped_callee_ignores', 0)} of the others the callee's code never reads what
  is dropped (harmless). Calls through a cast of a game function (`((T (*)(...)) func_X)(...)`, `cast`) are
  checked the same way: {c.get('cast_calls', 0)} of them, {c.get('dropped_through_cast', 0)} dropping arguments.
- **b. Calls with no arguments through an unprototyped function pointer (`M2C_UNK (**)()`), in C the port compiles:
  {c['table_calls']}** (the libraries' ones the runtime replaces by their code, SONY_SHAPES, aren't counted).
- **c. Calls with no arguments to a function still in assembly: {c['unmatched']}**, which no definition checks (the
  database records no signatures): {c['unmatched_takes_args']} callees take arguments by common.h's prototype or
  their code (they read an argument register before writing it), {c['unmatched_no_args']} read none,
  {c['unmatched_unknown']} unknown. Harmless while the callee is a stub; each becomes case a when it's matched.
"""]
    if ch["dropped"]:
        out.append("\n### a. Fewer arguments than the definition takes\n\n"
                   "`register`: what the dropped argument registers hold at the call on the PS1, by the caller's code "
                   "along its control flow: `pass-through` (the caller's own arguments, untouched: take them as "
                   "parameters and pass them on), `leftover` (an earlier call or instruction wrote them: the callee "
                   "gets that; read the assembly to see what it is), `stack argument` (a fifth parameter or later). "
                   "`callee reads`: which of the dropped arguments the callee's code can read (argument registers "
                   "live at its entry, stack arguments it loads); `none` means the callee ignores them, so what the "
                   "caller leaves there doesn't matter (the call is harmless as long as the callee's C doesn't read "
                   "them either); anything else under `leftover` or `stack argument` is a real bug for a person.\n\n"
                   "| caller | callee | declared | defined | args | register | callee reads |\n"
                   "| :-- | :-- | :-- | :-- | --: | :-- | :-- |\n")
        for d in sorted(ch["dropped"], key=lambda d: (d["file"], d["callee"])):
            decl = d["declared"] if d["declared"] != "none" else ("none" + (f" (common.h `{d['declaration']}`)"
                                                                            if d.get("declaration") else ""))
            decl = f"`{decl}`" if decl.startswith("(") else decl
            where = "" if d["definition"] == "matched C" else f" ({d['definition']})"
            reads = d.get("callee_reads")
            reads = "?" if reads is None else ", ".join(reads) or "none"
            out.append(f"| `{d['caller']}` | `{d['callee']}` | {decl} | `{d['defined']}`{where} | {d['args']} | "
                       f"{d.get('register', '?')} | {reads} |\n")
    if ch["table_calls"]:
        out.append("\n### b. No-argument calls through `(**)()`\n\n")
        out += [f"- `{t['caller']}`: `{t['call']}()`\n" for t in ch["table_calls"]]
    if ch["unmatched"]:
        takes = [u for u in ch["unmatched"] if u["params"]]
        rest = [u for u in ch["unmatched"] if not u["params"]]
        out.append("\n### c. No-argument calls to assembly\n\nCallees that take arguments:\n\n")
        out += [f"- `{u['caller']}` -> `{u['callee']}` (declared `{u['declared']}`): {u['evidence']}\n"
                for u in sorted(takes, key=lambda u: (u["callee"], u["caller"]))]
        if rest:
            out.append("\nCallees that read no argument register (or unknown): "
                       + ", ".join(f"`{u['caller']}` -> `{u['callee']}`" + (" (unknown)" if u["params"] is None else "")
                                   for u in sorted(rest, key=lambda u: (u["callee"], u["caller"]))) + ".\n")
    return "".join(out) + "\n"


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
first thing the runtime doesn't do yet. It draws through a software GPU (`psx/gpu.c`: 1024x512 VRAM, the GP0
primitives, libgpu's ordering tables walked in PS1 RAM) and shows the display area each VSync. Sound is a
software SPU (`psx/spu.c`) under libspu and a host libsnd (`psx/sdk`), mixed to SDL2's audio device.

## Build and run

```
cmake -S port -B port/build && cmake --build port/build && port/build/game
```

Needs cmake, clang and python3 (the build rewrites the game's C, below). Environment: `PSX_PORT_SOFT=1` goes on past
functions that are still assembly (they return 0) instead of stopping at the first; `PSX_TRACE=N` logs the first N
distinct game functions entered (default 200) in a build with the trace compiled in (`-DPSX_TRACE=ON`, `decomp port
--trace` or `PSX_PORT_TRACE=1` at configure time; off by default: a call to `psx/trace.S` first in every game
function, which keeps the argument registers); `PSX_HEADLESS=1` no window; `PSX_TIMEOUT=S` stops after S seconds;
`PSX_SOURCE_DIR` the exported tree if it moved; `PSX_DISC` the disc image (default: the one `decomp port --disc`
saved, {('`' + r['disc'] + '`') if r.get('disc') else 'none saved'}), `PSX_CD_FAST=1` a CdRead's sectors all at once,
`PSX_CD_LOG=1` every CD command, `PSX_CD_LOAD=FILE@ADDR` a disc file into RAM at CdInit (an overlay, to check it
resolves), `PSX_NULL_FAULT=1` RAM's KUSEG mirror unmapped (a null PS1 pointer faults); `PSX_PORT_DUMP_VRAM=file.png` the VRAM
at exit (also after a crash); `PSX_PORT_DUMP_SCREEN=file.png` what the SDL window shows, every 30 frames
(with `PSX_PORT_DUMP_EVERY=N` the display area every N vblanks as file-NNNNNN.png instead, headless too);
`PSX_GPU_SELFTEST=N` N frames drawn through libgpu (environments, an ordering table, polygons, a CLUT sprite, a
polyline) instead of the game; `PSX_MEMCARD=card.mcr` a memory card in slot 1, a 128 KB card image (raw frames, as
emulators keep them; created formatted when missing, written back after every write; without it there is no card,
and the card calls answer TIMEOUT a vblank later, as a console with an empty slot does). Keys: arrows (d-pad), Z/X/A/S
(cross/circle/square/triangle), Return (start), right shift (select), Q/W (L1/R1), 1/2 (L2/R2); `PSX_PAD_SCRIPT=
"start@300,cross@600-640"` presses buttons at those vblanks, alongside the keyboard (names: up down left right cross
circle square triangle l1 l2 r1 r2 start select; a press lasts 10 vblanks, `PSX_PAD_SCRIPT_HOLD=N`, unless given as
FROM-TO), for a run that reproduces. CMake options: `-DPSX_PTR32=OFF` (the C as it is,
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
| BIOS calls implemented behind the game's stub names (the C library, the heap, events, the memory card and its files) | {r['sdk_bios']} |
| Sony functions implemented (`psx/sdk/*.c`) | {len(r['sdk_implemented'])}: {', '.join(r['sdk_implemented'])} |
| SDK ranges: every function in them is the SDK's (the project's compiler regions holding Sony functions, or `--sdk-range`) | {', '.join(r.get('sdk_ranges') or []) or 'none'}: {r.get('sdk_range_functions', 0)} functions, {len(r.get('sdk_range_replaced') or [])} of them matched C the runtime replaces |
| Unnamed SDK functions forwarded to the runtime's by their Sony name or their code (`SONY_SHAPES`; `gen/psx_sdk_stubs.c`) | {len(r.get('sdk_aliases') or {})}{': ' + ', '.join(f"`{a}` = {b}" for a, b in (r.get('sdk_aliases') or {}).items()) if r.get('sdk_aliases') else ''} |
| The game's libcd functions the runtime's libcd replaces (`psx/sdk/libcd.c` over `psx/cd.c`; matched copies left out of the build) | {len(r.get('libcd') or {})}{': ' + ', '.join(f'`{n}`' + (f' ({a})' if a != n else '') for n, a in sorted((r.get('libcd') or {}).items())) if r.get('libcd') else ''}; {len(r.get('libcd_replaced') or [])} of them matched C |
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
  pointer goes through the runtime's trampoline (`psx/call32.S`, the PS1 address in x15/r10 as LLVM's `nest`
  argument; the argument registers the C didn't set keep what the caller had, as `jalr` does on the PS1) to the
  function table's (`psx_host_fn`: overlays told apart by their code in RAM; any other address ends the run with it). 32-bit
  address arithmetic stays as it is. All 4000 files' IR goes through it; the same 3 fail as before (their C).
- **The runtime's side**: a pointer the game passes is a PS1 address (`PSX_PTR`/`psx_host`), and a pointer the
  runtime hands the game must be in the window: RAM, the game's stack, or the PS1 heap (`InitHeap`/`malloc`, in RAM).
- **Calls through the game's function pointers** go through `psx_call32` and `psx_host_fn`; the SIGSEGV handler that sent a jump
  into RAM to the host function stays as a safety net for jumps from anywhere else.
- **Sound**: `psx/spu.c` is a software SPU written from psx-spx: 512 KB of SPU RAM, 24 ADPCM voices with the loop
  flags, ADSR, the 4.12 pitch counter (linear interpolation), fixed volumes, the CD input; its register file is the
  window's 0x1F801C00, so the game's own stores land in it (KON/KOFF consumed as triggers, ADSRVOL/ENDX written
  back). `psx/sdk/libspu.c` drives it as Sony's libspu does; `psx/sdk/libsnd.c` is libsnd on the host (VABs, the
  SEQ sequencer, the voice manager), ticked from the audio thread; the game's libsnd range is an SDK range, so its
  C (matched or not) is replaced wholesale. `psx/xa.c` decodes XA-ADPCM sectors the CD layer hands it
  (`psx_cd_xa_sector`). Output: SDL2's audio device, else a real-time thread (`PSX_AUDIO_WAV=file.wav` records it;
  `PSX_AUDIO=0` none). Not yet: reverb, noise, pitch modulation, vibrato/portamento, SEP files.
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
- **Mirrors**: KUSEG (0x00000000) and KSEG1 (0xA0000000) are RAM again (mach_vm_remap of the same pages), as on
  the PS1: the game reads through a null pointer (SLUS-01252's func_800FD5BC reads a camera pointer + 6 before the
  camera exists) and libcd patches the BIOS's tables at 0xA00003E0. `PSX_NULL_FAULT=1` leaves KUSEG unmapped.
- **Functions used as data** that are matched (their name is a host function, so `func_X + N` is host code, not the
  game's): {', '.join(f'`{n}`' for n in r['as_data_matched']) or 'none'}. Unmatched ones are absolute symbols at
  their PS1 address: {', '.join(f'`{n}`' for n in r['as_data_unmatched']) or 'none'}.
- **Raw addresses**: {hz['raw_address_casts']} casts of a constant address to a pointer in {hz['raw_address_files']} files; translated like
  any other 32-bit pointer.
- **Overlays** are compiled into the one executable and share addresses; the game loads their images from the disc
  with CdRead, which writes them into RAM, and calls through a pointer find the overlay in RAM by its first two
  instructions (each finished CdRead logs the overlay it brought in and how many of its functions resolve).
  **Hardware registers** (0x1F801000 on) are plain memory, the SPU's (0x1F801C00) read and written by the
  software SPU.
- **The CD** is the disc image read in place (`psx/cd.c`: 2352-byte Mode 1/Mode 2 or 2048-byte sectors, a .cue,
  ISO 9660) under libcd (`psx/sdk/libcd.c`, its model in `libcd.h`): commands complete at the next VSync or poll, a
  read moves 2.5 sectors a frame at double speed (one per poll), callbacks fire there. XA ADPCM goes to
  `psx_cd_xa_sector()`, decoded onto the SPU's CD input (`psx/xa.c`); CD-DA isn't played. A movie streams with
  CdRead2 into the St ring: STR video sectors (32-byte header: frame, chunk of n) put back together frame by frame,
  StGetNext handing out the oldest complete one and waiting a vblank while the drive streams and none is ready.
- **Movies**: libpress (`psx/sdk/libpress.c`) over a software MDEC (`psx/mdec.c`, from psx-spx): DecDCTvlc decodes
  a BS v1/v2/v3 frame's Huffman codes into run-length codes, DecDCTin hands them over, DecDCTout writes decoded
  16x16 macroblocks (15- or 24-bit) into the game's buffer; the out-callback runs at the end of DecDCTout on the
  main thread, chained transfers queued rather than nested.
- **Calls nothing answers** (named in the C, but neither matched, a stub nor a symbol):
  {', '.join(f'`{n}`' for n in r['unresolved'][:40]) or 'none'}.
- **Matched files that don't compile for the host**:{ex_lines or ' none.'}
- **Arguments the matched C drops**: {(r.get('call_hazards') or {}).get('counts', {}).get('dropped', 0)} calls pass
  fewer arguments than the callee takes (below).

{call_hazards_md(r.get('call_hazards'))}{cross_overlay_md(r.get('cross_overlay') or [])}{patches_md(r.get('patches') or [])}## PsyCross

[PsyCross](https://github.com/OpenDriver2/PsyCross) (OpenDriver2) reimplements PSY-Q's libraries on SDL2 (2.0.16+),
OpenAL-soft and OpenGL: a renderer that emulates VRAM, an SPU, a software GTE with PGXP. REDRIVER2 and the Silent Hill
PC port use it. Its repository says MIT, so linking it is fine. Its README names no operating system (macOS isn't
mentioned; Emscripten is) and lists the CMake build scripts as a TODO. With the port native on arm64, Homebrew's
SDL2 links; its PSY-Q headers declare `long`-based structs, which need the 32-bit layout this port's headers keep,
and its pointers would have to follow the window rule. A later `-DPSX_SDK=psycross` replacing
`psx/sdk` is worth it for the renderer and the SPU, which are most of the work left, but not before the CPU side
(this port's RAM, pointers and calls) runs a frame.
"""
