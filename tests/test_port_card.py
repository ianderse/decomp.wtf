"""The port's events and memory card (port_runtime/psx/sdk/libapi.c, sdk/libcard.c) and its scripted pad
(psx/pad_script.c), compiled into a small harness with clang: DeliverEvent/TestEvent/WaitEvent/UnDeliverEvent with
psx-spx's semantics (the test clears the ready flag, a disabled event misses its delivery, a callback event runs its
function and is never ready), the card's answers a vblank after the call (TIMEOUT without a card, NEW then IOE after
_card_clear with one, ERROR for a broken image), the card image's format and a file's round trip through the BIOS's
file calls and the image on disk; the bounded log counter; and the generator's side: the bios labels mapped to the
runtime's functions, the stub template's counter."""
from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from decompnet.rom import export_port as ep
from decompnet.rom.export_port import RUNTIME

CLANG = shutil.which("clang") or shutil.which("cc")
PSX = RUNTIME / "psx"
needs_clang = pytest.mark.skipif(CLANG is None, reason="needs clang")

HARNESS = r"""
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "psx_runtime.h"
#include "libapi.h"
#include "libcard.h"

/* what the runtime around them would be: a vblank delivers the card's events */
static int frames, callbacks;
void psx_log(const char *fmt, ...) { va_list ap; va_start(ap, fmt); vfprintf(stderr, fmt, ap); va_end(ap); }
int psx_frames(void) { return frames; }
int psx_vblank_wait(void) { frames++; psx_card_vblank(); return 1; }
static void on_event(void) { callbacks++; }
void *psx_host_fn(uint32_t addr) { (void)addr; return (void *)on_event; }
void psx_heap_init(uint32_t addr, uint32_t size) { (void)addr; (void)size; }
void *psx_malloc(uint32_t n) { return malloc(n); }
void psx_free(void *p) { free(p); }
uint16_t host_pad(void) { return 0; }

#define SW 0xF4000001u
#define HW 0xF0000011u
static unsigned sw[4], hw[4];
static const unsigned specs[4] = {0x0004, 0x8000, 0x0100, 0x2000};
static void open_card_events(void) { /* as the game's func_800DBEC8 */
    int i;
    for (i = 0; i < 4; i++) sw[i] = (unsigned)OpenEvent(SW, specs[i], 0x2000, 0);
    for (i = 0; i < 4; i++) hw[i] = (unsigned)OpenEvent(HW, specs[i], 0x2000, 0);
    for (i = 0; i < 4; i++) { EnableEvent(sw[i]); EnableEvent(hw[i]); }
}
static int wait_any(const unsigned *ev) { /* the game's func_800DC100 */
    for (;;) { int i; for (i = 0; i < 4; i++) if (TestEvent(ev[i]) == 1) return i; }
}
static void drain(const unsigned *ev) { int i; for (i = 0; i < 4; i++) TestEvent(ev[i]); }

int main(int argc, char **argv) {
    const char *cmd = argv[1];
    if (!strcmp(cmd, "events")) {
        unsigned a = (unsigned)OpenEvent(SW, 0x100, 0x2000, 0), b = (unsigned)OpenEvent(SW, 4, 0x2000, 0);
        unsigned c = (unsigned)OpenEvent(SW, 0x100, 0x2000, 0), d, cb;
        printf("handles %08X %08X %08X\n", a, b, c);
        EnableEvent(a); EnableEvent(b); /* c stays disabled */
        printf("before %d %d\n", TestEvent(a), TestEvent(b));
        printf("delivered %d\n", DeliverEvent(SW, 0x100));
        printf("test %d %d %d\n", TestEvent(a), TestEvent(a), TestEvent(b));
        EnableEvent(c);
        printf("disabled missed %d\n", TestEvent(c));
        DeliverEvent(SW, 0x100);
        UnDeliverEvent(SW, 0x100);
        printf("undelivered %d %d\n", TestEvent(a), TestEvent(c));
        DeliverEvent(SW, 4);
        printf("wait %d frames %d\n", WaitEvent(b), frames);
        DisableEvent(b);
        printf("wait disabled %d\n", WaitEvent(b));
        CloseEvent(b);
        d = (unsigned)OpenEvent(HW, 4, 0x1000, 0x80012345u);
        printf("reused %08X\n", d);
        EnableEvent(d);
        DeliverEvent(HW, 4);
        printf("callback %d ready %d\n", callbacks, TestEvent(d));
        cb = (unsigned)OpenEvent(HW, 4, 0x2000, 0);
        printf("unknown handle %d\n", TestEvent(0xF1000063u) + TestEvent(cb));
    } else if (!strcmp(cmd, "nocard")) {
        int r, f0;
        open_card_events();
        drain(sw);
        r = _card_info(0);
        printf("pending %d ready-at-once %d\n", psx_card_pending(), frames);
        f0 = frames;
        r = wait_any(sw);
        printf("sw %d after %d vblank hw %d\n", r, frames - f0, wait_any(hw));
        _card_load(0);
        printf("load %d\n", wait_any(sw));
        printf("open %d first %d status %X\n", psx_bios_open("bu00:X", 1),
               psx_bios_firstfile("bu00:*", malloc(40)), psx_bios_card_status(0));
    } else if (!strcmp(cmd, "card")) { /* PSX_MEMCARD set: the game's info -> NEW -> clear -> info -> load */
        uint8_t data[0x2100], back[0x2100], ent[40];
        int i, fd, n;
        open_card_events();
        drain(sw); drain(hw);
        _card_info(0);
        printf("info %d\n", wait_any(sw));
        drain(hw);
        _card_clear(0);
        printf("clear %d\n", wait_any(hw));
        drain(sw); drain(hw);
        _card_info(0);
        printf("info %d\n", wait_any(sw));
        drain(sw);
        _card_load(0);
        printf("load %d\n", wait_any(sw));
        printf("first %d\n", psx_bios_firstfile("bu00:BASLUS-01252*", ent));
        fd = psx_bios_open("bu00:BASLUS-01252ARC2", (2 << 16) | 0x200);
        for (i = 0; i < (int)sizeof data; i++) data[i] = (uint8_t)(i * 7 + 3);
        n = psx_bios_write(fd, data, sizeof data);
        printf("create fd %d wrote %d close %d\n", fd, n, psx_bios_close(fd));
        printf("again %d\n", psx_bios_open("bu00:BASLUS-01252ARC2", (1 << 16) | 0x200));
        fd = psx_bios_open("bu00:BASLUS-01252ARC2", 1);
        n = psx_bios_read(fd, back, sizeof back);
        psx_bios_close(fd);
        printf("read %d same %d\n", n, !memcmp(data, back, sizeof data));
        printf("first %d", psx_bios_firstfile("bu00:BASLUS-01252*", ent));
        printf(" name %s attr %X size %X sector %X", (char *)ent, ent[0x14], *(uint32_t *)(ent + 0x18),
               *(uint32_t *)(ent + 0x20));
        printf(" next %d\n", psx_bios_nextfile(ent));
        printf("slot2 %d\n", psx_bios_open("bu10:BASLUS-01252ARC2", 1));
    } else if (!strcmp(cmd, "reread")) { /* a second run reads what the first wrote */
        uint8_t back[0x2100];
        int i, fd = psx_bios_open("bu00:BASLUS-01252ARC2", 1), n = psx_bios_read(fd, back, sizeof back), ok = 1;
        for (i = 0; i < n; i++) ok &= back[i] == (uint8_t)(i * 7 + 3);
        printf("reread %d %d\n", n, ok);
        printf("erase %d open %d\n", psx_bios_erase("bu00:BASLUS-01252ARC2"),
               psx_bios_open("bu00:BASLUS-01252ARC2", 1));
    } else if (!strcmp(cmd, "bad")) {
        open_card_events();
        _card_info(0);
        printf("info %d\n", wait_any(sw));
    } else if (!strcmp(cmd, "pad")) {
        PsxPadPress p[8];
        int n = psx_pad_script_parse(argv[2], p, 8, 10), i;
        printf("%d", n);
        for (i = 0; i < n; i++) printf(" %04X:%d-%d", p[i].bits, p[i].from, p[i].to);
        printf("\n");
        if (n > 0)
            for (i = 3; i < argc; i++) printf("%04X ", psx_pad_script_bits(p, n, atoi(argv[i])));
        printf("\n");
    } else if (!strcmp(cmd, "due")) {
        unsigned c = 0, lines = 0;
        unsigned long i;
        for (i = 0; i < 30000; i++) lines += (unsigned)psx_log_due(&c);
        printf("%u %u", c, lines);
        c = PSX_COUNT_MAX - 1;
        lines = 0;
        for (i = 0; i < 5; i++) lines += (unsigned)psx_log_due(&c);
        printf(" %u %u", c, lines);
        c = PSX_LOG_LAST - 1;
        lines = 0;
        for (i = 0; i < 100000; i++) lines += (unsigned)psx_log_due(&c);
        printf(" %u\n", lines);
    }
    return 0;
}
"""


