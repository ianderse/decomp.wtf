#!/usr/bin/env python3
"""Compiler launcher for the game's files when pointers are __ptr32 (tools/ptr32.py): each file is compiled to LLVM
IR, every use of a 32-bit pointer (`ptr addrspace(271)`, a PS1 address) is translated to the host's memory
convention, and the IR is then compiled to the object.

The convention (psx/psx_runtime.h): PS1 address A lives at host address PSX_HOST_BASE + A (zero-extended), so PS1
RAM (0x80000000) is at 0x1_8000_0000, the game's stack is mapped in the same 4 GB window, and the low 32 bits of a
host pointer into the window are the PS1 address again. clang's backends lower an addrspace(271) access with a plain
zero-extension (below 4 GB, which arm64 macOS can't map) and can't call through one at all ("Cannot select:
AArch64ISD::CALL" / "X86ISD::CALL"), so nothing in addrspace(271) reaches them except values and arithmetic:

  1. load/store/cmpxchg/atomicrmw and llvm.memcpy/memmove/memset through an addrspace(271) pointer: the pointer
     becomes inttoptr(ptrtoint(p to i64) | PSX_HOST_BASE) (ptrtoint to a wider integer zero-extends), the access
     the ordinary-pointer form (memcpy.p0.p0 ...);
  2. addrspacecast addrspace(271) -> ptr (the game handing a pointer to code that uses ordinary pointers): the same;
  3. addrspacecast ptr -> addrspace(271) (a host pointer into the game, a local's address): its low 32 bits
     (ptrtoint to i32 truncates), right for anything in the window, the only memory the runtime may give the game;
  4. a game function's address taken as a 32-bit pointer or integer (addrspacecast (ptr @f ...), ptrtoint (ptr @f to
     i32)): the function's PS1 address (from --functions), so function pointers stored in RAM are MIPS addresses,
     round-trip, and compare equal to the ones in the game's data;
  5. a call through an addrspace(271) pointer: psx_host_fn(i32 address) (the runtime's lookup in the function table;
     a host function's truncated address comes back as itself) and the call through the host pointer it returns;
  6. ptrtoint/inttoptr between addrspace(271) and i32 stay as they are: PS1 address arithmetic.

A constant (a raw address, a data symbol's address) is translated as a constant expression; a 32-bit integer turned
into an ordinary pointer (`(T *)0x8002B590` through a macro that kept a plain `*`) is a PS1 address too.

CMake runs it as the C compiler launcher:
  cc_ptr32.py [--functions FILE] <compiler> <args...> -o <out.o> -c <in.c>
FILE has one `name 0xADDRESS` line per game function (gen/psx_function_addrs.txt). PSX_KEEP_IR=1 keeps the patched
.ll next to the object. Standard library only.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

PSX_HOST_BASE = 0x100000000
AS = "ptr addrspace(271)"
_NAME = r"[%@](?:[-\w.$]+|\"[^\"]*\")"
_DEF = re.compile(r"^(\s*)(%[-\w.$]+)\s*=\s*(.*)$")
_MEM = re.compile(r"@llvm\.(memcpy|memmove|memset)((?:\.p\d+)+)\.(i32|i64)\(")
_ATTRS = r"(?:\s+(?:align \d+|noundef|nonnull|noalias|readonly|writeonly|nocapture|captures\([^)]*\)|" \
         r"dereferenceable(?:_or_null)?\(\d+\)))*"
MEM_DECL = {
    "memcpy": "declare void @llvm.memcpy.p0.p0.{w}(ptr, ptr, {w}, i1 immarg)",
    "memmove": "declare void @llvm.memmove.p0.p0.{w}(ptr, ptr, {w}, i1 immarg)",
    "memset": "declare void @llvm.memset.p0.{w}(ptr, i8, {w}, i1 immarg)",
}
HOST_FN_DECL = "declare ptr @psx_host_fn(i32)"


class IRError(RuntimeError):
    pass


def _i32(v: int) -> int:
    """A 32-bit value as the signed literal LLVM prints."""
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v >= 1 << 31 else v


def _host_lit(v: int) -> int:
    return PSX_HOST_BASE + (v & 0xFFFFFFFF)


# ---------------------------------------------------------------------------------------------- operand parsing

def _balanced_end(s: str, i: int) -> int:
    """s[i] is an opening bracket: the index after its match (strings skipped)."""
    pairs = {"(": ")", "[": "]", "{": "}", "<": ">"}
    stack, j = [], i
    while j < len(s):
        c = s[j]
        if c == '"':
            j = s.find('"', j + 1)
            if j < 0:
                return len(s)
        elif c in pairs:
            stack.append(pairs[c])
        elif stack and c == stack[-1]:
            stack.pop()
            if not stack:
                return j + 1
        j += 1
    return j


def operand_end(s: str, i: int) -> int:
    """End of the IR operand starting at s[i]: a %value, @global, literal or a balanced constant expression."""
    m = re.match(_NAME + r"|-?\d+|true|false|null|undef|poison|zeroinitializer|none", s[i:])
    if m:
        return i + m.end()
    m = re.match(r"\w+(?:\s+\w+)*\s*(?=\()", s[i:])
    if m:
        return _balanced_end(s, i + m.end())
    return i


def split_top(s: str) -> list[str]:
    """s split at its top-level commas (brackets and strings respected), each part stripped."""
    parts, depth, cur, i = [], 0, [], 0
    while i < len(s):
        c = s[i]
        if c == '"':
            j = s.find('"', i + 1)
            j = len(s) if j < 0 else j + 1
            cur.append(s[i:j])
            i = j
            continue
        if c in "([{<":
            depth += 1
        elif c in ")]}>":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    parts.append("".join(cur).strip())
    return parts


# ---------------------------------------------------------------------------------------------- constants

class Transform:
    def __init__(self, functions: dict[str, int] | None = None):
        self.functions = functions or {}  # game function name -> PS1 address
        self.n = 0
        self.need_mem: set[tuple[str, str]] = set()
        self.need_host_fn = False
        self.dbg = ""  # the current line's debug location, for the calls it gets

    def tmp(self) -> str:
        self.n += 1
        return f"%__p32_{self.n}"

    def game_fn(self, operand: str) -> int | None:
        if operand.startswith("@"):
            return self.functions.get(operand[1:].strip('"'))
        return None

    # -- constant expressions

    def const(self, s: str) -> str:
        """Rewrite every addrspacecast / ptrtoint / inttoptr constant expression in s, innermost first."""
        out, i = [], 0
        pat = re.compile(r"\b(addrspacecast|ptrtoint|inttoptr) \(")
        while True:
            m = pat.search(s, i)
            if not m:
                out.append(s[i:])
                return "".join(out)
            end = _balanced_end(s, m.end() - 1)
            inner = self.const(s[m.end():end - 1])
            out.append(s[i:m.start()])
            out.append(self._const_node(m.group(1), inner))
            i = end

    def _const_node(self, op: str, inner: str) -> str:
        src, _, dst = inner.rpartition(" to ")
        dst = dst.strip()
        if op == "addrspacecast":
            if src.startswith(AS + " ") and dst == "ptr":
                return self.host_const(src[len(AS) + 1:].strip())
            if src.startswith("ptr ") and dst == AS:
                return self.ps1_const(src[4:].strip())
        elif op == "ptrtoint" and src.startswith("ptr ") and dst == "i32":
            addr = self.game_fn(src[4:].strip())
            if addr is not None:
                return str(_i32(addr))
        elif op == "inttoptr" and src.startswith("i32 ") and dst == "ptr":
            return self.host_const(f"inttoptr ({src} to {AS})")
        return f"{op} ({inner})"

    def ps1_const(self, x: str) -> str:
        """A host pointer constant (operand without its type) as an addrspace(271) constant."""
        addr = self.game_fn(x)
        if addr is not None:
            return f"inttoptr (i32 {_i32(addr)} to {AS})"
        if x == "null":
            return "null"
        return f"inttoptr (i32 ptrtoint (ptr {x} to i32) to {AS})"

    def host_const(self, x: str) -> str:
        """An addrspace(271) constant (operand without its type) as the host pointer constant it means."""
        if x == "null":  # PS1 address 0, which is in the window too
            return f"inttoptr (i64 {PSX_HOST_BASE} to ptr)"
        m = re.fullmatch(r"inttoptr \(i32 (-?\d+) to ptr addrspace\(271\)\)", x)
        if m:
            return f"inttoptr (i64 {_host_lit(int(m.group(1)))} to ptr)"
        m = re.fullmatch(r"inttoptr \(i32 ptrtoint \(ptr (" + _NAME + r") to i32\) to ptr addrspace\(271\)\)", x)
        if m:  # a host global's own address, round-tripped through 32 bits: itself (it is in the window)
            return m.group(1)
        return (f"getelementptr (i8, ptr inttoptr (i64 ptrtoint ({AS} {x} to i64) to ptr), i64 {PSX_HOST_BASE})")

    # -- values

    def to_host(self, x: str, pre: list[str], indent: str) -> str:
        """An addrspace(271) operand as an ordinary pointer operand; instructions it needs go to pre."""
        if not x.startswith("%"):
            return self.host_const(self.const(x))
        a, b, h = self.tmp(), self.tmp(), self.tmp()
        pre += [f"{indent}{a} = ptrtoint {AS} {x} to i64", f"{indent}{b} = or i64 {a}, {PSX_HOST_BASE}",
                f"{indent}{h} = inttoptr i64 {b} to ptr"]
        return h

    def _ptr_part(self, part: str, pre: list[str], indent: str) -> str:
        """`ptr addrspace(271) <attrs> X` -> `ptr <attrs> HOST`."""
        m = re.match(r"ptr addrspace\(271\)(" + _ATTRS + r")\s+", part)
        x = part[m.end():]
        return f"ptr{m.group(1)} " + self.to_host(x, pre, indent)

    # -- instructions

    def line(self, b: str) -> list[str]:
        """One instruction line: the lines it becomes."""
        if "addrspace(271)" not in b and "inttoptr" not in b and "ptrtoint" not in b:
            return [b]
        indent = re.match(r"\s*", b).group(0)
        dbg = re.search(r", !dbg !\d+", b)
        self.dbg = dbg.group(0) if dbg else ""
        m = _DEF.match(b)
        lhs, rhs = (f"{m.group(2)} = ", m.group(3)) if m else ("", b.strip())
        pre: list[str] = []
        rhs = self._instruction(lhs, rhs, pre, indent)
        if rhs is None:
            return pre
        return pre + [indent + lhs + self.const(rhs)]

    def _instruction(self, lhs: str, rhs: str, pre: list[str], indent: str) -> str | None:
        # 2./3. addrspacecast instructions
        m = re.fullmatch(r"addrspacecast (.*) to (ptr(?: addrspace\(271\))?)((?:, !.*)?)", rhs)
        if m:
            src, dst, md = m.groups()
            if src.startswith(AS + " ") and dst == "ptr":
                x = src[len(AS) + 1:].strip()
                if x.startswith("%"):
                    a, b = self.tmp(), self.tmp()
                    pre += [f"{indent}{a} = ptrtoint {AS} {x} to i64", f"{indent}{b} = or i64 {a}, {PSX_HOST_BASE}"]
                    return f"inttoptr i64 {b} to ptr{md}"
                return f"getelementptr i8, ptr {self.host_const(self.const(x))}, i64 0{md}"
            if src.startswith("ptr ") and dst == AS:
                x = src[4:].strip()
                addr = self.game_fn(x)
                if addr is not None:
                    return f"inttoptr i32 {_i32(addr)} to {AS}{md}"
                a = self.tmp()
                pre.append(f"{indent}{a} = ptrtoint ptr {self.const(x)} to i32")
                return f"inttoptr i32 {a} to {AS}{md}"
        # 4. a game function's address as an integer
        m = re.fullmatch(r"ptrtoint ptr (" + _NAME + r") to i32((?:, !.*)?)", rhs)
        if m and self.game_fn(m.group(1)) is not None:
            return f"bitcast i32 {_i32(self.game_fn(m.group(1)))} to i32{m.group(2)}"
        # a 32-bit integer as an ordinary pointer: a PS1 address
        m = re.fullmatch(r"inttoptr i32 (%[-\w.$]+) to ptr((?:, !.*)?)", rhs)
        if m:
            a, b = self.tmp(), self.tmp()
            pre += [f"{indent}{a} = zext i32 {m.group(1)} to i64", f"{indent}{b} = or i64 {a}, {PSX_HOST_BASE}"]
            return f"inttoptr i64 {b} to ptr{m.group(2)}"
        # 1. memory accesses
        m = re.match(r"(load (?:atomic )?(?:volatile )?)(.*)$", rhs)
        if m:
            parts = split_top(m.group(2))
            if len(parts) >= 2 and parts[1].startswith(AS + " "):
                parts[1] = self._ptr_part(parts[1], pre, indent)
                return m.group(1) + ", ".join(parts)
            return rhs
        m = re.match(r"(store (?:atomic )?(?:volatile )?)(.*)$", rhs)
        if m:
            parts = split_top(m.group(2))
            if len(parts) >= 2 and parts[1].startswith(AS + " "):
                parts[1] = self._ptr_part(parts[1], pre, indent)
                return m.group(1) + ", ".join(parts)
            return rhs
        m = re.match(r"(cmpxchg (?:weak )?(?:volatile )?|atomicrmw (?:volatile )?\w+ )(.*)$", rhs)
        if m:
            parts = split_top(m.group(2))
            if parts[0].startswith(AS + " "):
                parts[0] = self._ptr_part(parts[0], pre, indent)
                return m.group(1) + ", ".join(parts)
            return rhs
        mm = _MEM.search(rhs)
        if mm and "271" in mm.group(2):
            kind, width = mm.group(1), mm.group(3)
            end = _balanced_end(rhs, mm.end() - 1)
            args = split_top(rhs[mm.end():end - 1])
            args = [self._ptr_part(a, pre, indent) if a.startswith(AS) else a for a in args]
            spaces = ".p0" if kind == "memset" else ".p0.p0"
            self.need_mem.add((kind, width))
            return rhs[:mm.start()] + f"@llvm.{kind}{spaces}.{width}(" + ", ".join(args) + ")" + rhs[end:]
        # 5. calls through a 32-bit pointer
        m = re.match(r"((?:tail |musttail |notail )?(?:call|invoke)) addrspace\(271\) ", rhs)
        if m:
            return self._call(rhs, m, pre, indent)
        return rhs

    def _call(self, rhs: str, m: re.Match, pre: list[str], indent: str) -> str:
        head = m.group(1) + " "
        rest = rhs[m.end():]
        # the callee is the operand right before the argument list: a %value or a constant expression
        cm = re.search(r"(%[-\w.$]+)\(", rest)
        km = re.search(r"(addrspacecast|inttoptr) \(", rest)
        if km and (not cm or km.start() < cm.start()):
            start = km.start()
            end = _balanced_end(rest, km.end() - 1)
            callee = rest[start:end]
            if end >= len(rest) or rest[end] != "(":
                raise IRError(f"a call through a 32-bit pointer this doesn't understand: {rhs[:200]}")
            fm = re.fullmatch(r"addrspacecast \(ptr (" + _NAME + r") to ptr addrspace\(271\)\)", callee)
            if fm:  # a direct call written through a cast: call the function itself
                return head + rest[:start] + fm.group(1) + rest[end:]
            arg = self.const(callee)
            im = re.fullmatch(r"inttoptr \(i32 (-?\d+) to ptr addrspace\(271\)\)", arg)
            value = im.group(1) if im else f"ptrtoint ({AS} {arg} to i32)"
        elif cm:
            start, end = cm.start(1), cm.end(1)
            a = self.tmp()
            pre.append(f"{indent}{a} = ptrtoint {AS} {cm.group(1)} to i32")
            value = a
        else:
            raise IRError(f"a call through a 32-bit pointer this doesn't understand: {rhs[:200]}")
        h = self.tmp()
        pre.append(f"{indent}{h} = call ptr @psx_host_fn(i32 {value}){self.dbg}")
        self.need_host_fn = True
        return head + rest[:start] + h + rest[end:]


def patch(ir: str, functions: dict[str, int] | None = None) -> str:
    """The IR of one game file, translated (see the module docstring). functions: {name: PS1 address}."""
    t = Transform(functions)
    out = []
    in_fn = False
    for line in ir.split("\n"):
        if line.startswith("define "):
            in_fn = True
            out.append(line)
            continue
        if in_fn:
            if line == "}":
                in_fn = False
                out.append(line)
            elif not line.strip() or line.lstrip().startswith(";") or re.match(r"[-\w.$\"]+:", line):
                out.append(line)
            else:
                out += t.line(line)
            continue
        if re.match(r"declare .*@llvm\.(memcpy|memmove|memset)(\.p\d+)+\.i(32|64)\(", line) and "271" in line:
            continue
        if line.startswith("declare ") and "@psx_host_fn(" in line:
            continue
        out.append(line)
    text = "\n".join(out)
    for kind, width in sorted(t.need_mem):
        spaces = ".p0" if kind == "memset" else ".p0.p0"
        if not re.search(rf"declare [^\n]*@llvm\.{kind}{re.escape(spaces)}\.{width}\(", text):
            text += "\n" + MEM_DECL[kind].format(w=width) + "\n"
    if t.need_host_fn or "@psx_host_fn(" in text:
        text += "\n" + HOST_FN_DECL + "\n"
    return text


def leftovers(ir: str) -> list[str]:
    """Instructions that would still reach the backend with a 32-bit pointer in a place it can't translate."""
    bad = []
    for line in ir.split("\n"):
        s = line.strip()
        m = re.match(r"(?:%\S+ = )?(load|store|cmpxchg|atomicrmw)\b(?: atomic| volatile| weak)*(?: \w+(?= ))?\s*(.*)$", s)
        if m:
            parts = split_top(m.group(2))
            at = 0 if m.group(1) in ("cmpxchg", "atomicrmw") else 1
            if len(parts) > at and parts[at].startswith(AS + " "):
                bad.append(s)
                continue
        if re.search(r"\bcall addrspace\(271\)|@llvm\.mem\w+\.p271|addrspacecast", s):
            bad.append(s)
    return bad


def read_functions(path: str | None) -> dict[str, int]:
    out: dict[str, int] = {}
    if not path:
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) == 2 and not line.startswith("#"):
                out[parts[0]] = int(parts[1], 16)
    return out


def main(argv: list[str]) -> int:
    functions_file = None
    if argv[:1] == ["--functions"]:
        functions_file, argv = argv[1], argv[2:]
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
    try:
        patched = patch(ir, read_functions(functions_file))
    except IRError as e:
        print(f"cc_ptr32: {e}", file=sys.stderr)
        return 1
    bad = leftovers(patched)
    with open(ll, "w", encoding="utf-8") as fh:
        fh.write(patched)
    if bad:
        print(f"cc_ptr32: {ll}: {len(bad)} uses of a 32-bit pointer left untranslated, e.g. {bad[0][:200]}",
              file=sys.stderr)
        return 1
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
