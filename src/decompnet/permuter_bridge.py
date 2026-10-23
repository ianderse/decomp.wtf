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
    tc = toolchains.get(toolchain_id(cfg["compiler"]["toolchain"]))
    gcc_dir = toolchains.path(tc.id)
    as_bin = toolchains.binary("binutils-mips").parent / "mips-ps2-decompals-as"
    rows = net.store.all(
        "SELECT * FROM functions WHERE project_id=? AND status='open' AND best_source IS NOT NULL AND best_score > 0 "
        "AND best_score <= n_insns * 100 * ? ORDER BY CAST(best_score AS REAL) / (n_insns * 100) LIMIT ?",
        proj["id"], max_miss, limit)
    rows = [r for r in rows if experiments.arm(net.store, proj["id"], r["id"], "permuter") != "off"]
    names = {r["name"] for r in net.store.all("SELECT name FROM functions WHERE project_id=?", proj["id"])}
    units = net._unit_contexts(proj["id"])
    from .rom.export import own_source
    from .psx_gte import enabled as gte_enabled, header_path as gte_header
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
        d = out / fn["name"]
        d.mkdir(exist_ok=True)
        header = net._context(proj["id"], net._effective_unit(fn, units))["header"]
        src = own_source(fn["best_source"], fn["name"], names)
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
                continue
            (d / "base.c").write_text(pp.stdout, encoding="utf-8")
            (d / "target.s").write_text(PRELUDE + net._asm_text(fn, proj) + "\n", encoding="utf-8")
            res = subprocess.run([str(as_bin), "-EL", "-march=r3000", "-mtune=r3000", "-no-pad-sections", "-G0",
                                  "-o", str(d / "target.o"), str(d / "target.s")], capture_output=True, text=True)
            if res.returncode != 0:
                log(f"{fn['name']}: assembling the target failed, skipped: {res.stderr.strip()[:200]}")
                shutil.rmtree(d, ignore_errors=True)  # (no compile.sh: decomp-permuter refuses the whole batch)
                continue
        (d / "compile.sh").write_text(_compile_sh(net, proj, fn), encoding="utf-8")
        (d / "compile.sh").chmod(0o755)
        (d / "settings.toml").write_text(f'func_name = "{fn["name"]}"\ncompiler_type = "gcc"\n'
                                         f'objdump_command = "OBJDUMP"\n', encoding="utf-8")
        (d / "decomp.json").write_text(json.dumps({"slug": slug, "function": fn["name"], "best_score": fn["best_score"],
                                                   "n_insns": fn["n_insns"]}), encoding="utf-8")
        ok, dropped = repair_base(d)
        if not ok:
            log(f"{fn['name']}: base.c does not compile with this app's compiler, skipped")
            shutil.rmtree(d)
            continue
        if dropped:
            log(f"{fn['name']}: dropped {dropped} declaration(s) of the attempt that conflict with the header")
        written += 1
    (out / "setup.sh").write_text(
        '#!/bin/sh\n# Run once on the permuter machine: points every directory at its MIPS objdump.\n'
        'OBJ="$HOME/Library/Application Support/decompnet/toolchains/binutils-mips/mips-ps2-decompals-objdump"\n'
        '[ -x "$OBJ" ] || { echo "install decomp.wtf and its binutils-mips first (uv run decomp, Compilers & tools)"; exit 1; }\n'
        '# a directory without compile.sh (an export that stopped half way) makes decomp-permuter refuse them all\n'
        'cd "$(dirname "$0")" && for d in */; do [ -f "$d/compile.sh" ] || rm -rf "$d"; done\n'
        'for s in */settings.toml; do sed -i "" "s|^objdump_command = .*|objdump_command = \\"\'$OBJ\' -drz\\"|" "$s"; done\n'
        'echo "ready: $(ls -d */ | wc -l | tr -d " ") functions"\n', encoding="utf-8")
    (out / "setup.sh").chmod(0o755)
    log(f"{written} near misses written to {out}")
    return written


def import_results(net, slug: str, src: Path, log=print) -> dict:
    """decomp-permuter's best candidate per directory (output-<score>-<n>/source.c), the function's definition taken
    out of it and judged by the app like any attempt (by the "permuter" agent)."""
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
