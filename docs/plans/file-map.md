# SLUS-01252: the original source files

Status (2026-10-20): measured; main.c stays one file for now.

The main program's matched C is one translation unit (src/main.c). The question was whether the original file
boundaries can be recovered from the disc, so data (rodata, .sdata, .bss) could later be placed per file.

## Evidence, and how good each kind is

Each kind was tested where the answer is known: the 55 Sony library modules identified function by function (each
module is one source file; 172 neighbouring pairs, 16 of them across a module boundary).

| Evidence | What it found on Sony's code | Verdict |
| :-- | :-- | :-- |
| File names in the binary (`$Id:`, assert strings, `__FILE__`) | only Sony's: intr.c, sys.c, pad.c, vsync.c, s_crwa.c | no game file names survive |
| Zero padding between functions | inside a module: 0 of 156; at a module boundary: 3 of 16 | reliable when present, rare |
| Function start alignment | 26% on 16 bytes, 50% on 8 (chance: 25%, 50%) | functions are 4-aligned: no signal |
| Data order ("clean cuts": functions before a point use only lower data addresses than those after) | 0-3 of 16 boundaries, 3-7 false cuts inside modules, whatever the window | no signal |

## What that gives

- Sony's code: 55 files, certain (107 stretches counting the BIOS stubs), from identify_psyq's module names.
- The game's own code: 6 boundaries from padding; the other 659 KB (3,713 functions) is one block with no reliable
  evidence of where its files began.

## Decision

No split of main.c now: the only well-founded splits are Sony's (set aside as assembly anyway) and 6 cuts, and
guessed splits would misplace data once data is decompiled.

## What would change it

Another build of the game that kept symbols or file names: the Japanese release, a demo disc, a prototype. Measure
again then (the script was a scratch one: the method is above; the Sony-module test is the check for any new signal).
