# More PS1 compiler versions, built natively on macOS (on the M5 Ultra)

Status: 2.6.0 and 2.6.3 built natively on arm64 macOS (`docs/plans/psx-gcc-macos.sh <version> <old-gcc checkout>`), registered
in `toolchains.toml` and added to the alternates. The tarballs are committed in toolchain-builds/ and served from main. Not yet done:
the re-judge measurement, and everything Intel (Rosetta isn't installed on this machine; the arm64
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
