"""A compiler for an address range, hints that don't trust a far-off best attempt, sessions kept to a range. No
compiler needed: a project and function rows, with the compile stubbed."""

import json
import time
from types import SimpleNamespace

import pytest

from decompnet.app.engine import RomNetwork

MAIN, OTHER = "gcc-2.7.2-psx", "gcc-2.8.1-psx@2.34"


def _project(tmp_path, rows):
    """A PS1 project with functions (name, vram, meta, best_score, best_source)."""
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    cfg = {"kind": "rom", "platform": "psx", "compiler": {"toolchain": MAIN, "flags": []}}
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                         ("game", "Game", "", "mips", 0, str(tmp_path), json.dumps(cfg))).lastrowid
        db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                   "VALUES(?,?,?,?,?,?,?)", (pid, 1, "", None, "", time.time(), ""))
        for name, vram, meta, best, src in rows:
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,vram,meta,best_score,best_source,relocs,"
                       "rom_offset) VALUES(?,?,?,?,?,?,?,?,?,?)", (pid, name, 40, 10, vram, json.dumps(meta) if meta
                                                                   else None, best, src, "[]", vram - 0x80000000))
    return net


def _meta(net, name):
    return json.loads(net.store.one("SELECT meta FROM functions WHERE name=?", name)["meta"] or "{}")


def _best(net, name):
    return net.store.one("SELECT best_score FROM functions WHERE name=?", name)["best_score"]


def test_a_compiler_set_for_an_address_range(tmp_path, monkeypatch):
    """Open functions in the range get it (their best attempts judged again with it); one outside, a matched one
    and one the hint refresh gave a compiler keep theirs; clearing takes it back, hinted ones still alone."""
    hinted = {"toolchain": "gcc-2.8.1-psx", "toolchain_hint": "its code has the ori for li fingerprint"}
    net = _project(tmp_path, [("in_a", 0x8016A000, None, 300, "int a;"), ("in_b", 0x80171FFC, None, 200, "int b;"),
                              ("hinted", 0x8016B000, hinted, 100, "int h;"), ("out", 0x80172000, None, 50, "int o;"),
                              ("no_best", 0x8016C000, None, None, None)])
    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='matched' WHERE name='in_b'")
    compiled = []

    def compile_(proj, header, source, fn=None, toolchain=None):
        compiled.append((fn["name"], toolchain))
        return SimpleNamespace(ok=toolchain == OTHER, obj=b"")  # matches with the range's compiler, fails with 2.7.2
    monkeypatch.setattr(net, "_compile", compile_)
    monkeypatch.setattr(net, "_evaluate", lambda fn, proj, obj: (object(), True, []))

    res = net.set_region("game", "8016A000", "0x80172000", OTHER)
    assert [(r["from"], r["to"], r["toolchain"], r["open"], r["matched"]) for r in res["regions"]] == [
        (0x8016A000, 0x80172000, OTHER, 3, 1)]
    assert _meta(net, "in_a") == {"toolchain": OTHER, "region": "8016A000-80172000"}
    assert _meta(net, "no_best")["toolchain"] == OTHER
    assert _meta(net, "hinted") == hinted and _meta(net, "out") == {} and _meta(net, "in_b") == {}
    assert compiled == [("in_a", OTHER)] and _best(net, "in_a") == 0
    assert res["message"] == (f"{OTHER} set for 2 functions at 8016A000-80172000; best scores: 1 better, 0 worse, "
                              "0 same; 1 kept their hinted compiler")
    assert net.regions("game") == res["regions"]
    assert "set for 8016A000-80172000 by the project's owner" in "\n".join(
        net._extra_brief(net.store.one("SELECT * FROM functions WHERE name='in_a'"), net.project_row("game")))

    res = net.set_region("game", "8016A000", "80172000", None)
    assert res["regions"] == [] and json.loads(net.project_row("game")["config"])["regions"] == []
    assert _meta(net, "in_a") == {} and _meta(net, "hinted") == hinted
    assert _best(net, "in_a") == 10 * 100  # doesn't compile with the project's compiler: the worst score


def test_ranges_set_over_each_other_keep_what_the_latest_leaves(tmp_path, monkeypatch):
    net = _project(tmp_path, [("f", 0x80010000, None, None, None)])
    net.set_region("game", "80000000", "80020000", OTHER)
    net.set_region("game", "80008000", "80010000", "gcc-2.8.1-psx")
    got = [(r["from"], r["to"], r["toolchain"]) for r in net.regions("game")]
    assert got == [(0x80000000, 0x80008000, OTHER), (0x80008000, 0x80010000, "gcc-2.8.1-psx"),
                   (0x80010000, 0x80020000, OTHER)]
    assert _meta(net, "f")["toolchain"] == OTHER
    with pytest.raises(ValueError):
        net.check_region("game", "80010000", "80000000", OTHER)
    with pytest.raises(ValueError):
        net.check_region("game", "80000000", "80010000", "ido-7.1")  # an N64 compiler