@pytest.fixture(scope="module")
def harness(tmp_path_factory) -> Path:
    if CLANG is None:
        pytest.skip("needs clang")
    d = tmp_path_factory.mktemp("card")
    (d / "inc").mkdir()
    (d / "inc" / "psx_sdk_config.h").write_text("/* the generated SDK config: nothing from common.h */\n")
    (d / "harness.c").write_text(HARNESS, encoding="utf-8")
    exe = d / "harness"
    r = subprocess.run([CLANG, "-std=gnu11", "-O1", "-Wall", "-I", str(PSX), "-I", str(PSX / "sdk"), "-I",
                        str(d / "inc"), str(d / "harness.c"), str(PSX / "sdk" / "libapi.c"),
                        str(PSX / "sdk" / "libcard.c"), str(PSX / "pad_script.c"), "-o", str(exe)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return exe


def run(exe: Path, *args: str, card: Path | None = None) -> list[str]:
    env = {k: v for k, v in os.environ.items() if k != "PSX_MEMCARD"}
    if card is not None:
        env["PSX_MEMCARD"] = str(card)
    r = subprocess.run([str(exe), *args], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


@needs_clang
def test_events_deliver_test_clear(harness):
    out = run(harness, "events")
    assert out[0] == "handles F1000000 F1000001 F1000002"   # 0xF1000000 + slot, as the game saw them
    assert out[1] == "before 0 0"
    assert out[2] == "delivered 1"                           # a and not c: c is disabled
    assert out[3] == "test 1 0 0"                            # ready once: the test clears it
    assert out[4] == "disabled missed 0"                     # enabled after the delivery: it missed it
    assert out[5] == "undelivered 0 0"                       # UnDeliverEvent made both busy again
    assert out[6] == "wait 1 frames 0"                       # already ready: no vblank waited
    assert out[7] == "wait disabled 0"
    assert out[8] == "reused F1000001"                       # the lowest free slot
    assert out[9] == "callback 1 ready 0"                    # EvMdINTR: the function runs, never ready
    assert out[10] == "unknown handle 0"


@needs_clang
def test_no_card_delivers_timeout_a_vblank_later(harness):
    out = run(harness, "nocard")
    assert out[0] == "pending 2 ready-at-once 0"   # SwCARD and HwCARD on their way, nothing delivered in the call
    assert out[1] == "sw 2 after 1 vblank hw 2"    # the game's loop: index 2 is TIMEOUT (spec 0x100)
    assert out[2] == "load 2"
    assert out[3] == "open -1 first 0 status 11"   # no file calls on an empty slot; _card_status: timeout


@needs_clang
def test_card_image_created_formatted_and_a_file_round_trip(harness, tmp_path):
    card = tmp_path / "card.mcr"
    out = run(harness, "card", card=card)
    assert out[0] == "info 3"          # NEW: the card's "new card" flag at power-on
    assert out[1] == "clear 0"         # _card_clear's write: HwCARD IOE
    assert out[2] == "info 0"          # IOE now
    assert out[3] == "load 0"          # formatted: IOE
    assert out[4] == "first 0"         # no save on it: "no save", not "no card"
    assert out[5] == "create fd 2 wrote 8448 close 2"
    assert out[6] == "again -1"        # the BIOS doesn't create over a file
    assert out[7] == "read 8448 same 1"
    assert out[8] == "first 1 name BASLUS-01252ARC2 attr 50 size 4000 sector 40 next 0"
    assert out[9] == "slot2 -1"
    img = card.read_bytes()
    assert len(img) == 0x20000
    frames = [img[i * 128:(i + 1) * 128] for i in range(64)]

    def ok(f):
        x = 0
        for b in f[:0x7F]:
            x ^= b
        return x == f[0x7F]

    assert frames[0][:2] == b"MC" and ok(frames[0]) and frames[63] == frames[0]
    st = [struct.unpack_from("<I", frames[i])[0] for i in range(1, 16)]
    assert st == [0x51, 0x53] + [0xA0] * 13
    assert struct.unpack_from("<IH", frames[1], 4) == (0x4000, 1)        # 2 blocks; the next is block 2 (index 1)
    assert struct.unpack_from("<H", frames[2], 8)[0] == 0xFFFF
    assert frames[1][0x0A:0x0A + 17] == b"BASLUS-01252ARC2\0"
    assert all(ok(frames[i]) for i in range(1, 36))
    assert all(struct.unpack_from("<I", frames[i])[0] == 0xFFFFFFFF for i in range(16, 36))  # no broken sectors
    assert img[0x2000:0x2100] == bytes((i * 7 + 3) & 0xFF for i in range(0x100))            # block 1's data
    out = run(harness, "reread", card=card)
    assert out == ["reread 8448 1", "erase 1 open -1"]
    st = [struct.unpack_from("<I", card.read_bytes(), i * 128)[0] for i in (1, 2)]
    assert st == [0xA1, 0xA3]


@needs_clang
def test_a_broken_card_image_is_an_error(harness, tmp_path):
    card = tmp_path / "short.mcr"
    card.write_bytes(b"\0" * 1000)
    assert run(harness, "bad", card=card) == ["info 1"]   # ERROR (spec 0x8000)
    assert card.stat().st_size == 1000                     # never written


@needs_clang
def test_pad_script_parser(harness):
    out = run(harness, "pad", "start@300,cross@600-640, L1@5", "299", "300", "309", "310", "620", "5")
    assert out[0] == "3 0008:300-309 4000:600-640 0400:5-14"
    assert out[1].split() == ["0000", "0008", "0008", "0000", "4000", "0400"]
    assert run(harness, "pad", "start@300,jump@4")[0] == "-2"   # the second item doesn't parse
    assert run(harness, "pad", "start@30-20")[0] == "-1"


@needs_clang
def test_log_counter_is_bounded(harness):
    c, lines, c2, lines2, lines3 = map(int, run(harness, "due")[0].split())
    assert (c, lines) == (30000, 23)            # the first 20, then the 10,000th, 20,000th, 30,000th
    assert (c2, lines2) == (0x7FFFFFFF, 0)      # saturates at the top, never wraps, logs nothing there
    assert lines3 == 1                          # only the 10,000,000th itself, then never again


# ---- the generator

def test_bios_labels_map_to_the_runtimes_card_and_event_calls():
    rows = [{"name": n, "vram": v, "meta": json.dumps({"asm_code": f"glabel {n}\n  j bios_{lab}\n"})}
            for n, v, lab in [("func_8017CEE0", 0x8017CEE0, "A0_AB"), ("func_8017DD80", 0x8017DD80, "B0_0B_TestEvent"),
                              ("func_8017DE70", 0x8017DE70, "B0_42"), ("func_8017CEB0", 0x8017CEB0, "B0_4E__card_write"),
                              ("func_8017DD50", 0x8017DD50, "B0_07_DeliverEvent")]]
    sony = ep.sony_from_rows(rows)
    assert sony["func_8017CEE0"]["bios"] == "A0_AB" and sony["func_8017DD80"]["bios"] == "B0_0B"
    build = {"functions": [{"name": r["name"], "seg": "main", "vram": r["vram"]} for r in rows]
             + [{"name": "func_800DC288", "seg": "main", "vram": 0x800DC288, "c": "src/main/func_800DC288.c"}]}
    src = {"src/main/func_800DC288.c": "int func_800DC288(int p) { func_8017CEE0(p * 16); "
                                      "while (func_8017DD80(0) != 1) {} func_8017DE70(0, 0); "
                                      "func_8017CEB0(0, 0x3F, 0); func_8017DD50(1, 2); return 0; }"}
    p = ep.plan(build, src, {}, sony)
    by = {s["name"]: s for s in p["sdk"]}
    assert all(by[r["name"]]["kind"] == "bios" for r in rows)
    c = ep.sdk_stubs_c(p["sdk"])
    assert "int func_8017CEE0(int a) { return psx_bios_card_info(a); }" in c
    assert "int func_8017DD80(unsigned a) { return psx_bios_TestEvent(a); }" in c
    assert "int func_8017CEB0(int a, int b, const void *c) { return psx_bios_card_write(a, b, PSX_PTR(c)); }" in c
    assert "psx_bios_firstfile(PSX_PTR(a), PSX_PTR(b)) ? psx_ps1(PSX_PTR(b)) : 0u" in c  # the game's own pointer
    assert '#include "libcard.h"' in c and "psx_sdk_stub(" not in c


def test_every_bios_call_is_declared_by_the_runtime():
    headers = "".join((PSX / "sdk" / h).read_text() for h in ("libapi.h", "libcard.h"))
    import re
    for slot, (_, _, call) in ep.BIOS_LIBC.items():
        for fn in re.findall(r"\b(psx_\w+)\(", call):
            if fn not in ("psx_ps1",):
                assert re.search(rf"\b{fn}\(", headers), (slot, fn)


def test_libcard_names_are_implemented():
    for n in ("_card_info", "_card_load", "_card_clear", "_card_read", "_card_write", "InitCARD", "_bu_init",
              "TestEvent", "WaitEvent", "DeliverEvent"):
        assert n in ep.IMPLEMENTED
    text = (PSX / "sdk" / "libcard.c").read_text() + (PSX / "sdk" / "libapi.c").read_text()
    for n, lib in ep.IMPLEMENTED.items():
        if lib == "libcard" or n.endswith("Event"):
            assert f"#ifndef PSX_GAME_HAS_{n} " in text or f"#ifndef PSX_GAME_HAS_{n}\n" in text, n


def test_stub_template_counter_is_bounded():
    c = ep.sdk_stubs_c([{"name": "func_8017DDB0", "lib": "libapi", "bios": "B0_14", "label": "bios_B0_14_StopPAD2",
                         "kind": "stub", "called": True}])
    assert "static unsigned calls_func_8017DDB0;" in c
    assert 'return psx_sdk_stub("func_8017DDB0 (bios_B0_14_StopPAD2)", &calls_func_8017DDB0);' in c
    rt = (PSX / "psx_runtime.h").read_text()
    assert "int psx_sdk_stub(const char *name, unsigned *calls);" in rt
    disp = (PSX / "dispatch.c").read_text()
    body = disp[disp.index("int psx_sdk_stub("):]
    body = body[:body.index("\n}\n")]
    assert "psx_log_due(calls)" in body and "++*calls" not in body
