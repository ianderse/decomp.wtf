"""Pass-through arguments made explicit (port hazard (a): docs/plans/port.md, "Dropped arguments").

A matched function that calls another with fewer arguments than the callee's definition takes is byte-exact on the
PS1 when its own MIPS code leaves the dropped argument registers alone from its entry to the call: the callee reads
what the caller's caller put there. On a host the same C hands the callee whatever is in the register. The fix
changes nothing on MIPS: the caller takes those arguments as parameters (named for the register: a0, a1, ...) and
passes them on. A caller that gains parameters makes its own callers drop them, one level up, so the parameter
lists are worked out to a fixed point first and the sources rewritten callee before caller: every chain ends where
the argument is a named variable (or where a caller's code writes the register first, which stays a hazard).

Pure text work over the one-function-per-file C (the same regex/bracket approach as export_port.call_hazards, with
K&R definitions and declarations inside a body handled); whether a rewrite still matches is decided by compiling and
linking it as the judge does (decomp fix-passthrough).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .export_port import _close, _split_commas, cast_calls as _cast_calls, passes_through

NOTE = "pass-through argument made explicit: {regs} {verb} {callee} unchanged (port hazard (a))"

_TYPE_WORDS = {"void", "char", "short", "int", "long", "float", "double", "signed", "unsigned", "const", "volatile",
               "struct", "union", "enum", "register", "static", "extern", "inline", "__inline", "__inline__"}
_NOT_DECL = {"return", "else", "do", "case", "goto", "sizeof", "typeof", "__typeof__", "if", "while", "for", "switch"}
_KEYWORDS = _NOT_DECL | _TYPE_WORDS | {"break", "continue", "default", "typedef", "__attribute__", "asm", "__asm__"}


# ------------------------------------------------------------------------------------------------- C, as text

def mask(text: str) -> str:
    """The same text, the same length, with comments, the insides of string and character literals and preprocessor
    lines blanked (newlines kept): positions in it are positions in the source."""
    out = list(text)
    n, i = len(text), 0

    def blank(a: int, b: int) -> None:
        for k in range(a, b):
            if out[k] != "\n":
                out[k] = " "

    line_start = True
    while i < n:
        c = text[i]
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            blank(i, j)
            i = j
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            blank(i, j)
            i = j
            continue
        if c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            blank(i + 1, min(j, n))
            i = min(j + 1, n)
            line_start = False
            continue
        if c == "#" and line_start:
            j = i
            while j < n and text[j] != "\n":
                j += 2 if text.startswith("\\\n", j) else 1
            blank(i, j)
            i = j
            continue
        if c == "\n":
            line_start = True
        elif not c.isspace():
            line_start = False
        i += 1
    return "".join(out)


@dataclass
class Param:
    """One parameter: `template` is its declaration with \\0 where the name goes ("void *\\0", "void (*\\0)(int)")."""
    template: str
    name: str | None

    def decl(self, name: str | None = None) -> str:
        nm = name or self.name or ""
        return re.sub(r"\s+", " ", self.template.replace("\0", nm)).replace("* ", "*").strip() \
            if nm else self.template.replace("\0", "").strip()

    @property
    def type(self) -> str:
        return re.sub(r"\s+", " ", self.template.replace("\0", "")).strip()


def parse_param(text: str) -> Param:
    """A parameter declaration as written: its type with a slot for the name, and the name (None when unnamed)."""
    t = text.strip()
    m = re.search(r"\(\s*(\*+)\s*([A-Za-z_]\w*)?\s*\)", t)
    if m:  # a function pointer: T (*name)(args)
        if m.group(2):
            return Param(t[:m.start(2)] + "\0" + t[m.end(2):], m.group(2))
        return Param(t[:m.end(1)] + "\0" + t[m.end(1):], None)
    m = re.search(r"([A-Za-z_]\w*)\s*((?:\[[^\]]*\]\s*)*)$", t)
    if m and m.group(1) not in _TYPE_WORDS:
        rest = t[:m.start(1)]
        words = re.findall(r"[A-Za-z_]\w*", rest)
        if "*" in rest or [w for w in words if w not in ("const", "volatile", "register")]:
            return Param(rest + "\0" + m.group(2), m.group(1))
    return Param(t + " \0", None)


@dataclass
class Definition:
    name: str
    start: int  # the head's first character (its return type)
    name_at: int
    lp: int
    rp: int
    brace: int
    end: int  # after the closing brace
    ret: str  # the head before the name ("void", "static s32 *")
    form: str  # "void", "empty" (`()`), "ansi", "knr"
    params: list[Param]
    variadic: bool = False
    knr_names: list[str] = field(default_factory=list)


@dataclass
class Prototype:
    name: str
    start: int
    end: int  # after the ;
    n: int | None  # None: `()`
    text: str
    inside: bool = False  # declared inside a function body


def _knr_head(seg: str) -> bool:
    """A top-level piece cut at its first ';' that is the start of a K&R definition: `T f(a, b) T a`."""
    m = re.search(r"\b([A-Za-z_]\w*)\s*\(([^()]*)\)\s*([^()]*)$", seg)
    if not m or m.group(1) in _KEYWORDS or "=" in seg:
        return False
    names = [x.strip() for x in m.group(2).split(",")]
    after = m.group(3).strip()
    return bool(after) and not after.startswith("__attribute__") and all(
        re.fullmatch(r"[A-Za-z_]\w*", x) and x not in _TYPE_WORDS for x in names) and bool(names[0])


def toplevel(text: str, masked: str | None = None) -> tuple[dict[str, Definition], list[Prototype]]:
    """A file's function definitions and top-level prototypes, with positions."""
    s = masked if masked is not None else mask(text)
    defs: dict[str, Definition] = {}
    protos: list[Prototype] = []
    i, start, n = 0, 0, len(s)
    while i < n:
        c = s[i]
        if c == ";":
            seg = s[start:i]
            if _knr_head(seg):
                i += 1
                continue
            protos += _prototypes(text, s, start, i + 1)
            start = i + 1
        elif c == "{":
            end = _close(s, i, "{", "}")
            d = _definition(text, s, start, i, end + 1)
            if d is not None:
                defs[d.name] = d
                i = start = end + 1
                continue
            i = end + 1  # a struct body or an initializer: the declaration goes on to its ;
            continue
        i += 1
    return defs, protos


