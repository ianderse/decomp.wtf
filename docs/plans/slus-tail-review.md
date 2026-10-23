# SLUS-01252 hard-tail workflow — 2026-10-23

All five review recommendations are implemented locally. The live database contains 337 measured diagnoses and two candidate reviews. One active claim (`func_800EE774`) was preserved. The application was offline during delivery; its saved Codex helper profiles now use tiered routing on their next normal launch. No helpers were started by this work.

## What changed

1. **Persistent diagnosis and exhaustion.** Append-only diagnoses retain compiler/header/target/source evidence, lane, next hypothesis, and a waiting state. Automatic claims and both permuters skip exhausted functions under unchanged inputs. A changed header, compiler build, target, incumbent, or candidate review invalidates that state. Named claims remain possible, but an exhausted function needs `test_hypothesis` or a new evidence-backed diagnosis before compiling. Historical attempts do not consume the current claim's patience.
2. **Independent candidate review.** `inspect_candidate` and `review_candidate` separate the lowest instruction-diff source from a reviewed plausible source. Reviews recompile the candidate under current inputs and preserve reviewer evidence. They do not certify semantic equivalence. Briefs and the function editor show the reviewed candidate; suspect candidates remain preserved. Automated searches do not seed from a suspect source or a stale reviewed source.
3. **Mismatch routing and bounded alternatives.** Claim strategies include `stack`, `allocation`, `structural`, `library`, and `linking`. Actual diff markers classify new incumbents. Stack, linking, and library cases leave generic permutation queues. `test_hypothesis` records inputs and outcomes and deduplicates identical source/environment submissions, including concurrent requests. Interrupted reservations are retained rather than silently retried. `test_alternatives` tests at most eight explicit replacements of one unique source region, all from the same saved attempt.
4. **Compiler investigations.** Exact GCC/maspsx/as commands, source/header inputs, assembly, linked diffs, and GCC `-da` intermediate passes are retained for two representative functions. Four standalone reductions reproduce the relevant compiler behavior; they have no original target and do not count as matches.
5. **Strong models with normal patience.** Existing hard-function escalation remains. A function with many unsuccessful attempts now escalates even when it has no score. Diagnosed hard work also uses the strongest configured tier. No global patience increase was introduced. Both saved SLUS Codex helper profiles use tiered routing. The templates lane used by local-model helpers skips functions with 30+ attempts or 3+ abandonments.

## Measured inventory

The frozen baseline has 4,001 matched functions (610,088 bytes), 338 open functions (176,784 bytes), and 471 separately classified assembly entries (83,772 bytes). Every open incumbent was compiled once against its exact current recipe and linked against the ROM on a disposable database copy: 338 functions in 9.34 seconds with four compiler workers.

| Category | Functions |
|---|---:|
| Structural / mixed differences | 188 |
| Register allocation / scheduling | 92 |
| Stack offsets | 4 |
| Linking / encoding | 1 |
| libsnd regional investigation | 50 |
| Compile blockers | 3 |

The three compile blockers are stale local declarations: `g_primPtr` in `func_800279FC`, `POLY_FT4` in `func_80028560`, and `func_8014CF6C` in `func_80145FC4`. Their live diagnoses route them to structural work with the specific declaration conflict. No shared declaration was changed.

`func_80149820` retains its score-60 source as suspect (attempt 139913). Attempt 141255, freshly measured at 357, is the reviewed plausible starting point. Inspection confirms that it removes the known uninitialized conditions, initializes both product paths, and preserves separate x/y products. This is not a complete equivalence proof.

## Compiler boundaries

- **func_800E4384:** baseline score 2, frame 112, argument spill at sp+56 instead of target sp+48. Removing the reserve fixes the spill but reduces the frame to 104 and scores 22. The `.greg` dump identifies the spill/reload placement. A standalone pressure loop similarly grows its frame from 88 to 96 when an eight-byte volatile reserve is added. The unexplained boundary is the target's additional eight bytes *after* the natural spill, not ordinary reserve size.
- **func_800E6C90:** baseline score 10, 525 instructions. In `.lreg`, the high product and shift remain pseudo registers; `.greg` maps the high product to HI and inserts instruction 1378, HI → t0, before the shift into v1. An eight-line grid loop reproduces `mfhi $8; sra $3,$8,3`. Computing the remainder from the quotient leaves this sequence unchanged. The next investigation can use this small reproduction to study reload scratch selection.

Both functions are paused in automatic rotation until new inputs or an explicit recorded investigation. Neither is classified as impossible to express in C.

## Bounded pilot and validation

The twelve-function pilot used real MCP over localhost HTTP with the actual compiler and ROM linker on a frozen database copy. It made 24 unique trials, avoided 14 duplicate compiles, and produced **zero improvements and zero matches**. It is a workflow/integration pilot, not evidence of higher matching yield or a controlled model comparison. An initial stop when a source lacked the assumed simple declaration was retained; the pilot resumed against the same database and denominator. Two recovery replays account for the extra duplicate avoidances. All twelve claims were released, and the HTTP server stopped cleanly.

Reproduction commands (private game artifacts remain outside the repository):

```sh
.venv/bin/python docs/plans/slus-tail-inventory.py BASELINE_DB NEW_OUTPUT_DIR
.venv/bin/python docs/plans/slus-tail-pilot.py BASELINE_DB OUTPUT_DIR
.venv/bin/python docs/plans/slus-reload-repros.py NEW_OUTPUT_DIR
```

The pilot resumes an existing output directory and its retained ledger; use the original baseline, not a newer snapshot. The fixed budget covers 24 unique tool trials and four full-function diagnostic compiles. Standalone reductions are a separate four-compile investigation.

Validation: 50 focused tests passed, 27 platform-dependent tests skipped, and one known failing fixture test was deselected. Tests cover persistence/reopen behavior, source review separation, lane selection, strong-tier routing, bounded alternatives, concurrent duplicate suppression, interrupted records, replay after release, and a real HTTP tool session with clean shutdown. Platform-dependent demo tests remain skipped; the existing small-context fixture failure and Linux-GCC demo failure also reproduce on unmodified HEAD.

Scheduler lookup on a database copy, same selected function: warm median 1.29 ms before / 12.65 ms after; maximum 1.58 / 13.59 ms over 25 warm lookups. Cold lookup 2.49 / 19.58 ms. The extra evidence checks add roughly 11.4 ms to the median lookup in this measurement. This is a scheduler measurement, not full UI responsiveness acceptance.

Private evidence: `/Users/ianderse/Decomp/reviews/slus-01252-20261023/` contains immutable baseline/before-apply backups, inventory diffs, raw tool logs, pilot database/results, compiler traces/reductions, baseline test failures, and live application records. No matched source, raw game capture, or active claim was rewritten. The next efficacy boundary is a prospective run of the updated agents: the current pilot does not establish increased match rate.
