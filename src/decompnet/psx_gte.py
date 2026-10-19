"""The PS1's geometry coprocessor (GTE) from C: trusted gte_* macros the compiler step supplies.

Games drove the GTE from C through PSY-Q's macros (inline assembly underneath). Agents may not write inline
assembly (a match has to come from C), so functions using the GTE used to be set aside. Now psx_gte.h, which agents
can't change or redefine, is included before every PS1 compile, and they call gte_ldv0(), gte_rtps(),
gte_stsxy()... like the original code did. Ported from SynapticDreams/decomp.wtf (#71, MIT); the command table comes
from Rabbitizer's GTE macros (MIT, notice kept in the header).
"""

from __future__ import annotations

import re
from pathlib import Path

# types the PS1's libraries use with the GTE (new PS1 projects get them in context.h)
TYPES = """
/* PS1 vector and matrix types (as PSY-Q's libgte declares them) */
typedef struct { short vx, vy, vz, pad; } SVECTOR;
typedef struct { int vx, vy, vz, pad; } VECTOR;
typedef struct { short vx, vy; } DVECTOR;
typedef struct { unsigned char r, g, b, cd; } CVECTOR;
typedef struct { short x, y, w, h; } RECT;
typedef struct { short m[3][3]; int t[3]; } MATRIX;
"""

# the function field of each GTE command the header can emit
COMMANDS = {1, 6, 12, 16, 17, 18, 19, 20, 22, 27, 28, 30, 32, 40, 41, 42, 45, 46, 48, 61, 62, 63}
_OVERRIDE = re.compile(r"^\s*#\s*(?:define|undef)\s+(?:gte_\w*|__decomp_gte\w*|DECOMP_PSX_GTE_H)\b", re.M)


def header_path() -> Path:
    return Path(__file__).with_name("psx_gte.h").resolve()


def check_overrides(text: str) -> None:
    """Refuse source or a header that redefines the trusted macros (a way around the no-inline-asm rule)."""
    from .compiler import _COMMENTS_AND_STRINGS, SourceRejected

    text = _COMMENTS_AND_STRINGS.sub(" ", re.sub(r"\\\r?\n", "", text or ""))
    if _OVERRIDE.search(text):
        raise SourceRejected("the trusted GTE macros (gte_*) can't be redefined or undefined")


def enabled(cfg: dict) -> bool:
    """A PS1 project's compiler supplies the macros (unless its compiler settings say gte = false)."""
    from . import toolchains

    c = cfg.get("compiler", {})
    try:
        return bool(c.get("gte", True) and c.get("toolchain") and toolchains.get(c["toolchain"]).platform == "PS1")
    except Exception:
        return False


def count(code: bytes) -> int:
    """GTE instructions in a function's (little-endian) code."""
    return sum(int.from_bytes(code[i:i + 4], "little") >> 26 in (0x12, 0x32, 0x3A) for i in range(0, len(code) - 3, 4))
