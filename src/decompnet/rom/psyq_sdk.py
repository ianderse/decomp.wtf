"""Signatures from Sony's own PSY-Q libraries: per function, for rom/psyq.py's scanner.

The signature set Compilers & tools installs (lab313ru/psx_psyq_signatures) has one signature per library object,
covering all of its functions at once: where a game's copy of an object differs anywhere (another release, a
patched module), none of its functions are found. This makes one signature per function, straight from the
libraries (.LIB archives and .OBJ files in PSY-Q's own object format), with the bytes the linker fills in left
open, and writes them in the same format, so rom/psyq.py scans them like the others.

Sources: a folder with the libraries, or Sony's Runtime Library discs (the .bin/.iso images, as zip/7z/rar or not):
their LIB folders are read straight from the image. They are yours to have; nothing of them is bundled.

    python -m decompnet.rom.psyq_sdk <images or folders...> <out dir>   # out dir/<version>/<LIB>.json
"""

from __future__ import annotations

import json
import re
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# -- disc images: ISO 9660 in 2048-byte sectors, or 2352-byte raw ones (mode 1, or mode 2 form 1)


def _sector_layout(f) -> tuple[int, int]:
    """(sector size, data offset) of an image: where the primary volume descriptor (sector 16) reads "CD001"."""
    for size, skip in ((2048, 0), (2352, 16), (2352, 24)):
        f.seek(16 * size + skip)
        if f.read(6)[1:6] == b"CD001":
            return size, skip
    raise ValueError("not an ISO 9660 disc image")


