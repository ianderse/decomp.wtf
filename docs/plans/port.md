# PS1 export for port: the game's matched C on the host

Status (2026-10-23): native arm64. `decomp port <slug> [--build] [--main NAME]` (src/decompnet/rom/export_port.py,
the runtime in src/decompnet/rom/port_runtime/) writes `<project>/port/`, a CMake project over the exported tree.
On SLUS-01252 it compiles 3,997 of the 4,000 matched files to an arm64 Mach-O, links, opens a window (SDL2) and
runs the game's main (func_800C27B8) through 16 game functions to func_80176270, still in assembly (libgpu's
ClearImage by its shape: it calls the GPU table at D_801A1480 with the rect, 8 and the colour). InitGeom's
handwritten body (func_80178F28, where the first pass stopped) is replayed on the software GTE, and the event and
critical-section calls answer. With `PSX_PORT_SOFT=1` it went on through 70 distinct game functions, 5 functions
still in assembly and 15 Sony stubs before a null pointer ended it (a null PS1 pointer + 6, in func_800FD5BC: the
game reads a camera pointer before the camera exists, RAM at 6 on the PS1). With RAM's mirrors mapped and libcd
over the disc image (`--disc`, below) it goes on through 121: the main loop runs frames, and the game reads
COMM.IMG, COMM.SND and COMM.DAT from the disc (224, 161 and 33 sectors) before a function whose matched C drops an
argument hands its callee a host address (func_80104228, under the trace instrumentation; see the next pass).
No rendering or audio.

## The design

- **PS1 memory in a window of the host's.** PS1 address A is host address PSX_HOST_BASE + A, PSX_HOST_BASE =
  0x4_0000_0000 (one constant: psx/psx_runtime.h, export_port.py, tools/cc_ptr32.py; a test checks they agree). RAM
  is 2 MB at 0x4_8000_0000, the scratchpad at 0x4_1F80_0000, the game's stack 8 MB at 0x4_6000_0000 (the game sees
  sp as 0x6000xxxx), the game's string literals at 0x4_7000_0000; the low 32 bits of a host pointer into the window
  are the PS1 address again. The window is clear of everything macOS maps (the executable at 0x1_0000_0000 plus a
  slide, the dyld shared cache from 0x1_8000_0000 to about 0x2_2B00_0000), and its base is never mapped, so a null
  PS1 pointer reads 0x4_0000_0000 + offset and faults every time (reported as "a null PS1 pointer + 0xN"). The EXE's
  image is copied to its load address (host 0x4_800C_1400) and bss cleared with the range read from crt0 (constant tracking up to the first
  backward loop). The runtime goes through `psx_host(uint32_t)` and `psx_ps1(const void *)`.
- **Data symbols as absolute symbols** (gen/psx_symbols.S, `SYM(D_x) = PSX(0x801A18E0)`): every identifier the C and
  headers use that has an address (symbol_addrs.txt, build.json's symbols, undefined_syms_auto.txt, or the hex in an
  auto name such as D_801A18E0), so each file's own `extern` lands in that RAM. common.h's tentative definitions
  (`s32 D_801A1A40;`) are common symbols with `-fcommon`; the absolute one replaces them.
- **4-byte pointers.** tools/ptr32.py rewrites the game's files and headers into build/ptr32 at build time (the
  export stays the source): every pointer declarator gets `__ptr32 __uptr` (clang, `-fms-extensions`), `long` becomes
  `int`.
