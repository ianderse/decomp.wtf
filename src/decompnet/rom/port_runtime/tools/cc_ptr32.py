#!/usr/bin/env python3
"""Compiler launcher for the game's files when pointers are __ptr32 (tools/ptr32.py): clang's x86-64 backend can't
call through a 32-bit pointer ("Cannot select: X86ISD::CALL", "Cannot emit physreg copy instruction") or copy memory
through one ("cannot lower memory intrinsic in address space"), LLVM 21 and 22 (2026-10-23). So each file is compiled
to LLVM IR first, every call whose target is a `ptr addrspace(271)` (and every memcpy/memmove/memset on one) gets
an `addrspacecast` to an ordinary pointer, and the IR is then compiled to the object.

CMake runs it as the C compiler launcher: cc_ptr32.py <compiler> <args...> -o <out.o> -c <in.c>. Standard library only.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

AS = "ptr addrspace(271)"
_DEF = re.compile(r"^\s*(%[-\w.$]+)\s*=\s*(.*)$")
_CALL = re.compile(r"\b(call|invoke)\b(.*?)\s(%[-\w.$]+)\(")
_MEM = re.compile(r"@llvm\.(memcpy|memmove|memset)((?:\.p\d+)+)\.(i32|i64)\(")
MEM_DECL = {
    "memcpy": "declare void @llvm.memcpy.p0.p0.{w}(ptr, ptr, {w}, i1 immarg)",
    "memmove": "declare void @llvm.memmove.p0.p0.{w}(ptr, ptr, {w}, i1 immarg)",
    "memset": "declare void @llvm.memset.p0.{w}(ptr, i8, {w}, i1 immarg)",
}


def _ptr32_values(body: list[str], params: str) -> set[str]:
    """SSA values in a function whose type is ptr addrspace(271)."""
    vals = set(re.findall(r"ptr addrspace\(271\)[^,)%]*\s(%[-\w.$]+)", params))
    for line in body:
        m = _DEF.match(line)
        if not m:
            continue
        name, rhs = m.groups()
        if (rhs.startswith(f"load {AS},") or rhs.startswith(f"phi {AS} ") or f" to {AS}" in rhs
                or re.match(rf"(tail |notail |musttail )?call [^@%]*{re.escape(AS)} ", rhs)
                or re.match(rf"select i1 [^,]+, {re.escape(AS)} ", rhs)
                or re.match(rf"getelementptr (inbounds |nuw |nusw )*[^,]+, {re.escape(AS)} ", rhs)
                or rhs.startswith(f"freeze {AS} ")):
            vals.add(name)
    return vals


def _operand_end(s: str, i: int) -> int:
    """End of the IR operand starting at s[i]: a %value, @global, keyword or a balanced constant expression."""
    m = re.match(r"[%@][-\w.$]+|null|undef|poison", s[i:])
    if m:
        return i + m.end()
    m = re.match(r"\w+(?:\s+\w+)*\s*\(", s[i:])
    if not m:
        return i
    depth, j = 0, i + m.end() - 1
    while j < len(s):
        depth += {"(": 1, ")": -1}.get(s[j], 0)
        j += 1
        if depth == 0:
            return j
    return j


def _casts(args: str, out: list[str], indent: str, counter: list[int]) -> str:
    """Every `ptr addrspace(271) <attrs> <operand>` in a call's arguments becomes a cast `ptr <attrs> %__p32cN`."""
    res, i = [], 0
    while True:
        m = re.compile(r"ptr addrspace\(271\)((?:\s+(?:align \d+|noundef|nonnull|noalias|readonly|writeonly|"
                       r"captures\([^)]*\)|dereferenceable\(\d+\)))*)\s+").search(args, i)
        if not m:
            res.append(args[i:])
            return "".join(res)
        end = _operand_end(args, m.end())
        counter[0] += 1
        out.append(f"{indent}%__p32c{counter[0]} = addrspacecast {AS} {args[m.end():end]} to ptr")
        res.append(args[i:m.start()] + f"ptr{m.group(1)} %__p32c{counter[0]}")
        i = end


