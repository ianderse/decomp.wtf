"""decompnet.rom.passthrough: the C scanner (positions, K&R, prototypes inside a body), the fixed point over
pass-through calls (a caller that gains parameters makes its callers drop them), the rewrite (definition, call
sites, prototypes, the header's), and fix()'s dry run and apply order with the judge stubbed."""

import json
import struct
import time
from types import SimpleNamespace

from decompnet.rom import passthrough as pt

NOP, JR_RA = 0, 0x03E00008
FRAME = [0x27BDFFE8, 0xAFBF0010]  # addiu $sp, $sp, -24; sw $ra, 16($sp)
MOVE_A0_ZERO = 0x00002021  # addu $a0, $zero, $zero


def jal(vram: int) -> int:
    return 0x0C000000 | ((vram >> 2) & 0x3FFFFFF)


def code(*calls_to: int, before: tuple = ()) -> list[int]:
    words = FRAME + list(before)
    for v in calls_to:
        words += [jal(v), NOP]
    return words + [JR_RA, NOP]


def test_mask_keeps_positions():
    src = 'int f(void) { /* g( */ char *s = "h(1, 2)"; // i(\n#define X j(\n return 0; }'
    m = pt.mask(src)
    assert len(m) == len(src) and "g(" not in m and "h(" not in m and "i(" not in m and "j(" not in m
    assert m.index('"') == src.index('"') and m.count("\n") == src.count("\n")


def test_parse_param():
    assert (pt.parse_param("void *arg0").type, pt.parse_param("void *arg0").name) == ("void *", "arg0")
    fp = pt.parse_param("void (*cb)(s32)")
    assert fp.name == "cb" and fp.decl("a1") == "void (*a1)(s32)"
    assert pt.parse_param("s32").name is None and pt.parse_param("u8 *").name is None
    assert pt.parse_param("const char *const name").name == "name"
    assert pt.parse_param("s32 tbl[4]").decl("a0") == "s32 a0[4]"


def test_toplevel_definitions_prototypes_knr():
    src = ("M2C_UNK g();\nextern void h(s32, s32);\nstruct S { int a; };\n"
           "s32 kr(a, b)\nu32 a;\nchar *b;\n{\n    return a;\n}\n"
           "void f(void) {\n    void k(s32);\n    g();\n    k(1);\n}\n")
    defs, protos = pt.toplevel(src)
    assert set(defs) == {"kr", "f"}
    assert defs["kr"].form == "knr" and [p.decl() for p in defs["kr"].params] == ["u32 a", "char *b"]
    assert defs["f"].form == "void" and defs["f"].ret == "void"
    assert [(p.name, p.n) for p in protos] == [("g", None), ("h", 2)]
    inner = pt.body_prototypes(src, pt.mask(src), defs["f"])
    assert [(p.name, p.n, p.inside) for p in inner] == [("k", 1, True)]
    calls = pt.body_calls(pt.mask(src), defs["f"], {"g", "k"})
    assert [(c.callee, c.nargs) for c in calls] == [("g", 0), ("k", 1)]  # not the declaration of k


CALLEE = "void callee(void *p, s32 n) {\n    *(s32 *)p = n;\n}\n"
MID = "M2C_UNK callee();\n\nvoid mid(void *arg0) {\n    callee(arg0);\n}\n"  # drops $a1: passes it through
TOP = "void mid(void *);\n\ns32 top(void) {\n    mid(0);\n    return 1;\n}\n"  # mid() will want $a1 too: top's own
KR = "s32 kr(x)\ns32 x;\n{\n    callee();\n    return x;\n}\n"
LEFT = "void callee(void *p, s32 n);\nvoid left(void) {\n    callee();\n}\n"  # writes $a0 first: leftover


def _fns():
    v = {"callee": 0x80010000, "mid": 0x80010100, "top": 0x80010200, "kr": 0x80010300, "left": 0x80010400}
    words = {"callee": [JR_RA, NOP], "mid": code(v["callee"]), "top": code(v["mid"], before=(MOVE_A0_ZERO,)),
             "kr": code(v["callee"]),
             "left": code(v["callee"], before=(MOVE_A0_ZERO,))}
    srcs = {"callee": CALLEE, "mid": MID, "top": TOP, "kr": KR, "left": LEFT}
    return {n: pt.Fn(n, srcs[n], words[n], v[n]) for n in v}


