"""Replacing a match (Network.replace_match, POST /api/replace): another source that is byte-identical too takes
the matched one's place, credit unchanged; anything less changes nothing; a header edit rides along only with a
source proven to match against it. No compiler needed: the compile and the judge are stubbed (test_regions.py's
way), a source "matches" when it holds MATCH and, if it needs the edited header, finds it."""

import json
import time
from types import SimpleNamespace

import pytest

from decompnet.app.engine import RomNetwork
from decompnet.elfobj import FunctionObj
from decompnet.network import NetworkError

CODE = bytes.fromhex("0800e00300000000")  # jr $ra; nop
HEADER = "void caller(void);\n"


def _net(tmp_path, monkeypatch):
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    cfg = {"kind": "rom", "platform": "psx", "compiler": {"toolchain": "gcc-2.7.2-psx", "flags": []}}
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                         ("game", "Game", "", "mips", 0, str(tmp_path), json.dumps(cfg))).lastrowid
        db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                   "VALUES(?,?,?,?,?,?,?)", (pid, 1, HEADER, None, "", time.time() - 100, ""))
        matcher = db.execute("INSERT INTO agents(name,owner,token_hash,created_at,trusted,origin) "
                             "VALUES('codex-1','me','x',0,1,'cli')").lastrowid
        for name, status, src in (("caller", "matched", "void caller(void) { callee(); } /* MATCH */"),
                                  ("other", "matched", "void other(void) { } /* MATCH */"),
                                  ("todo", "open", None)):
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,vram,status,matched_source,matched_agent_id,"
                       "matched_at,best_score,best_source,relocs,rom_offset,asm,target,unit) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (pid, name, 8, 2, 0x80010000, status, src, matcher if src else None, 1000.0 if src else None,
                        0 if src else None, src, "[]", 0x100, "jr $ra", CODE, ""))
    target = FunctionObj("f", "mips", False, CODE, [])
    compiled = []

    def compile_(proj, header, source, fn=None, toolchain=None):
        compiled.append((fn["name"] if fn is not None else None, header, source))
        needs = "a0" in source and "caller(s32 a0)" not in header
        return SimpleNamespace(ok=not needs, obj=source.encode(), log="conflicting types for 'caller'" if needs else "")
    monkeypatch.setattr(net, "_compile", compile_)
    monkeypatch.setattr(net, "_target_obj", lambda fn, proj: target)
    monkeypatch.setattr(net, "_evaluate", lambda fn, proj, obj: (target, b"MATCH" in obj, []))
    agent, _ = net.local_agent("you")
    return net, agent, compiled


def _row(net, name):
    return net.store.one("SELECT * FROM functions WHERE name=?", name)


def test_a_matching_replacement_is_recorded_and_the_credit_stays(tmp_path, monkeypatch):
    net, you, _ = _net(tmp_path, monkeypatch)
    before = _row(net, "caller")
    new = "void caller(void) { callee(0); } /* MATCH */"
    res = net.replace_match(you, "caller", new, "game", note="pass-through argument made explicit")
    assert res["replaced"] is True and res["score"] == 0
    after = _row(net, "caller")
    assert after["matched_source"] == new and after["best_source"] == new and after["status"] == "matched"
    assert after["matched_agent_id"] == before["matched_agent_id"]  # codex-1 keeps it
    assert after["matched_at"] > before["matched_at"]  # the rebuilt-game check looks at it again
    att = net.store.all("SELECT * FROM attempts WHERE function_id=?", after["id"])
    assert [(a["via"], a["score"], a["agent_id"], a["source"]) for a in att] == [("replace", 0, you.id, new)]
    notes = net.store.all("SELECT text FROM notes WHERE function_id=?", after["id"])
    assert len(notes) == 1 and "pass-through argument made explicit" in notes[0]["text"] and "codex-1" in notes[0]["text"]
    assert net.store.one("SELECT detail FROM events WHERE kind='replace'")["detail"] == "caller"
    assert net._replace_stamp(after["project_id"]) is not None


def test_a_replacement_that_does_not_match_changes_nothing(tmp_path, monkeypatch):
    net, you, _ = _net(tmp_path, monkeypatch)
    before = dict(_row(net, "caller"))
    res = net.replace_match(you, "caller", "void caller(void) { callee(1); }", "game", note="x")
    assert res["replaced"] is False and res["text"].startswith("❌ NOT REPLACED")
    res = net.replace_match(you, "caller", "void caller(s32 a0) { callee(a0); } /* MATCH */", "game")
    assert res["replaced"] is False and "compile error" in res["text"]  # the header still says (void)
    assert dict(_row(net, "caller")) == before
    assert net.store.one("SELECT COUNT(*) AS n FROM attempts")["n"] == 0
    assert net.store.one("SELECT COUNT(*) AS n FROM notes")["n"] == 0
    assert net._replace_stamp(before["project_id"]) is None


