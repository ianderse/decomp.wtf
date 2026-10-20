# Where the efficiency comes from: experiments on SLUS-01252

Status (2026-10-19): the framework is built (experiments.py; the project page's Experiments panel).

Results so far:
- **Whole-game compiler survey (step 1), on the M5:** 1,152 open functions' best attempts, each with every PS1
  compiler setup: **none matched**. What's left on SLUS-01252 is source-level (registers, statement order, types),
  not the compiler or assembler; no alternates experiment needed.
- **Tier experiment (step 2):** 2026-10-19 18:15 to 2026-10-20 08:20, stopped early at about 150 functions per arm
  (the routing changed because of it). Matched before the stop: **auto 58.9%** [51, 66] (151), **strong 75.2%**
  [68, 81] (165), **cheap 61.6%** [54, 69] (146). Strong vs auto: **+16.2 points [6, 26]**, clear; cheap vs auto:
  +2.7 [-9, 13], too noisy. (Stopped early, so the strong arm's lead is somewhat overstated; it's large enough to
  act on.) Cost per match wasn't measured: Claude Code reports its cost when a session ends, after its claim, and
  the charge went nowhere (fixed: it goes to the function the agent claimed last).
  **Acted on:** functions of 150+ instructions go to the strongest tier from the first try, and every other tiered
  session takes the biggest open function (agents.py BIG_INSNS, the "biggest" claim order).
  **Next:** with cost recorded, a tier experiment on the smaller functions (auto vs strong) to see whether the
  strong tier pays for itself there too.

## What's built

- **Experiments**, one factor at a time: `memory` (recalls in the brief or not), `tier` (the tiered helpers' model:
  as the tier table says, always the strongest, always the cheapest), `permuter` (the permuters work on the function or
  leave it alone), `alternates` (near misses tried with the game's other compiler setups or not).
- A function's arm is a hash of the experiment's seed and its id: fixed across claims, reproducible from the seed. It
  joins when an agent first claims it; it's a success if it matches before the experiment stops; what agents spent
  while holding it is its cost.
- The report: per arm functions, matches, rate and cost per match with bootstrap 95% intervals, the difference from
  the baseline (the first arm) with its interval, "too noisy to act on" while that interval includes 0 (or an arm has
  fewer than 30 functions), and the per-arm sample size a 5-point difference needs at the rate seen so far.
- `decomp survey <slug> --all --dry-run --json out.json`: every open function's best attempt with every PS1 compiler
  setup (41 here: each GCC, ASPSX 2.21/2.34/2.56, -O1, plain GNU as), nothing recorded.

## The arithmetic first

About 24% of the functions agents claim go on to match (the memory A/B's control arm: 78 of 325). To tell 24% from
29% (two-sided 5%, 80% power) takes **1,220 functions per arm**; 10 points takes 320, 15 points 147. SLUS-01252 has
1,191 open functions. So:

- **No 5-point question can be answered on this game alone**, even with two arms. Anything we act on has to be a
  difference of about 10 points or more, or a cost difference (cost per match varies far more between arms than the
  match rate: Opus and Haiku differ by an order of magnitude per token).
- **One stochastic experiment at a time.** Two at once split the same 1,191 functions and both get weaker (they're
  independent, so it's allowed, but each then needs the whole game).
- **Decide the size up front and stop there.** Looking every day and stopping at the first clear result overstates
  it.

The memory A/B so far: 21.2% with memory (250 functions) vs 24.0% without (325); the difference, -2.8 points, has a
95% interval of [-9.7, +4.1]. Too noisy to act on, and nothing points to a benefit.

## Proposal

1. **Compiler setups: no A/B needed, it's deterministic.** Run the survey on every open function (below, on the M5).
   It says exactly how many functions each setup matches from today's best attempts. The 600-function survey found no
   setup beyond today's alternates; the whole-game run settles it for the other 591, and tells us whether the 41
   compiles per near miss that `alternates` costs are worth it.
2. **Tier: the experiment worth running.** `auto` vs `strong` vs `cheap` with tiered Claude Code and Codex helpers,
   until each arm has 300 claimed functions (about 900 claims: most of the open functions). It answers the cost
   question (does the cheap tier match as often for less, does the strong tier pay for itself), with 10-point
   resolution on the match rate. Start it from the Experiments panel; stop it at 300 per arm.
3. **Memory: finish what's running.** Keep the memory A/B to 300 functions per arm (it has 250 and 325), then decide:
   if the interval still includes 0, turn memory off and save its recall time.
4. **Permuter: measure it directly, not by A/B.** Its matches are attributed (`attempts.via = 'permuter'`, the remote
   imports): what an on/off experiment would add is only whether agents would have matched those anyway, and with
   1,191 functions it can't tell that apart from noise. Not worth the functions it would take from the tier
   experiment.
5. After 1-3: implement only what the intervals support (at most two changes), and measure them again the same way.

## On the M5 Ultra: the whole-game compiler survey

On a copy of the database and the project (game data: keep it on your machines). From the laptop:

```sh
mkdir -p ~/survey-slus/.decomp
sqlite3 ~/Decomp/.decomp/decomp.db ".backup $HOME/survey-slus/.decomp/decomp.db"
rsync -a ~/Decomp/slus-01252/ ians-mac-studio:survey-slus/slus-01252/
rsync -a ~/survey-slus/.decomp/ ians-mac-studio:survey-slus/.decomp/
```

On the M5:

```sh
cd ~/Projects/decomp.wtf && git pull
sqlite3 ~/survey-slus/.decomp/decomp.db "UPDATE projects SET root='$HOME/survey-slus/slus-01252' WHERE slug='slus-01252'"
DECOMP_HOME=~/survey-slus uv run decomp survey slus-01252 --all --dry-run --workers 24 --json ~/survey-slus/survey.json
```

About 49,000 compiles; a few minutes at 24 workers. It prints how many functions each setup matches; `survey.json`
has which functions (`by_function`). A setup with matches there: the function can be judged again on the laptop
(each match is checked by the judge as usual, and by the rebuilt-game check after it).
