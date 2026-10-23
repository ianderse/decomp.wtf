# PS1 export for port: the game's matched C on the host

Status (2026-10-23): native arm64. `decomp port <slug> [--build] [--main NAME]` (src/decompnet/rom/export_port.py,
the runtime in src/decompnet/rom/port_runtime/) writes `<project>/port/`, a CMake project over the exported tree.
**All three together (2026-10-23, the merge of the GPU, CD and sound branches, `--disc` the Arc the Lad II .bin,
built from a copy of the 12:22 export):** 3,968 matched files compiled (4,005 in the export: 3 don't compile, the
rest replaced by the runtime through the one aliases map: libcd's, the sound SDK range, and 25 Sony functions known
by their code). Strict: 54 game functions (16 at the start of the day, 23 with the GPU alone), past ClearImage and
_spu_t, to func_80158ECC, a game function still in assembly; no VSync yet. Soft: 118 distinct game functions, 6
functions in assembly passed over, the game reads COMM.IMG/COMM.SND/COMM.DAT, reaches VSync and draws: 680
primitives in 340 packets, 239 fills, 482 VRAM loads, double-buffered (display 0,0 and 0,224, drawing area
0,240/0,16 320x208), before the fault in func_800F5120 (the argument the trace instrumentation clobbers, below).
**On screen: "Working Designs Presents" in white on black** (the SDL window read back with PSX_PORT_DUMP_SCREEN),
and VRAM holds the title screen's art (the Arc the Lad II logo with New Game / Continue / Convert, the copyright
lines), the font and the sprite sheets (PSX_PORT_DUMP_VRAM).

Each branch on its own, as measured before the merge:

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
No rendering. Sound: a software SPU under libspu and a host libsnd, mixed to SDL2's audio device. With the disc, the
soft run goes through the game's sound set-up on them and loads COMM.SND's bank (SsVabOpenHead, SsVabTransBody:
294,944 bytes to SPU RAM) before the same func_800F5120 fault ends it, 114 distinct game functions: the 121 less
the 7 libsnd functions the runtime now answers. No SEQ is opened before that, so the run's own output is silent;
the same bank played through the same code outside the game sounds (below).

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
- **SDK ranges.** An address range whose every function is the SDK's, matched C or not: its C isn't compiled and
  each function is a Sony function (the runtime's when it has one, a labelled stub otherwise). The default is the
  project's compiler regions (`regions` in its config) that hold Sony functions the database names, each starting
  at the first of them; `decomp port --sdk-range FROM-TO` (repeatable, `none`) overrides. An unnamed SDK function
  identified from its code (export_port.SDK_IDENTIFIED, per project, with the evidence) forwards to the runtime's
  function of that name, as does a Sony row the database names by its address (SLUS-01252's func_80175434,
  SpuSetTransferStartAddr).
- **Sound.** psx/spu.c is a software SPU from psx-spx; psx/sdk/libspu.c drives it as Sony's libspu does, and
  psx/sdk/libsnd.c is libsnd on the host (below).
- **Trace.** The game's files build with `-finstrument-functions`; the run logs the first 200 distinct functions it
  enters, and on a crash the last 16 (a fault in the window is given as the PS1 address).

## The GPU (2026-10-23)

- **psx/gpu.c** (624 lines, written from psx-spx's GPU chapter, self-contained so a test compiles it alone): a
  1024x512 16-bit VRAM; GP0's polygons (flat/gouraud, textured with 4/8/15-bit textures and CLUTs, raw or
  modulated by the colour, 0000h transparent; quads as two triangles; edge functions with the top-left rule, so
  right and bottom edges aren't drawn; polygons wider than 1023 or taller than 511 skipped), rectangles (TILE,
  SPRT, 1/8/16 and variable), lines and polylines (flat/gouraud, terminated by 5xxx5xxx), the fill (02h, x and
  width in 16s, no clipping), VRAM-to-VRAM, CPU-to-VRAM with the data inline (DR_LOAD), the E1h-E6h state (texture
  page, texture window, drawing area, offset, mask set/check) and the four semi-transparency modes (textured: only
  texels with bit 15). GP1: reset, display enable, display start, display mode (256/320/368/512/640 x 240/480,
  24-bit), get info. Not done: dithering, interlaced fields, texture-cache effects, "draw to display area".
  `gpu_draw_chain(ram, addr)` walks a DMA2 linked list in the PS1's 2 MB (24-bit next pointers, 8-bit lengths; the
  real tags, no host pointers in them).
