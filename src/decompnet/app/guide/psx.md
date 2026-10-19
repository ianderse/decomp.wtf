# Matching PlayStation (PS1) code

CPU: MIPS R3000A (MIPS I). Things that look odd at first:
- Load delay slot: the instruction after a load can't use the loaded value; you'll see `nop`s or
  reordering there. Branch delay slots exist too; there's no branch-likely.
- `mult`/`div` put results in HI/LO (`mflo`, `mfhi`). Division is followed by checks for divide by zero
  (`break 7`) and overflow (`break 6`): the PSY-Q assembler adds them, and the project's build (GCC plus
  maspsx, which imitates Sony's ASPSX) reproduces that. Write a plain `/`.
- `$gp`-relative small data (-G4 or -G8): small globals are read with `lw $v0, %gp_rel(sym)($gp)`
  (one instruction) instead of `lui` + `lw`. In a ROM project, declare such globals in context.h
  *without* `extern` (`s32 D_8001B278;`): the compiler then treats them as small data. With `extern`
  it builds the full address and the function won't match. Arrays and structs larger than the limit
  are never gp-relative.

Compilers: PSY-Q SDK GCC 2.6/2.7.2/2.8.x and ASPSX versions differ in register allocation and
instruction scheduling. Your code goes through GCC, then maspsx (Sony's assembler's delay-slot and nop rules),
then GNU as. A near miss is also tried with the other GCC version, and a function that matches with it keeps
it. Code is mostly C.

Libraries: PSY-Q's libgte, libgpu, libgs, libspu, libcd, libetc, libapi and libc are usually linked
as library objects, not decompiled; their names and prototypes are in the project's headers.

GTE (the geometry coprocessor): every compile includes trusted `gte_*` macros (you can't redefine them, and raw
inline assembly is rejected). Types `SVECTOR`, `VECTOR`, `DVECTOR`, `CVECTOR`, `RECT`, `MATRIX` are in new projects'
context.h; add them with edit_context if yours lacks them.
- `gte_ldv0(p)`, `gte_ldv1(p)`, `gte_ldv2(p)` load a vector; `gte_ldv3(p0, p1, p2)` loads three.
- Commands: lowercase `gte_rtps()`, `gte_rtpt()`, `gte_nclip()`, `gte_avsz3()`... emit two nops first, as PSY-Q's
  macros do; uppercase `gte_RTPS()` emits only the command. `gte_mvmva(sf, mx, v, cv, lm)`, `gte_sqr(sf)`,
  `gte_op(sf)`, `gte_gpf(sf)`, `gte_gpl(sf)` take compile-time constants.
- Results: `gte_stsxy(p)`, `gte_stsxy3(p0, p1, p2)`, `gte_stsz(p)`, `gte_stotz(p)`, `gte_strgb(p)`, `gte_stflg(p)`.
- Registers: `gte_mfc2(reg)` / `gte_cfc2(reg)` read one (with the load-delay nop), `gte_mtc2(reg, value)` /
  `gte_ctc2(reg, value)` write one; `gte_lwc2(reg, p)` / `gte_swc2(reg, p)` move memory.
The memory helpers clobber memory, which can move instructions around them: compare the bytes, nops included. m2c
often has no draft for GTE code: work from the assembly. Reference: https://psx-spx.consoledev.net/geometrytransformationenginegte/

Typical patterns: `jtbl_` jump tables in .rodata for dense switches; overlays loaded at the same address
(separate units); `D_800xxxxx` data and `func_800xxxxx` names for things nobody named yet.
