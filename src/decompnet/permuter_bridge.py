"""Near misses for decomp-permuter (simonlindholm/decomp-permuter), run on another computer, and its results back.

The built-in permuter (app/permute.py) knows a handful of rewrites; decomp-permuter knows many more and is what the
decomp community uses for the last few percent (register allocation, statement order). It's CPU-hungry, so it runs
on a bigger machine: `export` writes one directory per near miss in the layout it expects (base.c: the function,
preprocessed; target.o: the original, assembled from the game's own instructions; compile.sh: this app's exact
pipeline for that function, GCC -> maspsx -> as; settings.toml), plus setup.sh, run once there to point the
directories at that machine's tools. `import_results` reads what it found back and puts each candidate through the
app's own judge, so only a real match or a real improvement is recorded.

The toolchains are this app's, in their usual place on a Mac (Library/Application Support/decompnet/toolchains):
install decomp.wtf on the permuter machine and the compilers once (Compilers & tools), and the paths line up.
"""

from __future__ import annotations

import json
import re
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import diagnosis, experiments, toolchains
from .compiler import block_comments
from .project import compiler_config, toolchain_id, with_toolchain

PRELUDE = """.set noat
.set noreorder
.set gp=32
.macro glabel label
    .global \\label
    .type \\label, @function
    \\label:
.endm
.macro jlabel label
    \\label:
.endm
.macro dlabel label
    .global \\label
    \\label:
.endm
.macro endlabel label
.endm
.macro nonmatching label, size=1
.endm
.section .text
"""
_TC_ROOT = "$HOME/Library/Application Support/decompnet/toolchains"


# maspsx needs Python 3.10 or newer, and a Mac's /usr/bin/python3 is 3.9: PY=/path/to/python overrides the search
_FIND_PYTHON = (
    'if [ -z "$PY" ]; then\n'
    '  PATH="$PATH:/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin"; for p in python3.14 python3.13 python3.12 python3.11 python3.10 python3 python $(ls -d "$HOME"/.local/share/uv/python/cpython-3.1*/bin/python3 2>/dev/null); do\n'
    '    q="$(command -v $p 2>/dev/null)" && "$q" -c "import sys; sys.exit(sys.version_info < (3, 10))" 2>/dev/null && PY="$q" && break\n'
    '  done\n'
    'fi\n'
    '[ -n "$PY" ] || { echo "compile.sh needs Python 3.10 or newer (maspsx): set PY=/path/to/python" >&2; exit 1; }\n'
    'export PY\n')


def _compile_sh(net, proj, fn) -> str:
    """The function's compile command as a shell script for the permuter machine: its toolchain folder is $TC (that
    machine's), Python is the first 3.10+ it finds (or $PY), and compile.sh's arguments (input.c -o output.o) stand in for src and obj."""
    import sys
    cfg = json.loads(proj["config"])
    if (own := net.fn_toolchain(fn)):
        cfg = {**cfg, "compiler": with_toolchain(cfg["compiler"], own)}
    cc = compiler_config(Path(proj["root"]), cfg)
    root = str(toolchains.root())
    words = []
    for a in cc.cmd:
        if a in ("{src}", "{obj}"):
            words.append('"$IN"' if a == "{src}" else '"$OUT"')
            continue
        a = a.replace("{include}", ".").replace(sys.executable, "$PY").replace(root, "$TC")
        if "\n" in a or "$1" in a or ";" in a:  # a sh -c script: its own sh expands $TC and its arguments
            words.append(shlex.quote(a))
        elif "$TC" in a or "$PY" in a:
            words.append('"' + a.replace('"', '\\"') + '"')
        else:
            words.append(shlex.quote(a))
    return ("#!/bin/sh\n# decomp.wtf's compile command for " + fn["name"] + "\n"
            'export TC="${TC:-$HOME/Library/Application Support/decompnet/toolchains}"\n'
            + _FIND_PYTHON +
            'IN="$1"; OUT="$3"\n'
            "exec " + " ".join(words) + "\n")


def _compiles(d: Path) -> tuple[bool, str]:
    """Whether the directory's base.c compiles with its own compile.sh, and the compiler's messages if it doesn't."""
    with tempfile.TemporaryDirectory() as t:
        r = subprocess.run(["/bin/sh", str(d / "compile.sh"), str(d / "base.c"), "-o", str(Path(t) / "o.o")],
                           capture_output=True, text=True, errors="replace", cwd=d,
                           env={**os.environ, "TC": str(toolchains.root())})
    return r.returncode == 0, r.stderr