def _definition(text: str, s: str, start: int, brace: int, end: int) -> Definition | None:
    head = s[start:brace]
    if re.match(r"\s*(?:typedef|struct|union|enum)\b[^(]*$", head) or re.match(r"\s*typedef\b", head):
        return None
    best = None
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", head):
        if m.group(1) in _KEYWORDS or head[:m.start()].count("(") != head[:m.start()].count(")"):
            continue
        if "=" in head[:m.start()]:
            return None
        best = m
    if best is None:
        return None
    lp = start + best.end() - 1
    rp = _close(s, lp)
    if rp >= brace:
        return None
    after = s[rp + 1:brace].strip()
    inner = s[lp + 1:rp]
    ws = start + len(head) - len(head.lstrip())
    ret = re.sub(r"\s+", " ", text[ws:start + best.start()]).strip()
    name = best.group(1)
    if after and not after.startswith("__attribute__"):
        names = [x.strip() for x in inner.split(",") if x.strip()]
        if not names or not all(re.fullmatch(r"[A-Za-z_]\w*", x) for x in names):
            return None
        types: dict[str, Param] = {}
        for decl in text[rp + 1:brace].split(";"):
            decl = decl.strip()
            if not decl:
                continue
            base, *rest = _split_commas(decl)
            first = parse_param(base)
            if first.name:
                types[first.name] = first
                stem = first.template.split("\0")[0]
                stem = re.sub(r"\*+\s*$", "", stem).strip()
                for r in rest:
                    p = parse_param(stem + " " + r.strip())
                    if p.name:
                        types[p.name] = p
        params = [types.get(x, Param("int \0", x)) for x in names]
        return Definition(name, ws, start + best.start(), lp, rp, brace, end, ret, "knr", params, False, names)
    t = re.sub(r"\s+", " ", text[lp + 1:rp]).strip()
    if t == "":
        return Definition(name, ws, start + best.start(), lp, rp, brace, end, ret, "empty", [])
    if t == "void":
        return Definition(name, ws, start + best.start(), lp, rp, brace, end, ret, "void", [])
    parts = [p.strip() for p in _split_commas(text[lp + 1:rp])]
    variadic = parts[-1] == "..."
    if variadic:
        parts = parts[:-1]
    return Definition(name, ws, start + best.start(), lp, rp, brace, end, ret, "ansi", [parse_param(p) for p in parts],
                      variadic)


def _prototypes(text: str, s: str, a: int, b: int, inside: bool = False) -> list[Prototype]:
    """The function prototypes of one declaration s[a:b] (ending with ;): only a declaration of a single function
    counts (`int f(void);`, `extern M2C_UNK f();`), at paren depth 0."""
    seg = s[a:b - 1]
    if re.match(r"\s*typedef\b", seg) or "=" in seg:
        return []
    found = []
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", seg):
        if m.group(1) in _KEYWORDS or seg[:m.start()].count("(") != seg[:m.start()].count(")"):
            continue
        found.append(m)
    if len(found) != 1:
        return []
    m = found[0]
    before = seg[:m.start()].strip()
    words = re.findall(r"[A-Za-z_]\w*", before)
    if not words or any(w in _NOT_DECL for w in words):
        return []
    lp = a + m.end() - 1
    rp = _close(s, lp)
    if rp >= b or s[rp + 1:b - 1].strip() and not s[rp + 1:b - 1].strip().startswith("__attribute__"):
        return []
    t = re.sub(r"\s+", " ", s[lp + 1:rp]).strip()
    nparams = None if t == "" else 0 if t == "void" else len([x for x in _split_commas(t) if x.strip() != "..."])
    ws = a + len(seg) - len(seg.lstrip())
    return [Prototype(m.group(1), ws, b, nparams, text[ws:b], inside)]


