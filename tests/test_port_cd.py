"""The port's disc reader (port_runtime/psx/cd.c): an ISO 9660 image written here by hand (2048-byte sectors, a
root directory longer than a sector, a nested directory, SYSTEM.CNF), then the same image as raw 2352-byte Mode 2
Form 1 and Mode 1 sectors, compiled into a small harness with clang."""
from __future__ import annotations

import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from decompnet.rom.export_port import RUNTIME

CLANG = shutil.which("clang") or shutil.which("cc")
pytestmark = pytest.mark.skipif(CLANG is None, reason="needs clang")

HARNESS = r"""
#include <stdio.h>
#include <string.h>
#include "cd.h"
int main(int argc, char **argv) {
    char err[512];
    uint32_t lba, size, off;
    int i;
    if (!psx_disc_open(argv[1], err, sizeof err)) { printf("open-failed %s\n", err); return 0; }
    printf("open %d %u boot=%s\n", psx_disc_sector_size(), psx_disc_sectors(), psx_disc_boot());
    for (i = 2; i < argc; i++) {
        if (!strncmp(argv[i], "read:", 5)) {          /* read:LBA:N -> the bytes' sum and first 8 */
            unsigned l, n, k, sum = 0;
            static unsigned char buf[16 * 2048];
            sscanf(argv[i] + 5, "%u:%u", &l, &n);
            k = (unsigned)psx_disc_read(l, buf, (int)n);
            for (unsigned j = 0; j < k * 2048; j++) sum += buf[j];
            printf("read %u %u ", k, sum);
            for (unsigned j = 0; j < 8; j++) printf("%02x", buf[j]);
            printf("\n");
        } else if (!strncmp(argv[i], "user:", 5)) {   /* user:LBA -> data size and offset in the raw sector */
            const uint8_t *r = psx_disc_raw((uint32_t)atoi(argv[i] + 5));
            int o = -1, n = r ? psx_disc_user(r, &o) : -1;
            printf("user %d %d\n", n, o);
        } else if (!strncmp(argv[i], "at:", 3)) {
            const char *n = psx_disc_name_at((uint32_t)atoi(argv[i] + 3), &off);
            printf("at %s %u\n", n ? n : "-", n ? off : 0);
        } else if (psx_disc_find(argv[i], &lba, &size)) {
            printf("find %u %u\n", lba, size);
        } else {
            printf("find - -\n");
        }
    }
    return 0;
}
"""


def _both(v: int, n: int) -> bytes:
    fmt = {2: "H", 4: "I"}[n]
    return struct.pack("<" + fmt, v) + struct.pack(">" + fmt, v)


def _record(name: bytes, lba: int, size: int, is_dir: bool) -> bytes:
    body = (_both(lba, 4) + _both(size, 4) + bytes([96, 1, 1, 0, 0, 0, 0]) + bytes([2 if is_dir else 0, 0, 0])
            + _both(1, 2) + bytes([len(name)]) + name)
    rec = bytes([0, 0]) + body
    if len(rec) % 2:
        rec += b"\0"
    return bytes([len(rec)]) + rec[1:]


def _directory(entries: list[tuple[bytes, int, int, bool]], own: int, parent: int, sectors: int) -> bytes:
    """the records, none crossing a sector boundary (padding instead), as ISO 9660 lays them out"""
    out = bytearray()
    recs = [_record(b"\0", own, sectors * 2048, True), _record(b"\1", parent, 2048, True)]
    recs += [_record(*e) for e in entries]
    for r in recs:
        if len(out) // 2048 != (len(out) + len(r) - 1) // 2048:
            out += b"\0" * (2048 - len(out) % 2048)
        out += r
    assert len(out) <= sectors * 2048
    return bytes(out) + b"\0" * (sectors * 2048 - len(out))


def make_iso() -> tuple[bytes, dict]:
    """sectors: 16 PVD, 17 terminator, 18-19 root (two sectors), 20 DATA/, 21 SYSTEM.CNF, 22-23 README.TXT,
    24-26 DATA/OVL.BIN, 27 FILLER (the fillers all share it)"""
    readme = bytes((i * 7 + 3) & 0xFF for i in range(3000))
    ovl = b"OVERLAY!" + bytes((i * 13 + 1) & 0xFF for i in range(5000 - 8))
    cnf = b"BOOT = cdrom:\\SLUS_999.99;1\r\nTCB = 4\r\n"
    fillers = [(f"FILLER{i:02d}.DAT;1".encode(), 27, 16, False) for i in range(60)]  # push README to sector 19
    root = _directory(fillers + [(b"DATA", 20, 2048, True), (b"README.TXT;1", 22, len(readme), False),
                                 (b"SYSTEM.CNF;1", 21, len(cnf), False)], 18, 18, 2)
    data = _directory([(b"OVL.BIN;1", 24, len(ovl), False)], 20, 18, 1)
    sectors = [bytes(2048)] * 16
    pvd = bytearray(2048)
    pvd[0:7] = b"\x01CD001\x01"
    pvd[80:88] = _both(28, 4)
    pvd[156:156 + 34] = _record(b"\0", 18, 4096, True)[:34]
    sectors += [bytes(pvd), b"\xffCD001\x01" + bytes(2041), root[:2048], root[2048:], data,
                cnf + bytes(2048 - len(cnf))]
    sectors += [readme[i:i + 2048].ljust(2048, b"\0") for i in (0, 2048)]
    sectors += [ovl[i:i + 2048].ljust(2048, b"\0") for i in (0, 2048, 4096)]
    sectors += [bytes(2048)]
    return b"".join(sectors), {"readme": readme, "ovl": ovl}


