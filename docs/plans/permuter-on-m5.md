# decomp-permuter on the M5 Ultra, for the laptop's near misses

Status (2026-10-19): automated. Set **Settings → Permuter machine** to the M5's SSH name (it must be reachable
without a password: `ssh-copy-id`) and the app does the rest every 20 minutes (app/remote_permuter.py): results come
back and through the judge, and a fresh batch of near misses starts there whenever the permuter isn't running or its
batch is 12 hours old (with `--stack-diffs`, so stack offsets count, as they do for the judge). The first hand-run
batch matched 50 functions. The steps below are what it automates, for doing it by hand.

## Why

On SLUS-01252, 380 open functions are within 5% of matching (about 110 KB of code), each after ~44 attempts by
agents. The compiler survey found no compiler setup that matches them. What's left is mostly register allocation
and statement order: decomp-permuter's job, and CPU-bound, so the M5 Ultra's cores are the place for it.

## On the laptop: export

```sh
uv run decomp permuter export slus-01252 ~/permuter-slus
rsync -a ~/permuter-slus/ ians-mac-studio:permuter-slus/
```

(`--max-miss 0.05` and `--limit 400` are the defaults.) One directory per function: `base.c` (the best attempt, preprocessed with the shared header), `target.o` (the
original instructions, assembled), `compile.sh` (decomp.wtf's exact pipeline for that function: its own compiler,
maspsx, modern GNU as), `settings.toml`, and `decomp.json` (what the import needs). The folder holds game code:
keep it on your own machines.

## On the M5 Ultra: once

```sh
cd ~/decomp.wtf && git pull
uv run python -c "from decompnet import toolchains; [toolchains.ensure(t) for t in ('gcc-2.6.0-psx', 'gcc-2.6.3-psx', 'gcc-2.7.2-psx', 'gcc-2.8.0-psx', 'gcc-2.8.1-psx', 'maspsx', 'binutils-mips')]"
git clone --depth 1 https://github.com/simonlindholm/decomp-permuter ~/decomp-permuter
cd ~/decomp-permuter && uv venv && uv pip install toml pycparser Levenshtein
```

## On the M5 Ultra: each batch

```sh
sh ~/permuter-slus/setup.sh
cd ~/decomp-permuter
.venv/bin/python permuter.py ~/permuter-slus/*/ -j 20 --best-only
```

`setup.sh` points the directories at this machine's MIPS objdump and prints `ready: <n> functions`. (Paste commands without trailing `# comments`: zsh doesn't treat `#` as a comment when typing commands unless `setopt interactivecomments` is on.) The permuter works on all the directories at once and writes each improvement as `output-<score>-<n>/source.c` inside the
function's directory. Let it run overnight (stop with Ctrl-C at any time: results so far are kept). `-j 20`
leaves cores for Qwen (oMLX) and Hindsight; use fewer while the laptop's agents are busy.

## Back on the laptop: import

```sh
rsync -a ians-mac-studio:permuter-slus/ ~/permuter-slus/
uv run decomp permuter import slus-01252 ~/permuter-slus
```

Each function's best candidate goes through the app's own judge, as the `permuter` agent: a real match is saved as a
match, a lower score becomes the function's best attempt (agents start from it), anything else is only recorded as
an attempt. Safe while the app runs. Import as often as you like during a run.

## Checked (2026-10-19, on the laptop)

- `compile.sh` builds `base.c` and its objdump lines up with `target.o`'s; decomp-permuter runs on the directories
  with our pipeline (30 iterations in a few seconds on a 95-instruction function).
- The import splices a candidate's function back into the agent's source and judges it.
- A function whose judge score is 1 (the same instructions, different bytes: a relocation or a data declaration)
  scores 0 in decomp-permuter: it can't help those. Only 1 of the 381 near misses is like that.

## Ideas for later

- If a batch finds matches, run the import on a schedule and export the next batch (5-15% off) automatically.
- decomp-permuter's `PERM_` macros let an agent suggest variations to try (`PERM_GENERAL(a, b)`): agents could
  write those for a near miss instead of compiling each variant themselves.