def body_prototypes(text: str, s: str, d: Definition) -> list[Prototype]:
    """Prototypes declared inside a definition's body (`void f(s32);` among its locals)."""
    out = []
    start = d.brace + 1
    depth = 0
    for k in range(d.brace + 1, d.end - 1):
        c = s[k]
        if c in "{}":
            depth += 1 if c == "{" else -1
            start = k + 1
        elif c == ";":
            if depth >= 0:
                out += _prototypes(text, s, start, k + 1, inside=True)
            start = k + 1
    return out


@dataclass
class Call:
    callee: str
    name_at: int
    lp: int
    rp: int
    nargs: int
    via: str = ""  # "" a direct call; "cast" through a cast of the callee; else the local pointer holding it
    sigs: list = field(default_factory=list)  # the function types written for a call through a cast (fn_type)


def body_calls(s: str, d: Definition, names: set[str]) -> list[Call]:
    """Direct calls of the named functions in a definition's body (masked text), not declarations, members or
    addresses taken."""
    out = []
    for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", s[d.brace:d.end]):
        name = m.group(1)
        if name not in names:
            continue
        at = d.brace + m.start()
        before = s[d.brace:at].rstrip()
        if before.endswith((".", "->", "&")) or re.search(
                r"\b(?:void|s8|u8|s16|u16|s32|u32|int|char|short|long|unsigned|signed|M2C_UNK\w*|extern|static|"
                r"[A-Za-z_]\w*_t)\s*\**$", before):
            continue
        lp = d.brace + m.end() - 1
        rp = _close(s, lp)
        args = s[lp + 1:rp].strip()
        out.append(Call(name, at, lp, rp, 0 if not args else len(_split_commas(args))))
    return out


def cast_calls(s: str, d: Definition, names: set[str]) -> list[Call]:
    """Calls of the named functions through a cast of them (export_port.cast_calls: `((T (*)(...)) func_X)(...)`,
    a local pointer that only holds func_X), positions in the file."""
    out = []
    for c in _cast_calls(s[d.brace:d.end], names):
        sigs = [{**g, "ret": (g["ret"][0] + d.brace, g["ret"][1] + d.brace),
                 "params": (g["params"][0] + d.brace, g["params"][1] + d.brace)} for g in c["sigs"]]
        out.append(Call(c["callee"], d.brace + c["at"], d.brace + c["lp"], d.brace + c["rp"], c["nargs"], c["via"],
                        sigs))
    return out


def all_calls(s: str, d: Definition, names: set[str]) -> list[Call]:
    """Direct calls and calls through a cast, in the order they're written."""
    return sorted(body_calls(s, d, names) + cast_calls(s, d, names), key=lambda c: c.lp)


# ------------------------------------------------------------------------------------------------- the plan

@dataclass
class Fn:
    name: str
    source: str  # its own matched source (export.own_source)
    words: list[int] | None  # its MIPS code
    vram: int


@dataclass
class Forward:
    caller: str
    callee: str
    nargs: int  # what the call passed
    upto: int  # positions nargs..upto-1 are now passed through
    need: int  # what the callee takes (after its own rewrite)
    status: str  # "pass-through", or why positions upto..need-1 stay a hazard ("leftover", "?", "stack argument")
    via: str = ""  # through a cast ("cast", or the local pointer's name): Call.via


@dataclass
class Plan:
    params: dict[str, list[Param]]  # each rewritten function's final parameters
    added: dict[str, int]  # how many each gained
    forwards: list[Forward]  # every call that drops arguments, fixed or not
    order: list[str]  # the rewritten functions, callee before caller
    level: dict[str, int]  # 1: forwards to a callee that wasn't rewritten; n: to one at level n-1
    defs: dict[str, Definition]
    masked: dict[str, str]
    unparsed: list[str]  # matched sources whose definition the scanner didn't find
    casts: int
    knr: list[str]
    cycles: list[str]


