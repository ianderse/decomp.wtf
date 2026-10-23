# PS1 reference evidence

`find_reference_examples(function="...", query="...", limit=3)` searches recorded local
PS1 matches and an optional local external index. Local examples include matching C,
target assembly, the current function-specific compiler/flags/assembler recipe,
the source hash, and the matching attempt/context version when available. A recorded
match is historical evidence; this read-only search does not replay the compiler.
Historical tool executable hashes were not stored, so the output says so.

Function-based retrieval prefers the same current compiler recipe and similar
instruction sequences, then unit and size. The mnemonic score is a retrieval aid,
not evidence of byte equivalence. Explicit queries search symbols, units, C and
assembly. Output is limited to five examples and bounded code/assembly excerpts.

External candidates are always **unverified for the target**. An index needs a
repository URL, full commit SHA, license, symbol, relative source path and SHA-256
for each example. Search verifies the local bytes before returning an excerpt and
rejects files escaping the configured root. It never downloads sources. The
default index is `<workspace>/.decomp/ps1-reference-index.json`, falling back to the
database directory for a plain `Network`. `search_examples(..., external_index=...)`
also accepts an explicit path. A local match and an external candidate can appear
together; at least one external slot is reserved when both exist and limit > 1.

The installed local index points at preserved external evidence; no upstream C,
SDK binary, ROM bytes or extracted target assembly is bundled in this repository.
The catalog and result JSON files contain metadata only. Preserve upstream license
files and per-file SPDX notices when using or adapting source.

To reproduce the bounded pilot and generate an index in its new evidence folder:

```sh
.venv/bin/python docs/plans/ps1-sdk-pilot.py \
  --source-db /path/to/decomp.db --output /path/to/NEW-pilot-directory
```

`--references-dir /path/to/previous-fetch-directory` reuses hash-checked pinned
sources. `--index-output /path/to/workspace/.decomp/ps1-reference-index.json`
also installs the metadata for retrieval. Existing output directories and existing
index files are refused; preserve or explicitly merge any prior configuration.
Reproducing this command is a new five-candidate experiment, not a resume/tuning
loop. The original run's sources, failures and denominator remain frozen.

## Pinned source findings

[sources.json](sources.json) pins three primary repositories and the five explicit
target correspondence hypotheses. It records these material differences:

- `sozud/psy-q-decomp` uses GCC 2.6.0 and PSY-Q 3.5 ASPSX in its Makefile, with
  comparisons against the 3.5 LIBSND archive. Its repository license is MIT.
- `Xeeynamo/psyz` has MIT-licensed `decomp/` source, a GCC 2.7.2 / ASPSX 2.56 build
  and additional flags. Several relevant MIDI functions are `INCLUDE_ASM`
  placeholders, so their declarations cannot serve as recovered C examples.
- `Xeeynamo/sotn-decomp` has a mixed licensing boundary: the repository is AGPL,
  while the selected libsnd C/header files carry explicit MIT SPDX notices. Its
  PS1 build defaults to ASPSX 2.34. Its sequence struct differs from the target;
  a similar compiler recipe does not establish SDK-version identity.

The local evidence fetch preserved 267 source/license/build metadata files, pinned
their commits and file hashes, and indexed 303 actual C definitions: 71 sozud,
95 psyz and 137 sotn. Assembly placeholders are excluded. Index tags connect the
five generic target names with upstream canonical symbols as hypotheses.

## Bounded pilot result

The [standalone pilot](../plans/ps1-sdk-pilot.py) opens the source database through
SQLite `mode=ro`, creates a backup, and operates on that copy. It freezes five
functions, five incumbent controls and exactly one source-assisted candidate per
function. It does not claim functions, record attempts, tune candidates, try
alternate compilers, rename symbols or adopt results. Existing output directories
are refused so failures and denominators remain visible.

The run on 2026-10-23 compiled and linked all five candidate objects using each
function's stored GCC 2.7.2 / ASPSX 2.34 recipe and the target's shared `SeqStruct`.
Candidates were extracted from pinned MIT-licensed `seqread.c`, adapted only with
the documented identifier/field mapping and local prototypes. No shared context
was changed. The full compile/link path uses `RomNetwork._compile` and `_evaluate`,
including target addresses, relocations, symbols and ROM reads on the snapshot.

| Target | Upstream hypothesis | Incumbent score | Candidate score | Exact match |
| --- | --- | ---: | ---: | --- |
| `_SsGetSeqData` | `_SsGetSeqData` | 4380 | 3980 | No |
| `func_8016BF80` | `_SsSetProgramChange` | 340 | 830 | No |
| `func_8016C988` | `_SsContRpn1` | 340 | 830 | No |
| `func_8016C9F4` | `_SsContRpn2` | 340 | 830 | No |
| `func_8016D370` | `_SsSetPitchBend` | 1000 | 1705 | No |

Scores are lower-is-better instruction-diff penalties, not percentages. The
source-assisted arm improved one of five controls and matched zero of five.
The remaining four candidates regressed and were retained as negative evidence.
The `_SsGetSeqData` improvement is a reviewable candidate, not a recovered original
or a live match. No source, score, claim or agent setting was changed on the live
database. Existing frozen evidence was preserved in place; this run added a new
derived subdirectory.

[pilot-results.json](pilot-results.json) records snapshot/report hashes, exact
scores and local evidence locations. Raw upstream bytes, licenses, candidates,
controls, contexts, compiler logs, objects and assembly diffs are preserved under:

`/Users/ianderse/Decomp/reviews/slus-01252-20261023/sdk-reference-pilot-20261023/`

The next boundary is one bounded structural investigation of the improved
`_SsGetSeqData` candidate against the target's register/prologue/indexing sequence.
Exact byte equality plus semantic/source review and replay remain the adoption
gate. No evidence here proves an exact 2.6/3.0 SDK-source variant or authorizes
treating upstream layouts as interchangeable with the target's 0xA8 record.
