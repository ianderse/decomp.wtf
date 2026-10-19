# More PS1 compiler versions, built natively on macOS (on the M5 Ultra)

Status: 2.6.0 and 2.6.3 built natively on arm64 macOS (`docs/plans/psx-gcc-macos.sh <version> <old-gcc checkout>`), registered
in `toolchains.toml` and added to the alternates. The tarballs are committed in toolchain-builds/ and served from main. Measured on the laptop (2026-10-19): 208 of 1605 stuck best attempts on SLUS-01252 match with GCC 2.6.3
(2.6.0: the same ones; ASPSX 2.21 on top: none more), once `//` comments are turned into `/* */` for them
(`line_comments = false`: GCC 2.6 predates `//`, and agents' headers use it). Not yet done: everything Intel (Rosetta isn't installed on this machine; the arm64
build prints garbage in warnings, e.g. `warning: *aV makes integer from pointer`, so the Intel builds are still worth
it). 2.5.7 fails to build (`va_list` clash between its gvarargs.h and Apple's SDK); 2.8.0, 2.91.66 and 2.95.2 not tried.
Written 2026-10-19. Do this on the M5 Ultra; the laptop can keep running the app meanwhile
(see the end of this document).

## Why

The judge tries GCC 2.7.2 and 2.8.1 (PSY-Q), older ASPSX versions, `-O1` and plain GNU `as` on near misses. On
SLUS-01252 those variants matched about 110 stuck functions that already had correct C. PSY-Q shipped other GCC
versions too: 2.5.7, 2.6.0, 2.6.3, 2.7.2-cdk, 2.8.0, 2.91.66 and 2.95.2. Parts of a game, and especially Sony's own
libraries, can come from any of them. The PSY-Q signature scan on SLUS-01252 points at SDK 2.60, 3.00 and 4.70 (it
can't separate them), and the SDK-to-compiler table in SynapticDreams's `rom/psyq.py` maps those to GCC 2.6.x and
2.95.2.

decompals/old-gcc publishes Linux builds of all of them, but macOS builds only of 2.7.2-psx, 2.7.2-cdk and
2.8.1-psx. Those macOS builds are native arm64, and they crash on some warnings and errors (old K&R variadic code
under Apple's arm64 calling convention; see `no_warnings_on` and `_explain_crash`).

## Goals

1. **Native macOS builds of the missing PSY-Q GCCs:** 2.5.7-psx, 2.6.0-psx, 2.6.3-psx, then 2.8.0-psx,
   2.91.66-psx and 2.95.2-psx if they matter.
2. **Intel (x86_64) builds**, run under Rosetta like the N64 KMC compiler already is. x86_64's variadic calling
   convention is the one this code was written for, so warnings and errors should print properly. That would let us
   remove the `-w` flag and the crash-explaining fallback for PS1.
3. **The new versions as alternates** in the judge and the variant search, measured on SLUS-01252's stuck functions.

No Docker: these are native builds.

## Steps

1. **Set up the M5 Ultra:** `git clone https://github.com/ianderse/decomp.wtf`, then `uv sync`, then start it once
   with `uv run decomp --no-browser` and stop it again. That gets the toolchains folder and Python environment in
   place. You don't need to import a game there for steps 2–5.
2. **Get the build scripts:** `git clone https://github.com/decompals/old-gcc`. Read `gcc-2.7.2-psx-macos.sh` and
   `gcc-2.8.1-psx-macos.sh`: they download the GNU source, apply the Linux Dockerfiles' patches (`patches/`), swap
   `varargs.h` for `stdarg.h`, update `config.guess`/`config.sub` for Apple Silicon, then configure and build `cc1`
   with the system compiler.
3. **Write a script per missing version** (start with `gcc-2.6.3-psx-macos.sh`), adapting the closest script:
   - Use the matching Dockerfile (`gcc-2.6.3-psx.Dockerfile`) for which patches apply to that version.
   - Expect a few modern-clang errors per version: K&R prototypes, implicit `int`, `sys_nerr` declarations.
     `-std=gnu89 -Wno-error -Wno-implicit-function-declaration` in `CFLAGS` gets most of the way.
   - Build twice: native (`arm64`) and Intel (`arch -x86_64 bash gcc-2.6.3-psx-macos.sh`). For the Intel build,
     check that `/usr/bin/cc` targets x86_64 under `arch -x86_64`; it does with Xcode's toolchain.
   - Smoke test: `./gcc -B./ -S -O2 -G8 t.c` on a file with a warning (`int f(void){int x; x = (void*)0; return x;}`).
     The Intel build must print the warning instead of crashing.
4. **Package and pin:** tar each build (`gcc-2.6.3-psx-macos-x86_64.tar.gz`), publish it as a release asset of the
   fork, or contribute the scripts upstream to decompals/old-gcc. Record its sha256.
5. **Register it in `src/decompnet/toolchains.toml`.** Copy the `gcc-2.7.2-psx` entry: same `cmd` template
   (maspsx and modern `as`), `requires = ["maspsx", "binutils-mips"]`. Add a part per platform: `macos-arm64` and
   `macos-x86_64` with the new tarball, and `linux-x86_64` with decompals's existing Linux build.
   - If the Intel builds work, point the `macos-arm64` part of `gcc-2.7.2-psx` and `gcc-2.8.1-psx` at the Intel
     builds too, and drop `no_warnings_on` and `explain_with` from both entries.
6. **Add them to the alternates:** in `src/decompnet/network.py`, add the new ids to `_PSX_OLDER` (for example
   `gcc-2.6.3-psx`, `gcc-2.6.3-psx@2.21`). The judge's signature includes the alternates, so the next app start
   re-judges every stuck best attempt automatically.
7. **Measure before keeping each one:** the re-judge's event says how many matched. Each alternate adds compiles to
   near misses, so drop any version that matches nothing.
8. **Commit, push, pull on the laptop, restart.**

## Done when

- At least 2.6.0 and 2.6.3 build natively on macOS, pinned with checksums, and install through the app's
  Compilers & tools page.
- On SLUS-01252, the re-judge reports how many stuck functions the new versions matched.
- Optional: Intel builds of 2.7.2-psx and 2.8.1-psx print warnings properly, and the workarounds are removed.

## Keeping the laptop running meanwhile

Yes, that's fine. The two machines don't share a database: each app has its own `~/Decomp`. Things to know:

- **The laptop's agents depend on the M5 Ultra:** Qwen (oMLX) and Hindsight both run there. Compiling GCC uses all
  its CPU cores in bursts, so Qwen and memory recall will be slower while builds run. `nice -n 10 make -j8` keeps
  more room for them. The memory A/B results from build hours will look worse for memory if recalls time out.
- **Changes reach the laptop through GitHub:** push from the M5 Ultra, then `git pull --rebase` on the laptop and
  restart the app there. Don't pull mid-build if you want the laptop's app to keep running unchanged: a restart is
  only needed for the new code to take effect.
- **Don't push the same file from both machines at once.** Work on one machine at a time per change, or pull before
  editing.

## Automation (2026-10-19)

Two halves: the M5 Ultra builds compilers, and the app works out per game whether they help.

### In the app: the compiler survey (done)

Whenever the registry's set of PS1 compilers changes (a new build pulled in), the autopilot's first step on each PS1
game installs the missing ones and tries every PS1 compiler setup (each GCC as is, with ASPSX 2.21, 2.34 and 2.56,
at `-O1`, and with plain GNU `as`) on up to 600 of the game's stuck best attempts. The setups that match some become
that game's alternates (`compiler.alternates` in its config), most matches first. The re-judge that follows uses
them on every best attempt. Installing a PS1 compiler from Compilers & tools starts the survey too. So a new build
only has to be registered: no measuring by hand, and no list of alternates to maintain in code.

### On the M5 Ultra: the build loop

`docs/plans/psx-gcc-build-all.sh <old-gcc checkout> [versions]` runs `psx-gcc-macos.sh` for each version not built
yet, smoke-tests it (a plain function, one with a warning, one with an error), packages it into `toolchain-builds/`
and prints the `toolchains.toml` part with its checksum. Failures are logged in `toolchain-builds/logs/` and
skipped. `psx-gcc-macos.sh` knows only 2.6.0 and 2.6.3 so far: each other version needs its source URL and patch
list (from its Dockerfile in decompals/old-gcc) added to the two `case` blocks.

### The agent's task (paste into a Claude Code session on the M5 Ultra)

> In ~/decomp.wtf (pull main first): build the remaining PSY-Q GCC versions natively on macOS: 2.8.0, 2.91.66,
> 2.95.2, then 2.5.7, and, if Rosetta is installed, Intel builds of 2.6.0, 2.6.3, 2.7.2 and 2.8.1. Follow
> docs/plans/m5-psx-compilers.md. For each version: add its source URL and the patches from its Dockerfile in
> decompals/old-gcc (cloned next to the repo) to docs/plans/psx-gcc-macos.sh, run
> `docs/plans/psx-gcc-build-all.sh ../old-gcc <version>`, and fix what modern clang rejects (K&R prototypes,
> varargs/stdarg clashes with Apple's SDK, implicit int, sys_nerr declarations) until it builds and passes the smoke
> test. Then register it in src/decompnet/toolchains.toml the way gcc-2.6.3-psx is (the same cmd template, requires,
> no_warnings_on and explain_with if the smoke test says it crashes, line_comments = false before GCC 2.7), add it to
> COMPILERS and SHORT_NAMES in src/decompnet/app/engine.py, and commit the tarball and the entry. Don't add it to
> ALTERNATES in network.py: the app's compiler survey decides per game. Run the test suite before each commit
> (uv run pytest -q; compare with main's results, which have known failures), push to main on github.com/ianderse/decomp.wtf,
> and note in this file what built, what didn't and why. Give up on a version after a reasonable effort and say
> what blocks it.

### Results of the agent's run (2026-10-19, arm64 only: Rosetta not installed)

- **2.8.0: built**, passes the smoke test (it crashes on warnings and errors like 2.7.2 and 2.8.1, so its entry has
  `no_warnings_on` and `explain_with`). Registered, with `COMPILERS` and `SHORT_NAMES`. Not added to `ALTERNATES`: the
  survey decides per game.
- **2.91.66 and 2.95.2: build, but cc1 crashes on every input** (`psx-gcc-macos-egcs.sh`, kept for the Intel try). The
  source passes plain `int`s (a literal `0`) through `...` to `gen_rtx` and reads them back as pointers or 64-bit
  words; Apple's arm64 ABI leaves the upper half of such a stack slot as garbage, so `copy_rtx_if_shared` gets
  `orig = 0x6`. That is not fixable with flags; under `arch -x86_64` it should work (the register is zero-extended there).
- **2.5.7: not built.** It is written in K&R varargs (`va_alist`, `va_dcl`) throughout, which arm64 can't run either, and
  its `gvarargs.h` clashes with Apple's `va_list`. Also an Intel-only job (its patches are in
  `gcc-2.5.7-psx.Dockerfile`; `psx-gcc-macos.sh` would need its case blocks back).
- **Intel builds of 2.6.0, 2.6.3, 2.7.2, 2.8.1: not done** (no Rosetta). Next step when it is installed
  (`softwareupdate --install-rosetta`): `arch -x86_64 docs/plans/psx-gcc-build-all.sh ../old-gcc 2.6.0 2.6.3 2.8.0 2.91.66 2.95.2`.

## Native arm64 builds of every version (and an upstream contribution)

Rosetta is going away (as recalled from Apple's 2025 announcement: fully available through macOS 27, then only a
limited subset for older games), and decompals/old-gcc's own arm64 macOS builds of 2.7.2 and 2.8.1 crash or print
garbage on some warnings, so native builds are worth fixing properly, for everyone, not just for this fork.

**The cause:** the old GCC sources pass values through `...` with a type that differs from what the callee reads:
an `int` (often a literal `0`) where `va_arg` reads a pointer or a 64-bit `HOST_WIDE_INT` (`gen_rtx`, `gen_rtvec`,
`emit_library_call`...; the 2.91.66/2.95.2 crash), K&R `va_alist`/`va_dcl` code (2.5.7), and pointers passed to
K&R diagnostic functions declared with `int` parameters (`error (s, v, v2)`; likely the garbled warnings, to be
confirmed). x86_64 tolerates these by accident; Apple's arm64 ABI puts every variadic argument in its own 8-byte
stack slot, and a 4-byte `int` written there leaves the upper half undefined.

**The fix, generic rather than per call site:**

1. **A widening header** (`arm64-varargs.h`), force-included with `-include` when building the compiler itself. It
   redefines each variadic entry point as a macro that passes every argument after the fixed ones through
   `#define DW(x) _Generic((x), double: (x), float: (double)(x), default: (long)(x))`, applied with a
   fixed-arity FOR_EACH macro (up to 16 arguments). Pointers and longs are unchanged, ints are sign-extended to 64
   bits (so a literal 0 read as a pointer is NULL), doubles untouched. Clang accepts `_Generic` in gnu89 mode. List
   the variadic functions per version with `grep -n '\.\.\.\|va_dcl' *.c *.h`. Each macro needs its function's
   fixed-argument count (`gen_rtx` 2, `gen_rtvec` 1, `error` 1, `error_with_file_and_line` 3...), so the header is
   a table, not magic. **Watch the definitions:** a function-like macro also expands at the function's own
   definition and prototype (`gen_rtx VPROTO((...))` is rescanned into `gen_rtx (...)`). Define the macros so they
   don't (a per-file `#include` after the definitions, or a `#define gen_rtx(...)` that is `#undef`ed around the
   definition), or call the real function through a parenthesised name, `(gen_rtx) (...)`.
2. **Diagnostics: reproduce first.** In 2.6.0, `error`, `warning` and `pedwarn` in `toplev.c` are already
   `VPROTO((char *s, ...))` (stdarg), so the K&R-`int`-parameter theory for the garbled warnings
   (`warning: *aV makes integer from pointer`) is unverified. Before converting anything, reproduce one garbled
   warning in lldb and find where the pointer is cut to 32 bits (the `v_*` helpers, the `vfprintf` path, a K&R
   function with `int` parameters). The cheapest fix that works wins: changing a parameter type to `long` or
   `char *` before rewriting a family to `stdarg`.
3. **2.5.7:** convert its `varargs.h` users to `stdarg.h` (as the 2.6+ scripts already sed), and drop its
   `gvarargs.h` in favour of the system's `stdarg.h`.

**Proof (required before using or publishing a build).** What counts as the truth, in order:

1. **The matched functions of SLUS-01252** (from the laptop's database): each one matched with the official
   compiler, so each must still assemble to the same bytes with the port. This is independent of my scripts and
   patches, which a comparison against a Rosetta build of the same patched source is not.
2. **A reference build of the same version** (the Rosetta build): the native compiler's assembly (`-S`) must be
   byte-identical on a corpus of the C files of the public PS1 decomps listed in qwen-fine-tuning.md, at `-O2 -G8`
   and `-O1`, and its stderr (warnings and errors) must be identical too. A file the reference compiler itself
   rejects (missing headers, project-specific macros) is dropped from the corpus, not counted as a difference. Any
   difference is a bug in the port, not noise.

`scripts/compare-compilers.sh` (to be created; the repo has no `scripts/` folder yet) takes two compiler folders and
a corpus folder, compiles each file with both, `cmp`s the `.s` files and the stderr, and lists the differences and
the dropped files. The corpus lives outside the repo (`~/Decomp/compiler-corpus/`; say how to rebuild it in the
script's header).

**Upstream:** once one version passes the proof, open a pull request to decompals/old-gcc with the header, the
diagnostic patches, the macOS build scripts (`psx-gcc-macos.sh`, `psx-gcc-macos-egcs.sh`), and the identical-output
results as evidence. Same license as old-gcc's patches (GPL-2.0, as GCC). Describe it as AI-assisted in the PR, as
the decomp community expects to know. **Licensing here:** the GCC binaries in `toolchain-builds/` are GPL-2.0, so
`toolchain-builds/README.md` names the exact source tarball and patch set (decompals/old-gcc at a commit) each one
was built from, as the `source =` lines in `toolchains.toml` do. Check Apple's own Rosetta announcement before the
PR or README cites its timeline: the paragraph at the top of this section is from memory.

**Order:** Rosetta builds first (quick, and they're the reference for the proof), then 2.95.2 natively (the newest;
SDK 4.70 pointed at it), then 2.91.66, the diagnostics fix for 2.6.x/2.7.2/2.8.x (which retires `-w` and
`explain_with` here), and 2.5.7 last.

### The agent's task for this (paste into a Claude Code session on the M5 Ultra)

> In ~/decomp.wtf (pull main first), follow "Native arm64 builds of every version" in
> docs/plans/m5-psx-compilers.md. First build the Intel references: install Rosetta if it isn't
> (`softwareupdate --install-rosetta --agree-to-license`; ask me if it needs a password), then
> `arch -x86_64 docs/plans/psx-gcc-build-all.sh ../old-gcc 2.5.7 2.6.0 2.6.3 2.7.2 2.8.0 2.8.1 2.91.66 2.95.2`.
> (Rosetta wasn't installed when this was written and I had said not to install it: ask before installing. If I say
> no, skip the Rosetta references and use the matched-functions proof only.) Then port 2.95.2 to native arm64 with the widening header and stdarg diagnostics, and write the identical-output
> check (scripts/compare-compilers.sh: two compiler folders and a corpus folder in, a list of differing files out).
> Build the corpus from the public PS1 decomps (shallow clones, INCLUDE_ASM lines removed, as qwen-fine-tuning.md
> describes) and from SLUS-01252's matched functions if the laptop's database is copied over. A port is done only
> when its output is byte-identical on the whole corpus. Register each finished native build (macos-arm64 part) and
> Intel build (macos-x86_64 part) in toolchains.toml, commit, push to main, and write the results here. Then do the
> other versions in the order above. Prepare the decompals/old-gcc pull request as a branch and a description in
> docs/plans/old-gcc-pr.md, but don't open it: I'll review and submit it.

### Results of the native arm64 port (2026-10-19)

- **2.95.2 and 2.91.66 now work natively on arm64**: `cc1` compiles, and warnings and errors print properly
  (`warning: assignment makes integer from pointer without a cast`), so their entries have no `no_warnings_on` or
  `explain_with`. Registered, with `COMPILERS` and `SHORT_NAMES`. `psx-gcc-macos-egcs.sh` does it with three changes,
  found in this order by debugging the crash (`copy_rtx_if_shared(orig=0x6)`) with lldb:
  1. `arm64-varargs.h` (written by `arm64-varargs-gen.py`): widens every argument after the fixed ones of the
     variadic functions in its table, with the names in prototypes and definitions put in parentheses first
     (`(gen_rtx) VPROTO(...)`) so the macros stay off them. `__VA_OPT__` handles calls with no variadic arguments, and
     the `_Generic` branches must not cast a pointer to double (clang checks every branch). Alone, this was not
     enough.
  2. **The function-pointer type `rtx (*)(rtx, ...)`** that `expr.h` and `expr.c` give `insn_gen_function` and
     `genfun`: `GEN_FCN (icode) (a, b)` called the generated `gen_*` functions through a variadic type, so the
     arguments went on the stack and the callee read registers. Calls through an unprototyped `()` pointer type
     work (checked with a 10-line test). This was the actual crash.
  3. **`HOST_WIDE_INT`**: the `xm-darwin.h` the older versions need forces it to 32-bit `int`; 2.9x needs the 64-bit
     one its own headers pick (`machmode.def`'s DImode masks overflowed otherwise).
- **Not proven yet.** There is no reference compiler here (Rosetta isn't installed), so "byte-identical to a
  reference build" and the matched-functions check against SLUS-01252 haven't been done. A wrong compiler can only
  fail to match, never match falsely, so registering them is safe, but the upstream PR must wait for the proof. The
  cheapest first check, on the laptop: re-run the compiler survey and see whether SLUS-01252's matched functions
  still match with 2.95.2 or 2.91.66 where the official Linux build would.
- **2.8.0 (and so 2.6.x, 2.7.2, 2.8.1): not converted.** The same header breaks their build: `error`, `warning` and
  `pedwarn` are declared unprototyped there (`extern void error ();`), so a call written `(error) (...)` is an
  undeclared identifier (a plain call gets an implicit declaration; a parenthesised name doesn't). Either make the
  macros emit `((void (*)()) error)(...)`-style calls with explicit declarations, or fix the cause of their garbled
  warnings directly: the plan's step 2 (reproduce one in lldb first). They keep `no_warnings_on` meanwhile.
- **2.5.7: not attempted** (K&R varargs throughout).

### The proof, and what it found (2026-10-19, later)

Rosetta is installed now. Intel builds (`BUILD_ARCH=x86_64 docs/plans/psx-gcc-build-all.sh ...`: the Command Line Tools'
`xcrun` is arm64 only, so `arch -x86_64 bash ...` can't find a compiler; the script builds with `cc -arch x86_64` and
the Intel tools run through Rosetta) of 2.6.0, 2.6.3, 2.8.0, 2.91.66 and 2.95.2 are in `toolchain-builds/` with
`macos-x86_64` parts. They print warnings and errors properly.

`scripts/compare-compilers.sh` compiles the 4,133 C files of nine public PS1 decomps (`~/Decomp/compiler-corpus/`,
INCLUDE_ASM lines removed) with the Intel and the arm64 build of each version, at `-O2 -G8` and `-O1`, and compares
the assembly and the stderr. Most files don't compile standalone (the reference rejects them too: "dropped"), so the
useful corpus is 388 files for 2.95.2, 191 for 2.91.66, 161 for 2.8.0 and only 3 for 2.6.x (`//` comments, modern C).
Final result of the arm64 builds against the Intel ones:

| version | files compared | assembly differences | stderr/exit differences |
|---|---|---|---|
| 2.95.2 | 388 | 0 | 0 |
| 2.91.66 | 191 | 0 | 0 |
| 2.8.0 | 161 | 0 | 30 (garbled/crashing diagnostics, the known problem) |
| 2.6.3, 2.6.0 | 3 | 0 | 58 (same) |

Not yet: the SLUS-01252 matched functions (they live in the laptop's database), and a comparison with the official
Linux i386 build, which can't run here; Intel and arm64 share my patch scripts, so they agree with each other but this
is not yet proof they agree with decompals's Linux builds. The survey on the laptop is that check.

**What the comparison found (all fixed, all in the committed scripts):**

1. A **double constant came out with garbage in one `.word`** (`.word 0x00000077`, differently from run to run, on both
   architectures: the Intel builds had it too). The sources copy a `CONST_DOUBLE`'s two 32-bit halves as one block
   (`real.h`: "use &CONST_DOUBLE_LOW(r) as the address of an array of ints"), but each rtx slot is 8 bytes on an LP64
   host, so the copy took padding for the high half. `const-double-fix.py` rewrites those copies (6 to 8 per version)
   to move the halves one by one. A first fix that moved both halves into one slot was wrong: the generic rtx code fills
   slots 2 and 3 by format, so constants built that way read garbage. The official macOS builds of 2.7.2 and 2.8.1 use
   the same `xm-darwin.h` and probably have the same bug: worth telling decompals.
2. **`HOST_WIDE_INT` must stay the 32-bit `int`** of `xm-darwin.h`, not the 64-bit `long` my first 2.9x builds used:
   those old sources assume the host word is the target word, and with `long` the code changes (`li $18,-65536`
   became `li $18,4294901760` and the delay slots moved; 33 of 388 files differed). The official i386 builds are 32-bit.
   (The 2.9x `machmode.def` "shift count >= width of type" warnings are the same on the official builds.)
3. Comments in the assembly (`# 0x00000001`) differ with a 64-bit `HOST_WIDE_INT`; `NO_COMMENTS=1` compares without them.

Remaining: the diagnostics of 2.8.0 and 2.6.x on arm64 (still `-w` and `explain_with`, or use the Intel builds under
Rosetta while it lasts), 2.5.7, the SLUS-01252 check, 2.7.2 and 2.8.1 Intel builds, and the upstream pull request
(draft in `docs/plans/old-gcc-pr.md`, not opened: it waits for the SLUS-01252 check).

### Checked on the laptop with SLUS-01252 (2026-10-19)

- **arm64 against Intel, on this game's code:** 400 random matched functions (own definitions with the shared header,
  `-O2 -G8`) compiled with both builds of 2.6.0, 2.6.3, 2.8.0, 2.91.66 and 2.95.2: identical assembly for every
  function both compile (2 fail on both builds of 2.6.x).
- **The rebuilt 2.6.x packages:** all 233 functions matched with 2.6.0 or 2.6.3 still match.
- **The double-constant bug is real in decompals's official macOS builds of 2.7.2 and 2.8.1** (the ones in use):
  `double f(void) { return 1.5; }` compiles to `.word 0x00000000` twice, and a float constant inside a function to
  `0x00000000`; global initializers come out right. 2.95.2 (fixed here) is correct. No function of SLUS-01252 uses a
  float or double literal (fixed-point code), so it doesn't affect this game, but it belongs in the upstream PR, and
  rebuilding 2.7.2 and 2.8.1 with `const-double-fix.py` is the next step for them.
- The app now reinstalls a compiler when the registry pins a rebuilt package (`toolchains.stale`), and surveys again
  when one changes.