def repair_base(d: Path, tries: int = 6) -> tuple[bool, int]:
    """Makes base.c compile. An attempt that declares something the project header has since declared differently
    stops compiling ("conflicting types for"); the attempt's own later declaration goes (the header's wins), when it is
    a one-line declaration. Returns (compiles, declarations dropped); the permuter stops on the first base that
    doesn't compile, so one stale attempt would end a whole run."""
    dropped = 0
    for _ in range(tries):
        ok, err = _compiles(d)
        if ok:
            return True, dropped
        lines = (d / "base.c").read_text(encoding="utf-8").split("\n")
        bad = sorted({int(m.group(1)) for m in re.finditer(r"base\.c:(\d+): conflicting types for", err)}, reverse=True)
        bad = [n for n in bad if n <= len(lines) and lines[n - 1].rstrip().endswith(";") and "{" not in lines[n - 1]]
        if not bad:
            return False, dropped
        for n in bad:
            del lines[n - 1]
        dropped += len(bad)
        (d / "base.c").write_text("\n".join(lines), encoding="utf-8")
    return _compiles(d)[0], dropped


def export(net, slug: str, out: Path, max_miss: float = 0.05, limit: int = 400, log=print) -> int:
    """One decomp-permuter directory per open function whose best attempt is within max_miss of the worst score,
    closest first. Returns how many were written."""
    proj = net._project(slug)
    cfg = json.loads(proj["config"])
    rows = net.store.all(
        "SELECT * FROM functions WHERE project_id=? AND status='open' AND best_source IS NOT NULL AND best_score > 0 "
        "AND best_score <= n_insns * 100 * ? ORDER BY CAST(best_score AS REAL) / (n_insns * 100) LIMIT ?",
        proj["id"], max_miss, limit)
    rows = [r for r in rows if experiments.arm(net.store, proj["id"], r["id"], "permuter") != "off"]
    names = {r["name"] for r in net.store.all("SELECT name FROM functions WHERE project_id=?", proj["id"])}
    units = net._unit_contexts(proj["id"])
    from .rom.export import own_source
    out.mkdir(parents=True, exist_ok=True)
    written = 0
    for fn in rows:
        if not diagnosis.eligible(net, fn):
            continue
        lane = diagnosis.latest(net, fn)
        if lane and lane["basis"] == diagnosis.basis(net, fn) and lane["category"] in ("stack", "linking", "library"):
            continue
        fn = diagnosis.machine_source(net, fn)
        if fn is None:
            continue
        header = net._context(proj["id"], net._effective_unit(fn, units))["header"]
        src = own_source(fn["best_source"], fn["name"], names)
        if _write_dir(net, proj, cfg, fn, src, header, out, {"slug": slug, "best_score": fn["best_score"]}, log):
            written += 1
    _setup_sh(out)
    log(f"{written} near misses written to {out}")
    return written


def sources_from_dir(folder: Path) -> dict[str, str]:
    """{function: source} from a folder of `<name>.c` files (a file whose stem has a dot in it, such as the
    passthrough folder's `<name>.before.c`, is not one)."""
    return {p.stem: p.read_text(encoding="utf-8") for p in sorted(Path(folder).glob("*.c")) if "." not in p.stem}


def sources_from_passthrough(project_root: Path) -> dict[str, str]:
    """The rewrites `decomp fix-passthrough` couldn't verify (<project>/passthrough/report.json's failed ones, each
    `<name>.failed.c`): near misses whose only difference is usually register allocation or save order."""
    folder = Path(project_root) / "passthrough"
    report = json.loads((folder / "report.json").read_text(encoding="utf-8"))
    out = {}
    for r in report["functions"]:
        f = folder / f"{r['name']}.failed.c"
        if r.get("status") == "failed" and f.exists():
            out[r["name"]] = f.read_text(encoding="utf-8")
    return out


def export_sources(net, slug: str, out: Path, sources: dict[str, str], note: str = "", log=print) -> int:
    """One decomp-permuter directory per (function, source) given, matched or open: the source as it stands (the
    function's own, as export.own_source makes one) against the function's header now. For a matched function
    decomp.json says `replace` and keeps the note, and the source goes beside it (source.c): import_results takes a
    byte-identical result through /api/replace (Network.replace_match) instead of as an attempt."""
    proj = net._project(slug)
    cfg = json.loads(proj["config"])
    units = net._unit_contexts(proj["id"])
    out.mkdir(parents=True, exist_ok=True)
    written = 0
    for name, src in sorted(sources.items()):
        fn = net.store.one("SELECT * FROM functions WHERE project_id=? AND name=?", proj["id"], name)
        if fn is None:
            log(f"{name}: no such function, skipped")
            continue
        header = net._context(proj["id"], net._effective_unit(fn, units))["header"]
        meta = {"slug": slug, "best_score": None, "replace": fn["status"] == "matched", "note": note}
        if _write_dir(net, proj, cfg, fn, src, header, out, meta, log):
            (out / name / "source.c").write_text(src, encoding="utf-8")
            written += 1
    _setup_sh(out)
    log(f"{written} functions written to {out}")
    return written