def plan(fns: dict[str, Fn], all_names: set[str] | None = None) -> Plan:
    """Each function's final parameter list, to a fixed point: a call that drops arguments whose registers the
    caller's code leaves alone (passes_through) makes the caller take them."""
    masked = {n: mask(f.source) for n, f in fns.items()}
    defs: dict[str, Definition] = {}
    unparsed = []
    for n, f in fns.items():
        d = toplevel(f.source, masked[n])[0].get(n)
        if d is None:
            unparsed.append(n)
        else:
            defs[n] = d
    names = set(defs)
    calls = {n: all_calls(masked[n], d, names) for n, d in defs.items()}
    casts = sum(len(cast_calls(masked[n], d, all_names or names)) for n, d in defs.items())
    final = {n: list(d.params) for n, d in defs.items()}
    forwards: dict[tuple[str, str, int], Forward] = {}
    for _ in range(64):
        changed = False
        for caller in sorted(defs):
            f = fns[caller]
            first: set[tuple] = set()
            for c in calls[caller]:
                g = defs[c.callee]
                need = len(final[c.callee])
                if g.variadic or c.nargs >= need:
                    continue
                key = (caller, c.callee, c.nargs)
                if key in first:  # judged at its first call (the later ones: rewrite passes them on too, or not)
                    continue
                first.add(key)
                # which jal to the callee this is, in the order written: a call after one that passed the argument
                # (func_X(a); ... func_X();) is judged at its own jal, after which the registers are the callee's
                nth = sum(1 for e in calls[caller] if e.callee == c.callee and e.lp < c.lp)
                top = min(need, 4)
                upto = c.nargs
                status = "pass-through"
                if f.words is None:
                    status = "?"
                else:
                    while upto < top:
                        st = passes_through(f.words, fns[c.callee].vram, upto, upto + 1, nth, vram=f.vram)
                        if st != "pass-through":
                            status = st
                            break
                        upto += 1
                if status == "pass-through" and upto < need:
                    status = "stack argument"
                via = c.via or (forwards[key].via if key in forwards else "")
                forwards[key] = Forward(caller, c.callee, c.nargs, upto, need, status, via)
                for i in range(len(final[caller]), upto):
                    tmpl = final[c.callee][i].template if i >= c.nargs else "s32 \0"
                    final[caller].append(Param(tmpl, f"a{i}"))
                    changed = True
        if not changed:
            break
    added = {n: len(final[n]) - len(defs[n].params) for n in defs if len(final[n]) > len(defs[n].params)}
    fw = sorted(forwards.values(), key=lambda x: (x.caller, x.callee, x.nargs))
    touched = added.keys() | {x.caller for x in fw if x.upto > x.nargs}
    deps = {n: {x.callee for x in fw if x.caller == n and x.upto > x.nargs and x.callee in touched and x.callee != n}
            for n in touched}
    order, done, cycles = [], set(), []
    while len(done) < len(touched):
        ready = sorted(n for n in touched if n not in done and deps[n] <= done)
        if not ready:
            rest = sorted(n for n in touched if n not in done)
            cycles += rest
            ready = rest[:1]
        for n in ready:
            order.append(n)
            done.add(n)
    level: dict[str, int] = {}
    for n in order:
        level[n] = 1 + max((level.get(g, 0) for g in deps[n] if g in added), default=0)
    return Plan({n: final[n] for n in touched}, added, fw, order, level, defs, masked, unparsed, casts,
                sorted(n for n, d in defs.items() if d.form == "knr"), cycles)


# ------------------------------------------------------------------------------------------------- rewriting

def declaration(name: str, ret: str, params: list[Param], variadic: bool = False) -> str:
    """`ret name(params);` for a prototype: storage class and inline dropped from the return type."""
    r = re.sub(r"\b(?:static|inline|__inline__|__inline|extern)\b\s*", "", ret).strip()
    ps = ", ".join(p.decl() for p in params) or "void"
    if variadic:
        ps += ", ..."
    sep = "" if r.endswith("*") else " "
    return f"{r}{sep}{name}({ps});"


def generic(p: Param) -> Param:
    """The parameter typed as s32, or void * for a pointer (for a callee whose types the caller can't name)."""
    t = p.type
    return Param("void *\0" if "*" in t or "[" in t else "s32 \0", p.name)


@dataclass
class Rewrite:
    name: str
    source: str
    variant: str  # "real" (the callee's own signature), "caller-return" (the caller's return type kept), "generic"
    regs: list[str]  # the registers made explicit, for the note
    callees: list[str]
    header_old: str | None = None
    header_new: str | None = None


