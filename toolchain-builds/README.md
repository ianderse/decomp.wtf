# Compilers built for macOS (Intel)

The Apple Silicon builds are released from https://github.com/ianderse/old-gcc (0.18 and later), with the 2.7.2 and
2.8.1 ones and the float/double constant fix; the toolchains take them from there. What's left here are the Intel
(`-x86_64`) builds, until that fork releases them too.

GCC is GPL-2.0. These are builds of decompals/old-gcc's patched GCC sources (https://github.com/decompals/old-gcc,
patches in its `patches/` folder; the gcc.gnu.org / ftp.gnu.org / egcs source tarballs it names), built by
`docs/plans/psx-gcc-macos.sh` (2.6.0, 2.6.3, 2.8.0) and `docs/plans/psx-gcc-macos-egcs.sh` (2.91.66, 2.95.2) in this
repository, with `docs/plans/arm64-varargs.h` and `docs/plans/const-double-fix.py` for the arm64 builds. Those scripts
and patches are the corresponding source for the binaries here. Each `-arm64` / `-x86_64` pair is checked to produce
byte-identical assembly (`scripts/compare-compilers.sh`); the results are in `docs/plans/m5-psx-compilers.md`.