- **libgpu (psx/sdk/libgpu.c, 729 lines)** has both halves of the library: what a game calls (ResetGraph,
  SetDispMask, Set/Get/PutDrawEnv and DispEnv, applied; DrawSync immediate; DrawSyncCallback run when DrawOTag
  returns; ClearOTag/ClearOTagR, AddPrim/AddPrims/CatPrim/TermPrim, DrawOTag/DrawOTagEnv/DrawOTagIO/DrawPrim;
  LoadImage/StoreImage/MoveImage/ClearImage, synchronous; LoadTPage/LoadClut/GetTPage/GetClut; SetDrawEnv/Mode/
  TPage/Area/Offset/Stp/TexWindow/Move and the primitive setters, which write the length and code bytes into the
  game's primitive in RAM; get_mode/cs/ce/ofs/tw) and the driver under it (_addque/_addque2 run at once, _clr,
  _dws, _drs, _ctl, _cwb, _cwc, _otc, _getctl, _param, _status, _reset, _sync, _exeque). SLUS-01252's libgpu is
  part unnamed assembly, part matched C that calls the driver through its table (D_801A1480 -> {ver, addque,
  addque2, clr, ctl, cwb, cwc, drs, dws, exeque, getctl, otc, param, reset, status, sync}, read from the EXE), so
  the driver is what its own SetDispMask (func_80176200, matched) reaches. GPUSTAT is written to 0x1F801814 as
  ready, for code that polls it.
