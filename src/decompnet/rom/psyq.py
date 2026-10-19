"""Sony's PSY-Q library functions in a PS1 game, found by byte signatures.

A PS1 game links Sony's libraries (libgpu, libcd, libspu, libetc, libapi, libc...) as precompiled objects. Agents
can't usefully match those (they were often written in assembly or built with another compiler), so the ones a
signature identifies exactly are set aside with their real names, and briefs of the functions that call them say
what they call. The signatures are lab313ru/psx_psyq_signatures, installed on request (Compilers & tools); none are
bundled. The matcher is ported from SynapticDreams/decomp.wtf (#70, MIT): a signature counts only where it matches
in exactly one place of a unit (overlays that share addresses are scanned separately), and a function is set aside
only when the signature covers all of it.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class Signature:
    object: str
    version: str
    pattern: tuple[int | None, ...]
    labels: tuple[tuple[str, int], ...]
    anchor: bytes
    anchor_at: int
    key: str


def parse(text: str, version: str) -> list[Signature]:
    """One library's signatures ([{"name", "sig": "A0 00 ?? ...", "labels": [{"name", "offset"}]}]); weak ones (under
    24 bytes, 16 known bytes or a 4-byte run to search for) are left out."""
    out = []
    for record in json.loads(text):
        if not isinstance(record, dict) or not isinstance(record.get("sig"), str):
            continue
        tokens = record["sig"].split()
        if any(not re.fullmatch(r"[0-9A-Fa-f]{2}|\?\?", t) for t in tokens):
            continue
        pattern = tuple(None if t == "??" else int(t, 16) for t in tokens)
        if len(pattern) < 24 or sum(t is not None for t in pattern) < 16:
            continue
        longest, longest_at, run, run_at = b"", 0, bytearray(), 0
        for i, value in enumerate((*pattern, None)):
            if value is not None:
                if not run:
                    run_at = i
                run.append(value)
            else:
                if len(run) > len(longest):
                    longest, longest_at = bytes(run), run_at
                run.clear()
        if len(longest) < 4:
            continue
        labels = tuple((lb["name"], lb["offset"]) for lb in record.get("labels") or []
                       if isinstance(lb, dict) and isinstance(lb.get("name"), str) and IDENT.fullmatch(lb["name"])
                       and isinstance(lb.get("offset"), int) and 0 <= lb["offset"] < len(pattern) and lb["offset"] % 4 == 0)
        if labels:
            out.append(Signature(str(record.get("name", "object"))[:120], version, pattern, labels, longest, longest_at,
                                 "".join(tokens).upper()))
    return out


def load(root: Path) -> list[Signature]:
    """Every SDK version's signatures in an installed copy (a folder per version: 260, 300, ..., 470)."""
    sigs = []
    for path in sorted(root.glob("*/*.json")):
        try:
            sigs += parse(path.read_text(encoding="utf-8", errors="replace"), path.parent.name)
        except (ValueError, OSError):
            continue
    return sigs


def _unique(data: bytes, sig: Signature, deadline: float) -> tuple[int | None, bool]:
    """(offset, timed out): the one place the whole pattern matches, word-aligned; None if none or several."""
    at, found = 0, None
    while True:
        if time.monotonic() >= deadline:
            return None, True
        hit = data.find(sig.anchor, at)
        if hit < 0:
            return found, False
        at = hit + 1
        start = hit - sig.anchor_at
        if start < 0 or start % 4 or start + len(sig.pattern) > len(data):
            continue
        if all(v is None or data[start + i] == v for i, v in enumerate(sig.pattern)):
            if found is not None and found != start:
                return None, False
            found = start


def scan(regions: list[tuple[str, bytes, int, int]], sigs: list[Signature], seconds: float = 120) -> dict:
    """regions: (unit, code bytes, load address, file offset). {"hits": [{unit, address, rom_offset, name, end,
    versions, objects}], "versions": SDK versions by how many symbols they explain, "truncated"}."""
    deadline = time.monotonic() + seconds
    labels: dict = {}
    by_version: dict[str, set] = {}
    truncated = False
    for unit, data, vram, flat in regions:
        seen: dict = {}
        for sig in sigs:
            if sig.key not in seen:
                seen[sig.key] = _unique(data, sig, deadline)
            offset, expired = seen[sig.key]
            if expired:
                truncated = True
                break
            if offset is None:
                continue
            for name, rel in sig.labels:
                key = (unit, flat + offset + rel, vram + offset + rel)
                ev = labels.setdefault(key, {}).setdefault(name, {"versions": set(), "objects": set(), "end": 0})
                ev["versions"].add(sig.version)
                ev["objects"].add(sig.object)
                ev["end"] = max(ev["end"], flat + offset + len(sig.pattern))
                by_version.setdefault(sig.version, set()).add((*key, name))
        if truncated:
            break
    hits = []
    for (unit, flat, address), names in sorted(labels.items()):
        if len(names) == 1:  # two libraries naming the same place: left alone
            name, ev = next(iter(names.items()))
            hits.append({"unit": unit, "rom_offset": flat, "address": address, "name": name, "end": ev["end"],
                         "versions": sorted(ev["versions"]), "objects": sorted(ev["objects"])})
    versions = sorted(((v, len(n)) for v, n in by_version.items()), key=lambda x: -x[1])
    return {"hits": hits, "versions": versions, "truncated": truncated}
