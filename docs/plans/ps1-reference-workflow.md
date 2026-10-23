# PS1 reference and diagnostic workflow

The hard remainder needs examples tied to a compiler and SDK variant, and experiments with retained inputs.
These additions do not rewrite game sources, accept external code as a match, or change claim ownership.

## Agent tools

- `find_reference_examples(function, query, limit=3)` returns recorded local C/assembly matches, their
  stored compiler recipe, and pinned external candidates. Local status is historical; it is not a fresh
  replay. External source is unverified for this target. Search by SDK symbol or an assembly pattern.
- `audit_call_boundaries(function)` checks the current candidate's calls against context.h and matched
  callee declarations. It reports lexical discrepancies and explicitly labeled assembly heuristics.
  An empty warning list is not proof of a valid ABI. Investigate against the target before editing types.
- `progress_report(hours=1)` reports exact bytes/functions, partial score drops, repeated sources and
  suspect reviews. Partial scores are penalties, not byte coverage. Session-hour rates require measured
  run intervals; old claims or submission timestamps are not used as duration estimates.

These tools are registered for both MCP clients and built-in API agents. PS1 briefs for functions with
at least ten attempts point agents to the reference and call audit tools. They require the app to load
the new code; an already running process is not hot-patched.

## Operator experiments

Run these from the repository with its Python environment. Output paths should be new so previous
evidence is retained. Proprietary SDK tools are not downloaded by the diagnostic scripts.

```sh
.venv/bin/python docs/plans/ps1-toolchain-probe.py /path/to/new-toolchain-results
.venv/bin/python docs/plans/ps1-sdk-pilot.py --source-db /path/to/decomp.db \
  --output /path/to/new-sdk-results
.venv/bin/python docs/plans/ps1-prototype-audit.py /path/to/decomp.db /path/to/new-audit-results
.venv/bin/python docs/plans/decomp-progress-report.py /path/to/decomp.db \
  --project slus-01252 --baseline /path/to/frozen-baseline.db \
  --output /path/to/new-progress-report.json
```

The compiler probe uses four frozen C reductions per selected installed lane. Its manifest records
source and tool hashes, commands, outcomes and differences. An optional original-ASPSX command must be
supplied explicitly; missing original tools remain an unperformed check. Microcases have no retail
target and cannot earn game match credit.

The prototype audit opens SQLite read-only and retains source, header and assembly copies with its
report. Its lexical reader does not resolve preprocessing, typedef equivalence, indirect calls or K&R
definitions. Reports are investigation aids, not automatic repairs.

Progress reports distinguish stored-snapshot deltas from window activity. If headers or other inputs
changed, a stored lower score needs recompilation before it can establish an improvement. The run
timer records start/finish events around each managed CLI/API run, including tool and I/O time.
Interrupted runs remain gaps; telemetry errors are logged without stopping the agent. Measured rates
include only output covered by completed measured intervals.

## External references

The optional local index is `<workspace>/.decomp/ps1-reference-index.json`. It points to files in an
operator-controlled source directory, with repository, exact commit, path, SHA-256, symbol and license.
Search checks file hashes and bounds reads; it does not fetch or execute repository content. Pinned
upstream files and game evidence stay outside this repository. Preserve the upstream license and
attribution if source is subsequently incorporated into a game decompilation.

Useful primary references are [Sozud's PSY-Q decomp](https://github.com/sozud/psy-q-decomp),
[PsyZ](https://github.com/Xeeynamo/psyz), and
[Symphony of the Night](https://github.com/Xeeynamo/sotn-decomp). A shared function name is a candidate
mapping, not proof of the same SDK version. Use `test_hypothesis` for bounded target-specific trials.

## First measured run (2026-10-23)

- SDK pilot: five incumbent controls plus five source-assisted candidates on a disposable database copy.
  All candidates compiled; zero exact matches. `_SsGetSeqData` improved from penalty 4380 to 3980 under
  the same frozen context; the other four candidates worsened. No candidate was promoted to live state.
  See [source provenance](../ps1-references/sources.json) and
  [pilot results](../ps1-references/pilot-results.json).
- Compiler reductions: sixteen requested compiles, sixteen successful, across four installed recipes.
  ASPSX emulation versions changed final code in every case. The modulo and quotient forms generated
  identical code within each recipe. No original ASPSX executable was supplied or run.
- Call audit: caught `func_80104228` calling `func_800F5120()` while the saved callee declares one pointer
  argument, consistent with the separately recorded native-port failure. No prototype was rewritten.
- Existing live agents continued running. The metadata index was installed for the next app startup;
  run timing and new tools require loading the updated application code. No historical agent-hours were
  inferred from claim leases or submission gaps.

Raw evidence is kept privately under
`/Users/ianderse/Decomp/reviews/slus-01252-20261023/`, including the SDK snapshot and pinned upstream files,
compiler artifacts, audit inputs and progress JSON. Game source and upstream implementation files are
not bundled with this code change.

Validation: 82 tests passed, 15 skipped for unavailable prerequisites, and one known missing-demo fixture
test was excluded after reproducing its identical failure on untouched HEAD. Coverage includes real
MCP HTTP calls for all three new tools, unchanged claims/attempts after reads, index hash/path validation,
prototype warnings and output bounds, and interrupted/error run-timing behavior. Python compilation and
`git diff --check` also passed. These checks establish delivery, not a sustained matching-rate improvement.
