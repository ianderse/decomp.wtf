# Fine-tuning the local Qwen model for PS1 matching

Status: plan only, nothing built yet. Written 2026-10-18. All of this runs on the M5 Ultra (256 GB), not on the
laptop (which has about 22 GB of free disk).

## Goal

A cheap local model that matches noticeably more functions than it does today, working next to Codex. It is not
meant to replace Codex. Success means more matches per hour for the local model, measured by the app's own judge.

Baseline on SLUS-01252, from the app's database:

| Model | Period | Matches per 100 compiles | Matches per hour | Failed compiles |
| :-- | :-- | --: | --: | --: |
| Qwen (local, oMLX) | after the maspsx fix | 1.7 | about 11 | 25% |
| Codex | after the maspsx fix | 20 | about 93 | 2% |

Measure Qwen again after the template routing in `b1e8d97` and before any fine-tuning. Templates alone may move the
baseline.

## The task the model learns

It's the task the app already gives agents: a claim brief goes in, matching C comes out. Training prompts must
mirror the brief exactly, so the tuned model works without changes to the app:

- `### Target assembly`, in splat/spimdisasm style passed through `scoring.compact_asm`
- `### What context.h declares for it`, from `network.header_excerpt`
- `### Looks like` plus the template's matching C, when a matched look-alike is 60% or more similar
- the compiler line: PSY-Q GCC 2.7.2 or 2.8.1, `-O2 -G8`, maspsx with ASPSX 2.77

The answer is the function's own definition plus the declarations it needs (`rom.export.own_source`), as plain C.

## Data

### A. This game (from the app's database)

- **Matched pairs:** about 230 functions written by agents. Leave out the roughly 1,450 autopilot machine drafts by
  default (`/* machine draft (m2c)` sources); their `M2C_FIELD` style is not what we want the model to learn.
  Optionally include drafts that agents later rewrote.
- **Trajectories:** about 2,300 Codex attempts. Each example is the brief, the previous attempt and its diff in, and
  the next attempt out. Keep only steps that improved the score. Diffs weren't stored, so recompute them by
  compiling the previous attempt (a couple of minutes).
- **Hold-out set:** about 200 functions kept out of training, for evaluation only. Group functions by their
  mnemonic sequence (twins and close look-alikes go in the same group) so no near-copy leaks across the split.

This needs the laptop's `~/Decomp/.decomp/decomp.db` and the `slus-01252` project folder. Either copy them to the
M5 Ultra, or run the export on the laptop and copy the JSONL over (it's small).

### B. Public PS1 decomps (the bulk of the data)

Repositories that use the same toolchain (PSY-Q GCC with maspsx):

- Xeeynamo/sotn-decomp (Castlevania: Symphony of the Night)
- Xeeynamo/croc (Croc: Legend of the Gobbos)
- TheMobyCollective/spyro-1
- Vatuu/silent-hill-decomp
- MediEvilDecompilation/medievil-decomp
- fuerchter/rayman-ps1-decomp
- FedericoMilesi/soul-re (Legacy of Kain: Soul Reaver)
- mkst/esa (Evo's Space Adventures)
- open-ribbon/open-ribbon

**How the harvest works:** none of these repositories includes the original games' files, and none are needed.

1. Shallow-clone a repository: `git clone --depth 1`.
2. Remove the `INCLUDE_ASM` and `INCLUDE_RODATA` lines from each `.c` file. Those functions aren't decompiled yet,
   and their assembly files only exist after splatting a game copy. Functions inside a `NON_MATCHING` `#else`
   branch stay out automatically.
3. Compile each `.c` file as a whole with our pipeline (GCC, maspsx, as 2.40) and the repository's include paths.
4. Disassemble the object with `spimdisasm elfObjDisasm`, so the assembly looks the way it does in the briefs.
5. Pair each function's assembly with its C definition (`own_source`), and build the declarations section with
   `header_excerpt` from the preprocessed file.
6. Skip files that don't compile. Skip functions with inline assembly or GTE macros, empty functions, and functions
   over about 400 instructions.

Every pair is consistent by construction: the C compiles to exactly that assembly with our flags, even where they
differ from what the original project used. Use GCC 2.7.2 for most of it and GCC 2.8.1 for some, to match the
target game's mix.

Expected yield: tens of thousands of functions written by people. Deduplicate exact repeats across repositories.
Record the repository and commit in each example's metadata. These projects' C is game code their teams published;
keep the dataset and model private.

### C. Format

Chat-style JSONL, the format `mlx-lm` LoRA training reads:

```json
{"messages": [{"role": "system", "content": "..."},
              {"role": "user", "content": "<brief>"},
              {"role": "assistant", "content": "<C>"}],
 "meta": {"source": "sotn-decomp@<commit>", "function": "...", "group": "<hash>", "toolchain": "gcc-2.7.2-psx"}}
```

Write the train and validation split by `group`, never by line.

## Model and training

- **Base model:** start from the base weights of the model oMLX serves today ("Qwen3.8-Flash-Next"), not the
  quantized build. First check that its architecture is an MoE and that `mlx-lm` LoRA supports it. If it doesn't,
  use a dense Qwen coder model of about 30B parameters, which 256 GB handles comfortably.
- **Training:** LoRA with `mlx_lm.lora`. Starting points: rank 16 to 64, learning rate 1e-4 to 2e-4, 1 to 3 epochs,
  sequences of 4k to 8k tokens.
- **Order:** public data first, then a short final pass on this game's agent-written matches and trajectories.
- **Serving:** fuse the adapter (`mlx_lm.fuse`), quantize to the format served today, and load it in oMLX as a new
  model name. In decomp.wtf, add it as a second endpoint and compare it with the current model.

## Evaluation

1. **Offline:** one shot per hold-out function. Send the brief, take the C, judge it with the app's compile-and-diff
   (`try_quietly` on a database copy). Report: matched on the first compile, matched within 4 compiles (feeding
   diffs back), average best score, and failed compile rate. Run the current model and the tuned one on the same
   set.
2. **Live:** run the tuned model as the app's API agent next to Codex for an hour. Compare matches per hour and
   failed compiles with the baseline above.

Ship it if both improve. Otherwise look at the failures by type before changing the data.

## To build in the fork (on the M5 Ultra)

- `src/decompnet/training.py`, with:
  - `export_project`: the pairs, trajectories and hold-out set from a project's database
  - `harvest_repo`: the public-decomp pipeline above
  - `evaluate`: the offline evaluation against an OpenAI-compatible endpoint
- CLI: `decomp training export <slug>`, `decomp training harvest <repo-url or path>`,
  `decomp training eval --endpoint ... --model ...`
- `scripts/train_lora.sh`: the `mlx_lm.lora` and `mlx_lm.fuse` commands with the settings above
- The toolchains are already in the registry with macOS arm64 builds (`gcc-2.7.2-psx`, `gcc-2.8.1-psx`, `maspsx`,
  `binutils-mips`). The arm64 GCCs need `-w` and crash on some error messages (handled by `compiler_config` and
  `_explain_crash`), so the harvest should treat a crash as a file to skip.

## Later

- Retrain cheaply as matches accumulate: every few hundred new agent-written matches, export again and run a short
  LoRA pass.
- If public pairs aren't enough, decomp.me scratches are another source, but check their terms first.
