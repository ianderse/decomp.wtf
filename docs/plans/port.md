# PS1 export for port: the game's matched C on the host

Status (2026-10-23): native arm64. `decomp port <slug> [--build] [--main NAME]` (src/decompnet/rom/export_port.py,
the runtime in src/decompnet/rom/port_runtime/) writes `<project>/port/`, a CMake project over the exported tree.
On SLUS-01252 it compiles 3,984 matched files (of 4,005 in today's export: 3 don't compile, 18 are replaced by the
runtime, below) to an arm64 Mach-O, links, opens a window (SDL2) and runs the game's main (func_800C27B8) through
23 game functions (was 16) to func_8017295C, still in assembly (libspu: a switch on reverb modes over the SPU's
registers at D_801A144C). It gets past ClearImage (func_80176270, found by its code and forwarded to the runtime's)
and draws through a software GPU: ResetGraph, SetDispMask, ClearImage's fill, ClearOTag and the environments run;
the game hasn't reached its first VSync, so nothing of its own is on screen yet. InitGeom's handwritten body
(func_80178F28) is replayed on the software GTE. With `PSX_PORT_SOFT=1` it goes on through 69 distinct game
functions (70 before; 1 of them, func_8017C4F4, is the runtime's ResetCallback now), 3 functions still in assembly
and 12 Sony stubs, one LoadImage (a CLUT at 0,508 from 0x8001F57C, overlay space the CD hasn't filled), before a
null pointer ends it (a null PS1 pointer + 6, in func_800FD5BC: D_801AC4C4, set by func_800CF0D0 when a scene is
loaded). No audio. `PSX_GPU_SELFTEST=N` draws N frames through libgpu as a game would, and the window shows them.

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
  three backends. Nothing of it was copied. Worth revisiting for a later accelerated backend (upscaling, PGXP needs
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

## What the next pass needs

1. **The rest of the CPU side:** func_8017295C (libspu, the next strict stop), the unlabelled BIOS/syscall wrappers
   and the Sony functions the C calls, enough that main reaches its first VSync. Sony stubs that return pointers
   (the CD's file lookups) need real implementations before soft mode means much. More SONY_SHAPES as they turn
   up: any matched m2c draft that calls through a library's table with `()` loses its arguments on the host.
2. **The GPU's next steps.** Dithering (E1h bit 9) and interlace; the game's own frames to compare with an
   emulator's VRAM; an accelerated backend later (PsyCross's renderer, above, or Metal), keeping gpu.c as the
   reference.
3. **SPU.** libsnd/libspu as stubs today; the SPU's 512 KB, ADPCM voices and the sequencer, through SDL audio or
   CoreAudio.
4. **CD from the disc image.** CdInit/CdSearchFile/CdRead against the user's .bin/.cue (the project already reads
   discs for overlays), so the game loads its own overlays and data; overlay code is already compiled in, and
   psx_function_at picks the one in RAM.
5. **The window's edges.** RAM's mirrors (KUSEG 0x00000000, KSEG1 0xA0000000) aren't mapped; a game function
   compared with an int through an 8-byte pointer (2 files, `extern M2C_UNK func_X`) compares with the host address;
   Linux is untested (nothing there needs the executable in the window any more).
6. **Checking.** The GTE against hardware traces or a reference, and the matched C against the original by replaying
   inputs (psx-spx is a reference, not a test).
