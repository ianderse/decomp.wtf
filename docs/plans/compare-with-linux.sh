#!/bin/bash
# The macOS builds of the PSY-Q GCCs against decompals/old-gcc's official Linux builds (static i386, run in a
# linux/amd64 container), on a corpus of C files: assembly per file, compared without comment lines (which name the
# host). Usage: compare-with-linux.sh <corpus dir with .c files (and their headers)> [versions...]
# Needs Docker. macOS builds compared: the installed ones (decomp.wtf's toolchains folder) and any Intel builds in
# $MAC_X86 (one folder per version, e.g. unpacked toolchain-builds/gcc-<v>-psx-macos-x86_64.tar.gz).
set -eu
CORPUS="$(cd "$1" && pwd)"; shift
VERSIONS="${*:-2.6.0 2.6.3 2.7.2 2.8.0 2.8.1 2.91.66 2.95.2}"
W="$(mktemp -d)"; TC="$HOME/Library/Application Support/decompnet/toolchains"
cat > "$W/compile-all.sh" <<'SH'
#!/bin/sh
G="$1"; O="$2"; C="$3"; mkdir -p "$O"
for f in "$C"/*.c; do n=$(basename "$f" .c)
  "$G/gcc" -B"$G/" -S -O2 -G8 -w -I "$C" "$f" -o "$O/$n.s" 2>/dev/null || { rm -f "$O/$n.s"; touch "$O/$n.fail"; }
done
SH
chmod +x "$W/compile-all.sh"; cp -R "$CORPUS" "$W/corpus"
for v in $VERSIONS; do
  mkdir -p "$W/linux/$v"; curl -sfL "https://github.com/decompals/old-gcc/releases/download/0.17/gcc-$v-psx.tar.gz" | tar -xz -C "$W/linux/$v"
  "$W/compile-all.sh" "$TC/gcc-$v-psx" "$W/out/$v/mac-arm64" "$W/corpus"
  [ -n "${MAC_X86:-}" ] && [ -d "$MAC_X86/$v" ] && "$W/compile-all.sh" "$MAC_X86/$v" "$W/out/$v/mac-x86_64" "$W/corpus"
done
docker run --rm --platform linux/amd64 -v "$W:/w" debian:bookworm-slim sh -c \
  "for v in $VERSIONS; do /w/compile-all.sh /w/linux/\$v /w/out/\$v/linux /w/corpus; done"
python3 - "$W" $VERSIONS <<'PY'
import re, sys
from pathlib import Path
w, versions = Path(sys.argv[1]), sys.argv[2:]
norm = lambda t: "\n".join(re.sub(r"\s+#.*$", "", l).rstrip() for l in t.splitlines()
                           if not l.lstrip().startswith("#") and ".ident" not in l and not l.strip().startswith(".file"))
print(f"{'version':8} {'build':11} {'identical':>9} {'different':>9} {'one fails':>9} {'both fail':>9}")
for v in versions:
    for b in ("mac-arm64", "mac-x86_64"):
        d = w / "out" / v / b
        if not d.exists(): continue
        c = {"same": 0, "diff": 0, "one": 0, "both": 0}; bad = []
        for f in sorted((w / "corpus").glob("*.c")):
            r, x = w / "out" / v / "linux" / f"{f.stem}.s", d / f"{f.stem}.s"
            if not r.exists() and not x.exists(): c["both"] += 1
            elif r.exists() != x.exists(): c["one"] += 1; bad.append(f.stem)
            elif norm(r.read_text(errors="replace")) == norm(x.read_text(errors="replace")): c["same"] += 1
            else: c["diff"] += 1; bad.append(f.stem)
        print(f"{v:8} {b:11} {c['same']:9} {c['diff']:9} {c['one']:9} {c['both']:9}  {' '.join(bad[:8])}")
print(f"(outputs kept in {w}/out)")
PY