def test_a_header_edit_goes_in_only_with_a_matching_source(tmp_path, monkeypatch):
    net, you, compiled = _net(tmp_path, monkeypatch)
    edit = {"header_old": "void caller(void);", "header_new": "void caller(s32 a0);", "message": "caller takes $a0"}
    # not a match against the would-be header: neither the header nor the source changes, nothing else compiled
    res = net.replace_match(you, "caller", "void caller(s32 a0) { callee(a0); }", "game", **edit)
    assert res["replaced"] is False
    assert net._context(_row(net, "caller")["project_id"])["header"] == HEADER
    assert [c[0] for c in compiled] == ["caller"]  # judged against the would-be header only
    assert "caller(s32 a0)" in compiled[0][1]
    # a match: the header edit goes through edit_context, which judges every matched function with the new source
    compiled.clear()
    new = "void caller(s32 a0) { callee(a0); } /* MATCH */"
    res = net.replace_match(you, "caller", new, "game", note="made explicit", **edit)
    assert res["replaced"] is True, res["text"]
    pid = _row(net, "caller")["project_id"]
    ctx = net._context(pid)
    assert ctx["header"] == "void caller(s32 a0);\n" and ctx["version"] == 2
    assert ("caller", ctx["header"], new) in compiled and any(c[0] == "other" for c in compiled)
    assert _row(net, "caller")["matched_source"] == new
    assert net.store.one("SELECT context_version FROM attempts WHERE via='replace'")["context_version"] == 2


def test_a_header_edit_another_match_rejects_changes_nothing(tmp_path, monkeypatch):
    net, you, _ = _net(tmp_path, monkeypatch)
    real = net._compile

    def compile_(proj, header, source, fn=None, toolchain=None):  # `other` breaks with the edited header
        if fn is not None and fn["name"] == "other" and "s32 a0" in header:
            return SimpleNamespace(ok=False, obj=None, log="too few arguments to function 'caller'")
        return real(proj, header, source, fn, toolchain)
    monkeypatch.setattr(net, "_compile", compile_)
    before = dict(_row(net, "caller"))
    res = net.replace_match(you, "caller", "void caller(s32 a0) { callee(a0); } /* MATCH */", "game",
                            header_old="void caller(void);", header_new="void caller(s32 a0);", message="m")
    assert res["replaced"] is False and "REJECTED" in res["text"]
    assert dict(_row(net, "caller")) == before and net._context(before["project_id"])["header"] == HEADER


def test_only_a_match_can_be_replaced(tmp_path, monkeypatch):
    net, you, _ = _net(tmp_path, monkeypatch)
    with pytest.raises(NetworkError, match="isn't matched"):
        net.replace_match(you, "todo", "void todo(void) { } /* MATCH */", "game")
    assert _row(net, "todo")["status"] == "open" and _row(net, "todo")["matched_source"] is None


def test_a_replacement_makes_the_relink_due(tmp_path, monkeypatch):
    net, you, _ = _net(tmp_path, monkeypatch)
    pid = _row(net, "caller")["project_id"]
    with net.store.tx() as db:
        cfg = json.loads(db.execute("SELECT config FROM projects WHERE id=?", (pid,)).fetchone()["config"])
        cfg["export"] = {"checked_matched": 2, "context_version": net._header_stamp(pid), "replaced": None}
        db.execute("UPDATE projects SET config=? WHERE id=?", (json.dumps(cfg), pid))
    assert net.relink_due("game") is False
    net.replace_match(you, "caller", "void caller(void) { callee(0); } /* MATCH */", "game")
    assert net.relink_due("game") is True


def test_the_endpoint_replaces_as_you(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from decompnet.app.server import build

    home = tmp_path / "home"
    (home / ".decomp").mkdir(parents=True)
    net, _, _ = _net(home, monkeypatch)
    mcp, _, security = build(net, home / ".decomp", 6498, "s3cret")
    client = TestClient(mcp.streamable_http_app(transport_security=security, host="127.0.0.1"),
                        base_url="http://127.0.0.1:6498")
    client.cookies.set("decomp_live", "s3cret")
    body = {"slug": "game", "name": "caller", "source": "void caller(s32 a0) { callee(a0); } /* MATCH */",
            "note": "made explicit", "header_old": "void caller(void);", "header_new": "void caller(s32 a0);",
            "message": "caller takes $a0"}
    assert client.post("/api/replace", json=body).status_code == 403  # no x-decomp header
    r = client.post("/api/replace", json=body, headers={"x-decomp": "1"}).json()
    assert r["replaced"] is True and r["score"] == 0, r
    assert net.store.one("SELECT a.name FROM attempts t JOIN agents a ON a.id=t.agent_id")["name"] == "you"
    r = client.post("/api/replace", json={**body, "name": "todo"}, headers={"x-decomp": "1"})
    assert r.status_code == 400 and "isn't matched" in r.json()["error"]