def rewrite(p: Plan, fn: Fn, variant: str = "real", header: str = "") -> Rewrite | None:
    """The caller's source with its new parameters, the arguments passed on at each call that forwards them, and its
    own prototypes of those callees corrected to their (rewritten) signatures. variant: "real" uses the callees'
    return and parameter types; "caller-return" keeps the return type of the caller's own prototype; "generic" also
    types every parameter s32 / void *; with "+first-call" (e.g. "real+first-call") only the first call of each
    callee in the source passes them on (on MIPS the later ones get whatever the register holds by then: they stay
    hazards, under a `()` prototype). Also the header edit when the header declares this function with other
    parameters: (header_old, header_new). None when nothing changes."""
    name = fn.name
    kind, first_only = variant.split("+")[0], variant.endswith("+first-call")
    d = p.defs.get(name)
    if d is None or name not in p.params:
        return None
    text, s = fn.source, p.masked[name]
    final = p.params[name]
    new_params = final if kind != "generic" else [generic(x) if i >= len(d.params) else x
                                                     for i, x in enumerate(final)]
    edits: list[tuple[int, int, str]] = []
    regs: list[str] = []
    if len(final) > len(d.params):
        if d.form in ("void", "empty"):
            edits.append((d.lp + 1, d.rp, ", ".join(x.decl() for x in new_params)))
        elif d.form == "ansi":
            edits.append((d.rp, d.rp, ", " + ", ".join(x.decl() for x in new_params[len(d.params):])))
        else:  # K&R: the names in the list, the declarations before the body
            edits.append((d.rp, d.rp, ", " + ", ".join(x.name for x in new_params[len(d.params):])))
            edits.append((d.brace, d.brace, "".join(x.decl() + ";\n" for x in new_params[len(d.params):])))
    fws = {(x.callee, x.nargs): x for x in p.forwards if x.caller == name and x.upto > x.nargs}
    callees = sorted({x.callee for x in fws.values()})
    short: set[str] = set()  # callees some call of which still passes fewer than they take
    seen: set[tuple[str, int]] = set()
    sig_edits: dict[tuple[int, int], str] = {}  # a cast's type, rewritten once however many calls go through it
    for c in all_calls(s, d, set(p.defs)):
        x = fws.get((c.callee, c.nargs))
        if x is not None and first_only and (c.callee, c.nargs) in seen:
            x = None
        seen.add((c.callee, c.nargs))
        takes = len(p.params.get(c.callee, p.defs[c.callee].params))
        if c.callee != name and not p.defs[c.callee].variadic and (x.upto if x else c.nargs) < takes and not c.via:
            short.add(c.callee)
        if x is None:
            continue
        args = ", ".join(final[i].name for i in range(x.nargs, x.upto))
        if c.nargs == 0:
            edits.append((c.lp + 1, c.rp, args))
        else:
            edits.append((c.rp, c.rp, ", " + args))
        regs += [f"$a{i}" for i in range(x.nargs, x.upto)]
        for g in c.sigs:  # the cast's own parameter list: the callee's real one (types only), or as wide as passed
            if g["n"] is None or g["n"] >= x.upto:
                continue
            gp = p.params.get(c.callee, p.defs[c.callee].params)
            have = [t.strip() for t in _split_commas(text[g["params"][0] + 1:g["params"][1]])] if g["n"] else []
            if kind == "real":
                types = [q.type for q in gp[:x.upto]]
                ret = re.sub(r"\b(?:static|inline|__inline__|__inline|extern)\b\s*", "", p.defs[c.callee].ret).strip()
                sig_edits[g["ret"]] = ret + " "
            else:
                types = have + [(generic(q) if kind == "generic" else q).type for q in gp[len(have):x.upto]]
            sig_edits[(g["params"][0] + 1, g["params"][1])] = ", ".join(types) or "void"
    edits += [(a, b, t) for (a, b), t in sig_edits.items()]
    regs = sorted(set(regs))
    _, protos = toplevel(text, s)
    protos += body_prototypes(text, s, d)
    hdr = {pr.name: pr for pr in toplevel(header)[1]} if header else {}
    insert = []
    # the prototypes of every function whose parameters change (the callees, and any other one this file declares,
    # often a definition own_source made a prototype): the full new signature, or `ret g();` where a call still
    # passes fewer arguments than it takes (a hazard left for later, but one that compiles)
    changed = sorted(set(callees) | {pr.name for pr in protos if pr.name in p.added and pr.name != name})
    for g in changed:
        gd = p.defs[g]
        gparams = p.params.get(g, gd.params)
        mine = [pr for pr in protos if pr.name == g]
        ret = gd.ret
        if (kind != "real" or g not in callees) and mine:
            m = re.match(rf"\s*(?:extern\s+)?(.*?)\b{re.escape(g)}\s*\(", mine[0].text, re.S)
            ret = m.group(1).strip() if m and m.group(1).strip() else gd.ret
        decl = declaration(g, ret, [generic(x) for x in gparams] if kind == "generic" else gparams, gd.variadic)
        if g in short or g not in callees:  # (one only declared here: `()` agrees with the header before and after)
            if any(_NARROW.search(x.type) for x in gparams):
                continue  # `()` would promote them: leave the old prototype
            decl = unprototyped(decl)
        for pr in mine:
            if pr.n is None and g not in callees:
                continue
            lead = "extern " if pr.text.lstrip().startswith("extern") else ""
            edits.append((pr.start, pr.end, lead + decl))
        h = hdr.get(g)
        if g in callees and not mine and not (h is not None and (h.n == len(gparams) or h.n is None and g in short)):
            insert.append(decl)
    for pr in protos:  # its own prototype in its own file
        if pr.name == name and pr.n != len(final):
            edits.append((pr.start, pr.end, declaration(name, d.ret, new_params, d.variadic)))
    if insert:
        edits.append((d.start, d.start, "".join(x + "\n" for x in insert) + "\n"))
    if not edits:
        return None
    out = text
    for a, b, t in sorted(edits, key=lambda e: (e[0], e[1]), reverse=True):
        out = out[:a] + t + out[b:]
    rw = Rewrite(name, out, variant, regs, callees)
    h = hdr.get(name)
    if h is not None and h.n is not None and h.n != len(final) and not h.inside:
        rw.header_old = h.text
        rw.header_new = declaration(name, d.ret, new_params, d.variadic)
    elif h is not None and h.n is None and any(re.search(r"\b(?:u8|s8|u16|s16|char|short|float)\b", x.type)
                                                for x in new_params):
        rw.header_old = h.text  # an unprototyped declaration isn't compatible with a narrow parameter
        rw.header_new = declaration(name, d.ret, new_params, d.variadic)
    return rw


