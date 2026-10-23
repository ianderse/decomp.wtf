# PS1 export for port: the game's matched C on the host

Status (2026-10-23): first pass in. `decomp port <slug> [--build] [--main NAME]` (src/decompnet/rom/export_port.py,
the runtime in src/decompnet/rom/port_runtime/) writes `<project>/port/`, a CMake project over the exported tree.
On SLUS-01252 it compiles 3,997 of the 4,000 matched files, links, opens a window and runs the game's main
(func_800C27B8) through 10 game functions and 10 Sony stubs to func_80178F28, the first function still in assembly
(libgte's InitGeom: handwritten, it enables COP2 and sets ZSF3/ZSF4 with ctc2). With `PSX_PORT_SOFT=1` it goes on
through 70 distinct game functions, 6 functions still in assembly and 19 Sony stubs before a null pointer from a
stub ends it (in func_800FD5BC). No rendering or
audio.

## The design

- **PS1 RAM at its own address.** 2 MB at 0x80000000 (and the scratchpad at 0x1F800000), MAP_FIXED; the EXE's image
  copied to its load address and bss cleared with the range read from crt0 (capstone at generation time, constant
  tracking up to the first backward loop). Raw addresses, pointers in the game's data and overlay images work as
  they are.
- **Data symbols as absolute symbols** (gen/psx_symbols.S): every identifier the C and headers use that has an
  address (symbol_addrs.txt, build.json's symbols, undefined_syms_auto.txt, or the hex in an auto name such as
  D_801A18E0), so each file's own `extern` lands in that RAM. common.h's tentative definitions (`s32 D_801A1A40;`)
  are common symbols with `-fcommon`; the absolute one replaces them.
- **4-byte pointers.** tools/ptr32.py rewrites the game's files and headers into build/ptr32 at build time (the
  export stays the source): every pointer declarator gets `__ptr32 __uptr` (clang, `-fms-extensions`), `long` becomes
  `int`. tools/cc_ptr32.py is the compiler launcher for those files: C to LLVM IR, an `addrspacecast` to an ordinary
  pointer before every call through a 32-bit pointer and every memcpy/memmove/memset on one, then IR to the object.
  Host code (small __PAGEZERO), the game's stack (8 MB near 0x60000000) and RAM are all below 4 GB.
- **Calls through PS1 addresses.** A function pointer from the game's data is a MIPS address; the jump faults (RAM
  isn't executable) and the SIGSEGV handler moves the pc to the host function at that address (gen/psx_functions.c,
  every function with its PS1 address and first two instructions, which tell overlays at one address apart).
- **Stubs.** Each function still in assembly: logs and stops (or returns 0 with PSX_PORT_SOFT=1). Each Sony
  function (the database's library and BIOS-stub rows, plus syscall wrappers found by their code): logs its first 20
  calls and returns 0. Implemented: VSync (window, pad, 60 Hz), the environment calls, PadInitDirect/PadStartCom,
  libgte's matrix arithmetic, the BIOS's memcpy/memset/strlen/rand family. psx/gte.c is a software GTE from psx-spx.
- **Trace.** The game's files build with `-finstrument-functions`; the run logs the first 200 distinct functions it
  enters, and on a crash the last 16.

## What was measured (Apple clang 21, Homebrew LLVM 22, ld64, macOS 26, M-series)

- **arm64 can't do it.** An arm64 executable linked with `-Wl,-pagezero_size` under 4 GB (0, 0x4000, 0x80000000) is
  killed at exec (SIGKILL, exit 137); without that, mmap at 0x80000000 fails (ENOMEM). The same 10-line test built
  `-arch x86_64` runs under Rosetta 2: MAP_FIXED at 0x80000000 works, the absolute symbol reads and writes RAM,
  and main lands below 4 GB (0x3a9470-0x4c29470 across runs). So the port is x86_64 on a Mac.
- **Absolute symbols:** ld64 accepts `_D_x = 0x801A18E0` in a PIE and reaches it through the GOT; a C tentative
  definition with `-fcommon` is replaced by it (ld warns). `-no_pie` also works (deprecated).
- **Without 4-byte pointers** the run dies in the third function: func_8017C4F4 calls `table[3]` from a pointer in
  RAM, reads 8 bytes, and jumps to 0x8017C8C08017C5F4 (two entries). 205 globals are declared as pointers (in
  common.h and the matched files) and common.h's structs have 15 pointer members in 10 structs (PORT.md lists them).
- **`__ptr32 __uptr`** works on Darwin x86_64 (`sizeof` 4, zero-extended), but clang's x86 backend can't select a
  call whose target is a 32-bit pointer (at -O0 always, at -O1 when the zero-extend isn't folded) and can't lower a
  memcpy on one: 127 of 4,000 files failed at -O1, 189 at -O0, in LLVM 21 and 22, and -mno-sse doesn't help. The IR
  patch fixes all of them; 3 files still fail, the same 3 that fail without the rewrite (conflicting prototypes in the
  matched source).
- gnu89 `extern inline` has no out-of-line copy; GCC inlined it, clang at -O1 didn't (an undefined symbol at link):
  every inline is always_inline in the game's files.
- Homebrew's SDL2 on Apple silicon is arm64 only, so an x86_64 build can't link it: the window goes through AppKit
  (psx/host_cocoa.m; SDL2 when it links for the architecture, headless otherwise).

## What the next pass needs

1. **The rest of the CPU side:** InitGeom, the unlabelled BIOS/syscall wrappers and the 162 Sony functions the C
   calls, enough that main runs a frame. Sony stubs that return pointers (malloc, the CD's file lookups) need real
   implementations before soft mode means much; InitHeap/malloc in the PS1 heap first.
2. **A renderer from the ordering tables.** ClearOTag/AddPrim/DrawOTag are stubs: walk the OT the game builds in
   RAM (24-bit links, the primitive codes), rasterise into a 1024x512 VRAM buffer (LoadImage/StoreImage/MoveImage on
   it), and present the display area. Software first; a GPU backend later. PsyCross (MIT) has one to compare with or
   link (`-DPSX_SDK=psycross`), but needs x86_64 SDL2/OpenAL builds here.
3. **SPU.** libsnd/libspu as stubs today; the SPU's 512 KB, ADPCM voices and the sequencer, through SDL audio or
   CoreAudio.
4. **CD from the disc image.** CdInit/CdSearchFile/CdRead against the user's .bin/.cue (the project already reads
   discs for overlays), so the game loads its own overlays and data; overlay code is already compiled in, and
   psx_function_at picks the one in RAM.
5. **arm64.** Needs a different memory convention: a base added to every PS1 address (an LLVM pass over the same IR
   cc_ptr32.py already handles), since arm64 macOS refuses memory below 4 GB.
6. **Checking.** The GTE against hardware traces or a reference, and the matched C against the original by replaying
   inputs (psx-spx is a reference, not a test).