- **libetc (psx/sdk/libetc.c)**: VSync(0) waits for the next vblank, VSync(n) until n since the last call, VSync(1)
  the horizontal blanks since the last vblank (from the clock), VSync(-1) the counter; each vblank presents the
  display area, pumps the window, reads the pad, then runs the vblank callbacks (VSyncCallback(s), InterruptCallback
  irq 0) on the main thread, not re-entered. DMACallback and the other interrupts are recorded, never delivered.
  SetVideoMode/GetVideoMode, ResetCallback, PadInit/PadRead (the pad's halfword byte-swapped), and the root counters
  (SetRCnt/GetRCnt/StartRCnt/StopRCnt/ResetRCnt: CNT0 33.8688 MHz, CNT1 hblanks, CNT2 sysclock or /8, CNT3
  vblanks, from the host's clock, wrapping at the target in RCntMdINTR mode).
- **The window**: each backend has `host_present(vram, x, y, w, h, rgb24)`: SDL2 a streaming texture scaled to the
  window with nearest-neighbour (SDL_HINT_RENDER_SCALE_QUALITY 0), AppKit a CGImage drawn with
  kCGInterpolationNone, headless nothing. `PSX_PORT_DUMP_VRAM=file.png` writes the VRAM at exit (after a crash
  too), `PSX_PORT_DUMP_SCREEN=file.png` reads the SDL window back every 30 frames (screencapture isn't allowed
  where this was measured), `PSX_GPU_SELFTEST=N` draws N frames through libgpu (a background fill, a gouraud
  triangle, an additive quad, four 4-bit CLUT sprites, a polyline) in place of the game: the dump of the SDL window
  shows them 2x at 640x480, and the AppKit build runs the same frames.
- **Unnamed Sony functions known by their code (SONY_SHAPES in export_port.py).** A function's words with lui
  immediates, %lo halves through a lui'd register and jal/j targets masked, hashed: PSY-Q's libraries are the same
  object code wherever they're linked. 19 shapes from SLUS-01252: in assembly ClearImage (func_80176270),
  MoveImage, DrawPrim, SetGraphReverse, SetDrawMode, get_tw, get_dx, each a forwarder to the runtime's (8 integer
  arguments passed through); matched C that is replaced, left out of the build: ClearOTagR (func_801763F8),
  DrawSync (func_80176240), _addque, _cwc, _getctl, _param, _status, ResetCallback (func_8017C4F4),
  InterruptCallback, VSyncCallbacks (func_8017C554), DMACallback, StopCallback, and the same 6 again in the
  ovl_sj00 overlay, which links its own copy of libetc and libgpu (18 replaced). Their m2c drafts call the table
  with no arguments (`M2C_FIELD(D_801A1480, M2C_UNK (**)(), 0x2C)()`): they match on the MIPS, where a0-a3 pass
  through, and lose them on the host (the call through a 32-bit pointer goes through psx_host_fn first); _cwc and
  _param write the GPU's and DMA's registers, which are plain memory here; libetc's table has null VSyncCallbacks
  and DMACallback slots until intInit fills them. Also: a function still in assembly named like one the runtime
  implements is the runtime's (LoadImage at 0x801762C0 had no library row, so it was a plain unmatched stub).
- **PsyCross, evaluated and not taken (2026-10-23, OpenDriver2/PsyCross at e56e4cd, MIT).** Its renderer is
  src/render/PsyX_render.cpp (1,902 lines) + glad.c (1,936) and src/gpu/PsyX_GPU.cpp (1,619), behind
  include/psx/libgpu.h (934) and include/PsyX (398), C++. What didn't fit: (1) it draws primitives into the OpenGL
  framebuffer at window resolution and samples a CPU copy of VRAM as a texture; VRAM is updated from the
  framebuffer only by a readback in DrawSync (GR_ReadFramebufferDataToVRAM), so drawing into an off-screen VRAM
  area, StoreImage of a drawn area, and a texture made by drawing are approximations, and the headless build, the
  VRAM dump and a unit test of rasterised pixels would need a GL context; (2) its default 64-bit layout
  (PsyX_config.h: USE_EXTENDED_PRIM_POINTERS 1, and PGXP depends on it) puts a uintptr_t in every primitive tag,
  which changes the game's data; with it off, nextPrim casts the 24-bit address to a pointer and would need
  psx_host(0x80000000 | addr) patched into the macro and the walk; (3) it is tied to SDL's GL context (an
  `extern SDL_Window *g_window`, SDL_memcpy, SDL_GL_*) and its own libgpu entry points (LIBGPU.C, 817 lines, using
  RECT16 and PsyX_BeginScene), so the AppKit and headless backends would draw nothing, and the CMake project would
  need C++ and OpenGL. Taking it would be about 5,800 lines vendored plus glue for the tags, the window rule and a
  GL backend, against 624 lines of gpu.c and 729 of libgpu.c written here, which keep VRAM exact and work in all
  three backends. Nothing of its renderer was copied (what the CD and sound branches took from it is below). Worth revisiting for a later accelerated backend (upscaling, PGXP needs
  the GTE's vertices per primitive, which the extended tags carry and the real tags can't), with gpu.c kept as the
  reference and for VRAM-side effects.

## What was measured (Apple clang 21, Homebrew LLVM 22, ld64, macOS 26, M-series)

- **The GPU on SLUS-01252 (2026-10-23, the export at 12:20, built from a copy of it in
  ~/Decomp/slus-01252/port-gpu because the live tree is rewritten on a cycle).** Strict: 23 game functions (16
  before), through ResetGraph, SetDispMask (func_80176200 -> _ctl), the double-buffer setup (SetDefDrawEnv,
  func_8017579C = SetDefDispEnv), ClearOTag x2, ClearImage (1 fill), _SsInit and co. (stubs), to func_8017295C
  (libspu, still assembly). Soft: 69 functions, one LoadImage (a 256x1 CLUT to 0,508 from 0x8001F57C, overlay
  space nothing has loaded yet), the same null pointer in func_800FD5BC. No VSync is reached in either, so the
  window stays black: the game draws nothing until the CD (overlays, scene data) and libspu get it further. The
  self-test frame renders as intended (dump checked by eye). tests/test_port_gpu.py: a flat triangle with its
  edges, 15-bit and 4-bit CLUT sprites (transparent texel), a semi-transparent tile, the fill's rounding, VRAM
  copies, clipping with an offset, the display registers, and an ordering table walked in a fake 2 MB RAM.

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

