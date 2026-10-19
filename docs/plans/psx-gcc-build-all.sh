#!/bin/bash
# Build every PSY-Q GCC version that isn't built yet, natively on macOS, with psx-gcc-macos.sh; package each one that
# passes the smoke test into toolchain-builds/ and print its toolchains.toml entry. A version that fails is logged and
# skipped, and the loop goes on (fixing it is the M5 Ultra agent's job: see m5-psx-compilers.md, "The agent's task").
#
# Usage (from the repository root): docs/plans/psx-gcc-build-all.sh <path to a checkout of decompals/old-gcc> [versions]
# Intel builds: arch -x86_64 docs/plans/psx-gcc-build-all.sh ...   (needs Rosetta: softwareupdate --install-rosetta)
set -u
OLDGCC="$(cd "$1" && pwd)"; shift
VERSIONS="${*:-2.5.7 2.6.0 2.6.3 2.8.0 2.91.66 2.95.2}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ARCH="$(uname -m)"
LOGS="$ROOT/toolchain-builds/logs"
mkdir -p "$LOGS"
cd "$ROOT/toolchain-builds"

smoke() {  # $1: the build folder. A plain function, one with a warning, one with an error: none may crash cc1
    local dir="$1" t; t="$(mktemp -d)"
    printf 'int f(int a, int b) { return a * b + 3; }\n' > "$t/ok.c"
    printf 'int g(void) { int x; x = (void *)0; return x; }\n' > "$t/warn.c"
    printf 'int h(void) { return 1 +; }\n' > "$t/err.c"
    "$dir/gcc" -B"$dir/" -S -O2 -G8 "$t/ok.c" -o "$t/ok.s" || { echo "  ok.c didn't compile"; return 1; }
    grep -q "jr" "$t/ok.s" || { echo "  ok.c: no MIPS code in the output"; return 1; }
    "$dir/gcc" -B"$dir/" -S -O2 -G8 "$t/warn.c" -o "$t/warn.s" 2> "$t/warn.log"
    if grep -q "fatal signal" "$t/warn.log"; then echo "  crashes on a warning (needs -w, like the arm64 builds)"; fi
    "$dir/gcc" -B"$dir/" -S -O2 -G8 "$t/err.c" -o "$t/err.s" 2> "$t/err.log"
    if grep -q "fatal signal" "$t/err.log"; then echo "  crashes on an error (needs explain_with)"; fi
    rm -rf "$t"
}

for v in $VERSIONS; do
    out="gcc-$v-psx-macos-$ARCH.tar.gz"
    if [ -f "$out" ]; then echo "== $v: $out exists, skipped"; continue; fi
    echo "== $v ($ARCH): building (log: $LOGS/$v-$ARCH.log)"
    if ! nice -n 10 bash "$ROOT/docs/plans/psx-gcc-macos.sh" "$v" "$OLDGCC" > "$LOGS/$v-$ARCH.log" 2>&1; then
        echo "  FAILED: $(tail -3 "$LOGS/$v-$ARCH.log" | tr '\n' ' ')"; continue
    fi
    dir="build-gcc-$v-psx-macos-$ARCH"
    if ! smoke "$dir"; then echo "  FAILED the smoke test"; continue; fi
    tar -czf "$out" -C "$dir" .
    sum="$(shasum -a 256 "$out" | cut -d' ' -f1)"
    plat="macos-arm64"; [ "$ARCH" = x86_64 ] && plat="macos-x86_64"
    echo "  built: $out ($sum)"
    cat <<EOF
  toolchains.toml part for gcc-$v-psx:
[["gcc-$v-psx".parts]]
url = "https://raw.githubusercontent.com/ianderse/decomp.wtf/main/toolchain-builds/$out"
sha256 = "$sum"
platforms = ["$plat"]
EOF
done