def note(rw: Rewrite) -> str:
    regs = ", ".join(rw.regs)
    return NOTE.format(regs=regs, verb="reaches" if len(rw.regs) == 1 else "reach", callee=", ".join(rw.callees))


def header_callers(p: Plan, name: str, fns: dict[str, Fn]) -> list[str]:
    """Matched functions that call `name` with fewer arguments than its new parameter list through the header's
    prototype (no prototype of their own): the header can't say the full signature until they pass them too."""
    n = len(p.params.get(name, ()))
    out = []
    for other, d in p.defs.items():
        if other == name:
            continue
        s = p.masked[other]
        if not any(c.callee == name and c.nargs < n for c in body_calls(s, d, {name})):
            continue
        protos = [pr for pr in toplevel(fns[other].source, s)[1] + body_prototypes(fns[other].source, s, d)
                  if pr.name == name]
        if not protos:
            out.append(other)
    return sorted(out)


def unprototyped(decl: str) -> str:
    """`ret name(params);` as `ret name();`: compatible with the old (void) callers and the new definition alike, as
    long as no parameter is narrower than int."""
    return re.sub(r"\(.*\)\s*;\s*$", "();", decl.strip(), flags=re.S)


_NARROW = re.compile(r"\b(?:u8|s8|u16|s16|char|short|float)\b")


# ------------------------------------------------------------------------------------------------- driving it

