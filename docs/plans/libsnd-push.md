# SLUS-01252: the libsnd push

Status (2026-10-22): plan. Needs the region compiler, the relaxed hint gate and the region lane (agents.py,
engine.py, network.py; see the commits of 2026-10-22) and a restart of `decomp serve`.

## Why this region

50 open functions at 0x8016A000-0x80172000, 5,893 instructions (13% of what's open), abandoned 8 times each on
average. They are Sony's libsnd (PSY-Q 2.6/3.0 objects around them: SESKON, SCSMVOL, SSINIT, SSCALL, PAUSE), not
the game's code, and two things kept them stuck:

- **The compiler.** Measured on all 50 functions' best attempts under every PS1 setup on hand (28: each GCC from
  2.6.0 to 2.95.2, plain, with ASPSX 2.21 or 2.34, and -O1; docs/plans/libsnd-survey.py did it, 2026-10-22): GCC 2.6.0,
  2.6.3 and 2.7.2 with ASPSX 2.34 score best (mean 15% of the worst score, lowest for 29-31 of 50), GCC 2.8.1 is the
  worst of the old family (31%), 2.91 and 2.95 are far off. 37 of the 50 carry ASPSX 2.34's `ori $r, $zero, imm`
  for `li`. Two voice setters (func_80170DC8, func_80170EE4) want -O1 (175 and 270 vs 545 and 410). The hint refresh
  had put 33 of them on gcc-2.8.1-psx@2.34 from 8 matched neighbours of 5-13 instructions, which compile the same
  under most setups: too small to tell compilers apart. Agents then tuned for 2.8.1 and reported the same diff each
  time: a CSE'd lui/addiu pointer and lui-$at-indexed table accesses the target has, which 2.8.1 splits.
- **The data.** 24 of the 50 index the same pointer table (D_801FAD50) through raw casts, each function declaring
  it its own way. None of the region's shared globals is declared in context.h.

Three project learnings (notes 3453, 5019, 5031, deleted 2026-10-22) told agents that GCC+maspsx can't produce this code and to
flag_for_review it. Once the region compiles with 2.8.1 those are wrong; the instructions below override them, and
they can be deleted (`decomp note delete <id>`).

## Steps

1. Restart `decomp serve`.
2. Agents tab > Compiler for an address range: 8016A000 to 80172000, gcc-2.7.2-psx@2.34 (the owner's range replaces
   the hints); then 80170DC8-80170E94 and 80170EE4-80170F48, gcc-2.7.2-psx/O1. Best scores are recomputed under the
   new compiler (all 17 that moved first improved).
3. Paste the instructions below into the project's instructions box.
4. Start two sessions with the address range 8016A000-80172000: Claude on Opus, high effort, and Codex on Sol,
   high effort, 2-3 functions per session. The first session's first job is the header block (below).
5. A day later: matches in the range (0 so far in a week), and the per-claim compile counts.

## Instructions (paste into the project's instructions box)

```
0x8016A000-0x80172000 is Sony's libsnd (PSY-Q 2.6/3.0 era), built with the project's GCC 2.7.2 but the older
assembler ASPSX 2.34 (constants loaded with `ori $r,$zero,imm`), measured on all 50 functions' best attempts:
gcc-2.7.2-psx@2.34 (and GCC 2.6.x with it) scores best, GCC 2.8.1 is the worst of the old family. Each function
there now compiles with that setup (func_80170DC8 and func_80170EE4: -O1); the earlier learnings saying the code
can't be produced are out of date. Do NOT flag_for_review these; match them. Saved best attempts were tuned under
other setups: read them for the structure, recompile under the function's own compiler, and work from there.

libsnd's data, declare it ONCE in context.h (edit_context) before working the functions, then use the struct:
- D_801FAD50: a table of pointers (indexed by the first argument, "sep") to arrays of 0xA8-byte sequence records
  (indexed by the second argument, "seq"); libsnd calls the table _ss_score and the record SeqStruct. Offsets seen
  across the 24 functions that use it: +4, +0xC, +0x10, +0x12 (u8s at 0x12-0x18), +0x1C, +0x24, +0x27-0x2C (0x2C used
  14 times), +0x3C s16, +0x3E u16, +0x40 s16, +0x42, +0x48, +0x4A (12 uses), +0x4C, +0x6C, +0x6E, +0x72/+0x74 s16
  volume left/right (SsSeqSetVol shape), +0x88 (17 uses), +0x90 u32 flags (bit 0x20), +0x94, +0x98 u32 countdown,
  +0x9C, +0xA0, +0xA4. Take the field types from the loads (lb/lbu/lh/lhu/lw) in the functions you work; pad the rest.
- D_801A73B8 .. D_801A73CF: one 24-byte voice record (24 of them; functions index with voice*24): fields at +0 (73B8),
  +2 (73BA), +4 (73BC), +6 (73BE), +8 (73C0), +0xA (73C2), +0xC (73C4), +0xE (73C6), +0x10 (73C8), +0x12 (73CA),
  +0x17 (73CF, a u8). Declare it as an array of a struct, not 11 separate globals.
- D_801A7220: 24 x 16-byte SPU voice register shadow (s16 left, right, then pitch/ADSR halfwords), ending at
  D_801A73A0 (per-voice dirty flags). D_801A1448: pointer to the SPU registers (_spu_RXX, 0x1F801C00).
- D_801F4CC0 .. D_801F4CE8: libsnd's sequencer state block (u8/u16 fields: CC0[0], [2], [7], CC7, CCA-CCF, CD6, CD8,
  CDA, CE8). Declare it as one struct too.
Name what you match after libsnd's own names where you recognise them (SsSeqSetVol, _SsVmSetSeqVol, _SsSndSetVol,
SsUtKeyOn/Off, SpuVm*), in snake_case otherwise.
```

## The header block (the first session writes it, from the assembly)

The field list above is what the open functions' sources agree on; the session should confirm each width against
the target assembly before declaring it, and keep unknown bytes as `u8 padNN[]`. A wrong width in a shared struct
costs every function in the region, so it must be checked, not copied.