1. **The rest of the CPU side:** func_80158ECC (a game function in assembly, the next strict stop), the BIOS wrappers
   and the Sony functions the C calls, enough that main reaches its first VSync. Sony stubs that return pointers
   (the CD's file lookups) need real implementations before soft mode means much. More SONY_SHAPES as they turn
   up: any matched m2c draft that calls through a library's table with `()` loses its arguments on the host.
2. **The GPU's next steps.** Dithering (E1h bit 9) and interlace; the game's own frames to compare with an
   emulator's VRAM; an accelerated backend later (PsyCross's renderer, above, or Metal), keeping gpu.c as the
   reference.
3. **Sound, the rest.** Reverb (the work area and the 32 registers are kept; nothing is mixed), noise, pitch
   modulation, the gaussian interpolation, vibrato/portamento and SEP files in libsnd; CoreAudio for the AppKit
   host (it runs the mixer without output); a listening test against the console.
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
6. **The CD's rest.** CD-DA (CdlPlay) isn't played; XA goes to the SPU's `psx_cd_xa_sector` (psx/xa.c); the St ring has no
   MDEC behind it (libpress is stubs); the BIOS's own file calls (B0 open/read on "cdrom:") are stubs.
7. **Checking.** The GTE against hardware traces or a reference, and the matched C against the original by replaying
   inputs (psx-spx is a reference, not a test).

## Sound (2026-10-23)

### libsnd: replaced wholesale, not run as C (option (a) of the brief)

Two ways were open: (a) treat libsnd's address range as Sony's and play SEQs with a host sequencer, or (b) run the
decompiled libsnd as C over an emulated SPU register file. Measured on SLUS-01252 (the database and the export's
assembly, docs/plans/libsnd-push.md for the range):

- **Coverage.** The libsnd range (the project's compiler regions, 8016A000-80172000) has 125 functions and 8,193
  instructions: 34 matched (437 instructions, 5%), 41 Sony rows (1,863) and 50 open (5,893). The matched ones are
  wrappers and accessors (`_SsInit(0)`, `_SsStart(0)`, `SpuSetReverb(1)`, flag setters, table getters); the whole
  path a SEQ takes (SsSeqCalledTbyT, _SsGetSeqData, GetMetaEvent, the note handlers func_8016BFEC/func_8016CA60,
  SpuVmAlloc, the key-on paths func_8016E650/func_8016EFF0/func_8016F558, SsSeqOpen/Play, SsVabOpenHead's
  _SsVabOpenHeadWithMode) is assembly. Under (b) not one note plays until about 91 functions (7,756 instructions)
  are matched or written by hand against libsnd's own data layout.
- **Hardware reads.** Through _spu_RXX (D_801A1448, gp-relative) libsnd's code (matched and not) writes voice
  VOLL/VOLR/PITCH/SSA/ADSR1/ADSR2 (3 functions), KON/KOFF/EON (func_8016F558) and NON (func_8016EE00), and reads
  only voice ADSRVOL (once, func_8016F558's voice allocator) and voice volumes (func_80170E94, func_80170F48).
  libspu's reads are SPUSTAT/SPUCNT handshakes (_spu_init, _spu_writeByIO, _spu_t), which a host libspu replaces.
  So (b) was feasible in principle (a polled register file answers all of them) but useless today.
- **The game's own code** never touches libsnd's private state (the score table D_801FAD50, the voice records
  D_801A73B8, the sequencer block D_801F4CC0, the voice register shadow D_801A7220): only libspu's helpers read the
  register file. Replacing libsnd changes nothing the game can see.

So libsnd's range is an SDK range: its 124 functions from 8016A05C are Sony's (33 matched files aren't compiled),
the 16 unnamed entry points the game's driver calls are identified (SsInit, SsInitHot, SsStart, SsStart2, SsEnd, SsQuit,
SsSetSerialAttr, SsSeqClose, SsVabClose, SsVabOpenHead, SsVabTransBody, SsVabTransCompleted, SsSetMono,
SsSetStereo, SsUtReverbOn/Off; the evidence for each is in export_port.SDK_IDENTIFIED) and forward to
psx/sdk/libsnd.c, and the rest are stubs. The range starts at 8016A05C, libsnd's first Sony function (SsSetMVol):
the region's 8016A028 is the game's (it calls CdRead). The SPU is still modelled as the register file (b) would
have needed: libspu's matched helpers that read and write 0x1F801C00 see the same state, and if the decompiled
libsnd is ever complete, `--sdk-range none` runs it over the same SPU.

### What runs

- **psx/spu.c** (388 lines): the register file is the window's 0x1F801C00 (0x280 bytes), the truth for both the
  host libspu and the game's raw stores; each mix step polls it (KON/KOFF consumed as triggers, KOFF first; a
  repeat address software wrote is noticed by comparing with the last one seen) and writes back ADSRVOL, ENDX, the
  repeat address a loop-start flag sets, SPUSTAT's mirror of SPUCNT and the current main volume. 512 KB of SPU RAM;
  24 voices of 4-bit ADPCM (filters 0-4, shift 13-15 as 9, the loop-start/end/repeat flags, loop end without repeat
  releasing at level 0); the envelope exactly as psx-spx gives it (AdsrCycles/AdsrStep, exponential increase
  slowed above 0x6000, exponential decrease scaled by the level); the 4.12 pitch counter (clamped at 0x4000) with
  linear interpolation between decoded samples (one sample of delay); fixed volumes (a sweep holds the last fixed
  level), the main volume, the CD input (with SPUCNT's CD bit and the CD volume), mute; 44.1 kHz, so no
  resampling. One recursive lock; the mix runs on the audio thread.
- **psx/xa.c** (104 lines): `int psx_cd_xa_sector(const uint8_t *raw2352)` for the CD layer: a raw Form 2 sector
  (sync and header included) whose subheader says audio; the 18 sound groups, 4- or 8-bit, mono or stereo, 37.8 or
  18.9 kHz, the decoder's history kept across sectors (`psx_cd_xa_reset` for a new stream), resampled linearly to
  44.1 kHz onto the SPU's CD input (a 0.74 s queue: the caller paces sectors at the disc's rate, filters the file and
  channel it wants, and can read `spu_cd_queued()`).
- **psx/sdk/libspu.c** (636 lines): libspu's functions on the register file (voice and common attributes with the
  ADSR mode/rate packing, key on/off and status, reverb switches/voices/depth/mode with the work-area sizes, noise
  and pitch-LFO voice bits, IRQ address), SpuMalloc (first fit from 0x1010 to the reverb work area, 16-byte blocks,
  the table on the host), transfers that land in SPU RAM at once (SpuIsTransferCompleted is always 1; a transfer
  callback is called through psx_host_fn), and the PsyCross-style shortcuts (SpuSetVoiceVolume ... SpuGetVoice*).
