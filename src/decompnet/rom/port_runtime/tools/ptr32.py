#!/usr/bin/env python3
"""Give the game's C the PS1's data layout on a 64-bit host: every pointer declarator becomes a 32-bit, zero-extended
pointer (clang's `__ptr32 __uptr`, with -fms-extensions) and `long` becomes `int`. With PS1 RAM at 0x80000000 and the
host code and the game's stack below 4 GB, every such pointer is valid, and a pointer stored in the game's RAM is the
4 bytes the game wrote.

Token level, no parser: a `*` is a pointer declarator when it follows a type (a builtin type, a typedef name, a
struct/union/enum tag or body), a qualifier, another declarator `*`, or a `(` that follows a type (`void (*fp)(...)`,
`(u8 (**)())`), or a declaration's comma (`void *p, *q;`). Everything else (multiplication, dereference) is left
alone. Calls and memory copies through these pointers need tools/cc_ptr32.py (clang's x86-64 backend can't do them).

  ptr32.py --src DIR --out DIR --list FILE [--headers DIR]

rewrites each path in FILE (relative to --src) to the same path under --out, and every *.h in --headers to
--out/include, skipping outputs newer than their input. Used by the port's CMakeLists.txt, and by decomp.wtf's
generator for its compile check. Standard library only.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

PTR = " __ptr32 __uptr "
BUILTIN = {"void", "char", "short", "int", "long", "float", "double", "signed", "unsigned", "_Bool", "__int128"}
QUALIFIERS = {"const", "volatile", "restrict", "__restrict", "__restrict__", "__const", "__volatile__", "__volatile"}
TAGS = {"struct", "union", "enum"}
KEYWORDS = BUILTIN | QUALIFIERS | TAGS | {
    "auto", "break", "case", "continue", "default", "do", "else", "extern", "for", "goto", "if", "inline", "register",
    "return", "sizeof", "static", "switch", "typedef", "while", "asm", "__asm__", "__attribute__", "__extension__",
    "__inline", "__inline__", "__typeof__", "typeof"}

_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<comment>/\*.*?\*/|//[^\n]*)
  | (?P<str>"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*')
  | (?P<ident>[A-Za-z_]\w*)
  | (?P<num>\.?\d[\w.]*(?:[eEpP][+-]\w*)?)
  | (?P<punct>\.\.\.|->|\+\+|--|<<=|>>=|<<|>>|<=|>=|==|!=|&&|\|\||[-+*/%&|^]=|.)
""", re.S | re.X)


def tokens(text: str) -> list[tuple[str, str]]:
    out, i = [], 0
    while i < len(text):
        m = _TOKEN.match(text, i)
        kind = m.lastgroup
        out.append((kind, m.group()))
        i = m.end()
    return out


def split_lines(text: str):
    """(is_directive, chunk): preprocessor directives (with continuations) apart from code."""
    out, buf = [], []
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("#"):
            if buf:
                out.append((False, "".join(buf)))
                buf = []
            d = [line]
            while d[-1].rstrip("\r\n").endswith("\\") and i + 1 < len(lines):
                i += 1
                d.append(lines[i])
            out.append((True, "".join(d)))
        else:
            buf.append(line)
        i += 1
    if buf:
        out.append((False, "".join(buf)))
    return out


def typedef_names(text: str) -> set[str]:
    """Names declared by typedefs (outside struct bodies; a function pointer typedef's name in its first group)."""
    names: set[str] = set()
    for is_dir, chunk in split_lines(text):
        if is_dir:
            continue
        toks = [t for t in tokens(chunk) if t[0] not in ("ws", "comment")]
        i = 0
        while i < len(toks):
            if toks[i] != ("ident", "typedef"):
                i += 1
                continue
            j, brace, paren, prev = i + 1, 0, 0, None
            while j < len(toks):
                k, v = toks[j]
                if v == "{":
                    brace += 1
                elif v == "}":
                    brace -= 1
                elif brace == 0:
                    if v == "(":
                        paren += 1
                    elif v == ")":
                        paren -= 1
                    elif v == ";" and paren == 0:
                        break
                    elif k == "ident" and v not in KEYWORDS:
                        nxt = toks[j + 1][1] if j + 1 < len(toks) else ";"
                        if paren == 0 and nxt in (";", ",", "[", "("):
                            names.add(v)
                        elif paren == 1 and prev == "*" and nxt in (")", "["):
                            names.add(v)
                prev = v if brace == 0 else prev
                j += 1
            i = j + 1
    return names


STORAGE = {"extern", "static", "register", "auto", "typedef", "inline", "__inline", "__inline__", "__extension__"}