def _msf(lba: int) -> bytes:
    a = lba + 150
    bcd = lambda x: (x // 10) << 4 | x % 10  # noqa: E731
    return bytes([bcd(a // 4500), bcd(a // 75 % 60), bcd(a % 75)])


def to_raw(iso: bytes, mode: int, form2_at: int | None = None) -> bytes:
    """2048-byte sectors as raw 2352-byte ones: Mode 2 Form 1 (sub-header 00 00 08 00) or Mode 1; form2_at makes
    one sector a Form 2 audio one (sub-header 01 00 64 04)"""
    sync = b"\0" + b"\xff" * 10 + b"\0"
    out = bytearray()
    for lba in range(len(iso) // 2048):
        data = iso[lba * 2048:(lba + 1) * 2048]
        if mode == 1:
            out += sync + _msf(lba) + b"\x01" + data + bytes(288)
        elif lba == form2_at:
            out += sync + _msf(lba) + b"\x02" + b"\x01\x00\x64\x04" * 2 + bytes(2324) + bytes(4)
        else:
            out += sync + _msf(lba) + b"\x02" + b"\x00\x00\x08\x00" * 2 + data + bytes(280)
    return bytes(out)


@pytest.fixture(scope="module")
def harness(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("cd")
    (d / "harness.c").write_text(HARNESS, encoding="utf-8")
    exe = d / "harness"
    psx = RUNTIME / "psx"
    r = subprocess.run([CLANG, "-std=c11", "-D_DEFAULT_SOURCE", "-O1", "-Wall", "-include", "stdlib.h",
                        "-I", str(psx), str(d / "harness.c"), str(psx / "cd.c"), "-o", str(exe)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return exe


def run(exe: Path, image: Path, *args: str) -> list[str]:
    r = subprocess.run([str(exe), str(image), *args], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


def test_lookup_size_and_read_on_an_iso(harness, tmp_path):
    iso, files = make_iso()
    img = tmp_path / "game.iso"
    img.write_bytes(iso)
    out = run(harness, img, "README.TXT", "\\readme.txt;1", "cdrom:\\DATA\\OVL.BIN;1", "data/ovl.bin",
              "DATA", "NOPE.BIN", "DATA/NOPE", "FILLER59.DAT", "read:24:3", "at:25", "at:18")
    assert out[0] == "open 2048 28 boot=SLUS_999.99"
    assert out[1] == out[2] == "find 22 3000"       # in the root's second sector, any case, ";1" optional
    assert out[3] == out[4] == "find 24 5000"       # nested, '\' or '/', "cdrom:" prefix
    assert out[5:8] == ["find - -"] * 3              # a directory isn't a file; missing names
    assert out[8] == "find 27 16"
    want = files["ovl"] + bytes(3 * 2048 - 5000)
    assert out[9] == f"read 3 {sum(want)} {want[:8].hex()}"
    assert out[10] == "at DATA/OVL.BIN 2048"
    assert out[11] == "at - 0"


def test_mode2_raw_sectors_unwrap_to_the_same_files(harness, tmp_path):
    iso, files = make_iso()
    img = tmp_path / "game.bin"
    img.write_bytes(to_raw(iso, 2, form2_at=27))
    out = run(harness, img, "DATA/OVL.BIN", "read:22:2", "user:22", "user:27")
    assert out[0] == "open 2352 28 boot=SLUS_999.99"
    assert out[1] == "find 24 5000"
    readme = files["readme"] + bytes(4096 - 3000)
    assert out[2] == f"read 2 {sum(readme)} {readme[:8].hex()}"
    assert out[3] == "user 2048 24"                  # Form 1: 2048 bytes after the sub-header
    assert out[4] == "user 2324 24"                  # Form 2 (XA): 2324


def test_mode1_raw_and_cue_sheet(harness, tmp_path):
    iso, files = make_iso()
    (tmp_path / "Game (USA).bin").write_bytes(to_raw(iso, 1))
    cue = tmp_path / "Game (USA).cue"
    cue.write_text('FILE "Game (USA).bin" BINARY\n  TRACK 01 MODE1/2352\n    INDEX 01 00:00:00\n', encoding="ascii")
    out = run(harness, cue, "README.TXT", "user:22")
    assert out[0] == "open 2352 28 boot=SLUS_999.99"
    assert out[1] == "find 22 3000"
    assert out[2] == "user 2048 16"


def test_not_a_disc_and_chd(harness, tmp_path):
    junk = tmp_path / "junk.bin"
    junk.write_bytes(bytes(2352 * 20))
    assert run(harness, junk)[0].startswith("open-failed ")
    chd = tmp_path / "game.chd"
    chd.write_bytes(b"MComprHD")
    assert "chdman" in run(harness, chd)[0]