def disc_files(image: Path, want=lambda path: True):
    """(path, bytes) of each file on a disc image that `want(path)` accepts."""
    with open(image, "rb") as f:
        size, skip = _sector_layout(f)

        def read(lba: int, n: int) -> bytes:
            out = bytearray()
            for i in range((n + 2047) // 2048):
                f.seek((lba + i) * size + skip)
                out += f.read(2048)
            return bytes(out[:n])

        pvd = read(16, 2048)

        def walk(lba: int, length: int, prefix: str):
            data, i = read(lba, length), 0
            while i < len(data):
                n = data[i]
                if n == 0:
                    i = (i // 2048 + 1) * 2048
                    continue
                rec = data[i:i + n]
                ext, ln, flags = struct.unpack_from("<I", rec, 2)[0], struct.unpack_from("<I", rec, 10)[0], rec[25]
                name = rec[33:33 + rec[32]]
                if name not in (b"\0", b"\1"):
                    nm = prefix + name.decode("ascii", "replace").split(";")[0]
                    if flags & 2:
                        yield from walk(ext, ln, nm + "/")
                    elif want(nm):
                        yield nm, read(ext, ln)
                i += n

        root = pvd[156:190]
        yield from walk(struct.unpack_from("<I", root, 2)[0], struct.unpack_from("<I", root, 10)[0], "")


def unpack(archive: Path, into: Path) -> list[Path]:
    """The disc images (or library files) in a zip, 7z or rar archive (bsdtar reads all three)."""
    subprocess.run(["bsdtar", "-xf", str(archive), "-C", str(into)], check=True, capture_output=True)
    return [p for p in into.rglob("*") if p.is_file()]


def library_files(source: Path):
    """(path, bytes) of every .LIB and .OBJ in a source: a folder, a disc image, or an archive of either."""
    is_lib = lambda p: bool(re.search(r"\.(LIB|OBJ)$", p, re.IGNORECASE))
    if source.is_dir():
        for p in sorted(source.rglob("*")):
            if p.is_file() and is_lib(p.name):
                yield p.relative_to(source).as_posix(), p.read_bytes()
        return
    if source.suffix.lower() in ARCHIVES:
        with tempfile.TemporaryDirectory(prefix="psyq-sdk-") as tmp:
            for p in unpack(source, Path(tmp)):
                if is_lib(p.name):
                    yield p.name, p.read_bytes()
                elif p.suffix.lower() in (".bin", ".iso", ".img"):
                    yield from _image_files(p, is_lib)
        return
    yield from _image_files(source, is_lib)


ARCHIVES = (".zip", ".7z", ".rar", ".lzh", ".lha")


def _image_files(image: Path, want):
    """A disc image's files, and those in archives on it (2.0 keeps its libraries in an LZH); nothing from one that
    isn't a file system (a disc's audio tracks)."""
    packed = lambda p: Path(p).suffix.lower() in ARCHIVES
    try:
        for path, data in disc_files(image, lambda p: want(p) or packed(p)):
            if not packed(path):
                yield path, data
                continue
            with tempfile.TemporaryDirectory(prefix="psyq-sdk-") as tmp:
                inner = Path(tmp) / Path(path).name
                inner.write_bytes(data)
                try:
                    yield from library_files(inner)
                except subprocess.CalledProcessError:
                    continue
    except ValueError:
        return


# -- PSY-Q's object format (LNK, version 2) and its library archives (LIB, version 1)


@dataclass
class Function:
    name: str
    code: bytes
    open: set[int] = field(default_factory=set)  # byte offsets the linker fills in (not part of the signature)


class Unknown(ValueError):
    pass


def _expression(d: bytes, i: int) -> int:
    """Skip one patch expression; return where it ends."""
    op = d[i]
    i += 1
    if op == 0x00:  # a constant
        return i + 4
    if op in (0x02, 0x04, 0x06, 0x0C, 0x16, 0x18, 0x1A):  # a symbol, a section's base/start/end... (a 16-bit number)
        return i + 2
    if 0x20 <= op <= 0x3E:  # an operator on two expressions (+, -, *, /, &, |, <<, >>...)
        return _expression(d, _expression(d, i))
    raise Unknown(f"patch expression 0x{op:02X}")


# which bytes of the patched word each kind of patch fills in (little endian)
PATCH_BYTES = {0x10: (0, 1, 2, 3), 0x4A: (0, 1, 2), 0x52: (0, 1), 0x54: (0, 1), 0x56: (0, 1), 0x64: (0, 1),
               0x1E: (0, 1), 0x22: (0, 1), 0x0A: (0, 1, 2, 3), 0x08: (0, 1, 2, 3)}


def parse_object(d: bytes) -> list[Function]:
    """The code functions (global and local symbols in code sections) of one LNK object."""
    if d[:3] != b"LNK":
        raise Unknown("not a PSY-Q object")
    i, section = 4, 0
    code: dict[int, bytearray] = {}
    opened: dict[int, set[int]] = {}
    chunk: dict[int, int] = {}  # section -> where its last code record began (patch offsets count from there)
    names: dict[int, str] = {}  # section number -> name
    symbols: list[tuple[int, int, str]] = []  # (section, offset, name)

    def text(n: int) -> str:
        return d[n + 1:n + 1 + d[n]].decode("latin-1")

    while i < len(d):
        op = d[i]
        i += 1
        if op == 0:  # end
            break
        if op == 2:  # code bytes, into the current section
            n = struct.unpack_from("<H", d, i)[0]
            chunk[section] = len(code.setdefault(section, bytearray()))
            code[section].extend(d[i + 2:i + 2 + n])
            i += 2 + n
        elif op == 6:  # switch to a section
            section = struct.unpack_from("<H", d, i)[0]
            i += 2
        elif op == 8:  # uninitialised bytes (bss)
            i += 4
        elif op == 10:  # a patch: kind, offset in the code record before it, the expression for its value
            kind, at = d[i], struct.unpack_from("<H", d, i + 1)[0] + chunk.get(section, 0)
            opened.setdefault(section, set()).update(at + b for b in PATCH_BYTES.get(kind, (0, 1, 2, 3)))
            i = _expression(d, i + 3)
        elif op == 12:  # a global symbol defined here: number, section, offset, name
            sect, off = struct.unpack_from("<HI", d, i + 2)
            symbols.append((sect, off, text(i + 8)))
            i += 8 + 1 + d[i + 8]
        elif op == 14:  # a symbol from elsewhere: number, name
            i += 2 + 1 + d[i + 2]
        elif op == 16:  # a section: number, group, alignment, name
            names[struct.unpack_from("<H", d, i)[0]] = text(i + 5)
            i += 5 + 1 + d[i + 5]
        elif op == 18:  # a local symbol: section, offset, name
            sect, off = struct.unpack_from("<HI", d, i)
            symbols.append((sect, off, text(i + 6)))
            i += 6 + 1 + d[i + 6]
        elif op == 20:  # a group: number, type, name
            i += 3 + 1 + d[i + 3]
        elif op == 28:  # a source file: number, name
            i += 2 + 1 + d[i + 2]
        elif op == 46:  # processor type
            i += 1
        elif op == 48:  # a common (uninitialised) symbol: number, section, size, name
            i += 8 + 1 + d[i + 8]
        elif op in (34,):  # debug: next line
            pass
        elif op in (36,):  # debug: line += byte
            i += 1
        elif op in (38, 32):  # debug: line += word / set line
            i += 2 if op == 38 else 4
        elif op == 30:  # debug: set file and line
            i += 6
        elif op == 74:  # debug: function start (section, offset, file, line, frame register and size, return
            # register, saved-register mask and its offset, name)
            sect, off = struct.unpack_from("<HI", d, i)
            symbols.append((sect, off, text(i + 28)))
            i += 28 + 1 + d[i + 28]
        elif op in (76, 78, 80):  # debug: function end, block start, block end (section, offset, line)
            i += 10
        elif op == 82:  # debug: a definition (section, value, class, type, size, name)
            i += 14 + 1 + d[i + 14]
        elif op == 84:  # debug: a definition with dimensions (... dims and their sizes, tag, name)
            dims = struct.unpack_from("<H", d, i + 14)[0]
            i += 16 + 4 * dims
            i += 1 + d[i]
            i += 1 + d[i]
        else:
            raise Unknown(f"record 0x{op:02X} at {i - 1}")
    out = []
    for sect, body in code.items():
        if not names.get(sect, "").startswith(".text"):
            continue
        here = sorted({(off, name) for s, off, name in symbols if s == sect and not name.startswith(("L", "."))})
        for k, (off, name) in enumerate(here):
            end = here[k + 1][0] if k + 1 < len(here) else len(body)
            if end > off:
                out.append(Function(name, bytes(body[off:end]),
                                    {b - off for b in opened.get(sect, ()) if off <= b < end}))
    return out


def parse_library(d: bytes) -> list[tuple[str, list[Function]]]:
    """(module name, its functions) for each module of a LIB archive."""
    if d[:3] != b"LIB":
        raise Unknown("not a PSY-Q library")
    out, p = [], 4
    while p + 20 <= len(d):
        name = d[p:p + 8].decode("latin-1").strip()
        _, header, size = struct.unpack_from("<III", d, p + 8)
        if size == 0 or header > size:
            break
        out.append((name, parse_object(d[p + header:p + size])))
        p += size
    return out


# -- signatures


def signature(fn: Function) -> str:
    return " ".join("??" if k in fn.open else f"{b:02X}" for k, b in enumerate(fn.code))


def version_of(source: Path) -> str:
    """The SDK version a source holds, from its name, as the signature set names them (2.6 -> "260", 3.6.1 ->
    "3610", "Psy-Q_46" -> "460"; a libs/<version> folder: that name)."""
    if source.is_dir() and re.fullmatch(r"\d{3,4}", source.name):
        return source.name
    name = source.stem if source.is_file() else source.name
    m = (re.search(r"(?:version|library|psy-?q)[ _-]*(\d)\.(\d)(?:\.(\d))?", name, re.IGNORECASE)
         or re.search(r"psy-?q[ _-]*(\d)(\d)(\d)?\b", name, re.IGNORECASE)
         or re.search(r"(\d)\.(\d)(?:\.(\d))?", name))
    if not m:
        return re.sub(r"\W+", "-", source.stem).strip("-").lower() or "unknown"
    return f"{m.group(1)}{m.group(2)}{m.group(3) or ''}0"


def write(source: Path, out: Path, log=print) -> dict:
    """Every SDK library in `source` as per-function signatures under out/<version>/<LIB>.json."""
    # Sony's libraries: not the samples' or the betas' (which share their names)
    files = [(p, b) for p, b in library_files(source) if not re.search(r"\b(SAMPLE|BETA)\b", p.upper())]
    version = version_of(source)
    target = out / version
    target.mkdir(parents=True, exist_ok=True)
    counts = {}
    for path, data in files:
        lib = Path(path).name.upper()
        try:
            modules = parse_library(data) if data[:3] == b"LIB" else [(Path(path).stem, parse_object(data))]
        except (Unknown, struct.error, IndexError) as e:
            log(f"{path}: skipped ({e})")
            continue
        records = [{"name": f"{module}.OBJ", "sig": signature(fn), "labels": [{"name": fn.name, "offset": 0}]}
                   for module, fns in modules for fn in fns if fn.code]
        (target / f"{lib}.json").write_text(json.dumps(records), encoding="utf-8")
        counts[lib] = len(records)
    log(f"{source.name}: PSY-Q {version}, {sum(counts.values())} functions in {len(counts)} libraries")
    return {"version": version, "libraries": counts, "files": files}


SOURCE = re.compile(r"\.(zip|7z|rar|bin|iso|img)$", re.IGNORECASE)
PARSER = 3  # what the signatures were made with: a newer parser makes them all again


def prepare(sdk: Path, out: Path, log=print) -> str | None:
    """Signatures for every SDK source in `sdk` (archives and disc images, and the libs/<version> folders this
    writes), each made once (out/manifest.json): a disc's library files are also kept in sdk/libs/<version>, so the
    images can be deleted afterwards. Returns a fingerprint of what's there (None: no SDK sources)."""
    if not sdk.is_dir():
        return None
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    if manifest.get("parser") != PARSER:  # made with an older parser: all of them again
        import shutil
        for old in out.iterdir():
            if old.is_dir():
                shutil.rmtree(old)
        manifest = {"parser": PARSER}
    sources = [p for p in sorted(sdk.iterdir()) if p.is_file() and SOURCE.search(p.name)]
    sources += [p for p in sorted((sdk / "libs").glob("*")) if p.is_dir()]
    for src in sources:
        key = f"{src.relative_to(sdk).as_posix()}:{src.stat().st_size if src.is_file() else len(list(src.iterdir()))}"
        if key in manifest:
            continue
        try:
            res = write(src, out, log)
        except (OSError, ValueError, subprocess.CalledProcessError) as e:
            log(f"{src.name}: skipped ({e})")
            manifest[key] = None
            continue
        if not res["libraries"]:  # nothing PSY-Q in it (the converted ELF libraries, a docs disc)
            manifest[key] = None
            manifest_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
            continue
        if src.is_file() and res["files"]:  # the library files themselves, so the image can go
            keep = sdk / "libs" / res["version"]
            keep.mkdir(parents=True, exist_ok=True)
            for path, data in res["files"]:
                (keep / Path(path).name.upper()).write_bytes(data)
            manifest[f"libs/{res['version']}:{len(list(keep.iterdir()))}"] = res["version"]
        manifest[key] = res["version"]
        manifest_path.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    versions = sorted({v for k, v in manifest.items() if v and k != "parser"})
    if not versions:
        return None
    import hashlib
    return hashlib.sha1(json.dumps(sorted(manifest.items())).encode()).hexdigest()[:12]


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    out = Path(argv[-1]).expanduser()
    for src in argv[:-1]:
        write(Path(src).expanduser(), out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