def _write_dir(net, proj, cfg: dict, fn, src: str, header: str, out: Path, meta: dict, log=print) -> bool:
    """One decomp-permuter directory for fn with this source (its own) against this header: base.c (preprocessed),
    target.o (the original instructions, assembled), compile.sh, settings.toml and decomp.json (meta, the function's
    name and size). False, and no directory, when one of them can't be made."""
    tc = toolchains.get(toolchain_id(cfg["compiler"]["toolchain"]))
    gcc_dir = toolchains.path(tc.id)
    as_bin = toolchains.binary("binutils-mips").parent / "mips-ps2-decompals-as"
    from .psx_gte import enabled as gte_enabled, header_path as gte_header
    d = out / fn["name"]
    d.mkdir(exist_ok=True)
    own_tc = toolchains.get(toolchain_id(net.fn_toolchain(fn) or cfg["compiler"]["toolchain"]))
    if not own_tc.line_comments:
        header, src = block_comments(header), block_comments(src)
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        (t / "context.h").write_text(header, encoding="utf-8")
        pre = '#include "psx_gte.h"\n' if gte_enabled(cfg) else ""
        if pre:
            (t / "psx_gte.h").write_text(gte_header().read_text(encoding="utf-8"), encoding="utf-8")
        (t / "f.c").write_text(pre + '#include "context.h"\n' + src + "\n", encoding="utf-8")
        pp = subprocess.run([str(gcc_dir / "gcc"), f"-B{gcc_dir}/", "-E", "-P", "-I", str(t), str(t / "f.c")],
                            capture_output=True, text=True, errors="replace")
        if pp.returncode != 0:
            log(f"{fn['name']}: preprocessing failed, skipped")
            shutil.rmtree(d, ignore_errors=True)  # a half-written directory stops decomp-permuter's whole run
            return False
        (d / "base.c").write_text(pp.stdout, encoding="utf-8")
        (d / "target.s").write_text(PRELUDE + net._asm_text(fn, proj) + "\n", encoding="utf-8")
        res = subprocess.run([str(as_bin), "-EL", "-march=r3000", "-mtune=r3000", "-no-pad-sections", "-G0",
                              "-o", str(d / "target.o"), str(d / "target.s")], capture_output=True, text=True)
        if res.returncode != 0:
            log(f"{fn['name']}: assembling the target failed, skipped: {res.stderr.strip()[:200]}")
            shutil.rmtree(d, ignore_errors=True)  # (no compile.sh: decomp-permuter refuses the whole batch)
            return False
    (d / "compile.sh").write_text(_compile_sh(net, proj, fn), encoding="utf-8")
    (d / "compile.sh").chmod(0o755)
    (d / "settings.toml").write_text(f'func_name = "{fn["name"]}"\ncompiler_type = "gcc"\n'
                                     f'objdump_command = "OBJDUMP"\n', encoding="utf-8")
    (d / "decomp.json").write_text(json.dumps({**meta, "function": fn["name"], "n_insns": fn["n_insns"]}),
                                   encoding="utf-8")
    ok, dropped = repair_base(d)
    if not ok:
        log(f"{fn['name']}: base.c does not compile with this app's compiler, skipped")
        shutil.rmtree(d)
        return False
    if dropped:
        log(f"{fn['name']}: dropped {dropped} declaration(s) of the attempt that conflict with the header")
    return True


def _setup_sh(out: Path) -> None:
    (out / "setup.sh").write_text(
        '#!/bin/sh\n# Run once on the permuter machine: points every directory at its MIPS objdump.\n'
        'OBJ="$HOME/Library/Application Support/decompnet/toolchains/binutils-mips/mips-ps2-decompals-objdump"\n'
        '[ -x "$OBJ" ] || { echo "install decomp.wtf and its binutils-mips first (uv run decomp, Compilers & tools)"; exit 1; }\n'
        '# a directory without compile.sh (an export that stopped half way) makes decomp-permuter refuse them all\n'
        'cd "$(dirname "$0")" && for d in */; do [ -f "$d/compile.sh" ] || rm -rf "$d"; done\n'
        'for s in */settings.toml; do sed -i "" "s|^objdump_command = .*|objdump_command = \\"\'$OBJ\' -drz\\"|" "$s"; done\n'
        'echo "ready: $(ls -d */ | wc -l | tr -d " ") functions"\n', encoding="utf-8")
    (out / "setup.sh").chmod(0o755)