def patch(ir: str) -> str:
    lines = ir.split("\n")
    out, need = [], set()
    counter = [0]
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("define "):
            j = i
            while j < len(lines) and lines[j] != "}":
                j += 1
            body = lines[i + 1:j]
            vals = _ptr32_values(body, line)
            out.append(line)
            for b in body:
                indent = re.match(r"\s*", b).group(0)
                m = _CALL.search(b)
                if m and m.group(3) in vals:
                    counter[0] += 1
                    out.append(f"{indent}%__p32c{counter[0]} = addrspacecast {AS} {m.group(3)} to ptr")
                    head = b[:m.start(3)].replace(" addrspace(271) ", " ", 1)  # `call addrspace(271) void %f(`
                    b = head + f"%__p32c{counter[0]}" + b[m.end(3):]
                elif re.search(r"\b(call|invoke) addrspace\(271\) ", b):
                    # a constant callee: addrspacecast (ptr @f to ptr addrspace(271)), inttoptr (i32 C to ...)
                    b = re.sub(r"addrspacecast \(ptr (@[-\w.$]+) to ptr addrspace\(271\)\)\(", r"\1(", b, count=1)
                    b = re.sub(r"inttoptr \(i32 (-?\d+) to ptr addrspace\(271\)\)\(",
                               lambda k: f"inttoptr (i64 {int(k.group(1)) & 0xFFFFFFFF} to ptr)(", b, count=1)
                    b = b.replace(" addrspace(271) ", " ", 1)
                mm = _MEM.search(b)
                if mm and "271" in mm.group(2):
                    kind, width = mm.group(1), mm.group(3)
                    spaces = ".p0" if kind == "memset" else ".p0.p0"
                    args = _casts(b[mm.end():], out, indent, counter)
                    b = b[:mm.start()] + f"@llvm.{kind}{spaces}.{width}(" + args
                    need.add((kind, width))
                out.append(b)
            out.append("}")
            i = j + 1
            continue
        if re.match(r"declare .*@llvm\.(memcpy|memmove|memset)(\.p\d+)+\.i(32|64)\(", line) and "271" in line:
            i += 1
            continue
        out.append(line)
        i += 1
    text = "\n".join(out)
    for kind, width in sorted(need):
        spaces = ".p0" if kind == "memset" else ".p0.p0"
        if not re.search(rf"declare [^\n]*@llvm\.{kind}{re.escape(spaces)}\.{width}\(", text):
            text += "\n" + MEM_DECL[kind].format(w=width) + "\n"
    return text


def main(argv: list[str]) -> int:
    cc, args = argv[0], argv[1:]
    if "-c" not in args or "-o" not in args or not any(a.endswith(".c") for a in args):
        return subprocess.call([cc, *args])
    k = args.index("-o")
    obj = args[k + 1]
    ll = obj + ".ll"
    front = args[:k] + ["-o", ll] + args[k + 2:]
    front = [a for a in front if not re.match(r"-O[0-3sz]?$", a)]
    r = subprocess.call([cc, *front, "-S", "-emit-llvm", "-O0", "-Xclang", "-disable-O0-optnone"])
    if r != 0:
        return r
    with open(ll, encoding="utf-8") as fh:
        ir = fh.read()
    with open(ll, "w", encoding="utf-8") as fh:
        fh.write(patch(ir))
    keep = []
    it = iter(args)
    for a in it:  # what codegen needs: target, optimisation, debug info
        if a in ("-arch", "-target", "-isysroot"):
            keep += [a, next(it)]
        elif re.match(r"(-O[0-3sz]?|-g\w*|-mmacosx-version-min=.*|--target=.*|-m\w[\w=.-]*)$", a):
            keep.append(a)
    r = subprocess.call([cc, *keep, "-Wno-override-module", "-c", ll, "-o", obj])
    if r == 0 and not os.environ.get("PSX_KEEP_IR"):
        os.unlink(ll)
    return r


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