- **The translation.** tools/cc_ptr32.py is the compiler launcher for those files: C to LLVM IR (-O0, optnone off),
  a text transform, then IR to the object at -O1. On IR where the game's pointers are `ptr addrspace(271)`:
  (1) every load/store/cmpxchg/atomicrmw and llvm.memcpy/memmove/memset through one goes through
  `inttoptr(ptrtoint(p to i64) | 0x400000000)` (ptrtoint to a wider integer zero-extends; the base has no low
  bits), in the p0 form; (2) `addrspacecast` from 271 to an ordinary pointer: the same; (3) `addrspacecast` to 271:
  the low 32 bits (`ptrtoint to i32`), right for the window only, so a string literal of the game's file whose
  address goes 32-bit is moved there first: renamed to an absolute symbol `__psx_lit_<sha1 of its bytes>`, its bytes
  left in build/literals; at link time `cc_ptr32.py --collect-literals` lays them out from PS1 0x70000000
  (build/psx_literals.S: the symbols and the bytes) and the runtime copies them in at start-up. Not in PS1 RAM:
  SLUS-01252's bss ends at 0x801FFA20, 0x5D0 bytes below the top, and where the game puts its heap is only known at
  run time. Any other data of a game file whose address goes 32-bit fails the compile (none: across the 3,997
  files' IR only the 2 string literals are host data the game points at); (4) a game function's address as a 32-bit pointer or integer: its PS1 address, from
  gen/psx_function_addrs.txt; (5) a call through a 32-bit pointer: `psx_host_fn(i32)`, then the call through the
  host pointer it returns; (6) ptrtoint/inttoptr between 271 and i32 stay (PS1 address arithmetic). Constants (raw
  addresses, a symbol's address) are folded as constant expressions; an i32 made into an ordinary pointer is a PS1
  address too. A check after the transform fails the compile if any access through addrspace(271) is left.
- **Calls through PS1 addresses.** psx_host_fn looks the address up in gen/psx_functions.c (every function with its
  PS1 address and first two instructions, which tell overlays at one address apart); any other address ends the
  run with the PS1 address in the message. No host function is ever handed to the game as a pointer (the runtime
  registers no callbacks yet; one that does needs a PS1 identity of its own in the table). The SIGSEGV handler that moved a jump into RAM to
  the host function stays as a safety net for jumps not made through a 32-bit pointer.
- **Stubs.** Each function still in assembly: logs and stops (or returns 0 with PSX_PORT_SOFT=1); a handwritten one
  that only sets GTE control registers from constants (mfc0/mtc0 $12, li, ctc2, jr $ra: InitGeom's body) is
  replayed on the software GTE instead. Each Sony function (the database's library and BIOS-stub rows, plus syscall
  wrappers found by their code): logs its first 20 calls and returns 0. Implemented: VSync (window, pad, 60 Hz), the
  environment calls, PadInitDirect/PadStartCom, libgte's matrix arithmetic, InitGeom/SetGeomOffset/SetGeomScreen,
  the BIOS's memcpy/memset/strlen/rand family, InitHeap/malloc/free/calloc/realloc (a first-fit heap in RAM),
  OpenEvent (handles 0xF1000000 + n)/CloseEvent/EnableEvent/DisableEvent and Enter/ExitCriticalSection. psx/gte.c
  is a software GTE from psx-spx.
- **Trace.** The game's files build with `-finstrument-functions`; the run logs the first 200 distinct functions it
  enters, and on a crash the last 16 (a fault in the window is given as the PS1 address).

## What was measured (Apple clang 21, Homebrew LLVM 22, ld64, macOS 26, M-series)

- **arm64 with the window (2026-10-23).** A 10-line test: `mmap(0x1_8000_0000, 2 MB, MAP_FIXED|MAP_ANON)` succeeds,
  so does an 8 MB stack at 0x1_6000_0000, and so do 2 MB at 0x4_8000_0000 and 8 MB at 0x4_6000_0000 (the window's
  base now); `_D_801A18E0 = 0x1801A18E0` as an absolute symbol in an asm file
  links into an arm64 PIE and reads/writes that memory. clang's AArch64 backend takes `__ptr32 __uptr` (address
  space 271; its datalayout has `p271:32:32` like x86's) for loads, stores and GEP, lowered as `mov w8, w0; ldr w8,
  [x8]` (below 4 GB: the zero-extension that must not be used), and fails only on calls through one ("Cannot
  select: AArch64ISD::CALL"), as x86 did; the frontend's IR is the same as x86's. After the transform all 3,997
  files' IR compiles for arm64 (Homebrew clang 22 over the whole tree, then Apple clang 21 in the build).
- **The shared region, and why the base moved to 0x4_0000_0000.** The window was first at 0x1_0000_0000, which put
  RAM at 0x1_8000_0000, where macOS's shared region starts; the dyld shared cache is slid into it at boot (vmmap: it
  ended near 0x2_2B00_0000 this boot), and the space before it is a no-access reservation (`mach_vm_region_recurse`: 0x1_8000_0000-0x1_8795_8000
  at protection 0, the cache's first text at 0x1_879E_0000, printf at 0x1_87D1_1964, this boot). A hinted mmap
  there is refused (the kernel won't place it inside the region) but MAP_FIXED works; ram.c uses MAP_FIXED only when
  every region it would cover is unmapped or no-access (kept as a guard; it shouldn't trigger at 0x4_xxxx_xxxx). RAM
  fitted only because of the boot's slide. That window also held the executable (0x1_0000_0000 plus a small slide:
  0x1_0049_4460, 0x1_0471_0460, 0x1_00FE_C000 across runs) and the host's main stack (near 0x1_6758_C000), which let
  the game's string literals round-trip through 32 bits by luck and put the game's stack next to the host's. At
  0x4_0000_0000 none of that is near.
- **SLUS-01252 on arm64:** strict 16 game functions (to func_80176270, libgpu's ClearImage by its shape), soft 70
  (5 functions in assembly, 15 Sony stubs, then a null PS1 pointer + 6 in func_800FD5BC: host 0x4_0000_0006,
  unmapped), the same at both bases. Its 2 string literals the game points at ("POLYF4", "read retry\n") land at
  0x4_7000_0000 and 0x4_7000_0008. The first pass's x86_64 build got 10
  (to InitGeom) and 70. The same tree still builds for x86_64 (`-DCMAKE_OSX_ARCHITECTURES=x86_64`, Rosetta 2) at no
  extra cost and runs the same 16 and 70.
- **History: x86_64 with RAM at 0x80000000 (the first pass).** An arm64 executable linked with `-Wl,-pagezero_size`
  under 4 GB (0, 0x4000, 0x80000000) is killed at exec (SIGKILL, exit 137); without that, mmap at 0x80000000 fails
  (ENOMEM). The same test built `-arch x86_64` runs under Rosetta 2: MAP_FIXED at 0x80000000 works, the absolute
  symbol reads and writes RAM, and main lands below 4 GB (0x3a9470-0x4c29470 across runs). So that build was x86_64.
- **Absolute symbols:** ld64 accepts `_D_x = 0x801A18E0` in a PIE and reaches it through the GOT; a C tentative
  definition with `-fcommon` is replaced by it (ld warns). `-no_pie` also works (deprecated).
- **Without 4-byte pointers** the run dies in the third function: func_8017C4F4 calls `table[3]` from a pointer in
  RAM, reads 8 bytes, and jumps to 0x8017C8C08017C5F4 (two entries). 205 globals are declared as pointers (in
  common.h and the matched files) and common.h's structs have 15 pointer members in 10 structs (PORT.md lists them).
- **`__ptr32 __uptr`** works on Darwin x86_64 (`sizeof` 4, zero-extended), but clang's x86 backend can't select a
  call whose target is a 32-bit pointer (at -O0 always, at -O1 when the zero-extend isn't folded) and can't lower a
  memcpy on one: 127 of 4,000 files failed at -O1, 189 at -O0, in LLVM 21 and 22, and -mno-sse doesn't help. The
  first pass's IR patch cast those to ordinary pointers; the translation replaces it. 3 files still fail, the same 3
  that fail without the rewrite (conflicting prototypes in the matched source).
- **The game's own memory is almost all RAM.** Across the 3,997 files' IR: no global with an initializer except 2
  string literals (every other global is a tentative definition an absolute symbol replaces), no atomics, inline
  asm or invokes; 9,346 loads and 6,393 stores through a 32-bit pointer, 4,304 casts from one (clang's frontend
  casts to an ordinary pointer for member access), 723 casts to one (locals' addresses), 416 calls through one, 157
  memcpy/memset on one, 288 raw addresses.
- gnu89 `extern inline` has no out-of-line copy; GCC inlined it, clang at -O1 didn't (an undefined symbol at link):
  every inline is always_inline in the game's files.
- Homebrew's SDL2 on Apple silicon is arm64 only: the native build's window is SDL2 (sdl2-config); an x86_64 build
  falls back to AppKit (psx/host_cocoa.m), headless otherwise.

## The CD: libcd over the disc image (2026-10-23)

`decomp port <slug> --disc PATH` saves the game's disc image (.bin/.cue/.iso) in port.json and gen/psx_config.c;
`PSX_DISC` overrides it at run time; the image is read in place, never copied.

- **psx/cd.c** (335 lines, plain C, no runtime dependency): the sector size from the volume descriptor's "CD001" at
  sector 16 (2048, 2352 Mode 2 or Mode 1, 2448 with subchannel data, as decompnet/rom/psx.py's reader does), a .cue's
  FILE line, a .chd refused with the chdman command to convert it; whole raw sectors through a 64-sector cache (a
  2048-byte image's get a made-up Mode 2 Form 1 header), user data per sector mode and form (16/2048, 24/2048,
  24/2324); ISO 9660 lookup by path (`\` or `/`, `cdrom:` and `;1` optional, any case, directories longer than a
  sector, nested ones), every file indexed by LBA so the logs name the file a read is in, SYSTEM.CNF's BOOT.
- **psx/sdk/libcd.c** (661 lines) and **libcd.h** (178): a drive that is deterministic per frame. A command's result
  is written back at once and its completion (and the sync callback) comes at the next CD event point: a VSync
  (libetc's frame calls `psx_cd_vblank`) or a CdSync/CdReady/CdReadSync poll. Reads move 2.5 sectors a frame at
  double speed, 1.25 at single, one per poll, to the end in a blocking wait (`PSX_CD_FAST=1`: a CdRead at once).
  CdRead writes 2048/2340/2328 bytes a sector straight into the game's buffer and fires the read callback at the end;
  ReadN/ReadS without a CdRead make a current sector (CdReady reports CdlDataReady, the ready callback fires,
  CdGetSector copies from it and goes on where it stopped, then the data callback); CdlModeRT routes Form 2 audio
  sectors through the channel filter to `psx_cd_xa_sector(raw2352)` (a weak no-op until the SPU defines it); CdRead2
  with CdlModeStream fills the St ring (StSetRing's ring_size x 2016 bytes of frame data, then the 32-byte STR
  headers; StGetNext/StFreeRing hand frames out oldest first; a frame that doesn't fit is dropped). Commands: Nop,
  Setloc, Play (logged, CD-DA not played), ReadN/ReadS, Standby/Stop/Pause, Init/Reset, Mute/Demute, Setfilter,
  Setmode, Getparam, GetlocL/GetlocP, GetTN/GetTD, SeekL/SeekP, Test, GetID. CdSearchFile, CdIntToPos/CdPosToInt,
  CdMix (kept in `psx_cd_mix` for the SPU), CdReadFile, CdDataSync, CdDiskReady. Callbacks are called through
  psx_host_fn with their result bytes in a runtime buffer in the window (`psx_runtime_alloc`, 0x7F000000).
- **Which functions are libcd.** SLUS-01252's libcd is half matched C: CdSync, CdReady, CdControl/F/B, the callback
  setters are matched wrappers over the drive's internals (CD_cw, CD_sync, getintr: hardware, still assembly), and
  CdControl's core, CdRead and CdRead2 are assembly; the database names 30 of its functions, not the wrappers. The
  generator (`libcd_aliases`) takes a libcd function by name, by the library's own name for a second copy (SJ00's
  CdDataCallback at 0x8006DAD0), and the unnamed ones by their offset from a named one in the same object (CdRead,
  StClearRing) checked against their code with immediates and jump targets masked (LIBCD_LAYOUT: a different libcd
  version doesn't match and keeps its stubs). Each becomes the runtime's: a thunk under the game's name in
  gen/psx_sdk_stubs.c, the matched copy left out of the build (16 files here), the dispatch table pointing at it.
- **Overlays.** SLUS-01252's eight overlays (SDIC/SMITH/YEND/YGUILD/YPROP/YSEG/YSHOP.BIN at 0x80026000, SJ00.STG at
  0x8006D800) are byte for byte the export's segments. A finished CdRead logs the overlay it brought in;
  `PSX_CD_LOAD=SMITH.BIN@0x80026000,SJ00.STG@0x8006D800,YSHOP.BIN@0x80026000,SDIC.BIN@0x80026000` gives 107 of 107,
  66 of 66, 58 of 58 and 28 of 28 functions resolving to the right overlay through psx_function_at. The game
  itself reads no overlay before the run ends.
- **SYSTEM.CNF** boots SLUS_012.52 (LBA 25, 921,600 bytes), whose first 4 KB are the export's baserom.exe.
- **RAM's mirrors** are mapped now (ram.c, mach_vm_remap of the same pages at KUSEG 0x00000000 and KSEG1
  0xA0000000): func_800FD5BC's null read is the PS1's behaviour, and libcd's _96_init writes the BIOS's tables at
  0xA00003E0. `PSX_NULL_FAULT=1` leaves KUSEG unmapped to find null pointers.
- **From PsyCross** (github.com/OpenDriver2/PsyCross, MIT, at e56e4cd; LICENSE as psx/sdk/LICENSE.PsyCross): 93 of
  libcd.h's 178 lines are its include/psx/libcd.h (of 262): the CdlMode/CdlStat bits, command numbers, interrupt
  codes, CdlLOC/CdlFILTER/CdlATV/CdlFILE/StHEADER and the St constants, with u_long made 32-bit; CdIntToPos and
  CdPosToInt (12 lines of its 1,032-line LIBCD.C); and the shape of the model: CdRead queues and CdReadSync drains
  (mode 1 returning the sectors left), and its spooler's per-sector order (ready callback with CdlDataReady,
  CdGetSector from the current sector, then the data callback). Not taken: its image reader (a fixed 2352-byte Mode
  2 sector; CdSearchFile reads one sector of the root directory at LBA 22, so a deeper or longer directory isn't
  found), the cue parser (asserts MODE2/2352), the SDL thread that streams ReadS, CdControl/CdControlB/CdControlF
  (Setloc, ReadS, Pause and Setfilter/Setmode as no-ops; the rest "Unhandled"), CdSync (asserts on mode 0),
  CdReadCallback/CdSyncCallback/CdComstr (unimplemented), and its St functions, which are prototypes only. CD-XA is
  on its TODO list; here it is the hook.

## What the next pass needs

1. **The rest of the CPU side:** ClearImage (func_80176270, unlabelled: the next strict stop), the unlabelled
   BIOS/syscall wrappers and the 162 Sony functions the C calls, enough that main runs a frame. Sony stubs that return pointers (malloc, the CD's file lookups) need real
   implementations before soft mode means much; InitHeap/malloc in the PS1 heap first.
2. **A renderer from the ordering tables.** ClearOTag/AddPrim/DrawOTag are stubs: walk the OT the game builds in
   RAM (24-bit links, the primitive codes), rasterise into a 1024x512 VRAM buffer (LoadImage/StoreImage/MoveImage on
   it), and present the display area. Software first; a GPU backend later. PsyCross (MIT) has one to compare with or
   link (`-DPSX_SDK=psycross`); native arm64 means Homebrew's SDL2/OpenAL link, but its pointers must follow the
   window rule.
3. **SPU.** libsnd/libspu as stubs today; the SPU's 512 KB, ADPCM voices and the sequencer, through SDL audio or
   CoreAudio.
4. **Arguments the matched C drops.** An m2c draft that calls `func_800F5120()` from a `void` function matches on
   the PS1 because `$a0` passes through; on the host it does only while nothing touches x0. `-finstrument-functions`
   (the trace) loads x0 with the function's own address for `__cyg_profile_func_enter`, so func_80104228 hands
   func_800F5120 a host code address and the soft run ends there (121 functions). Built without the instrumentation
   the same tree runs on for 15 s (about 900 frames) in its frame loop and stops advancing at func_800DB780, a state
   handler still in assembly. A trace hook that keeps the argument registers (`-finstrument-function-entry-bare`
   with an assembly hook that saves x0-x7 and identifies the function by its return address) would keep both.
5. **The window's edges.** A game function compared with an int through an 8-byte pointer (2 files, `extern
   M2C_UNK func_X`) compares with the host address; Linux is untested and maps no mirrors (mach_vm_remap is
   macOS's; memfd_create would do it there).
6. **The CD's rest.** CD-DA (CdlPlay) isn't played; XA goes to the SPU's `psx_cd_xa_sector`; the St ring has no
   MDEC behind it (libpress is stubs); the BIOS's own file calls (B0 open/read on "cdrom:") are stubs.
7. **Checking.** The GTE against hardware traces or a reference, and the matched C against the original by replaying
   inputs (psx-spx is a reference, not a test).