def import_results(net, slug: str, src: Path, log=print, post=None) -> dict:
    """decomp-permuter's best candidate per directory (output-<score>-<n>/source.c), the function's definition taken
    out of it and judged by the app like any attempt (by the "permuter" agent). A matched function's directory
    (export_sources) is a replacement instead: judged byte for byte and, through post(path, body), sent to
    /api/replace (stats["replace"])."""
    from .forks import csource
    proj = net._project(slug)
    agent, _ = net.local_agent("permuter")
    stats = {"directories": 0, "candidates": 0, "matched": 0, "improved": 0}
    for d in sorted(p for p in src.iterdir() if p.is_dir() and (p / "decomp.json").exists()):
        stats["directories"] += 1
        meta = json.loads((d / "decomp.json").read_text(encoding="utf-8"))
        # outputs not judged yet (a judged one gets a .judged file: an import every 20 minutes doesn't judge it again)
        outs = sorted((p for p in d.glob("output-*/source.c") if not (p.parent / ".judged").exists()),
                      key=lambda p: int(re.match(r"output-(\d+)", p.parent.name).group(1)))
        if not outs:
            continue

        def judged() -> None:  # its best judged: the rest of this batch of outputs scored worse in the permuter
            for p in outs:
                (p.parent / ".judged").touch()
        text = outs[0].read_text(encoding="utf-8", errors="replace")
        span = csource.find(text, meta["function"])
        if span is None:
            judged()
            continue
        stats["candidates"] += 1
        fn = net.store.one("SELECT * FROM functions WHERE project_id=? AND name=?", proj["id"], meta["function"])
        if meta.get("replace") and fn is not None and fn["status"] == "matched" and (d / "source.c").exists():
            stats.setdefault("replace", []).append(_replace_one(net, proj, fn, d, meta, outs[0], span, text, post, log))
            judged()
            continue
        if fn is None or fn["status"] != "open" or not fn["best_source"]:
            judged()
            continue
        # the new definition in place of the old one, in the agent's source (its externs and prototypes stay)
        source = csource.replace(fn["best_source"], meta["function"], text[span[0]:span[1]].strip())
        try:
            res = net.try_quietly(agent, proj, fn, source)
        except Exception as e:  # claimed by an agent right now: next import
            log(f"{meta['function']}: {e}")
            continue
        judged()
        if res["matched"]:
            stats["matched"] += 1
        elif res["score"] is not None and fn["best_score"] is not None and res["score"] < fn["best_score"]:
            stats["improved"] += 1
    log(f"permuter results: {stats}")
    return stats


def _replace_one(net, proj, fn, d: Path, meta: dict, out: Path, span, text: str, post, log=print) -> dict:
    """A matched function's permuted source (export_sources): its definition spliced into source.c, judged as the
    judge does (rom.passthrough.verify), and when byte-identical posted to /api/replace with the note (when post is
    given). What happened, with decomp-permuter's score of its best output."""
    from .forks import csource
    from .rom.passthrough import verify
    name = meta["function"]
    score = int(re.match(r"output-(\d+)", out.parent.name).group(1))
    source = csource.replace((d / "source.c").read_text(encoding="utf-8"), name, text[span[0]:span[1]].strip())
    header = net._context(proj["id"], net._effective_unit(fn, net._unit_contexts(proj["id"])))["header"]
    exact, why = verify(net, proj, fn, source, header)
    row = {"function": name, "permuter_score": score, "exact": exact, "why": why, "replaced": False}
    if exact:
        (d / "matched.c").write_text(source, encoding="utf-8")
        if post is not None:
            res = post("/api/replace", {"slug": meta["slug"], "name": name, "source": source,
                                        "note": meta.get("note") or "permuted"})
            row["replaced"] = bool(res.get("replaced"))
            row["why"] = "" if row["replaced"] else (res.get("text") or res.get("error") or "")[:400]
    log(f"  {name}: permuter score {score}, " + ("byte-identical" if exact else why[:200])
        + (", replaced" if row["replaced"] else ""))
    return row
