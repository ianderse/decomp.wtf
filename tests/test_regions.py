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
                       "rom_offset,asm) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (pid, name, 40, 10, vram, json.dumps(meta) if meta else None, best, src, "[]",
                        vram - 0x80000000, "ori $v0, $zero, 1\njr $ra"))
    return net


def _meta(net, name):
    return json.loads(net.store.one("SELECT meta FROM functions WHERE name=?", name)["meta"] or "{}")


def _best(net, name):
    return net.store.one("SELECT best_score FROM functions WHERE name=?", name)["best_score"]


def test_a_compiler_set_for_an_address_range(tmp_path, monkeypatch):
    """Open functions in the range get it (their best attempts judged again with it), one the hint refresh gave a
    compiler too (the owner's range wins); one outside and a matched one keep theirs; clearing takes it back."""
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
    assert _meta(net, "hinted") == {"toolchain": OTHER, "region": "8016A000-80172000"}
    assert _meta(net, "out") == {} and _meta(net, "in_b") == {}
    assert sorted(compiled) == [("hinted", OTHER), ("in_a", OTHER)] and _best(net, "in_a") == 0
    assert res["message"] == (f"{OTHER} set for 3 functions at 8016A000-80172000; best scores: 2 better, 0 worse, "
                              "0 same; 1 had a hinted compiler or one of their own, replaced")
    assert net.regions("game") == res["regions"]
    assert "set for 8016A000-80172000 by the project's owner" in "\n".join(
        net._extra_brief(net.store.one("SELECT * FROM functions WHERE name='in_a'"), net.project_row("game")))

    res = net.set_region("game", "8016A000", "80172000", None)
    assert res["regions"] == [] and json.loads(net.project_row("game")["config"])["regions"] == []
    assert _meta(net, "in_a") == {} and _meta(net, "hinted") == {}
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


def test_hints_skip_the_check_for_a_best_attempt_far_off(tmp_path, monkeypatch):
    """A best attempt nowhere near matching gets the neighbours' compiler without being compiled both ways (then
    judged again with it); a close one still has to score at least as well with it."""
    near = [(f"m{i}", 0x80010000 + 0x100 * i, {"toolchain": OTHER}, None, None) for i in range(4)]
    net = _project(tmp_path, near + [("far", 0x80010500, None, 500, "int f;"),  # 50% of the worst score
                                     ("close", 0x80010600, None, 50, "int c;")])  # 5%
    with net.store.tx() as db:
        db.execute("UPDATE functions SET status='matched' WHERE name LIKE 'm%'")
    compiled = []

    def compile_(proj, header, source, fn=None, toolchain=None):
        compiled.append((fn["name"], toolchain))
        return SimpleNamespace(ok=toolchain == MAIN, obj=b"")  # the close one matches with the project's compiler
    monkeypatch.setattr(net, "_compile", compile_)
    monkeypatch.setattr(net, "_evaluate", lambda fn, proj, obj: (object(), True, []))
    assert net.refresh_toolchain_hints("game") >= 2
    assert _meta(net, "far")["toolchain"] == OTHER and "toolchain_hint" in _meta(net, "far")
    assert "toolchain" not in _meta(net, "close") and _meta(net, "close")["hint_checked"] == [OTHER, 50, False]
    assert sorted(compiled) == [("close", MAIN), ("close", OTHER), ("far", OTHER)]  # far: only its rescore
    assert _best(net, "far") == 1000 and _best(net, "close") == 50
    compiled.clear()
    net.refresh_toolchain_hints("game")  # nothing new: nothing compiled again
    assert compiled == []


def test_a_session_kept_to_an_address_range_claims_in_it(tmp_path):
    """Claims (and the helper's guess of its next one) come from the range, pins outside it wait; with nothing left
    in it, the rest of the game, and the claim says so."""
    from decompnet.app import agents

    net = _project(tmp_path, [("small_out", 0x80010000, None, None, None), ("in_a", 0x8016A000, None, None, None),
                              ("in_b", 0x8016B000, None, None, None), ("pinned_out", 0x80020000, None, None, None)])
    with net.store.tx() as db:
        db.execute("UPDATE functions SET n_insns=5 WHERE name IN ('small_out', 'pinned_out')")  # easiest first
        db.execute("UPDATE projects SET config=json_set(config, '$.priority', json('[\"pinned_out\"]'))")
    a = net.authenticate(net.register_agent("a", "you"))
    b = net.authenticate(net.register_agent("b", "you"))
    net.agent_region[a.id] = (0x8016A000, 0x80172000)
    assert agents.next_function(net, "game", a.id)["name"] == "in_a"
    assert agents.next_function(net, "game", b.id)["name"] == "pinned_out"
    assert net.claim_row(a, "game")["name"] == "in_a"
    with net.store.tx() as db:
        db.execute("DELETE FROM claims")
        db.execute("UPDATE functions SET status='matched' WHERE name LIKE 'in_%'")
    assert agents.next_function(net, "game", a.id)["name"] == "pinned_out"  # the range is done: the usual order
    text = net.claim(a, "game")
    assert "Nothing open and unclaimed is left in your address range 8016A000-80172000" in text
    assert "pinned_out" in text
