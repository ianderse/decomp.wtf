# Runtime progress workflow

Use `decomp workflow --help` for the executable interface. Each project opts in with
`<project>/runtime-workflow.json`; `slus-01252-runtime.json` is the initial SLUS plan.
The active project file, live database, and captured scenario results are the current
state. Old narrative run descriptions remain historical evidence.

## Bounded runtime priorities

Each blocker specifies a function, `exact` or `port` lane, milestone, evidence and
next hypothesis. An active exact blocker also requires `attempts_at_start` and
`max_attempts`. Initialize the baseline from the live function's attempt count.
A priority is not a claim: the scheduler preserves held work, region restrictions,
release cooldowns and other agents' claims. Active runtime priorities are explicit
investigations and may revisit an exhausted function by name; the existing exhausted
compile gate still requires `test_hypothesis` or a new measured diagnosis. Compiles
stop when the investigation's attempt budget is consumed. Updating a budget requires
new evidence in the project plan; do not reset it simply to keep a loop running.
Port blockers never enter the exact-match queue. Matched status does not resolve
port compilation or runtime acceptance. `GET /api/workflow?slug=SLUG` exposes the
plan and budget-eligible names (an eligible name may still be claimed or cooling down).

`func_80155B6C` already has a local declaration-order patch. Preserve and verify it.
`func_801668AC` starts with eight additional attempts for the New Game constructor.
`func_80156FF0` remains waiting for a new hypothesis. No shared declarations or
incumbent sources are replaced by installing the plan.

## Repeatable port scenarios

Build an isolated copy of the exported tree with the existing port patches. Run:

```sh
decomp workflow run /path/to/isolated-project intro /path/to/new-evidence/intro
decomp workflow run /path/to/isolated-project title /path/to/new-evidence/title
decomp workflow run /path/to/isolated-project card-absent /path/to/new-evidence/card-absent
decomp workflow run /path/to/isolated-project card-fresh /path/to/new-evidence/card-fresh
decomp workflow run /path/to/isolated-project new-game /path/to/new-evidence/new-game
```

Each output directory must be new. The runner keeps raw logs, numbered screen captures,
WAV output, the input script, environment, port metadata, and SHA-256 identities for
the executable, source, runtime, generated code, patches, CMake cache and disc. It
clears inherited `PSX_*` settings, uses a headless run with an internal timer and an
external watchdog, and creates fresh memory cards only inside that run's directory.
A signal, insufficient vblanks, missing read/decode evidence, or missing captures fails
the scenario. Survival yields `needs_visual_review`, never automatic game acceptance.

Inspect the numbered images against `expected_screen`, then record the observation:

```sh
decomp workflow review /path/to/new-evidence/title accepted \
  --reviewer YOUR_NAME --evidence 'Title menu visible in screen-001500.png; no fault.'
```

Reviews are separate, create-only records bound to the result and capture hashes.
A failed runtime check cannot be accepted. Corrections require another run directory.
Audio captures permit listening/comparison; nonzero samples alone do not prove fidelity.

## Diagnosis validity

Briefs and reports name changed inputs: header, context, compiler, compiler binaries,
target, relocations, symbols, GP, applicable region, incumbent or candidate review.
An unrelated region assignment no longer reopens an exhausted diagnosis, including
legacy records. Full compiler/trial identities remain unchanged for deduplication.
The project-wide symbol stamp deliberately remains conservative: a target-only
relocation list does not cover macros or references introduced by future candidates.
No historical diagnosis is rewritten; each report freezes observed validity reasons.

## Consolidated evidence

```sh
decomp workflow report /path/to/decomp.db slus-01252 /path/to/new-report \
  --baseline /path/to/frozen-baseline.db --since UNIX_TIMESTAMP \
  --runs /path/to/new-evidence
```

The command first takes a consistent SQLite backup, including committed WAL data.
It writes `snapshot.db`, `report.json`, and `report.txt`; existing destinations are
never overwritten. Reports separate stored exact matches, historical partial score
drops and their review status, live blocker state/budget, diagnosis invalidations,
and runtime scenarios/reviews. Changed targets and changed inputs are not silently
credited as comparable improvements.

Agent timing uses paired run events. Background-process/controller timing has its
own denominator and includes idle waits; it is not CPU time or agent time. Remote
permuter polling records running/not-running observations, not guessed process bounds.
Open/interrupted sessions and uninstrumented history remain explicit gaps. Deployment
requires restarting the app; its existing helper profiles resume normally. Verify
`/api/workflow`, worker start events, helper sessions, and claims after restart.

For subsequent normal exports, `decomp port slus-01252 --build --scenarios` rebuilds
and runs every configured scenario, retaining evidence under the project's
`runtime-runs/<unique-run>/`. Supply scenario names after `--scenarios` to select a
subset. A failed runtime check makes the command fail; successful automated checks
still print `needs_visual_review` until their captures are reviewed.

## Deployment evidence, 2026-10-26

Installed the plan at `~/Decomp/slus-01252/runtime-workflow.json`, restarted the app,
and verified `/api/workflow` and all three resumed Codex helper sessions. A clean
shutdown check found zero remaining claims. Local background workers now record
start/finish events, with remote-controller observations kept separately.

Private evidence is retained under `~/Decomp/reviews/workflow-20261026/`:
`report-delivery/report.txt` and `report.json` are the consolidated snapshot;
`runs-verified/` contains the corrected five-scenario replay and image-bound reviews.
Intro video, title, absent-card and fresh-card screens were visually accepted.
New Game ran for 35 seconds / 2,097 vblanks and read SG00.STG, with no fault, but its
screen at vblank 1,800 was black: visual acceptance was rejected. The existing
func_80155B6C patch compiled. This does not prove func_801668AC causes the black screen.
The constructor investigation retained its reviewed score-1041 baseline after seven
new unsuccessful compiles; its next specific hypothesis and remaining budget are in
the live plan/report. No new exact match was claimed for this investigation.

The initial validation build accidentally omitted SDK metadata because a standalone
SQL query quoted `asm` as an identifier. Its failed replays remain in `runs/`, and the
corrected build is separate at `game-verified/`; application export SQL needed no fix.
Audio WAVs were retained, but sound fidelity was not auditioned. Validation covered
97 distinct focused tests across the implementation runs, including real localhost
MCP transport, priority/cooldown/budget behavior, legacy diagnosis validity, immutable
snapshots, process isolation, review binding, port generation and card/call handling.
