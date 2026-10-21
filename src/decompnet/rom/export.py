"""Export a ROM project as a rebuildable source tree: the base for ports and mods.

The tree is a normal decomp project. splat's assembly for every function and all data (with the project's names),
the matched functions as C, a linker script and a build script that rebuilds the game's image and checks it against
the original byte for byte. Each function is its own object: a matched one is compiled exactly the way decomp.wtf
verified it, the rest are assembled. Data stays labeled assembly; a matched function's own constants (strings,
jump tables) are mapped onto the game's copy at the addresses the matcher verified, so nothing is duplicated.

Anyone can keep working on it outside decomp.wtf: edit src/, run build.py, and the build says whether it still
matches.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from .. import toolchains
from ..limits import capped
from . import match
from .symbols import AUTO_NAME

AS_FLAGS = {
    "n64": ["-EB", "-march=vr4300", "-mabi=32"],
    "psx": ["-EL", "-march=r3000", "-mabi=32"],
    "ps2": ["-EL", "-march=r5900", "-mabi=eabi"],
}
LD_ENDIAN = {"n64": "-EB", "psx": "-EL", "ps2": "-EL"}
IMAGE_EXT = {"n64": ".z64", "psx": ".exe", "ps2": ".bin"}
_ADDR_COMMENT = re.compile(r"/\*\s*([0-9A-F]+)\s+([0-9A-F]{8})\b")
_BSS_COMMENT = re.compile(r"/\*\s*([0-9A-F]{8})\s*\*/\s*\.space")
_DEFINES = re.compile(r"^\s*(?:glabel|dlabel|jlabel|alabel|ehlabel)\s+(\w+)|^\s*\.globl\s+(\w+)|^\s*(\w+):", re.M)
DISCARD = ["*(.reginfo)", "*(.MIPS.abiflags)", "*(.MIPS.options)", "*(.pdr)", "*(.comment)", "*(.mdebug*)",
           "*(.gnu.attributes)", "*(.note*)", "*(.gptab.*)"]


class ExportError(RuntimeError):
    pass


def _first_address(path: Path) -> tuple[int | None, int | None]:
    """(ROM offset, VRAM) of the first instruction or data word in a splat assembly file."""
    text = path.read_text(errors="replace", encoding="utf-8")
    m = _ADDR_COMMENT.search(text)
    if m:
        return int(m.group(1), 16), int(m.group(2), 16)
    m = _BSS_COMMENT.search(text)
    if m:
        return None, int(m.group(1), 16)
    return None, None


def _symbol_lines(net, proj) -> list[str]:
    """symbol_addrs.txt entries for every function and every named symbol, so splat writes the project's names."""
    syms = net.symbols(proj["id"])
    rows = net.store.all("SELECT name, vram, size, rom_offset, unit FROM functions WHERE project_id=? "
                         "ORDER BY vram, rom_offset", proj["id"])
    funcs = {r["vram"] for r in rows}
    lines = []
    for r in rows:  # with its ROM offset: overlays share addresses, and splat then names it in its own overlay only
        rom = f" rom:0x{r['rom_offset']:X}" if r["rom_offset"] is not None else ""
        lines.append(f"{r['name']} = 0x{r['vram']:08X}; // type:func{rom}")
    segs = [s for s in net.cfg(proj).get("segments") or [] if s.get("vram") is not None]
    # a name inside a function (a label someone named, GetPointRecord inside func_8014A248): splat would start a
    # function there and cut the real one short. C that uses it still links (build.json's absolute symbols)
    spans = sorted((r["vram"], r["vram"] + r["size"]) for r in rows if r["size"])
    starts = [a for a, _ in spans]
    import bisect
    for s in sorted(syms.by_name.values(), key=lambda s: s.address):
        if s.address in funcs or AUTO_NAME.match(s.name):
            continue
        k = bisect.bisect_right(starts, s.address) - 1
        inside = [(a, b) for a, b in spans[max(0, k - 8):k + 1] if a < s.address < b]
        if inside and not any(g["vram"] <= s.address < g["vram"] + g["rom_end"] - g["rom_start"]
                              for g in segs if sum(1 for h in segs if h["vram"] == g["vram"]) > 1):
            continue
        at = [g for g in segs if g["vram"] <= s.address < g["vram"] + g["rom_end"] - g["rom_start"]]
        if len(at) > 1:  # overlays share this address: only where it isn't inside one of their functions
            free = [g for g in at if not any(r["unit"] == g["name"] and r["vram"] <= s.address < r["vram"] + r["size"]
                                             for r in rows)]
            if len(free) == 1:
                lines.append(f"{s.name} = 0x{s.address:08X}; // segment:{free[0]['name']}")
            continue  # (otherwise unknown: splat would put it in every one of them; C still links by its address)
        lines.append(f"{s.name} = 0x{s.address:08X};")
    return lines