def test_plan_to_a_fixed_point_callee_first():
    p = pt.plan(_fns())
    fw = {(x.caller, x.callee): (x.nargs, x.upto, x.need, x.status) for x in p.forwards}
    assert fw[("mid", "callee")] == (1, 2, 2, "pass-through")
    assert fw[("top", "mid")] == (1, 2, 2, "pass-through")  # `mid(0)` writes $a0, but $a1 is top's own
    assert fw[("kr", "callee")] == (0, 2, 2, "pass-through")
    assert fw[("left", "callee")] == (0, 0, 2, "leftover")
    assert [x.decl() for x in p.params["mid"]] == ["void *arg0", "s32 a1"]
    assert [x.decl() for x in p.params["kr"]] == ["s32 x", "s32 a1"]
    assert [x.decl() for x in p.params["top"]] == ["s32 a0", "s32 a1"]  # a0 unused, a1 handed on
    assert p.order.index("mid") < p.order.index("top") and "left" not in p.order and "callee" not in p.order
    assert p.knr == ["kr"] and p.level == {"mid": 1, "kr": 1, "top": 2} and p.added == {"mid": 1, "kr": 1, "top": 2}


def test_a_chain_one_level_up():
    fns = _fns()
    fns["top"] = pt.Fn("top", "void mid(void *);\n\ns32 top(void *arg0) {\n    mid(arg0);\n    return 1;\n}\n",
                       code(fns["mid"].vram), fns["top"].vram)
    p = pt.plan(fns)
    assert p.order.index("mid") < p.order.index("top") and p.level["top"] == 2
    rw = pt.rewrite(p, fns["top"])
    assert "void mid(void *arg0, s32 a1);" in rw.source  # its prototype of mid follows mid's rewrite
    assert "s32 top(void *arg0, s32 a1) {" in rw.source and "mid(arg0, a1);" in rw.source
    assert rw.regs == ["$a1"] and rw.callees == ["mid"]
    assert pt.note(rw) == "pass-through argument made explicit: $a1 reaches mid unchanged (port hazard (a))"


def test_rewrite_forms_and_the_header():
    fns = _fns()
    p = pt.plan(fns)
    rw = pt.rewrite(p, fns["mid"], header="void mid(void *);\n")
    assert rw.source == "void callee(void *p, s32 n);\n\nvoid mid(void *arg0, s32 a1) {\n    callee(arg0, a1);\n}\n"
    assert (rw.header_old, rw.header_new) == ("void mid(void *);", "void mid(void *arg0, s32 a1);")
    kr = pt.rewrite(p, fns["kr"])
    assert kr.source.startswith("void callee(void *p, s32 n);\n\ns32 kr(x, a0, a1)") is False  # x is $a0 already
    assert "s32 kr(x, a1)\ns32 x;\ns32 a1;\n{" in kr.source and "callee(x, a1);" in kr.source
    assert kr.source.startswith("void callee(void *p, s32 n);\n\n")  # no prototype of its own: one added
    # "caller-return" keeps the caller's own return type for the callee; "generic" types the parameters plainly
    alt = pt.rewrite(p, fns["mid"], "caller-return")
    assert alt.source.startswith("M2C_UNK callee(void *p, s32 n);")
    assert pt.rewrite(p, fns["mid"], "generic").source.startswith("M2C_UNK callee(void *p, s32 n);")
    assert pt.rewrite(p, fns["left"]) is None and pt.rewrite(p, fns["callee"]) is None


def test_a_call_still_short_gets_an_unprototyped_declaration():
    fns = _fns()
    fns["two"] = pt.Fn("two", "M2C_UNK callee();\nvoid two(void) {\n    callee();\n    callee();\n}\n",
                       code(fns["callee"].vram, fns["callee"].vram), 0x80010500)
    p = pt.plan(fns)
    rw = pt.rewrite(p, fns["two"], "real+first-call")
    assert "void callee();" in rw.source and "void two(void *a0, s32 a1) {" in rw.source
    assert rw.source.count("callee(a0, a1);") == 1 and rw.source.count("callee();") == 2  # the decl and the 2nd