- **psx/sdk/libsnd.c** (806 lines): VABs (SsVabOpenHead, the body to SpuMalloc'd SPU RAM, Sticky, Partly,
  SsVabTransfer, SsVabClose), SEQs (SsSeqOpen/Close/Play/Stop/Pause/Replay/SetVol, SsIsEos, l_count plays, the
  NRPN loop markers, tempo events, running status), the tick (SsSetTickMode's rate, from the audio thread once
  SsStart/SsStart2 is called; a game that calls SsSeqCalledTbyT itself takes over), the voice manager (free voices,
  then the quietest releasing one, then the oldest of no higher priority), note pitch from the tone's center/shift
  and the channel's bend (pbmin/pbmax), volume and pan from velocity, tone, program, VAB, channel volume/expression/
  pan and the sequence's volume (SsSetMono centres it), SsVoKeyOn/Off and SsUtKeyOn/Off for sound effects,
  SsSetMVol/RVol/SerialAttr/SerialVol, SsUtSetReverb*, SpuVmSet/GetProgVol/Pan.
- **psx/audio.c** (132 lines): SDL2's audio device (44.1 kHz, S16, 512 frames, the callback mixing) when the port
  links SDL2 (`PSX_AUDIO_SDL`, set by CMake for `PSX_HOST=sdl`); otherwise, or for `PSX_HEADLESS` runs, a thread
  that runs the mixer at real time so the tick still goes on, writing a WAV with `PSX_AUDIO_WAV=path`; `PSX_AUDIO=0`
  turns both off.
- Every Sony function in libspu.c/libsnd.c is a weak definition (a game that has one as matched C keeps its own),
  and export_port.IMPLEMENTED reads their names from the files.