def rewrite_code(text: str, types: set[str]) -> str:
    toks = tokens(text)
    out: list[str] = []
    sig: list[tuple[int, str, str]] = []  # significant tokens: (index in out, kind, value)
    pointer_star: set[int] = set()        # indices in sig of declarator stars
    brace_type: list[bool] = []           # for each open brace: a struct/union/enum body?
    closed_type_brace: set[int] = set()   # sig indices of } closing a type body
    local = set(types)
    paren = 0
    stmt_decl: bool | None = None         # does the current statement (at this paren depth 0) declare something?
    after_comma = False                   # just after a declaration's `,`: a `*` here is a declarator
    outer: list[bool | None] = []         # stmt_decl of the declarations around each struct body

    def is_type(n: int) -> bool:
        if n < 0:
            return False
        _, k, v = sig[n]
        if k == "ident" and (v in BUILTIN or v in local):
            return True
        if k == "ident" and n > 0 and sig[n - 1][2] in TAGS:
            return True
        if v == "}" and n in closed_type_brace:
            return True
        return False

    for kind, v in toks:
        if kind in ("ws", "comment"):
            out.append(v)
            continue
        n = len(sig)
        if paren == 0 and stmt_decl is None and v not in STORAGE and v not in QUALIFIERS and v not in ("{", "}", ";"):
            stmt_decl = (kind == "ident" and (v in BUILTIN or v in local or v in TAGS))
        if v == "*":
            prev = n - 1
            ptr = after_comma
            if prev >= 0 and not ptr:
                pv = sig[prev][2]
                if is_type(prev) or pv in QUALIFIERS or (pv == "*" and prev in pointer_star):
                    ptr = True
                elif pv == "(" and is_type(prev - 1):
                    ptr = True  # void (*fp)(...), u8 (**)()
            out.append(v)
            if ptr:
                out.append(PTR)
                pointer_star.add(n)
            sig.append((len(out) - 1 - (1 if ptr else 0), kind, v))
            continue
        after_comma = False
        if v == "(":
            paren += 1
        elif v == ")":
            paren = max(0, paren - 1)
        elif v == "," and paren == 0 and stmt_decl:
            after_comma = True
        elif v == "{":
            brace_type.append(n > 0 and (sig[n - 1][2] in TAGS or (n > 1 and sig[n - 2][2] in TAGS)))
            if not brace_type[-1]:
                stmt_decl = None  # a block (or an initializer: its statement goes on after the })
        elif v == "}":
            if brace_type and brace_type.pop():
                closed_type_brace.add(n)
                stmt_decl = outer.pop() if outer else None  # `} *p, *q;` goes on declaring
            else:
                stmt_decl = None
        elif v == ";" and paren == 0:
            stmt_decl = None
        out.append(v)
        sig.append((len(out) - 1, kind, v))
        if v == "{" and brace_type and brace_type[-1]:
            outer.append(stmt_decl)
            stmt_decl = None  # struct members are statements of their own

    # long -> int (needs both neighbours): long is 32 bits on the PS1; `long long` stays
    for i, (at, kind, v) in enumerate(sig):
        if kind == "ident" and v == "long":
            before = sig[i - 1][2] if i else ""
            after = sig[i + 1][2] if i + 1 < len(sig) else ""
            if before == "long" or after == "long" or after == "double":
                continue
            out[at] = "" if after == "int" else "int"
    return "".join(out)


def rewrite(text: str, types: set[str]) -> str:
    """A whole file: code and #define bodies rewritten, other directives as they are."""
    local = set(types) | typedef_names(text)
    out = []
    for is_dir, chunk in split_lines(text):
        if not is_dir:
            out.append(rewrite_code(chunk, local))
            continue
        m = re.match(r"(\s*#\s*define\s+\w+(?:\([^)]*\))?)(.*)", chunk, re.S)
        out.append(m.group(1) + rewrite_code(m.group(2), local) if m else chunk)
    return "".join(out)


def header_types(headers: list[Path]) -> set[str]:
    names: set[str] = set()
    for h in headers:
        names |= typedef_names(h.read_text(encoding="utf-8", errors="replace"))
    return names


_SELF = Path(__file__).stat().st_mtime  # a new rewriter rewrites everything


def _newer(src: Path, dst: Path) -> bool:
    return not dst.exists() or dst.stat().st_mtime < max(src.stat().st_mtime, _SELF)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--list", required=True, help="file with one source path (relative to --src) per line")
    ap.add_argument("--headers", help="directory whose *.h are rewritten into --out/include")
    ap.add_argument("--stamp", help="touch this file when done")
    a = ap.parse_args(argv)
    src, out = Path(a.src), Path(a.out)
    headers = sorted(Path(a.headers).glob("*.h")) if a.headers else []
    headers = [h for h in headers if h.name != "psx_gte.h"]  # the host's own is force-included
    types = header_types(headers)
    stale_headers = any(_newer(h, out / "include" / h.name) for h in headers)
    n = 0
    for h in headers:
        dst = out / "include" / h.name
        if _newer(h, dst):
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(rewrite(h.read_text(encoding="utf-8", errors="replace"), types), encoding="utf-8")
            n += 1
    for line in Path(a.list).read_text(encoding="utf-8").splitlines():
        p = line.strip()
        if not p or p.startswith("#"):
            continue
        s, d = src / p, out / p
        if stale_headers or _newer(s, d):
            d.parent.mkdir(parents=True, exist_ok=True)
            d.write_text(rewrite(s.read_text(encoding="utf-8", errors="replace"), types), encoding="utf-8")
            n += 1
    if a.stamp:
        Path(a.stamp).parent.mkdir(parents=True, exist_ok=True)
        Path(a.stamp).write_text(f"{n}\n", encoding="utf-8")
        os.utime(a.stamp)
    print(f"ptr32: {n} files rewritten into {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
