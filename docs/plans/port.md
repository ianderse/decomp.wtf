# PS1 export for port: the game's matched C on the host

Current replay (2026-10-26): see the [runtime workflow evidence](../workflows/runtime-progress.md).
Intro, title and both card scenarios passed visual review. New Game now survives 2,097
vblanks with the existing patches but stays black; gameplay acceptance remains open.
The dated descriptions below preserve earlier observations.

Status (2026-10-24): **the intro movie plays.** The soft run reaches XMV/A2MV24.STR, streams it through the St
ring and decodes it with libpress over a software MDEC (psx/mdec.c): 385 frames in 30 s, 15.0 a second, the first
at vblank 260, every frame LoadImage'd strip by strip into VRAM and shown 24-bit; the vblank count goes on climbing
(1,798 at 30 s, 4,497 at 75 s with the movie still running); the XA soundtrack plays alongside it (the WAV's peak
31,199, RMS 5,000-8,000 from 4 s on). "Movies" below. Before that (2026-10-23): native arm64. `decomp port <slug> [--build] [--main NAME]` (src/decompnet/rom/export_port.py,
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
argument hands its callee a host address (func_80104228, under the trace instrumentation; since the trace keeps the
argument registers, the run goes on to func_800DB780, still assembly: "Dropped arguments" below).
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
  gen/psx_function_addrs.txt; (5) a call through a 32-bit pointer: a call to `psx_call32` (psx/call32.S) with
  the C call's own return type and arguments plus the PS1 address as LLVM's `nest` argument (x15 on arm64, r10 on
  x86_64: outside the argument registers, and set by the compiler at the call itself, which inline asm writing x16
  before an ordinary call couldn't promise), the trampoline keeping x0-x7, x8 and q0-q7 around the lookup and
  branching to the host function (see "Calls through PS1 addresses"); (6) ptrtoint/inttoptr between 271 and i32 stay (PS1 address arithmetic). Constants (raw
  addresses, a symbol's address) are folded as constant expressions; an i32 made into an ordinary pointer is a PS1
  address too. A check after the transform fails the compile if any access through addrspace(271) is left.
- **Calls through PS1 addresses.** The trampoline (psx/call32.S, 2026-10-24): on the PS1 a `jalr` leaves every
  argument register the C didn't set as the caller had it, and matched C relies on it (`(*slot)()` or `handler()`
  whose callee reads $a0, the caller's object still there). The port used to call psx_host_fn first and then the
  pointer it returned, so such a call arrived with x0 = the callee's own host address: SLUS-01252's New Game
  faulted in func_800C58EC writing `*(u16 *)(arg0 + 0x84)` at its own address truncated, plus 0x84. Now x0-x7, the
  struct-return pointer x8 and q0-q7 reach the callee as the call site left them, and `br` (no frame) returns it
  straight to the caller. As far as the caller's compiled code leaves them alone: a register the C never sets
  holds whatever clang left there, which is the caller's own argument only while nothing used it in between (port
  patches like func_800CA600.c and func_80137FD4.c, which pass the object, stay the sure fix). On arm64 a
  direct-mapped cache of 1,024 {PS1 address, the two code words in RAM there, host function} answers without
  saving anything (psx_function_at depends on just those); a miss, or other code in RAM at the address, asks
  psx_call32_lookup in C. x86_64 has the same trampoline without the cache (a `jmp`). tests/test_port_call32.py runs
  it with every argument register set. psx_host_fn looks the address up in gen/psx_functions.c (every function with its
  PS1 address and first two instructions, which tell overlays at one address apart); any other address ends the
  run with the PS1 address in the message. No host function is ever handed to the game as a pointer (the runtime
  registers no callbacks yet; one that does needs a PS1 identity of its own in the table). The SIGSEGV handler that moved a jump into RAM to
  the host function stays as a safety net for jumps not made through a 32-bit pointer.
- **Stubs.** Each function still in assembly: logs and stops (or returns 0 with PSX_PORT_SOFT=1); a handwritten one
  that only sets GTE control registers from constants (mfc0/mtc0 $12, li, ctc2, jr $ra: InitGeom's body) is
  replayed on the software GTE instead. Each Sony function (the database's library and BIOS-stub rows, plus syscall
  wrappers found by their code): logs its first 20 calls (then every 10,000th, the count saturating) and returns 0.
  Implemented: VSync (window, pad, 60 Hz), the
  environment calls, PadInitDirect/PadStartCom, libgte's matrix arithmetic, InitGeom/SetGeomOffset/SetGeomScreen,
  the BIOS's memcpy/memset/strlen/rand family, InitHeap/malloc/free/calloc/realloc (a first-fit heap in RAM),
  the events (handles 0xF1000000 + slot; delivered, tested, waited for: "Events and the memory card" below), the
  memory card and Enter/ExitCriticalSection. psx/gte.c
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
- **Trace.** Opt-in (`decomp port --trace`, `-DPSX_TRACE=ON`, or `PSX_PORT_TRACE=1` at configure time): the run
  logs the first 200 distinct functions it enters, and on a crash the last 16 (a fault in the window is given as the
  PS1 address). It used to be `-finstrument-functions`, whose `__cyg_profile_func_enter(fn, site)` call loads x0
  with the function's address and so broke every matched function that passes an argument through (below). Now
  tools/cc_ptr32.py --trace puts an inline-asm `bl psx_trace_thunk` first in each game function (the PS1 address in
  x16, the host function in x17, clobbers x10-x15, lr, q16-q31 and the flags declared); psx/trace.S saves x0-x9,
  x16-x17 and q0-q15 around the C hook `psx_trace_enter(ps1, fn)`. `~{lr}`, not `~{x30}`: LLVM ignores the
  latter, so the function got no frame and its `ret` came back to itself. Off by default, the run log still
  reports what it reaches (unmatched, Sony stubs, calls through PS1 addresses) and the frames it ran.

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

## Movies: libpress over a software MDEC (2026-10-24)

**The game's STR player** (main's func_800DD7B4 state, the overlay ovl_sj00 = SJ00.STG at 0x8006D800 linked with
its own copy of libpress and libetc's callbacks): func_800DDB0C resets the MDEC (0x8006E614, MDEC_reset(0)), sets
DecDCToutCallback(func_800DDF20), seeks the movie (CdlSeekL), StSetRing(0x800AA000, 32 sectors) and
StSetStream(1 = 24-bit, frames 1..0x0FFFFFFF), CdRead2(0x1C0: double speed, real-time XA, streaming), then VLC-decodes
the first frame. Each frame after (func_800DD85C -> func_800DDDB4): DecDCTin(the last VLC'd buffer, 3 = 24-bit),
DecDCTout(the strip buffer 0x800A7000, 24 x 240 / 2 words: one 16-pixel column strip), DecDCToutSync; the callback
LoadImage's the strip at x, moves x on by 24 halfwords and starts the next DecDCTout, and after 20 strips (480
halfwords = 320 24-bit pixels) sets D_801A3EF4 and flips buffers; meanwhile func_800DE058 fetches the next frame
(func_800DE0E8 spins on StGetNext up to 8M times with nothing else in the loop, checks the frame's first two words
against the sector header's copy, ends the movie at the frame count in D_80185114), DecDCTvlc's it (0x8006E4F8)
into the other buffer and StFreeRing's it; func_800DE268 spins on D_801A3EF4. Start (pad 0x800) stops it.

**The stream**: A2MV24.STR is 320x240, BS v2, quant 1, 9 or 10 video sectors a frame plus one XA sector (file 1
channel 1, stereo 37.8 kHz) in every 8: 10 sectors a frame at 150 sectors a second is 15 frames a second. Video
sectors (Mode 2 Form 1, sub-mode 0x48) carry the 32-byte STR header (0x0160, 0x8001, chunk, chunks, frame, size,
width, height, the BS header's first two words) and 2016 bytes; libcd's St ring puts a frame's chunks back to back
and StGetNext hands out the oldest complete frame. XA sectors (Form 2, audio) go to `psx_cd_xa_sector` as before.
What was missing: the player spins on StGetNext while the CD interrupt fills the ring, so StGetNext now waits for
the next vertical blank when nothing is ready and the drive is streaming (psx_vblank_wait: present, the CD's 2.5
sectors, the vblank callbacks), which paces the movie at the disc's speed; after 600 vblanks with no frame it stops
waiting.

**libpress** (psx/sdk/libpress.c, written from psx-spx's MDEC and BS sections): DecDCTvlc (BS v1/v2/v3: DC as 10
bits or v3's Huffman differences, AC by psx-spx's table, escape + 16 raw bits, end of frame; resumable with
DecDCTvlcSize), DecDCTin (the mode bits into MDEC(1)'s command word as the overlay's copy does; MDEC(2)/(3) commands
load the tables), DecDCTout (rl_decode_block, the IDCT with the scale matrix, yuv_to_rgb, 15/24-bit or mono, rows of
16 pixels a macroblock), DecDCTReset/MDEC_reset (the standard quant table, which is what the overlay uploads, and the
scale matrix), Get/PutEnv, the *Sync calls (nothing to wait for) and both callbacks. The out-callback runs at the end
of DecDCTout on the main thread; one that starts the next DecDCTout is queued, not nested, so the whole frame's
strips are decoded and loaded before the first DecDCTout returns, and the spin on D_801A3EF4 finds it set. The
overlay's API wrappers (matched m2c drafts that drop their arguments) and MDEC_reset are aliases to the runtime's
through SDK_IDENTIFIED (which now takes matched functions too); DecDCToutCallback (main, named) and DecDCTin
(overlay, named) are the runtime's by name. A callback set through a call without a prototype arrives as the host
function's 64-bit address (cc_ptr32 translates a function's address only where it becomes 32 bits): libpress finds
it in the function table.

**func_800DDF20**, the callback, is still assembly: a port patch for an unmatched function (find_patches now takes
those: its C in the port only, `src/<seg>/<name>.c`, the stub dropped) in `port-mdec/port/patches/func_800DDF20.c`,
written from its MIPS. It is not in `~/Decomp/slus-01252/port/patches/` yet: copy it there (or match the function)
for the real port to play movies.

**SONY_SHAPES fix**: the interrupt table's slot 1 is DMACallback (ResetGraph calls its wrapper with 2, CdDataCallback
with 3, DecDCToutCallback with 1) and slot 5 VSyncCallback (_SsStart's VSyncCallback(SsSeqCalledTbyT)); they were
labelled the other way round.

Measured (`port-mdec`, a copy of the 2026-10-24 export, soft, headless, `PSX_PORT_DUMP_EVERY=120`): black until the
movie starts at vblank 260 (4.3 s); a sunset over a castle and its reflection at 10 s, the "Arc The Lad" title card at
16 s, a battle grid with a party facing armoured enemies at 20 s, a red airship's hull at 24 s, the stone statues and a ring-shaped
platform at 70 s. 15.0 frames a second throughout; PORT.md's run line counts the decoded frames and DecDCTout
completions (`press:` at exit). Tests: tests/test_port_mdec.py (a flat macroblock round trip, zigzag, the IDCT against
a float one within 2.5, psx-spx's AC codes, BS v2/v3 frames encoded in the test, the St ring on a synthetic disc
with interleaved XA sectors and the vblank pacing).

## Events, the memory card and the pad (2026-10-24)

**What the game does after the movie.** It re-reads COMM.IMG/SND/DAT and checks the memory cards: func_800DBEC8 (at
boot) opened 8 events, SwCARD (0xF4000001) and HwCARD (0xF0000011) with the specs IOE 0x0004, ERROR 0x8000, TIMEOUT
0x0100 and NEW 0x2000, EvMdNOINTR, into D_801A1630..D_801A164C; func_800DC288(port) drains the four SwCARD ones with
TestEvent, calls `_card_info(port * 16)` (func_8017CEE0, bios A0 AB) and spins on TestEvent (func_8017DD80, B0 0B)
until one fires: IOE returns 0, TIMEOUT 1 ("no card"), ERROR 2, NEW clears the card (func_800DF510, then
`_card_clear`: _new_card + _card_write(chan, 0x3F, 0)) and waits on the HwCARD four. It asks slot 2 three times, then
slot 1 three times. The port's TestEvent was a stub returning 0, so the game spun forever at vblank 5466, and the
stub's call counter wrapped negative and logged every call: a 7 GB log.

**Events** (psx/sdk/libapi.c) as psx-spx's EvCB: a slot's status 0 free, 0x1000 disabled (OpenEvent leaves it so),
0x2000 enabled/busy, 0x4000 ready; DeliverEvent(class, spec) makes every enabled/busy match ready (EvMdNOINTR) or
calls its function (EvMdINTR, never ready); TestEvent returns 1 for a ready one and makes it busy again (the test
clears the flag), WaitEvent blocks with a vblank pump, UnDeliverEvent takes ready ones back. Handles stay 0xF1000000 +
slot (the lowest free one). A game spinning on TestEvent never calls VSync, where the "interrupts" happen here, so a
miss while the card has an event on its way waits a vblank (and 100,000 misses with no vblank pump one anyway).

**The card** (psx/sdk/libcard.c, from psx-spx's memory card chapters): `PSX_MEMCARD=path` backs slot 1 with a 128 KB
image (raw frames, an emulator's .mcr; created formatted when missing: "MC" in frame 0, 15 free directory frames
0xA0, no broken sectors, frames 36-62 0xFF, frame 63 a copy of frame 0; written back whole after a write); slot 2 is
always empty; without the variable there is no card. `_card_info`/`_card_load` deliver SwCARD and HwCARD a vblank
later (from libetc's frame, never inside the call): TIMEOUT without a card, ERROR for a file of the wrong size, NEW
for the card's power-on "new card" flag (info) or an unformatted card (load), else IOE; `_card_read`/`_card_write`
deliver HwCARD only (a write clears the flag; `_new_card` lets one through while it is set). The BIOS's file calls
on "bu00:" (open with 0x200 creating mode >> 16 blocks, lseek, read, write, close, format, firstfile/nextfile with
'?'/'*' and the 40-byte direntry, erase) are synchronous. All of it reaches the game through the generator's
BIOS_LIBC table by bios label (A0 70/AB/AC/AD, B0 07/0A/0B/20, 32-36, 41-43, 45, 4A-4C, 4E/4F, 50, 5C/5D), and
PSY-Q's names (InitCARD, _card_info, ... _card_format) for a game that has them named.

**The pad.** SLUS-01252's pad reader func_800C305C was still assembly, so the game never saw a button (the keyboard
reached PadInitDirect's buffer, nothing read it). A port patch (`port-card/port/patches/func_800C305C.c`, from its
MIPS) runs it: libpad's state machine (PadGetState/PadInfoMode/PadSetAct/PadSetActAlign: libpad's own functions,
matched as the game's at func_8017E360/E430/E050/E090, now SDK_IDENTIFIED and answered by the runtime's libpad: a
digital pad on port 1, PadStateFindCTP1) and the bits into D_801A1AEC and the pressed/repeat words.
`PSX_PAD_SCRIPT="start@300,cross@600-640"` (psx/pad_script.c) presses buttons at vblanks for a run that reproduces:
up down left right cross circle square triangle l1 l2 r1 r2 start select, 10 vblanks a press
(`PSX_PAD_SCRIPT_HOLD`) unless FROM-TO, ORed with the keyboard in `psx_pad_bits`, which libpad's buffers and PadRead
read each vblank.

**Two more patches the card check needed** (same folder): func_80137FD4 calls its state's function from D_8018DC34
without the object ($a0 passes through on the PS1; on the host x0 was the state function's own address, and
func_8013838C wrote through it); func_80138064 passes func_800DC844/func_800DD354 an out-pointer that is the caller's
$s0 on the PS1 (an uninitialised `register` in the C), garbage on the host, which faulted once a card was present.

**Logs.** psx_sdk_stub's counter saturates and logs the first 20 calls, then every 10,000th with its count up to the
10,000,000th (`psx_log_due` in psx_runtime.h: at most 1,020 lines a site); the per-call logs in libetc (callbacks,
SetRCnt), libapi (OpenEvent, DeliverEvent) and libcard go through it, and libgpu/libsnd's `logged++ < N` counters no
longer wrap. A 200 s soft run that used to write 7 GB writes 13 KB.

Measured (`port-card`, a copy of the 2026-10-24 export with the five patches, soft, headless,
`PSX_PAD_SCRIPT=start@300`): Start skips the movie at vblank 300; the card check runs at vblanks 442-447 (slot 2
three times, slot 1 three times, SwCARD/HwCARD TIMEOUT a vblank after each `_card_info`, the game's loop returning
index 2); the title screen (Arc The Lad II, New Game / Continue / Convert) from 10 s; left alone, at 70 s the game
replays the intro movie as its attract mode, this time to its end (150 s), checks the cards again (vblank 9309) and
is back on the title at 160 s (a 200 s run, 1,282 movie frames decoded, a 13 KB log). Down
and Cross on Continue: "No Memory Card was found." (Retry / Cancel) without a card; with `PSX_MEMCARD` pointing at a
new file the card is created formatted, `_card_info` answers NEW, the game clears it (HwCARD IOE), lists
BASLUS-01252ARC2-00* and BASLUS-01224ARC1-00* (Arc 1's saves, for Convert: none), and Continue shows "Load, Card 1:
1-3 [No Data Present]". Tests: tests/test_port_card.py.

## Calls through function pointers keep the argument registers (2026-10-24)

**The bug.** New Game (`PSX_PAD_SCRIPT="start@300,cross@900"`) ended about 20 s in with a segmentation fault in
func_800C58EC, which writes `*(u16 *)(arg0 + 0x84)`: arg0 was func_800C58EC's own host address cut to 32 bits.
Some caller invokes it through a 32-bit function pointer with fewer arguments than it reads, `(*slot)()` or
`handler()`, byte-exact on the MIPS because $a0 still holds the caller's object at the `jalr`. The port's call was
`h = psx_host_fn(addr); h()`, so x0 was psx_host_fn's result, the callee's own address. The port patch for the one
audited table call (func_800CA600.c) didn't help, so another site does it too, and the class is every call through
a pointer that passes fewer arguments than the callee reads.

**The fix: psx/call32.S.** cc_ptr32.py's case 5 now emits `call <ret> @psx_call32(ptr nest %ps1, <the C call's own
arguments>)`: LLVM puts the `nest` argument in x15 on arm64 (r10 on x86_64), outside the argument registers, and
sets it at the call itself; inline asm that writes x16 before an ordinary call (as the brief suggested, and as the
trace does for its own call) can't promise the register allocator leaves it alone until the `bl`. The trampoline
looks the address up and `br`s to the host function, so the callee returns straight to the caller with the stack
arguments where the caller put them. x0-x7, x8 and q0-q7 reach the callee as the call site left them: the C call's
explicit arguments where clang put them, and the registers it doesn't set holding whatever the caller's compiled
code left there, which is the caller's own argument as long as nothing in between used the register (that is not
guaranteed the way it is on the MIPS, where GCC's code is the code; port patches that pass the object explicitly
stay the sure fix). On arm64 a direct-mapped cache of 1,024 slots {PS1 address, the two code words in RAM there,
host function} answers with x9-x14/x16 only: psx_function_at's answer depends on the address and those two words
alone (overlays at one address are told apart by them), so a slot whose words are still in RAM is the same answer,
and an overlay load misses. A miss saves the registers and calls psx_call32_lookup (psx_host_fn: the first-call
log, the error and the exit for an address with no function, then the slot filled). x86_64 has the trampoline
without the cache (`jmp *%r11`), assembled by the tests, not run. tests/test_port_call32.py runs the arm64 one with
x0-x8 and d0-d7 set to known values through the lookup, a cache hit and a cache miss on changed code words, and
checks that an IR call with a `nest` argument reaches the lookup with the address and the callee with x0/x1.

**The fault report** now prints the host backtrace, walked from the interrupted fp/lr (`backtrace()` stops at the
first frame outside the thread's stack, and the game runs on the PS1 stack at 0x60000000 in the window), each frame
checked to be in the game's stack, PS1 RAM or the host stack before it is read; and says what a faulting window
address looks like: "the PS1 address of func_X's code" for a function's own address used as data, or "looks like a
host code address truncated: a function pointer or return address stored as 32 bits" when its low 32 bits fall in
the executable's __TEXT (with the function and offset it was cut from).

Measured (`port-call`, a copy of the 2026-10-24 export with port-card's seven patches, soft, headless): the
generator's strict run stops at func_80156FF0 (as before), the soft one runs its 5 s (292 frames, the movie
playing). New Game, 90 s: Start skips the movie at vblank 300, the title from 10 s, Cross at 900 fades it out
(15.5 s), the screen black from 16 s while the game reads SG00.STG (225,280 bytes, the VAB heads and bodies to SPU
RAM, 284,672 more bytes), then func_800C5804 and **func_800C58EC are called through their PS1 addresses and return**,
and run_callback_and_record_coordinate_deltas, func_800C3F68, func_800C3E0C, func_80150408 and func_80150434 after
them. The run stops at vblank 1058 (17.6 s) with a segmentation fault in func_80166B80 at PS1 0x01FB002D; the new
backtrace names the chain: func_80166B80 <- func_80157F94 <- assert_fail (a misnomer: the window system's set-up)
<- func_80150434 <- initialize_world_section <- func_800C2838 <- func_800C27B8 (main). The cause is soft mode's
stubs: func_801574C8 builds a window from func_80155B6C (matched, but its file declares func_80155AD4 after calling
it, which clang refuses: "conflicting types", so it is excluded and stubbed) and func_801668AC (still assembly, 124
instructions), both returning 0, and func_80166B80 reads the null object's fields. A port patch moving
func_80155B6C's prototype up compiles it; func_801668AC needs matching. The still-assembly functions New Game reaches,
in order: func_80156FF0, func_8014F118, func_800C2F94, func_80137BD8, func_800F8C18, func_800F94BC, func_800C3944,
func_800F9B98, func_800FA818, func_800F8EF8, func_800D76C8, func_800C5DB0, func_800C45B4, func_801668AC (and
func_80155B6C's excluded C). The title path (`start@300`, 30 s) is unchanged: the title from 10 s, still there at
25 s, no fault.

## What the next pass needs

Current priorities and repeatable acceptance checks live in
[the runtime progress workflow](../workflows/runtime-progress.md). The active project's
`runtime-workflow.json` and frozen progress reports supersede this historical list.

1. **New Game's window construction:** verify the existing `func_80155B6C` declaration-order
   port patch, then investigate `func_801668AC` with an explicit eight-attempt budget and
   replay the New Game scenario. `func_80158ECC` is now matched; it is not the next task.
   Run the strict probe again for its current first stop; strict and soft milestones differ.
2. **The GPU's next steps.** Dithering (E1h bit 9) and interlace; the game's own frames to compare with an
   emulator's VRAM; an accelerated backend later (PsyCross's renderer, above, or Metal), keeping gpu.c as the
   reference.
3. **Sound, the rest.** Reverb (the work area and the 32 registers are kept; nothing is mixed), noise, pitch
   modulation, the gaussian interpolation, vibrato/portamento and SEP files in libsnd; CoreAudio for the AppKit
   host (it runs the mixer without output); a listening test against the console.
4. **Arguments the matched C drops.** An m2c draft that calls `func_800F5120()` from a `void` function matches on
   the PS1 because `$a0` passes through; on the host it does only while nothing touches x0. `-finstrument-functions`
   (the trace) loaded x0 with the function's own address for `__cyg_profile_func_enter`, so func_80104228 handed
   func_800F5120 a host code address and the soft run ended there (121 functions). The trace is now opt-in and keeps
   the argument registers (Trace, above); with it on or off the soft run gets past func_80104228 and idles in its
   frame loop for the 15 s (893 frames, see "Dropped arguments" below), stopping advancing at func_800DB780, a state
   handler still in assembly. That is luck, not a fix: nothing at run time can repair a call that drops an argument,
   so the generator now lists them (PORT.md, `port/hazards.json`) for the fleet to fix in the C.
5. **The window's edges.** A game function compared with an int through an 8-byte pointer (2 files, `extern
   M2C_UNK func_X`) compares with the host address; Linux is untested and maps no mirrors (mach_vm_remap is
   macOS's; memfd_create would do it there).
6. **The CD's rest.** CD-DA (CdlPlay) isn't played; XA goes to the SPU's `psx_cd_xa_sector` (psx/xa.c); the St ring feeds
   libpress over the software MDEC (Movies, above); the BIOS's own file calls (B0 open/read on "cdrom:") are stubs.
7. **Movies, the rest.** func_800DDF20 matched (or its port patch copied into the real port); func_800C2F94 (still
   assembly) takes the STR header's bytes 0x1C/0x1D (zero in A2MV24.STR) and is passed over; the MDEC's exact
   rounding (psx-spx calls its IDCT and YUV constants approximate) against a console capture; the 15-bit path has a test
   but no game using it yet, DecDCTvlcSize's partial decodes neither; the movie's end and the state after it
   (the run was still in the movie at 75 s).
8. **Checking.** The GTE against hardware traces or a reference, and the matched C against the original by replaying
   inputs (psx-spx is a reference, not a test).

## Dropped arguments (2026-10-23)

**The trace.** Measured on SLUS-01252 with the disc, built from a copy of the 12:22 export, 15 s soft runs. Trace
on (`--trace`): 109 distinct game functions, 77,719 calls, func_80104228 enters and calls func_800F5120 with its
argument intact, no fault; 893 frames, the GPU at 894 fills and 1,794 VRAM loads, 6 functions in assembly reached,
the last func_800DB780. Trace off (the default now): the same path (the same 6 functions in assembly, func_80104228
reached through its PS1 address, func_800DB780 last), 894 frames, 895 fills, 1,796 loads. Before (`-finstrument-functions`): the segmentation fault in
func_800F5120 at PS1 address 0x005C4D58 (the low half of func_80104228's host address) after 112 functions.
tests/test_port_hazards.py has the same case in miniature (`int caller(void) { return callee() + 1; }` with
`callee(int)`): 3703 through the thunk, something else under -finstrument-functions.

**The list.** `generate()` writes `port/hazards.json` and PORT.md's "Arguments the matched C drops" (call_hazards in
export_port.py: a regex/bracket scan of the one-function-per-file C, not clang's AST; 0.5 s over 4,005 files). Over
the 3,935 files the port compiles:

- a. **207 calls in 203 functions pass fewer arguments than the callee's definition takes**: 149 through an
  unprototyped `M2C_UNK func_X();`, 32 through `(void)`, 16 through a prototype with fewer parameters, 10 with no
  declaration of the caller's own. In 187 of them the caller's MIPS code leaves the registers untouched from its
  entry to the call (pass-through: take the argument as a parameter and hand it on); in 20 an earlier call or
  instruction wrote them first. Most-called callees: func_800FCD34 (26 callers), func_800F5FFC (25), func_800FD044
  (10), func_800F5120 (8: func_80104228 is one), func_8015CA60 (6), func_800D8F80 (5). Two are against the runtime's
  own prototypes: func_800F53C0 calls GetTPage with 2 of 4, func_8016A028 calls CdRead with none.
- b. **1 call with no arguments through an unprototyped table slot** in compiled C (func_800CA600's
  `(*((M2C_UNK (**)()) (arg0 + 0xD8)))()`); the 12 others in the export are libgpu/libetc/SJ00 code the runtime
  replaces by its code.
- c. **156 no-argument calls to functions still in assembly** (88 of them Sony's): no definition, and the database
  records no signatures (functions.meta has `library`, `alike`, `toolchain`..., not parameters), so
  the evidence is common.h's prototype or the callee's code: 41 read an argument register before writing it
  (func_800F67C4, called with `()` from 25 functions, reads $a0), 115 read none, 0 unknown.

A generator-side mitigation isn't possible without changing the C's meaning: a corrected prototype via `-include`
makes clang demand an argument the caller doesn't have (its own incoming one isn't a named variable in a `void f(void)`),
and rewriting the caller's parameter list in the port's copy is the same edit the fleet should make in the export,
minus the match check. So the port runs as it is, and the list is where to act. ps1_diagnostics.audit_call_boundaries
(another session's, uncommitted at the time) does a per-function lexical audit of the same class from the database;
this one is export-wide and checks against the callee's definition.

**The fix for (a): `decomp fix-passthrough <slug>`** (rom/passthrough.py, tests/test_passthrough.py). Reads the
database (each matched function's own source, export.own_source; its target code for the register scan), finds
every call that passes fewer arguments than the callee's definition takes, and for each dropped register the
caller's MIPS leaves alone from entry to the call (passes_through, one register at a time) gives the caller the
parameter (its own when it has one at that position, else `s32 aN` typed like the callee's) and passes it on. A
caller that gains parameters makes its own callers drop them: worked to a fixed point, rewritten callee before caller.
The caller's prototype of the callee becomes the callee's real signature (`ret f();` where some call of it still
passes fewer); a prototype of the function in context.h that no longer fits is edited too, first to `ret f();`
(every other match that declares or calls it the old way still compiles) and to the full prototype as the last
step of `--apply`. Each rewrite is compiled and linked as the judge does (`_compile` with the function's own
compiler against the would-be header, `_evaluate`), byte for byte, trying in turn the callee's own types, the
caller's own return type for the callee, plain `s32`/`void *`, and each of those passing the argument at the first
call only. `--dry-run` (the default) writes `<project>/passthrough/` (before/after/failed per function and
report.json); `--apply` submits each verified one through `POST /api/replace` (Network.replace_match: credit stays
with the matcher, a note "pass-through argument made explicit: $a0 reaches func_X unchanged (port hazard (a))",
a "replace" event the relink notices; matched_at kept), stopping at the first refusal.

The dry run on SLUS-01252 (2026-10-23, 4,010 matched functions, about 20 s):

- 237 calls drop arguments: 206 pass every dropped register through, 28 leftover (written or after another call
  first), 3 need a stack argument (a fifth or later parameter). 208 calls are forwarded (2 of them in part) by
  219 rewrites: 208 at level 1, 9 at level 2 and 2 at level 3 (the chains: a caller that gained parameters, one level
  up); 66 functions gain parameters, the other 153 already had them and only pass them on.
- **211 verify byte for byte, 8 don't.** By variant: 196 with the callee's own signature, 12 keeping the caller's
  return type for it (the callee returns void or a narrower type than the caller assumed: a return-value hazard of
  its own, left as it is), 2 with plain types, 1 passing the argument at its first call only. 3 header edits
  (SpuSetTransferStartAddr, func_800F563C, func_800F5684, all `(void)` in context.h).
- The 8: naming the incoming value at the call changes GCC 2.7.2's register choices, so these are a person's (or the
  permuter's) to finish: func_80028064 and func_800E254C ($s0/$s1 swapped), func_8013A14C ($ra/$s2 saved in the
  other order), func_801338FC, get_word16_table_0x801D2090 (calls func_80113D6C twice: $a0 kept in $s0 for the
  second call; passing it at the first only is further off), func_80163D18, func_80139260 ($a1 forwarded, $a2 a
  leftover: $a0/$a3 swapped) and func_800ED7B4 (func_8015E468 returns `void *`, which it dereferences as `u16 *`;
  with its own return type, $s4/$s5 swapped).
- 31 calls still drop arguments after it (the 28 leftover, the 3 stack arguments): `residual` in report.json.

Against the regex detector, over the same export copy (all matched files, not only the 3,935 the port compiles, and
without the runtime's prototypes: 208 dropped, 186 pass-through, where the port's list says 207/187): all 186 are
among the tool's (counted by caller and callee): 180 in rewrites that verify, 6 in the failures. The tool finds 22 more: 11 calls to func_800F5498, a K&R
definition (`s32 func_800F5498(a) u32 a; {`) the detector doesn't see, all verified, and 11 one level up the
chains (9 verified; func_80139260 and func_80163D18 are the other 2 failures). None of the 22 comes from a declaration inside a body; 57 calls through a cast of a game
function (`((T (*)()) func_X)(...)`) are seen by neither and not rewritten.

**The rest (2026-10-23, later).**

- *Calls through a cast.* Both detectors now see them (export_port.cast_calls: a cast in the call, `(*(T (*)()) &f)()`,
  a local pointer that only ever holds one game function). All 57 on SLUS-01252 pass what the callee's definition
  takes, so none drops an argument and nothing was rewritten.
- *Flow-aware scan.* passes_through now follows the caller's control flow (a call on another branch doesn't count; a
  loop's back edge does; a later call of the same callee is judged at its own jal). That turned 2 leftovers into
  pass-through: func_80111734 and func_80112B10 -> func_8010B800. Both were verified and applied.
- *The permuter* (`decomp permuter export --passthrough`, run on the Mac Studio with -j 8 for about 6 h;
  `import --replace`). Matched and replaced: func_80028064 and func_8013A14C (note "pass-through argument made
  explicit (permuted)"). Not matched, best judge score: func_800ED7B4 25, get_word16_table_0x801D2090 20,
  func_800E254C 200, func_801338FC 220, func_80163D18 210, func_80139260 30 (nothing better than the base).
  func_800E254C's callee func_800D8F80 ignores its argument (`n = 0` first), so that call is harmless anyway.
- *Leftovers, classified* (31 calls in the last dry run's residual; the `callee reads` column in PORT.md computes
  the same thing):
  - (i) value known in the C: ovl_yseg_func_800267C4 -> func_80159DC8, where $a1 is `amount`. Made explicit and
    replaced. func_800ED738 -> draw_text_glyphs was not a dropped argument at all: a `Pair2` passed by value is
    two words on MIPS ($a2/$a3) but one 8-byte register on the host. It now passes `p.x, p.y` with the real
    signature, byte-identical, replaced.
  - (ii) callee ignores it (its code never reads the register, and neither does its C): 24 calls. These are
    func_80027F1C, func_80027D2C (2), func_800287A0 (2), func_800273C8, func_8002754C (`s16 arg0` never used);
    func_800D8F80 (4: func_800271F4, func_800286E4's later calls, func_800E0F4C, func_800E254C; it assigns
    `n = 0` first); func_800DAD44 (6 callers: `a1`, `a2` never used); func_80168B68 (2: `arg3` unused);
    func_80138528 (2: a2/a3 only go on to func_800FE158, which takes one); func_8010522C's `a1` (shadowed by a
    local); func_801555AC (2: `a1` unused); func_80151EF0's `arg2` (overwritten unless the code is 4, which
    func_80151D34 handles before the call); func_800DDDB4 (its C never names a0-a2; the liveness scan says
    $a1/$a2 only because it is conservative through func_80176200 and the call through 0x8006E538).
    Follow-up: dropping those unused parameters from the definitions would remove the hazards outright.
  - (iii) stack arguments: func_80028414 -> func_8002B68C ignores both stack arguments (`a4`, `a5` unused).
  - Real bugs for a person (2), both fixed by a port patch now (below):
    - func_80026F50 -> ovl_yprop_func_80028724. $a1 is whatever func_800290C0 -> draw_window_frame's last
      callee left, and the callee passes it to func_800E9B3C and draw_text_glyphs. (Not a game bug after all:
      the callee is the wrong overlay's, see "Port patches".)
    - func_8002B68C -> func_800E8AAC. The 7th argument's stack slot is the caller's saved $ra, and the callee
      tests `arg6 == 1`, which is never true on the PS1 and undefined on the host. Passing 0 would be honest,
      but it can't match: the slot is where $ra is saved.
- Not redone in this pass: the fresh export and port build. Counts from the last dry run, after these replacements:
  39 calls dropped before them; 6 are now fixed, 2 are real bugs, and the rest are harmless (callee ignores them)
  or near misses kept as before. Two calls against the runtime's own prototypes (func_800F53C0 -> GetTPage,
  func_8016A028 -> CdRead) are outside fix-passthrough's scope (no game definition).

**Port patches (2026-10-23, later).** Matched C that has to read a leftover register or stack slot to match can't
be fixed in the export, so the port has its own copies: `port/patches/<function>.c` (or `<segment>/<function>.c`
when overlays share the name) replaces the export's file in the port build only (export_port.find_patches;
`gen/game_patches.txt` tells tools/ptr32.py to read the patch in place of `../source/<path>`, and CMake depends on
it). Each starts with a comment block saying what the PS1 does and why the port differs; PORT.md lists them
("Port patches": name, first comment line) and port.json has them. A patch for a function that is unknown (renamed
since), in no or several segments, or not matched stops the generator. The port folder is never wiped by `decomp
port` (only `gen/`, `include/`, `psx/`, `tools/` are rewritten), so `patches/` stays and is committed with the rest.

- `func_8002B68C.c`: the 7th argument passed as 0. The PS1 passes its saved return address there (0x8002xxxx),
  never 1, so the callee's `== 1` branch (the cursor, func_800DF934) never runs; 0 reproduces that.
- `func_80026F50.c`: the analysis turned up something else. `jal 0x80028724` reaches whatever overlay is
  loaded there, and with func_80026F50 (ovl_yguild) running that is ovl_yguild's own func_80028724, a one-argument
  list menu (`func_800E06BC(D_80038A78, D_801AC4F4, p, 18, 3)`); the C calls it `ovl_yprop_func_80028724`
  because the address's symbol is ovl_yprop's, which matches on the PS1 (the name is a relocation) and links to
  another function in the port (ovl_yprop's status page, reading `arg0 + 0xDC`). $a1 at the call is data-dependent
  garbage, which is a finding about the original code: draw_window_frame's last callee is AddPrim (it leaves the
  window primitive's address & 0x00FFFFFF) or, with semi-transparency on, enqueue_unit_textured_quad
  (0xFF000000), and nothing between its return and the jal touches $a1. But no code reads it: ovl_yguild's
  func_80028724 loads $a1 itself before any use, and func_800E06BC, which gets that as `unused`, zeroes $a1
  first. The result matters instead: func_80028724 and func_800E06BC are void in C but each ends in a call and
  leaves $v0 alone, so `result` is func_800DF824's pad code (2 cancel, 1 confirm, 3/4 ...). The patch calls
  ovl_yguild's func_80028724 and func_80026250 (the export's C names ovl_yprop's 0x80026250 too, another
  function) and replicates func_800DF824's decode of D_801AC4E2 for the result, without its sound effect.
- Measured on SLUS-01252 with the disc, from a copy of the 21:19 export, with and without the patches: the same
  depth (strict stops at func_80156FF0, soft still running at 5 s, 256 frames, reading SJ00.STG; neither patched
  function is reached that early); dropped-argument calls 32 -> 30.
- Left as they are: the wrong-overlay names are general. 28 calls in matched overlay C name another overlay's
  function at the same address; 14 of them have a function of the caller's own overlay there (8 more
  ovl_yguild callers of ovl_yprop_func_80026250, ovl_yprop's func_800277E0/func_8002790C ->
  ovl_yshop_func_80027A48, ovl_yend's func_800274F4 -> ovl_yguild_func_800275D4, ...), and the other 14 call an
  address where the caller's overlay starts no function at all (yguild's func_800287A0 -> yshop's
  func_800273F0/func_80027460, yshop's func_80027614 -> yseg's func_80026C8C ...: an overlay split worth
  checking). All of them link to the wrong code in the port; the fix belongs in the symbol naming (the overlay's
  own symbol at each jal), not in patches. Done since: "Cross-overlay names" below.

## Cross-overlay names (2026-10-24)

Where the names came from: the project's symbol table (RomNetwork.symbols, rom/symbols.py) has one name per address,
and the import adds every function to it in turn, so at an address two overlays share the last one's name stays
(`ovl_yprop_func_80028724`, splat having prefixed the clash). The stored assembly says `jal func_80028724`; the brief
showed it through rename_tokens with that table, and the brief's "calls"/"called by"/"Data it uses" lists and the
callee signatures came from xrefs, keyed by address alone (every overlay's function at that address mixed). An agent
on ovl_yguild's func_80026F50 was shown ovl_yprop's name and ovl_yprop's prototype and wrote them; the bytes match
(a relocation to the address), the port links ovl_yprop's function.

- **The brief** now names an address as the function's own overlay has it (RomNetwork.overlay_view, unit_label; for
  segments that share addresses only): its own function there; else, where the table's name is another overlay's
  function, `func_<addr>` inside one of its functions or `D_<addr>` outside them, with a note under "Overlay
  addresses" ("no function of ovl_yguild starts at 0x800273F0 (ovl_yshop's func_800273F0 does ...); the code at this
  address in ovl_yguild is inside its func_8002735C (+0x94), not a function start"). Calls, callers, data used and
  callee signatures come from the function's own relocations and its own overlay's (and the resident program's)
  functions. Only names change: every one of them resolves to the same address in the matcher. The PS1 guide says
  never to write another overlay's name for an address in yours.
- **The export and the port** check it (export_port.cross_overlay_refs, on build.json and the C actually built):
  a reference in an overlay's matched C to a function of another overlay that shares its addresses is an error
  when it is a call, or an address taken that links to a host function (that overlay's matched C or its stub);
  a prototype nothing uses, or the address of a function still in assembly that nothing calls (an absolute symbol
  at its PS1 address, the right value), is listed but not an error. The export logs the errors and keeps the list
  (`cross_overlay` in its result, the error count in projects.config `export`); the port logs them, writes them to
  port.json and PORT.md's "Cross-overlay names" (a hazard class, with the caller's own function there or what its
  overlay has at that address). Patches count: the check reads a patched file's patch.
- **The fix, through POST /api/replace** (names only, each judged byte-identical by replace_match), 93 functions:
  - 13 functions, 14 calls, to the caller's overlay's own function at the address (the 14 of the 28 above:
    10 ovl_yguild functions -> its own func_80026250 / func_80028724, ovl_yprop's func_800277E0/func_8002790C ->
    its func_80027A48, ovl_yend's func_800274F4 -> its func_800275D4), plus 4 ovl_smith functions that take the
    address of another overlay's function where smith has its own (`assert_n(&ovl_yguild_func_80027304)` ->
    `&func_80027304`, and ovl_yshop_func_80026310, ovl_yseg_func_800267C4, ovl_yshop_func_80027BC8 the same way).
    Note: "callee named by this overlay's own function at the address (port correctness; same bytes)".
  - 76 functions, 115 addresses: this overlay's data (or a code address inside one of its functions) named by
    another overlay's function, `(u8 *) func_8002899C + 0x60` in ovl_yshop, whose code ends at 0x800288C0, for
    ovl_yguild's function; in the port that read the other overlay's host code. Renamed `D_<addr>` (a prototype of
    it made `extern u8 D_<addr>[];`), an alias `func_<addr>_<overlay>` inside one of its functions, or
    `D_<addr>_<overlay>` where the automatic name is taken (ovl_smith's function at 0x8002B54C is *named*
    `D_8002B54C`, see below) or declared otherwise in that source (func_80026320 has `extern u32 D_80027494;`).
- **The 14 calls with no own function** (the plan's second 14) are not calls, and no overlay misses a split: the 11
  from func_80027614, func_80028154 (3 each), func_80027F1C, func_800287A0 (2 each) and func_80026178 are
  prototypes left in twin-copied sources ("identical to func_80026D0C (matched), with its names changed"), where
  own_source turned the twin's helper definitions into prototypes nothing calls; the other 3 are data (above). Per
  address, the caller's overlay's image (game.overlays.exe through its segment):

  | caller (overlay) | name | in the caller's overlay | evidence |
  | :-- | :-- | :-- | :-- |
  | func_80026178 (yshop) | func_80026264 | inside func_80026248 +0x1C | `addiu $a0, $a0, -0x7690` after `lui`; no `jr $ra` before |
  | func_80027614 (yshop) | func_80026C8C | inside func_80026B34 +0x158 | `addiu $a0, ...` between `lui $a0` and `jal 0x800C3564` |
  | func_80027614 (yshop) | func_80026D34 | inside func_80026D2C +0x8 | the third instruction after `addiu $sp, -32; sw $ra` |
  | func_80027614 (yshop) | func_80026DA4 | inside func_80026D2C +0x78 | right after `jal 0x80155698; nop` |
  | func_800264F8 (yshop) | func_8002899C | data, after its last function (ends 0x800288C0) | halfword tables and zeros |
  | func_80027ACC (yshop) | func_80028AAC | data, after its last function | zeros; read as `((s32 *) &x)[3]` |
  | func_80026E08 (yseg) | func_800273C8 | data, after its last function (ends 0x80027360) | `(s32) x + 8` passed on |
  | func_80027F1C (yprop) | func_800273F0 | inside func_800272C4 +0x12C | the `nop` in `jal 0x800C3564`'s delay slot |
  | func_80027F1C (yprop) | func_80027460 | inside func_80027424 +0x3C | after `jal 0x800286A4` |
  | func_80028154 (yguild) | func_80026C8C | inside func_80026B84 +0x108 | `addiu $s1, $s1, 1` in a loop (`bne` back at +0x110) |
  | func_80028154 (yguild) | func_80026D34 | inside func_80026CF0 +0x44 | `addiu $a0, ...` after `lui $a0` |
  | func_80028154 (yguild) | func_80026DA4 | inside func_80026CF0 +0xB4 | the delay slot of `jal 0x80027DB4` |
  | func_800287A0 (yguild) | func_800273F0 | inside func_8002735C +0x94 | the instruction after `j 0x80027458; nop` |
  | func_800287A0 (yguild) | func_80027460 | inside func_8002735C +0x104 | `jal 0x8016766C` |

  None follows a `jr $ra` and its delay slot, so there is nothing to split (and no person-facing split exists
  anyway: split_function is the agents' MCP tool only, no /api route).
- **Counts** (export_port.cross_overlay_refs on the export, before: today's tree with the 93 replacements' renames
  undone; after: the export of 2026-10-24 07:33, identical, 4040 of 4851 in C, fallback 0):

  | | before | after |
  | :-- | --: | --: |
  | calls naming another overlay's function | 14 (all with an own function there) | 0 |
  | addresses taken, linking to another overlay's host function | 119 (4 with an own function) | 0 |
  | **errors** | **133** | **0** |
  | prototypes nothing uses (not errors) | 70 | 70 |
  | addresses of an uncalled assembly function (its PS1 address: not errors) | 12 | 12 |

  The old scan (`name(` in the C, any use) went from 28 to 11, the 11 unused prototypes.
- Left alone, the same root (one name per address): a rename of an address renames every overlay's symbol there,
  so overlay functions carry other overlays' data names: ovl_smith's `D_8002B54C`, `str_800288AC`, `obj_800288E0`
  and `build_filtered_slot_list` (renames `func_8002B54C -> D_8002B54C` and so on, meant for another overlay's
  data), and ovl_yend's `yend_draw_marked_text`, which ovl_yseg/ovl_yshop/ovl_yguild C used for their own data at
  0x80027398 (now `D_80027398`). rename_symbol would need a unit too. port/patches/func_80026F50.c is still needed
  (the result and the dropped $a1); its note about the names is history now that the export's C names
  ovl_yguild's functions.

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
