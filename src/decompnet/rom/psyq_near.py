"""Sony's library code that the exact signatures miss: near-identical copies, and the code between found functions.

identify_psyq (app/engine.py) sets aside what a signature matches byte for byte. Two kinds of library code slip
through it, and agents then spend their attempts on Sony's code: on SLUS-01252, 48 open functions in libsnd's
address range had taken 5,613 attempts (2026-11-10).

- A copy from another SDK revision than any signature, or with one constant changed, fails the exact match.
  `near_hits` compares each open function with every library function word by word, leaving out the bytes the linker
  fills in: Snd_nextseq, SsUtBuildADSR and SsSeqClose were identical but for those, SsUtGetVagAtr 94% the same.
- A library's functions that no signature covers at all (a revision missing from the user's psyq-sdk/) are still
  linked where the library is: between functions the signatures found. `library_blocks` finds the stretches of a unit
  that are mostly found library code.
"""

from __future__ import annotations

from dataclasses import dataclass

from .psyq import Signature

MIN_WORDS = 8  # shorter functions look alike by chance (a stub, a getter)
MIN_KNOWN = 0.6  # a pattern with fewer of its words fully known than this says too little to compare
SURE = 0.95  # this much the same: the library function (set aside with its name)
LIKELY = 0.85  # this much: probably the library function (set aside for a person to confirm)


@dataclass(frozen=True)
class Pattern:
    name: str
    object: str
    version: str
    words: tuple[tuple[int, int], ...]  # (value, mask) per instruction word: mask covers the bytes the pattern knows


def _words(pattern: tuple[int | None, ...]) -> tuple[tuple[int, int], ...]:
    out = []
    for i in range(0, len(pattern) - len(pattern) % 4, 4):
        value = mask = 0
        for k, b in enumerate(pattern[i:i + 4]):  # little-endian, as PS1 code is
            if b is not None:
                value |= b << (8 * k)
                mask |= 0xFF << (8 * k)
        out.append((value, mask))
    return tuple(out)


def patterns(sigs: list[Signature]) -> list[Pattern]:
    """One pattern per library function: a per-function signature as it is, an object's cut at its labels."""
    seen, out = set(), []
    for sig in sigs:
        cuts = sorted(off for _, off in sig.labels)
        for name, off in sig.labels:
            end = next((c for c in cuts if c > off), len(sig.pattern))
            words = _words(sig.pattern[off:end])
            if len(words) < MIN_WORDS or sum(m == 0xFFFFFFFF for _, m in words) < MIN_KNOWN * len(words):
                continue
            key = (name, words)
            if key not in seen:
                seen.add(key)
                out.append(Pattern(name, sig.object, sig.version, words))
    return out


def similarity(code: bytes, pattern: Pattern) -> float:
    """The share of instruction words that agree wherever the pattern knows the bytes, over the longer of the two."""
    n = len(code) // 4
    same = sum(1 for i, (value, mask) in enumerate(pattern.words[:n])
               if int.from_bytes(code[4 * i:4 * i + 4], "little") & mask == value)
    return same / max(n, len(pattern.words))


def near_hits(functions: list[tuple[int, bytes]], pats: list[Pattern], floor: float = LIKELY) -> list[dict]:
    """functions: (id, code). For each one this close to a library function: {id, similarity, name (None when two
    library functions tie for it), candidates: [Pattern] at that similarity}. Compared only with patterns of about
    its length: a copy from another revision adds or drops a few instructions, not a tenth of them."""
    by_len: dict[int, list[Pattern]] = {}
    for p in pats:
        by_len.setdefault(len(p.words), []).append(p)
    out = []
    for fid, code in functions:
        n = len(code) // 4
        if n < MIN_WORDS:
            continue
        slack = max(1, n // 10)
        best, tied = 0.0, []
        for length in range(n - slack, n + slack + 1):
            for p in by_len.get(length, ()):
                s = similarity(code, p)
                if s > best + 1e-9:
                    best, tied = s, [p]
                elif abs(s - best) <= 1e-9 and s > 0:
                    tied.append(p)
        if best >= floor:
            names = {p.name for p in tied}
            out.append({"id": fid, "similarity": best, "name": next(iter(names)) if len(names) == 1 else None,
                        "candidates": tied})
    return out


MIN_BLOCK = 8  # found library functions a stretch needs to count as a library's code
# other functions allowed between two found ones inside a stretch. SLUS-01252's libsnd has 20-odd unfound functions in
# a row between found ones: 12 caught 28 of its 48, 24 caught 47, and nothing more outside Sony's code at either
MAX_GAP = 24
VERSION = 1  # identify_psyq runs again on every project when this changes


def library_blocks(rows: list[tuple[int, str, int, bool]]) -> list[tuple[str, int, int]]:
    """rows: (id, unit, vram, is a found library function). The stretches of each unit that are a library's code:
    runs of found library functions with at most MAX_GAP others between consecutive ones, and at least MIN_BLOCK
    found; as (unit, first vram, last vram). A game's own code is linked as a whole before or after Sony's
    libraries, not threaded through them, so what lies inside such a stretch is almost always library code too."""
    out = []
    by_unit: dict[str, list[tuple[int, bool]]] = {}
    for _, unit, vram, lib in rows:
        if vram is not None:
            by_unit.setdefault(unit, []).append((vram, lib))
    for unit, items in by_unit.items():
        items.sort()
        run: list[int] = []  # vrams of the found ones in the current stretch
        gap = 0
        for vram, lib in items:
            if lib:
                if run and gap > MAX_GAP:
                    if len(run) >= MIN_BLOCK:
                        out.append((unit, run[0], run[-1]))
                    run = []
                run.append(vram)
                gap = 0
            else:
                gap += 1
        if len(run) >= MIN_BLOCK:
            out.append((unit, run[0], run[-1]))
    return out