def _segments(config: dict) -> list[dict]:
    """Segments in ROM order with start/end offsets."""
    raw = config.get("segments", [])
    starts = []
    for seg in raw:
        starts.append(seg[0] if isinstance(seg, list) else seg.get("start"))
    out = []
    for i, seg in enumerate(raw):
        start = starts[i]
        end = next((s for s in starts[i + 1:] if s is not None), None)
        if isinstance(seg, list):
            if len(seg) == 1:
                continue  # the end marker
            seg = {"start": seg[0], "type": seg[1], "name": seg[2] if len(seg) > 2 else f"{seg[0]:X}"}
        out.append(dict(seg) | {"start": start, "end": end})
    return out


def _defined_name(head: str) -> str | None:
    """The function a definition's head (everything before its `{`) defines: the name right before its last,
    balanced, parameter list (not a macro's, earlier on: `#define F(p) (...)` then `void f(void) {`)."""
    k = len(head.rstrip())
    if not k or head[k - 1] != ")":
        return None
    depth = 0
    for i in range(k - 1, -1, -1):
        depth += {")": 1, "(": -1}.get(head[i], 0)
        if depth == 0:
            m = re.search(r"\b([A-Za-z_]\w*)\s*$", head[:i])
            return m.group(1) if m else None
    return None


def own_source(source: str, name: str, functions: set[str]) -> str:
    """A function's matched source with only its own definition: agents often match several functions with one
    compile, and each keeps that whole source. The other functions' definitions become prototypes (calls keep their
    argument types), except inline ones; everything else (types, externs, globals) stays."""
    from ..forks.csource import _blank

    flat = _blank(source)
    cuts, depth, start = [], 0, 0  # start: where the current top-level declaration began
    for k, ch in enumerate(flat):
        if ch == "{":
            if depth == 0:
                cuts.append([_defined_name(flat[start:k]), start, k, None])
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                if cuts and cuts[-1][3] is None:
                    cuts[-1][3] = k + 1
                start = k + 1
        elif ch == ";" and depth == 0:
            start = k + 1
    out, at = [], 0
    for fname, head_at, brace, end in cuts:
        if fname is None or fname == name or fname not in functions or end is None:
            continue
        if re.search(r"\binline\b|__inline", flat[head_at:brace]):
            continue  # an inline helper is compiled into the function: its body stays
        out += [source[at:brace].rstrip(), ";"]
        at = end
    return "".join(out) + source[at:]


