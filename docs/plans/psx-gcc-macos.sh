#!/bin/bash
# Build an old PSY-Q GCC (2.6.0 or 2.6.3) natively on macOS, from decompals/old-gcc's patches.
# Usage: psx-gcc-macos.sh <version> <path to a checkout of decompals/old-gcc>
# For an Intel build run it as: arch -x86_64 bash psx-gcc-macos.sh ...
# Output: ./build-gcc-<version>-psx-macos-<arch>/{gcc,cc1,cpp}
set -e

VERSION="$1"
OLDGCC="$(cd "$2" && pwd)"
PATCHES="$OLDGCC/patches"
ARCH="$(uname -m)"
OUTDIR="$(pwd)/build-gcc-$VERSION-psx-macos-$ARCH"
WORKDIR="${WORKDIR:-$(mktemp -d)}"
[ -n "$KEEP" ] || trap 'rm -rf "$WORKDIR"' EXIT

case "$VERSION" in
    2.6.3) URL="https://mirrors.slackware.com/slackware/slackware-2.2.0/source/d/gcc/gcc-2.6.3.tar.gz" ;;
    2.6.0) URL="http://www.nic.funet.fi/index/gnu/funet/historical-funet-gnu-area-from-early-1990s/gcc-$VERSION.tar.gz" ;;
    *) echo "unsupported version $VERSION"; exit 1 ;;
esac

export PATH="/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
unset CC CXX

cd "$WORKDIR"
curl -fL "$URL" | tar xz
cd gcc-$VERSION
chmod -R u+w .

sed -i '' 's/include <varargs\.h>/include <stdarg\.h>/g' *.c

# the patches the Dockerfiles apply, per version (not the g++ ones: only cc1, cpp and the gcc driver are built)
case "$VERSION" in
    2.6.3)
        patch -u -p1 obstack.h -i "$PATCHES/obstack-2.7.2.h.patch"
 ;;
    2.6.0)
        patch -u -p1 obstack.h -i "$PATCHES/obstack-2.7.2.h.patch"
        patch -u -p1 collect2.c -i "$PATCHES/collect2-2.6.0.c.patch"
        patch -u -p1 cccp.c -i "$PATCHES/cccp-2.6.0.c.patch"
        patch -u -p1 gcc.c -i "$PATCHES/gcc-2.6.0.c.patch"
        patch -u -p1 config/mips/mips.h -i "$PATCHES/mipsel-2.6.patch" ;;
esac
# sdbout-2.6.x.c.patch, as sed: Apple's patch rejects its odd hunk header
sed -i '' -e 's/ && !defined(WINNT)$//' -e 's/^#if defined(USG) && !defined(MIPS) && !defined (hpux).*$/& \&\& !defined(_WIN32) \&\& !defined(__linux__)/' sdbout.c
patch -su -p1 < "$PATCHES/psx-2.5.7.patch"
if [ "$VERSION" = 2.6.3 ]; then touch -c cp/parse.y cp/parse.h cp/parse.c; fi
touch insn-config.h

# (after the patches, whose context has the originals) macOS declares sys_nerr as const int and sys_errlist as const char *const []
sed -i '' -e 's/^extern int sys_nerr;/extern const int sys_nerr;/' \
    -e 's/^extern char \*sys_errlist\[\];/extern const char *const sys_errlist[];/' gcc.c

# modern config.guess/config.sub (Apple Silicon), psx* added to the OS list; same as gcc-2.7.2-psx-macos.sh
chmod +w config.guess config.sub
curl -fsL "https://raw.githubusercontent.com/gcc-mirror/gcc/74af13c174714dd3b9f1ded4b39955f003c16361/config.guess" -o config.guess
curl -fsL "https://raw.githubusercontent.com/gcc-mirror/gcc/6fad101f3063d722e3348d07dc93cf737f8709e4/config.sub"   -o config.sub
chmod +x config.guess config.sub
sed -i '' 's/| hiux\* | abug | nacl\*/| psx* \\\
'"$(printf '\t')"'     | hiux* | abug | nacl*/' config.sub

cp "$PATCHES/xm-darwin.h" config/
chmod +w configure
awk '/^\t\*\)$/ { buf=$0; next }
     buf != "" { if (!done && /echo.*Configuration.*not supported/) {
       print "\t*-apple-darwin*)"; print "\t\txm_file=xm-darwin.h"
       print "\t\tfixincludes=Makefile.in"; print "\t\t;;"; done=1 }
       print buf; buf="" } { print }' configure > configure.tmp
mv configure.tmp configure
chmod +x configure

./configure \
    --target=mips-sony-psx \
    --prefix=/opt/cross \
    --with-endian-little \
    --with-gnu-as \
    --host="$(./config.guess)" \
    --build="$(./config.guess)"

# 2.5.7 and 2.6.0's configure take the host for a MIPS box and add its cc flags (-Olimit 3000), which clang rejects
sed -i '' -e 's/-Wf,-XNg1500,-XNh2000//' -e 's/-Olimit 3000//' Makefile

nice -n 10 make --jobs "$(sysctl -n hw.ncpu)" cpp cc1 xgcc \
    CFLAGS="-std=gnu89 -w -Wno-int-conversion -Wno-implicit-function-declaration -Wno-return-mismatch -DHAVE_STRERROR"

./cc1 -quiet -O2 "$OLDGCC/tests/little_endian.c" -o little_endian.s
grep -E 'lbu\s\$2,0\(\$4\)' little_endian.s
./cc1 -quiet -help </dev/null 2>&1 | grep -- -msoft-float

mkdir -p "$OUTDIR"
cp cpp cc1 xgcc "$OUTDIR/"
mv "$OUTDIR/xgcc" "$OUTDIR/gcc"
echo "Done: $OUTDIR/ ($(file "$OUTDIR/cc1" | cut -d: -f2 | xargs))"
