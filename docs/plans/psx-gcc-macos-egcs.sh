#!/bin/bash
# The 2.91.66 (egcs 1.1.2) and 2.95.2 half of psx-gcc-macos.sh: their source has the compiler in gcc/ and libiberty/
# beside it, and a configure per directory. Called by psx-gcc-macos.sh, same arguments.
set -e

VERSION="$1"
OLDGCC="$(cd "$2" && pwd)"
PATCHES="$OLDGCC/patches"
HERE="$(cd "$(dirname "$0")" && pwd)"
ARCH="$(uname -m)"
OUTDIR="$(pwd)/build-gcc-$VERSION-psx-macos-$ARCH"
WORKDIR="${WORKDIR:-$(mktemp -d)}"
[ -n "$KEEP" ] || trap 'rm -rf "$WORKDIR"' EXIT

export PATH="/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
unset CC CXX

cd "$WORKDIR"
mkdir "gcc-$VERSION"
case "$VERSION" in
    2.91.66)
        curl -fL "https://gcc.gnu.org/pub/gcc/old-releases/egcs/egcs-1.1.2.tar.bz2" | tar xj --strip-components=1 -C "gcc-$VERSION"
        OBSTACK=gcc/obstack.h ;;
    2.95.2)
        curl -fL "https://mirrors.kernel.org/gnu/gcc/gcc-2.95.2.tar.gz" | tar xz --strip-components=1 -C "gcc-$VERSION"
        OBSTACK=include/obstack.h ;;
esac
cd "gcc-$VERSION"
chmod -R u+w .

sed -i '' 's/include <varargs\.h>/include <stdarg\.h>/g' */*.c
patch -u -p1 "$OBSTACK" -i "$PATCHES/obstack-$VERSION.h.patch"
patch -u -p1 gcc/config/mips/mips.h -i "$PATCHES/mips.patch"
patch -su -p1 < "$PATCHES/psx-2.91.patch"
touch gcc/insn-config.h

# Apple's arm64 variadic ABI (see arm64-varargs.h): the header widens the arguments of every variadic call, so the
# names in prototypes and definitions that mention "..." go in parentheses first, to keep the macros off them
if [ "$(uname -m)" = arm64 ]; then
    names="$(sed -n 's/^#define \([a-z_]*\)(a1.*/\1/p' "$HERE/arm64-varargs.h" | paste -sd'|' -)"
    sed -E -i '' "s/(^|[^A-Za-z0-9_(])($names)([[:space:]]+P?V?PROTO)/\1(\2)\3/" gcc/*.c gcc/*.h
    touch gcc/cstamp-h.in   # depends on acconfig.h, which sed just rewrote: don't let make try to regenerate it
    WIDEN="-include $HERE/arm64-varargs.h"
    # GEN_FCN (icode) (a, b) calls the generated gen_* functions through a pointer typed rtx (*)(rtx, ...). A call
    # through a variadic type puts its arguments on the stack, the gen_* functions read registers: unprototyped
    # pointer types (as genoutput.c already writes the table) call them the ordinary way
    sed -i '' -e 's/PROTO *((rtx, \.\.\.))/()/' -e 's/(rtx (\*) (rtx, \.\.\.)/(rtx (*) ()/' gcc/expr.h gcc/expr.c
fi

# modern config.guess/config.sub (Apple Silicon), psx* added to the OS list; same as psx-gcc-macos.sh
for d in . gcc libiberty; do
    [ -f "$d/config.guess" ] && [ -f "$d/config.sub" ] || continue
    chmod +w "$d/config.guess" "$d/config.sub"
    curl -fsL "https://raw.githubusercontent.com/gcc-mirror/gcc/74af13c174714dd3b9f1ded4b39955f003c16361/config.guess" -o "$d/config.guess"
    curl -fsL "https://raw.githubusercontent.com/gcc-mirror/gcc/6fad101f3063d722e3348d07dc93cf737f8709e4/config.sub"   -o "$d/config.sub"
    chmod +x "$d/config.guess" "$d/config.sub"
    sed -i '' 's/| hiux\* | abug | nacl\*/| psx* \\\
'"$(printf '\t')"'     | hiux* | abug | nacl*/' "$d/config.sub"
done

cp "$PATCHES/xm-darwin.h" gcc/config/
# 2.9x needs a 64-bit HOST_WIDE_INT for the target's DImode masks (it picks long on this LP64 host itself); the
# 32-bit one the older versions' xm-darwin.h forces makes machmode.def's shifts overflow
sed -i '' '/HOST_BITS_PER_WIDE_INT/d; /define HOST_WIDE_INT/d' gcc/config/xm-darwin.h
chmod +w gcc/configure
awk '/^\t\*\)$/ { buf=$0; next }
     buf != "" { if (!done && /echo.*Configuration.*not supported/) {
       print "\t*-apple-darwin*)"; print "\t\txm_file=xm-darwin.h"
       print "\t\tfixincludes=Makefile.in"; print "\t\t;;"; done=1 }
       print buf; buf="" } { print }' gcc/configure > gcc/configure.tmp
mv gcc/configure.tmp gcc/configure
chmod +x gcc/configure

HOST="$(./config.guess)"
export CFLAGS="-std=gnu89 -w -Wno-int-conversion -Wno-implicit-function-declaration -Wno-return-mismatch"
GCC_CFLAGS="$CFLAGS ${WIDEN:-}"
for d in libiberty gcc; do
    (cd $d && ./configure \
        --target=mips-sony-psx \
        --prefix=/opt/cross \
        --with-endian-little \
        --with-gnu-as \
        --host="$HOST" \
        --build="$HOST")
done

# libiberty's own sys_nerr and sys_errlist declarations clash with the SDK's const ones
sed -i '' -e 's/^extern int sys_nerr;/extern const int sys_nerr;/' \
    -e 's/^extern char \*sys_errlist\[\];/extern const char *const sys_errlist[];/' libiberty/strerror.c

nice -n 10 make -C libiberty --jobs "$(sysctl -n hw.ncpu)" CFLAGS="$CFLAGS"
nice -n 10 make -C gcc --jobs "$(sysctl -n hw.ncpu)" cpp cc1 xgcc CFLAGS="$GCC_CFLAGS"

cd gcc
./cc1 -version </dev/null 2>&1 | grep -- -msoft-float
./cc1 -version </dev/null 2>&1 | grep -- -msplit-addresses
./cc1 -version </dev/null 2>&1 | grep -- -mgpopt

mkdir -p "$OUTDIR"
cp cpp cc1 xgcc "$OUTDIR/"
mv "$OUTDIR/xgcc" "$OUTDIR/gcc"
echo "Done: $OUTDIR/ ($(file "$OUTDIR/cc1" | cut -d: -f2 | xargs))"