def export_rom(net, slug: str, out: Path, log=print) -> dict:
    """Write the rebuildable tree to `out`, build it once and report: {"path", "identical", "matched", ...}."""
    proj = net._project(slug)
    cfg = net.cfg(proj)
    platform = cfg.get("platform", "n64")
    if platform not in AS_FLAGS:
        raise ExportError(f"exporting a rebuildable project isn't supported for {platform} yet")
    work = Path(proj["root"])
    configs = sorted(work.glob("*.yaml"))
    if not configs:
        raise ExportError("this project has no splat configuration to export from")
    image_name = cfg["rom"]["file"]
    image = (work / image_name).read_bytes()

    if out.exists():
        keep = {".git"}
        for child in out.iterdir():
            if child.name not in keep:
                shutil.rmtree(child) if child.is_dir() else child.unlink()
    out.mkdir(parents=True, exist_ok=True)

    # 1. splat again, with the project's names
    log("writing the assembly with the project's names (splat)")
    shutil.copyfile(configs[0], out / "splat.yaml")
    shutil.copyfile(work / image_name, out / image_name)
    for extra in ("reloc_addrs.txt",):
        if (work / extra).exists():
            shutil.copyfile(work / extra, out / extra)
    (out / "symbol_addrs.txt").write_text("\n".join(_symbol_lines(net, proj)) + "\n", encoding="utf-8")
    res = subprocess.run([sys.executable, "-m", "splat", "split", "splat.yaml"], cwd=out, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise ExportError("splat failed:\n" + (res.stdout + res.stderr)[-1500:])
    for generated in (out / "src").glob("*.c"):
        generated.unlink()  # splat's INCLUDE_ASM files: this tree builds one object per function instead
    for ld in out.glob("*.ld"):
        ld.rename(out / f"splat-{ld.name}")

    # 2. which function is where, and which are matched
    all_rows = net.store.all("SELECT * FROM functions WHERE project_id=?", proj["id"])
    # overlays share addresses: a function is its segment's (unit) at that address; by address alone for projects
    # whose units aren't splat's segments
    by_seg = {(r["unit"], r["vram"]): r for r in all_rows}
    by_vram = {r["vram"]: r for r in all_rows}
    names = {r["name"] for r in all_rows}
    units = net._unit_contexts(proj["id"])
    # every header read once: an agent editing one while this runs would otherwise give the files written below
    # one version and the compile check another (a match then "doesn't compile any more")
    headers = {u: net._context(proj["id"], u)["header"] for u in {"", *units}}
    header = headers[""]
    (out / "include").mkdir(exist_ok=True)
    gte = ""
    if platform == "psx":  # the trusted GTE macros every PS1 compile includes (psx_gte.py)
        from ..psx_gte import enabled, header_path
        if enabled(cfg):
            shutil.copyfile(header_path(), out / "include" / "psx_gte.h")
            gte = '#include "psx_gte.h"\n'
    # a function built with a compiler from before // comments (GCC 2.6): the tree's C uses /* */ ones throughout
    from .. import toolchains
    from ..compiler import block_comments
    from ..project import toolchain_id
    oldc = any(not toolchains.get(toolchain_id(t)).line_comments for t in
               {net.fn_toolchain(r) for r in all_rows if r["status"] == "matched"} - {None})
    fix = block_comments if oldc else (lambda text: text)
    (out / "include" / "common.h").write_text(gte + fix(header), encoding="utf-8")
    for u in units:
        (out / "include" / f"common_{u}.h").write_text(gte + fix(headers[u]), encoding="utf-8")
    functions = []
    for path in sorted((out / "asm" / "nonmatchings").glob("**/*.s")):
        rom_off, vram = _first_address(path)
        if vram is None:
            continue
        functions.append({"name": path.stem, "seg": path.parent.name, "vram": vram, "rom": rom_off,
                          "asm": path.relative_to(out).as_posix()})
    functions.sort(key=lambda f: f["vram"])

    seg_units = bool({r["unit"] for r in all_rows} & {f["seg"] for f in functions})
    objects, private, as_fallback = [], [], []
    for f in functions:
        row = by_seg.get((f["seg"], f["vram"])) if seg_units else by_vram.get(f["vram"])
        f["obj_asm"] = f"build/{Path(f['asm']).with_suffix('.o').as_posix()}"
        if row is None or row["status"] != "matched" or not row["matched_source"]:
            f["kind"] = "asm"
            continue
        unit = net._effective_unit(row, units)
        hdr = headers[unit or ""]
        own = own_source(row["matched_source"], row["name"], names)
        compiled = net._compile(proj, hdr, own, row)
        if not compiled.ok:
            as_fallback.append((f["name"], "doesn't compile any more"))
            f["kind"] = "asm"
            continue
        linked = match.link(compiled.obj, row["name"], net._rom_target(row), net.symbols(proj["id"]), net.reader(proj))
        if not linked.exact:
            as_fallback.append((f["name"], "no longer matches"))
            f["kind"] = "asm"
            continue
        bases, ok = {}, True
        for section, offsets in linked.placements.items():
            base = {addr - off for off, addr in offsets.items()}
            if len(base) != 1:
                ok = False
                break
            bases[section] = base.pop()
        if not ok:
            as_fallback.append((f["name"], "its own data isn't laid out like the game's"))
            f["kind"] = "asm"
            continue
        src = out / "src" / f["seg"] / f"{f['name']}.c"
        src.parent.mkdir(parents=True, exist_ok=True)
        include = "common.h" if not unit else f"common_{unit}.h"
        src.write_text(f'#include "{include}"\n\n{fix(own).strip()}\n', encoding="utf-8")
        f["kind"] = "c"
        f["src"] = src.relative_to(out).as_posix()
        if (own := net.fn_toolchain(row)):  # matched with another compiler version than the project's
            f["toolchain"] = own
        f["obj"] = f"build/src/{f['seg']}/{f['name']}.o"
        private += [(f["obj"], section, base) for section, base in bases.items()]

    # 3. everything else splat wrote: entry code, data, bss, binary blobs
    config = yaml.safe_load((out / "splat.yaml").read_text(encoding="utf-8"))
    segments = _segments(config)
    pieces = []  # (segment name, vram or None, rom offset or None, object, sections)
    for path in sorted((out / "asm").glob("*.s")) + sorted((out / "asm" / "data").glob("**/*.s")):
        rom_off, vram = _first_address(path)
        obj = f"build/{path.relative_to(out).with_suffix('.o').as_posix()}"
        objects.append({"kind": "asm", "src": path.relative_to(out).as_posix(), "obj": obj})
        pieces.append((path, vram, rom_off, obj))
    blobs = []
    for seg in segments:
        if seg.get("type") == "bin":
            name = seg.get("name") or f"{seg['start']:X}"
            blob = out / "assets" / f"{name}.bin"
            if not blob.exists():
                blob.parent.mkdir(exist_ok=True)
                blob.write_bytes(image[seg["start"]:seg["end"] or len(image)])
            stub = f"asm/bin/{name}.s"
            obj = f"build/asm/bin/{name}.o"
            objects.append({"kind": "asm", "src": stub, "obj": obj})
            blobs.append((seg, obj, stub, f"assets/{name}.bin"))

    # 4. what the linker needs: segments, pieces, functions, symbols (build.py writes game.ld from it)
    defined = set()
    for path in (out / "asm").glob("**/*.s"):
        defined.update(next(x for x in m.groups() if x) for m in _DEFINES.finditer(path.read_text(errors="replace",
                                                                                                  encoding="utf-8")))
    defined.update(f["name"] for f in functions)
    layout = []
    for seg in segments:
        kind = seg.get("type")
        name = re.sub(r"\W", "_", str(seg.get("name") or f"{seg['start']:X}"))
        if kind == "header" or (kind == "hasm" and "vram" not in seg):
            layout.append({"name": name, "kind": "raw", "start": seg["start"], "objects": [f"build/asm/{seg.get('name', 'header')}.o"]})
        elif kind == "bin":
            _, obj, stub, blob = next(b for b in blobs if b[0] is seg)
            layout.append({"name": name, "kind": "raw", "start": seg["start"], "objects": [obj], "stub": stub,
                           "incbin": blob})
        elif kind == "code":
            vram, start, end = int(seg["vram"]), int(seg["start"]), seg["end"] or 1 << 32
            data, bss = [], []
            for path, pv, prom, obj in pieces:
                if prom is not None and start <= prom < end:
                    data.append({"vram": pv, "obj": obj})
                elif prom is None and pv is not None and vram <= pv and path.name.endswith(".bss.s"):
                    bss.append({"vram": pv, "obj": obj})
            bss_vram = next((int(sub["vram"]) for sub in seg.get("subsegments", [])
                             if isinstance(sub, dict) and sub.get("type") == "bss" and "vram" in sub), None)
            layout.append({"name": name, "kind": "code", "start": start, "end": seg["end"], "vram": vram,
                           "bss_vram": bss_vram, "bss_size": int(seg.get("bss_size") or 0),
                           "pieces": sorted(data, key=lambda d: d["vram"]), "bss": sorted(bss, key=lambda d: d["vram"])})
    syms = net.symbols(proj["id"])
    absolute = {s.name: s.address for s in syms.by_name.values() if s.name not in defined}
    for auto in ("undefined_syms_auto.txt", "undefined_funcs_auto.txt"):
        p = out / auto
        if p.exists():
            for m in re.finditer(r"^\s*(\w+)\s*=\s*(0x[0-9A-Fa-f]+)\s*;", p.read_text(encoding="utf-8"), re.M):
                if m.group(1) not in defined:
                    absolute.setdefault(m.group(1), int(m.group(2), 16))
    fn_json = []
    for f in functions:
        entry = {"name": f["name"], "seg": f["seg"], "vram": f["vram"], "rom": f["rom"], "asm": f["asm"]}
        if f["kind"] == "c":
            entry["c"] = f["src"]
            entry["own"] = {sec: base for (obj, sec, base) in private if obj == f["obj"]}
            if f.get("toolchain"):
                entry["toolchain"] = f["toolchain"]
        fn_json.append(entry)

    # 5. build description, build script, objdiff, docs
    ext = IMAGE_EXT[platform]
    base = f"baserom{ext}"
    (out / image_name).rename(out / base)
    yml = out / "splat.yaml"
    yml.write_text(re.sub(r"(^\s*target_path:\s*).*$", rf"\g<1>{base}", yml.read_text(encoding="utf-8"), count=1, flags=re.M),
                   encoding="utf-8")
    build = {
        "title": proj["name"], "platform": platform, "image": base, "output": f"build/game{ext}",
        "sha1": hashlib.sha1(image).hexdigest(), "compiler": cfg["compiler"], "gp": cfg.get("gp") or 0,
        "big_endian": platform == "n64",
        "as_flags": [*AS_FLAGS[platform], "-G0", "-no-pad-sections", "-I", "include"],
        "ld_flags": [LD_ENDIAN[platform], "--no-check-sections"],
        "assembly": objects, "segments": layout, "functions": fn_json,
        "symbols": {k: v for k, v in sorted(absolute.items(), key=lambda kv: kv[1])},
    }
    (out / "build.json").write_text(json.dumps(build, indent=1), encoding="utf-8")
    shutil.copyfile(Path(__file__).with_name("export_build.py"), out / "build.py")
    units_json = []
    for f in functions:
        unit = {"name": f"{f['seg']}/{f['name']}", "target_path": f["obj_asm"],
                "metadata": {"complete": f["kind"] == "c", "source_path": f.get("src")}}
        if f["kind"] == "c":
            unit["base_path"] = f["obj"]
        units_json.append(unit)
    (out / "objdiff.json").write_text(json.dumps({"build_base": False, "build_target": False, "units": units_json}, indent=2),
                                      encoding="utf-8")
    matched = sum(1 for f in functions if f["kind"] == "c")
    _write_docs(out, proj["name"], platform, base, matched, len(functions), as_fallback)
    # everything made from the game itself stays local: build.py regenerates it from your copy
    (out / ".gitignore").write_text("build/\nbaserom*\nasm/\nassets/\n*.ld\n*_auto.txt\n.splat/\n*.d\n",
                                    encoding="utf-8")

    # 6. prove it
    log("building the exported project to check it rebuilds the game byte for byte")
    proc = subprocess.run(capped([sys.executable, "build.py"]), cwd=out, capture_output=True, text=True, timeout=3600,
                          encoding="utf-8", errors="replace")
    identical = proc.returncode == 0 and "identical" in proc.stdout
    if not identical:
        log((proc.stdout + proc.stderr)[-3000:])
    return {"path": str(out), "identical": identical, "matched": matched, "functions": len(functions),
            "fallback": as_fallback, "log": (proc.stdout + proc.stderr)[-3000:]}


def _write_docs(out: Path, title: str, platform: str, base: str, matched: int, total: int, fallback) -> None:
    names = {"n64": "Nintendo 64", "psx": "PlayStation", "ps2": "PlayStation 2"}
    notes = "".join(f"\n- `{n}`: kept as assembly, {why}" for n, why in fallback)
    (out / "README.md").write_text(f"""# {title} (decompilation)

A matching decompilation of {title} for the {names[platform]}, started with [decomp.wtf](https://decomp.wtf).
{matched} of {total} functions are C that compiles to exactly the original code; the rest are still assembly.
`build.py` rebuilds the game from this tree and checks it against your original, byte for byte.

## Build

1. Put your own copy of the game here as `{base}` (it is in `.gitignore`; never commit it).
2. `python build.py` (needs decomp.wtf installed for the period compiler and assembler).

## Layout

| Path | What it is |
| :-- | :-- |
| `src/<segment>/<function>.c` | Decompiled functions, one per file, each matching the original. |
| `asm/nonmatchings/` | Assembly of every function (the target for the decompiled ones). |
| `asm/data/`, `asm/*.s`, `assets/` | Data, start-up code and binary parts, still as assembly or binary. |
| `include/common.h` | Types, structs and prototypes worked out so far. |
| `game.ld` | Places every function and data block at its original address. |
| `build.json`, `build.py` | How each file is built, and the build. |
| `objdiff.json` | Per-function units for [objdiff](https://github.com/encounter/objdiff). |

Keep going with any tool: write a function in C under `src/`, add it to `build.json` (see the existing entries),
run `build.py`. When it says *identical*, the function matches. This is the base for ports and mods: once
everything is C, the game can be rebuilt, changed and moved to other platforms.

The game's code and data belong to their rights holder: keep this tree to yourself unless you have the right to
share it.{chr(10) + chr(10) + '## Notes' + notes if notes else ''}
""", encoding="utf-8")
    (out / "AGENTS.md").write_text("""# Working on this decompilation

- Goal: C that the original compiler turns into exactly the original bytes. `python build.py` must end with
  "identical".
- Pick a function from `asm/nonmatchings/`, write it as `src/<segment>/<name>.c` (one function, `#include
  "common.h"` first), add an entry `{"kind": "c", "src": ..., "obj": ...}` to `build.json` objects and point the
  function's line in `game.ld` at the new object.
- Shared types and prototypes go in `include/common.h`. Never edit `baserom*`, `asm/` or the addresses in
  `game.ld` to force a match.
""", encoding="utf-8")