def _project(tmp_path, monkeypatch, fns, header):
    from decompnet.app.engine import RomNetwork
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    cfg = {"kind": "rom", "platform": "psx", "compiler": {"toolchain": "gcc-2.7.2-psx", "flags": []}}
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                         ("game", "Game", "", "mips", 0, str(tmp_path), json.dumps(cfg))).lastrowid
        db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                   "VALUES(?,?,?,?,?,?,?)", (pid, 1, header, None, "", time.time(), ""))
        for f in fns.values():
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,vram,status,matched_source,relocs,"
                       "rom_offset,asm,target,unit) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (pid, f.name, 4 * len(f.words), len(f.words), f.vram, "matched", f.source, "[]", 0, "",
                        struct.pack(f"<{len(f.words)}I", *f.words), ""))
    judged = []

    def compile_(proj, header, source, fn=None, toolchain=None):
        judged.append((fn["name"], header))
        return SimpleNamespace(ok=True, obj=source.encode(), log="")
    monkeypatch.setattr(net, "_compile", compile_)
    from decompnet.elfobj import FunctionObj
    target = FunctionObj("f", "mips", False, bytes.fromhex("0800e00300000000"), [])
    monkeypatch.setattr(net, "_target_obj", lambda fn, proj: target)
    monkeypatch.setattr(net, "_evaluate", lambda fn, proj, obj: (target, b"BAD" not in obj, []))
    return net, judged


def test_fix_dry_run_then_apply_callee_first(tmp_path, monkeypatch):
    fns = _fns()
    fns["top"] = pt.Fn("top", "s32 top(void *arg0) {\n    mid(arg0);\n    return 1;\n}\n", code(fns["mid"].vram),
                       fns["top"].vram)
    fns["kr"] = pt.Fn("kr", KR.replace("return x;", "return x; /* BAD */"), fns["kr"].words, fns["kr"].vram)
    net, judged = _project(tmp_path, monkeypatch, fns, "void mid(void *);\n")
    res = pt.fix(net, "game", out_dir=tmp_path / "passthrough", log=lambda *_: None)
    assert (res["verified"], res["failed"], res["skipped"]) == (2, 1, 0)
    st = {r["name"]: r for r in res["functions"]}
    assert st["kr"]["status"] == "failed" and len(st["kr"]["why"]) == len(pt.VARIANTS)
    # mid's header prototype goes unprototyped first (top still calls it the old way until it's replaced)
    assert st["mid"]["header"] == {"old": "void mid(void *);", "new": "void mid();"}
    assert "void mid();" in [h for n, h in judged if n == "top"][0]
    out = tmp_path / "passthrough"
    assert (out / "mid.before.c").read_text() == MID and "callee(arg0, a1)" in (out / "mid.after.c").read_text()
    assert (out / "kr.failed.c").exists() and json.loads((out / "report.json").read_text())["summary"]["verified"] == 2
    posted = []

    def post(path, body):
        posted.append((path, body))
        return {"replaced": True, "score": 0}
    res = pt.fix(net, "game", apply=True, post=post, log=lambda *_: None)
    assert [b["name"] for _, b in posted] == ["mid", "top", "mid"]  # callee first; then mid's full prototype
    assert posted[0][1]["header_new"] == "void mid();" and posted[0][1]["note"].startswith("pass-through argument")
    assert (posted[2][1]["header_old"], posted[2][1]["header_new"]) == ("void mid();", "void mid(void *arg0, s32 a1);")
    assert res["applied"] == ["mid", "top"]


def test_apply_stops_at_the_first_refusal(tmp_path, monkeypatch):
    fns = _fns()
    fns["top"] = pt.Fn("top", "s32 top(void *arg0) {\n    mid(arg0);\n    return 1;\n}\n", code(fns["mid"].vram),
                       fns["top"].vram)
    net, _ = _project(tmp_path, monkeypatch, fns, "")
    posted = []
    res = pt.fix(net, "game", apply=True, post=lambda path, body: posted.append(body["name"]) or
                 {"replaced": False, "text": "❌ NOT REPLACED"}, log=lambda *_: None)
    assert posted == [res["functions"][0]["name"]] and res["stopped"]["text"] == "❌ NOT REPLACED"