def load(net, slug: str) -> tuple:
    """(project row, {name: function row} of the matched ones, {name: Fn}, every function's name) from the
    database, read only. Each Fn's source is the function's own (export.own_source), its words its target code."""
    import struct

    from .export import own_source

    proj = net._project(slug)
    rows = net.store.all("SELECT * FROM functions WHERE project_id=?", proj["id"])
    names = {r["name"] for r in rows}
    matched = {r["name"]: r for r in rows if r["status"] == "matched" and r["matched_source"]}
    fns = {}
    for n, r in matched.items():
        t = bytes(r["target"] or b"")
        words = list(struct.unpack(f"<{len(t) // 4}I", t[:len(t) // 4 * 4])) if t else None
        fns[n] = Fn(n, own_source(r["matched_source"], n, names), words, r["vram"])
    return proj, matched, fns, names


def all_code(net, proj) -> dict[str, Fn]:
    """Every function with its target code (matched or not), as Fn with no source: what callee_reads follows."""
    import struct
    out = {}
    for r in net.store.all("SELECT name, vram, target FROM functions WHERE project_id=?", proj["id"]):
        t = bytes(r["target"] or b"")
        if len(t) >= 4:
            out[r["name"]] = Fn(r["name"], "", list(struct.unpack(f"<{len(t) // 4}I", t[:len(t) // 4 * 4])), r["vram"])
    return out


def verify(net, proj, row, source: str, header: str) -> tuple[bool, str]:
    """Compile and judge a source as the judge judges a match (net._compile with the function's own compiler, then
    net._evaluate: linked at its address): (exact, what went wrong)."""
    from .. import scoring

    res = net._compile(proj, header, source, row)
    if not res.ok:
        lines = [ln.strip() for ln in res.log.strip().splitlines() if "error" in ln.lower()] or \
            res.log.strip().splitlines()[:3]
        return False, "compile error: " + " | ".join(lines)[:400]
    cand, exact, problems = net._evaluate(row, proj, res.obj)
    if cand is None:
        return False, "no function of that name in the object"
    target = net._target_obj(row, proj)
    if exact is None:
        exact = cand.fingerprint() == target.fingerprint()
    if exact:
        return True, ""
    d = scoring.diff(target, cand)
    diffs = [f"{m} {t or '-'} / {c or '-'}" for m, t, c in d.rows if m != " "][:4]
    return False, f"score {d.score}: " + " | ".join(diffs) + ("; " + "; ".join(problems) if problems else "")


VARIANTS = ("real", "caller-return", "generic", "real+first-call", "caller-return+first-call", "generic+first-call")


def callee_reads(fns: dict[str, Fn], x: Forward, _memo: dict | None = None) -> dict:
    # fns: every function whose code is known (matched or not: a call to one still in assembly reads what its code
    # reads), each with its vram
    """For a call that still drops arguments: which of them the callee's code can read ({"reads": ["$a1", "stack
    4"]}; [] when it reads none of them, so what the caller leaves there doesn't matter on MIPS), by liveness at its
    entry (export_port.mips_arg_live, a call to another matched function reading what that one reads) and its loads
    of the stack arguments. {} when the callee's code isn't known."""
    from .export_port import mips_arg_live, mips_stack_arg_read
    g = fns.get(x.callee)
    if g is None or g.words is None:
        return {}
    by_vram: dict[int, list[Fn]] = {}
    for f in fns.values():
        by_vram.setdefault(f.vram, []).append(f)
    memo = {} if _memo is None else _memo
    busy: set[int] = set()

    def reads_of(v: int):
        if v in memo:
            return memo[v]
        fs = by_vram.get(v) or []
        if len(fs) != 1 or fs[0].words is None or v in busy:  # unknown, an overlay's address, or recursion
            return None
        busy.add(v)
        memo[v] = mips_arg_live(fs[0].words, v, reads_of)
        busy.discard(v)
        return memo[v]
    live = mips_arg_live(g.words, g.vram, reads_of)
    regs = [f"$a{i}" for i in range(x.upto, min(x.need, 4)) if i in live]
    stack = [f"stack {k}" for k in range(max(4, x.upto), x.need) if mips_stack_arg_read(g.words, k)]
    return {"reads": regs + stack}


def fix(net, slug: str, out_dir=None, limit: int | None = None, apply: bool = False, post=None, log=print) -> dict:
    """The dry run (and with apply, the submissions): plan, rewrite and verify each caller callee-first (each
    variant in VARIANTS until one is byte-identical), write before/after into out_dir, and with apply post each
    verified one to /api/replace (post(path, body) -> dict), stopping at the first that isn't replaced."""
    import json
    from pathlib import Path

    proj, rows, fns, names = load(net, slug)
    p = plan(fns, names)
    units = net._unit_contexts(proj["id"])
    headers: dict[str, str] = {}
    results: list[dict] = []
    status: dict[str, str] = {}
    pending_full: list[dict] = []
    order = p.order[:limit] if limit else p.order
    for n in order:
        row = rows[n]
        unit = net._effective_unit(row, units)
        if unit not in headers:
            headers[unit] = net._context(proj["id"], unit)["header"]
        mine = [x for x in p.forwards if x.caller == n and x.upto > x.nargs]
        bad = sorted({x.callee for x in mine if x.callee in p.added and x.upto > len(p.defs[x.callee].params)
                      and status.get(x.callee) != "verified"})
        entry = {"name": n, "level": p.level.get(n, 1), "added": p.added.get(n, 0),
                 "forwards": [{"callee": x.callee, "args": x.nargs, "now": x.upto, "takes": x.need} for x in mine]}
        if bad:
            entry.update(status="skipped", why=f"its callee {', '.join(bad)} wasn't verified")
            status[n] = "skipped"
            results.append(entry)
            log(f"  {n}: skipped ({entry['why']})")
            continue
        why, done, closest = [], None, None  # closest: (score, variant, source) of the nearest miss
        for variant in VARIANTS:
            rw = rewrite(p, fns[n], variant, headers[unit])
            if rw is None:
                why.append(f"{variant}: nothing to rewrite")
                break
            header = headers[unit]
            if rw.header_old:
                # `ret f();` first: every other match that declares or calls f the old way still compiles against
                # it; the full prototype once they're all rewritten (the last step of --apply)
                if not any(_NARROW.search(x.type) for x in p.params[n]):
                    rw.header_new = unprototyped(rw.header_new)
                    entry["header_callers"] = header_callers(p, n, fns)
                header = header.replace(rw.header_old, rw.header_new, 1)
            ok, msg = verify(net, proj, row, rw.source, header)
            if ok:
                done = rw
                headers[unit] = header
                break
            why.append(f"{variant}: {msg}")
            m = re.match(r"score (\d+)", msg)
            if m and (closest is None or int(m.group(1)) < closest[0]):
                closest = (int(m.group(1)), variant, rw.source)
        if done is None:
            # the nearest miss is the one to finish (by hand or decomp-permuter: `decomp permuter export
            # --passthrough`); "real" when none compiled
            rw0 = rewrite(p, fns[n], "real", headers[unit])
            entry.update(status="failed", why=why, source=closest[2] if closest else rw0.source if rw0 else None)
            if closest:
                entry.update(closest={"score": closest[0], "variant": closest[1]})
            status[n] = "failed"
        else:
            entry.update(status="verified", variant=done.variant, regs=done.regs, callees=done.callees,
                         note=note(done), source=done.source)
            if done.header_old:
                entry["header"] = {"old": done.header_old, "new": done.header_new}
                if "header_callers" in entry:
                    pending_full.append({"name": n, "old": done.header_new,
                                         "new": declaration(n, p.defs[n].ret, p.params[n], p.defs[n].variadic),
                                         "callers": entry["header_callers"], "source": done.source})
            status[n] = "verified"
        results.append(entry)
        log(f"  {n}: {entry['status']}" + (f" ({entry['variant']})" if entry["status"] == "verified"
                                           else f": {entry['why'][0][:200]}"))
    code, memo = all_code(net, proj), {}
    summary = {
        "matched": len(fns), "unparsed": p.unparsed, "knr": p.knr, "casts_seen": p.casts, "cycles": p.cycles,
        "dropped_through_cast": sum(1 for x in p.forwards if x.via),
        "forwarded_through_cast": sum(1 for x in p.forwards if x.via and x.upto > x.nargs),
        "dropped_calls": len(p.forwards),
        "dropped_by_status": {k: sum(1 for x in p.forwards if x.status == k) for k in sorted({x.status for x in p.forwards})},
        "forwarded_calls": sum(1 for x in p.forwards if x.upto > x.nargs),
        "rewritten": len(order), "gain_parameters": len(p.added),
        "levels": {str(k): sum(1 for n in order if p.level.get(n) == k) for k in sorted(set(p.level.values()))},
        "verified": sum(1 for r in results if r["status"] == "verified"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "skipped": sum(1 for r in results if r["status"] == "skipped"),
        "variants": {v: sum(1 for r in results if r.get("variant") == v) for v in VARIANTS},
        "header_edits": [{"name": r["name"], **r["header"], "callers": r.get("header_callers", [])}
                         for r in results if r.get("header")],
        "residual": [{"caller": x.caller, "callee": x.callee, "args": x.nargs, "takes": x.need, "from": x.upto,
                      "why": x.status, **({"via": x.via} if x.via else {}), **callee_reads(code, x, memo)}
                     for x in p.forwards if x.upto < x.need],
    }
    summary["residual_callee_ignores"] = sum(1 for r in summary["residual"] if r.get("reads") == [])
    if out_dir is not None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        for old in out.glob("*.c"):
            old.unlink()
        for r in results:
            (out / f"{r['name']}.before.c").write_text(rows[r["name"]]["matched_source"], encoding="utf-8")
            if r.get("source"):
                suffix = "after" if r["status"] == "verified" else "failed"
                (out / f"{r['name']}.{suffix}.c").write_text(r["source"], encoding="utf-8")
        (out / "report.json").write_text(json.dumps({"summary": summary, "functions": [
            {k: v for k, v in r.items() if k != "source"} for r in results]}, indent=1) + "\n", encoding="utf-8")
    summary["functions"] = results
    if not apply:
        return summary
    applied = []
    for r in results:
        if r["status"] != "verified":
            continue
        body = {"slug": slug, "name": r["name"], "source": r["source"], "note": r["note"]}
        if r.get("header"):
            body.update(header_old=r["header"]["old"], header_new=r["header"]["new"],
                        message=f"{r['name']} takes its pass-through arguments ({', '.join(r['regs'])})")
        res = post("/api/replace", body)
        if not res.get("replaced"):
            summary["stopped"] = {"name": r["name"], "text": res.get("text") or res.get("error")}
            log(f"stopped at {r['name']}: {(res.get('text') or res.get('error') or '')[:400]}")
            break
        applied.append(r["name"])
        log(f"  replaced {r['name']}")
    else:
        for f in pending_full:  # the header's full prototype, once every caller through it passes the arguments
            if not all(status.get(c) == "verified" for c in f["callers"]):
                continue
            res = post("/api/replace", {"slug": slug, "name": f["name"], "source": f["source"],
                                        "note": "the header's prototype made full again",
                                        "header_old": f["old"], "header_new": f["new"],
                                        "message": f"{f['name']}'s full prototype, now that its callers pass it on"})
            log(f"  header prototype of {f['name']}: {'full' if res.get('replaced') else 'left unprototyped'}")
    summary["applied"] = applied
    return summary
