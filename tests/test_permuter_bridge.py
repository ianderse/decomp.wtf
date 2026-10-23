"""decompnet.permuter_bridge for (function, source) pairs: which sources a folder or fix-passthrough's report gives,
and the import of a matched function's permuted source as a replacement (judged, then posted to /api/replace)."""

import json
from types import SimpleNamespace

from decompnet import permuter_bridge as pb


def test_sources_from_a_folder_and_from_the_passthrough_report(tmp_path):
    (tmp_path / "func_A.c").write_text("void func_A(void) {}\n")
    (tmp_path / "func_B.before.c").write_text("x")  # not a <name>.c
    (tmp_path / "notes.txt").write_text("x")
    assert pb.sources_from_dir(tmp_path) == {"func_A": "void func_A(void) {}\n"}
    pt = tmp_path / "proj" / "passthrough"
    pt.mkdir(parents=True)
    (pt / "report.json").write_text(json.dumps({"summary": {}, "functions": [
        {"name": "f", "status": "failed"}, {"name": "g", "status": "verified"}, {"name": "h", "status": "failed"}]}))
    (pt / "f.failed.c").write_text("void f(s32 a0) {}\n")
    (pt / "g.after.c").write_text("void g(void) {}\n")
    assert pb.sources_from_passthrough(tmp_path / "proj") == {"f": "void f(s32 a0) {}\n"}  # (h has no file)


class _Net:
    def __init__(self, status="matched"):
        self.row = {"name": "f", "status": status, "best_source": None, "unit": ""}

    def _project(self, slug):
        return {"id": 1, "slug": slug}

    def local_agent(self, name):
        return SimpleNamespace(id=1), ""

    @property
    def store(self):
        net = self
        return SimpleNamespace(one=lambda *a: net.row)

    def _unit_contexts(self, pid):
        return {}

    def _effective_unit(self, fn, units):
        return ""

    def _context(self, pid, unit):
        return {"header": "typedef int s32;\n"}


def _batch(tmp_path, out_source):
    d = tmp_path / "f"
    (d / "output-0-1").mkdir(parents=True)
    (d / "decomp.json").write_text(json.dumps({"slug": "game", "function": "f", "replace": True,
                                                "note": "pass-through argument made explicit (permuted)"}))
    (d / "source.c").write_text("void g(s32);\nvoid f(s32 a0) {\n    g(a0);\n}\n")
    (d / "output-0-1" / "source.c").write_text("typedef int s32;\nvoid g(s32);\n" + out_source)
    return d


def test_a_permuted_match_is_judged_then_replaced(tmp_path, monkeypatch):
    import decompnet.rom.passthrough as pt
    d = _batch(tmp_path, "void f(s32 a0) {\n    s32 t = a0;\n    g(t);\n}\n")
    judged = []
    monkeypatch.setattr(pt, "verify", lambda net, proj, row, source, header: judged.append(source) or (True, ""))
    posted = []
    stats = pb.import_results(_Net(), "game", tmp_path, log=lambda *_: None,
                              post=lambda path, body: posted.append((path, body)) or {"replaced": True})
    # the definition spliced into the function's own source (its prototypes kept)
    assert judged == ["void g(s32);\nvoid f(s32 a0) {\n    s32 t = a0;\n    g(t);\n}\n"]
    assert posted == [("/api/replace", {"slug": "game", "name": "f", "source": judged[0],
                                        "note": "pass-through argument made explicit (permuted)"})]
    assert stats["replace"] == [{"function": "f", "permuter_score": 0, "exact": True, "why": "", "replaced": True}]
    assert (d / "matched.c").read_text() == judged[0] and (d / "output-0-1" / ".judged").exists()


def test_a_near_miss_is_reported_not_posted(tmp_path, monkeypatch):
    import decompnet.rom.passthrough as pt
    _batch(tmp_path, "void f(s32 a0) {\n    g(a0 + 0);\n}\n")
    monkeypatch.setattr(pt, "verify", lambda *a: (False, "score 20: i sw $ra / sw $s2"))
    posted = []
    stats = pb.import_results(_Net(), "game", tmp_path, log=lambda *_: None, post=lambda *a: posted.append(a))
    assert posted == [] and stats["replace"][0]["exact"] is False and stats["replace"][0]["why"].startswith("score 20")