On SLUS-01252 (port built to its own folder, 2026-10-23): strict as before (16 game functions, stops at
func_80176270); soft: the same game functions and the same end (a null PS1 pointer + 6 in func_800FD5BC); its count
is 65 instead of 70 because the five libsnd wrappers it passed through (func_8016A10C, func_8016A860, func_8016ADD8,
func_801711F0, func_80171C1C) are now the runtime's, not instrumented game C; it reaches 7 Sony stubs instead of
15 (the sound set-up's are answered) and 4 functions in assembly instead of 5 (libspu's _spu_t, func_8017295C, is
no longer reached).

With the disc (`--disc`, merged with the CD layer): strict 15 (the CD layer's own count, unchanged by sound); soft
114 against the CD branch's 121, the difference exactly func_8016A10C, func_8016A860, func_8016ADD8, func_801711F0,
func_801712D8 (SsVabOpenHead), func_801717D0 (SsVabTransCompleted) and func_80171C1C, and the same end (func_800F5120
given a host address). load_vab_bank opens COMM.SND as VAB 0 (21 programs, 92 tones, 92 VAGs; the header at
0x80020800, the body at 0x80026000) and its 294,944-byte body goes to SPU RAM. Nothing keys a voice before the fault:
`PSX_AUDIO_WAV` recorded 3 s at peak 0. The bank itself, taken from the disc and played through spu.c, libspu.c and
libsnd.c (program 0, its centre note 82, for 2 s): peak 20,470, RMS 6,068.

Generated (without the disc): 3,969 matched files compiled (4,002 before: the 33 in libsnd's range are replaced),
124 functions in the SDK range, 19 functions forwarded by name (the 16 identified, and 3 Sony rows the database
names by address: SpuSetTransferStartAddr, and SpuDataCallback/SpuCallback in an overlay's copy of libetc).

Tests (tests/test_port_spu.py, clang into a harness): ADPCM blocks by hand and against a Python model of psx-spx's
decoder (7 shift/filter cases), envelopes against a Python model of psx-spx's ADSR (4 settings, 2,000 samples
each, and one by hand), a two-voice mix to exact samples (with ENDX, the repeat addresses and ADSRVOL written
back), a key-on by a raw register store, half-speed pitch interpolation, XA sectors (4/8-bit, mono/stereo, both
rates) against a Python model of the XA decoder, a VAB + SEQ played through the host libsnd (pitch an octave up,
the body at SpuMalloc's first address, the note off at the 30th tick of a 120 bpm quarter, end of sequence), and
the generator's SDK ranges, identification and forwarding.

### PsyCross: what was taken and what wasn't

PsyCross (https://github.com/OpenDriver2/PsyCross, MIT, Copyright (c) 2020 REDRIVER2 Project; its licence is in
psx/sdk/LICENSE.PsyCross), at e56e4cd:

- **Taken: include/psx/libspu.h**, 452 of its 533 lines, as psx/sdk/libspu.h (the constants, SpuVoiceAttr,
  SpuCommonAttr, SpuReverbAttr and the rest, the prototypes); left out: the C++ guards and the SpuSt streaming API
  (69 lines, not implemented here). Its structs have no pointers or longs, so they are the PS1's layout.
- **Followed, not copied: src/psx/LIBSPU.C** (375 lines): its function surface and the shortcut pattern (one
  SpuVoiceAttr with one mask bit for SpuSetVoiceVolume, SpuSetVoiceAR, ...), and SpuWrite calling the transfer
  callback. Its own bodies mostly forward to SPU-AL or are `PSYX_UNIMPLEMENTED()` (SpuSetCommonAttr, the reverb
  parameters, SpuMallocWithStartAddr, the IRQ calls); SpuMalloc is a bump allocator that frees everything at once.
- **Not taken: src/audio/PsyX_SPUAL.cpp** (848 lines) and its header (41): it plays each voice as an OpenAL source,
  decoding the whole sample at key-on with a float ADPCM decoder (`pow`, rounding) and approximate loop points
  (its own FIXME), with no ADSR, no register file, no ENDX or envelope read-back and no CD/XA. A register-level
  mixer needs the decoder per block with the hardware's integer arithmetic and flags, so psx/spu.c's decoder,
  envelope, mixer and XA decoder are written from psx-spx. OpenAL isn't a dependency; output is SDL2's.
