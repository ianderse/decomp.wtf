"""Attempts that can't pay off go elsewhere: Sony's library code the exact signatures miss (rom/psyq_near.py), and
functions agents keep giving up on without getting closer (Network._stall_check). On SLUS-01252, 48 open functions in
libsnd's address range had taken 5,613 agent attempts (2026-11-10)."""

import json
import struct
import time

from decompnet.app.engine import RomNetwork
from decompnet.network import STALL_ABANDONS
from decompnet.rom import psyq_near as near
from decompnet.rom.psyq import Signature


def _code(words):
    return b"".join(struct.pack("<I", w) for w in words)


def _sig(obj, functions):
    """A signature for one object holding functions [(name, words)], word values None where the linker fills in."""
    pattern, labels = [], []
    for name, words in functions:
        labels.append((name, len(pattern)))
        for w in words:
            pattern += [None] * 4 if w is None else list(struct.pack("<I", w))
    pattern = tuple(pattern)
    return Signature(obj, "300", pattern, tuple(labels), b"", 0, obj)


BODY = [0x27BDFFE0 + i for i in range(20)]  # 20 distinct instruction words


def test_a_copy_differing_only_where_the_linker_fills_in_is_the_library_function():
    linked = list(BODY)
    linked[3] = None  # a jal target, say
    pats = near.patterns([_sig("SSPLAY.OBJ", [("SsSeqPlay", linked)])])
    game = list(BODY)
    game[3] = 0x0C012345  # whatever the game's link put there
    [hit] = near.near_hits([(1, _code(game))], pats)
    assert hit["similarity"] == 1.0 and hit["name"] == "SsSeqPlay"


def test_another_revision_is_likely_not_sure_and_a_tie_names_nothing():
    pats = near.patterns([_sig("A.OBJ", [("SsUtGetVagAtr", BODY)]), _sig("B.OBJ", [("SsSepClose", [w ^ 1 for w in BODY])])])
    revised = list(BODY)
    revised[5] ^= 0xFF  # two instructions changed in a later SDK
    revised[9] ^= 0xFF
    [hit] = near.near_hits([(1, _code(revised))], pats)
    assert near.LIKELY <= hit["similarity"] < near.SURE and hit["name"] == "SsUtGetVagAtr"
    pats = near.patterns([_sig("SSCLOSE.OBJ", [("SsSeqClose", BODY)]), _sig("SEPCLOSE.OBJ", [("SsSepClose", BODY)])])
    [tie] = near.near_hits([(2, _code(BODY))], pats)
    assert tie["similarity"] == 1.0 and tie["name"] is None  # identical code under two names: for a person
    assert near.near_hits([(3, _code([0x1000 + i for i in range(20)]))], pats) == []  # the game's own code


def test_an_object_is_cut_at_its_labels():
    pats = near.patterns([_sig("LIBGPU.OBJ", [("LoadImage", BODY[:10]), ("StoreImage", BODY[10:])])])
    assert {p.name: len(p.words) for p in pats} == {"LoadImage": 10, "StoreImage": 10}


def test_open_functions_between_found_library_functions_are_in_a_library_block():
    rows, vram = [], 0x80010000
    for i in range(40):  # the game's own code first: nothing found there
        rows.append((len(rows), "main", vram, False)); vram += 0x40
    for i in range(30):  # then Sony's libraries: mostly found, with unfound stretches up to 20 long
        rows.append((len(rows), "main", vram, i % 10 < 3 or i == 29)); vram += 0x40
    [(unit, first, last)] = near.library_blocks(rows)
    assert unit == "main" and first == 0x80010000 + 40 * 0x40 and last == vram - 0x40
    assert near.library_blocks([(i, "main", 0x80010000 + 4 * i, i < 3) for i in range(10)]) == []  # too few found


def _project(tmp_path):
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                         ("game", "Game", "", "mips", 0, str(tmp_path), json.dumps({"kind": "rom"}))).lastrowid
        for name, best, meta in [("far", 4000, None), ("near_miss", 300, None), ("given_back", 4000, {"for_agents": True})]:
            fid = db.execute("INSERT INTO functions(project_id,name,size,n_insns,status,unit,best_score,meta) "
                             "VALUES(?,?,?,?,?,?,?,?)", (pid, name, 400, 100, "open", "main", best,
                                                         json.dumps(meta) if meta else None)).lastrowid
            db.execute("INSERT INTO attempts(function_id,agent_id,score,compiled,source,created_at) VALUES(?,?,?,?,?,?)",
                       (fid, 0, best, 1, "int f(void) { return 0; }", 1.0))
    return net


def _give_up(net, name, times, start=0):
    out = None
    for k in range(start, start + times):
        agent, _ = net.local_agent(f"agent{k}")
        net.claim_row(agent, "game", name, "finish")
        out = net.release(agent, name, "")
    return out


def test_a_function_agents_keep_giving_up_on_goes_to_a_person(tmp_path):
    net = _project(tmp_path)
    _give_up(net, "far", STALL_ABANDONS - 1)
    assert net.store.one("SELECT status FROM functions WHERE name='far'")["status"] == "open"
    said = _give_up(net, "far", 1, start=STALL_ABANDONS)
    row = net.store.one("SELECT status, meta FROM functions WHERE name='far'")
    meta = json.loads(row["meta"])
    assert row["status"] == "asm" and f"{STALL_ABANDONS} agents gave up on it" in meta["review"]
    assert meta["asm_code"].startswith("set aside for a person") and "goes to a person" in said
    net.return_to_agents("game", "far")  # a person gives it back: no rule takes it again
    _give_up(net, "far", STALL_ABANDONS, start=100)
    assert net.store.one("SELECT status FROM functions WHERE name='far'")["status"] == "open"


def test_near_misses_and_functions_given_back_stay_with_agents(tmp_path):
    net = _project(tmp_path)
    _give_up(net, "near_miss", STALL_ABANDONS + 2)  # 3% off: the permuter's to find
    _give_up(net, "given_back", STALL_ABANDONS + 2, start=50)
    assert {r["status"] for r in net.store.all("SELECT status FROM functions WHERE name IN ('near_miss', 'given_back')")} == {"open"}


def test_getting_closer_starts_the_count_again(tmp_path):
    net = _project(tmp_path)
    _give_up(net, "far", STALL_ABANDONS - 1)
    with net.store.tx() as db:  # someone improves it
        fid = db.execute("SELECT id FROM functions WHERE name='far'").fetchone()["id"]
        db.execute("UPDATE functions SET best_score=3000 WHERE id=?", (fid,))
        db.execute("INSERT INTO attempts(function_id,agent_id,score,compiled,source,created_at) VALUES(?,?,?,?,?,?)",
                   (fid, 0, 3000, 1, "int f(void) { return 1; }", time.time()))
    _give_up(net, "far", 2, start=20)
    assert net.store.one("SELECT status FROM functions WHERE name='far'")["status"] == "open"


def test_neighbour_notes_follow_the_setting(tmp_path):
    """Settings turn the matched neighbours' C in claim briefs on and off (Network.neighbour_notes)."""
    from decompnet.network import Network

    net = Network(tmp_path / "app.db")
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                         ("game", "Game", "", "mips", 0, str(tmp_path), json.dumps({"kind": "rom"}))).lastrowid
        db.execute("INSERT INTO functions(project_id,name,size,n_insns,status,unit,vram,matched_source,relocs) "
                   "VALUES(?,?,?,?,?,?,?,?,?)", (pid, "before", 40, 10, "matched", "main", 0x1000,
                                                 "int before(void) { return 7; }", "[]"))
        db.execute("INSERT INTO functions(project_id,name,size,n_insns,status,unit,vram,asm,seed,relocs) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?)", (pid, "mine", 40, 10, "open", "main", 0x1040, "glabel mine", "", "[]"))  # (no draft to make)
        db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                   "VALUES(?,?,?,?,?,?,?)", (pid, 1, "", None, "", 0.0, ""))
    fn = net.store.one("SELECT * FROM functions WHERE name='mine'")
    agent = net.authenticate(net.register_agent("a1", "you"))
    assert "int before(void) { return 7; }" in net._brief(fn, agent, header="CLAIMED")
    net.neighbour_notes = False
    assert "int before(void)" not in net._brief(fn, agent, header="CLAIMED")


def test_the_permuter_check_does_not_find_the_ssh_that_asks(tmp_path):
    """With the permuter machine the app's own, the ssh client asking is on the machine pgrep looks at, its command
    line holding the pattern: "permuter.py .*<folder>/" matched it, so a batch always looked running and new ones
    came only with the 3-hour rotation (SLUS-01252, 2026-11-11)."""
    import shutil
    import subprocess
    import sys

    import pytest

    from decompnet.app.remote_permuter import RemotePermuter

    if shutil.which("pgrep") is None:
        pytest.skip("no pgrep here")
    folder = f"permuter-test-{time.time_ns()}"  # no permuter runs on it
    old, new = f"'permuter.py .*{folder}/'", RemotePermuter._batch_pattern(None, folder)
    check = lambda pattern: subprocess.run(["sh", "-c", f"pgrep -f {pattern} >/dev/null && echo yes || echo no"],
                                           capture_output=True, text=True, check=False).stdout.strip()

    def while_asked(pattern):  # what the check says while the ssh asking it runs on this machine
        asking = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "ssh", "host",
                                   f"pgrep -f {pattern} >/dev/null && echo yes"])
        try:
            time.sleep(0.3)
            return check(pattern)
        finally:
            asking.kill()
            asking.wait()

    assert while_asked(old) == "yes"  # the old pattern found the ssh asking
    assert while_asked(new) == "no"


def test_a_crash_of_the_arm64_ps1_gcc_is_explained_by_a_native_gcc(tmp_path):
    """macOS builds are Apple Silicon only: what 2.7.2's crashes mean comes from GCC 2.95.2's native build, not
    from the same version built for Intel under Rosetta."""
    import pytest

    from decompnet import toolchains
    from decompnet.compiler import compile_source
    from decompnet.network import Network
    from decompnet.project import compiler_config

    if toolchains.current_platform() != "macos-arm64" or not all(
            toolchains.installed(t) for t in ("gcc-2.7.2-psx", "gcc-2.95.2-psx", "maspsx", "binutils-mips")):
        pytest.skip("needs the PS1 GCCs on an Apple Silicon Mac")
    net = Network(tmp_path / "app.db")
    proj = {"root": str(tmp_path)}

    def explained(src):
        crashed = compile_source(compiler_config(tmp_path, {"compiler": {"toolchain": "gcc-2.7.2-psx",
                                                                         "flags": ["-O2", "-G8"]}}), "", src)
        assert "got fatal signal" in crashed.log  # (the crash this is about)
        return net._explain_crash(proj, "gcc-2.7.2-psx", "", src, crashed).log

    said = explained("int f(int a) { return a + undeclared_thing; }\n")
    assert "`undeclared_thing' undeclared" in said and "GCC 2.95.2 says it" in said and "psx-cc" not in said


def test_compiler_dump_shows_what_gcc_made_of_a_source(tmp_path):
    """The hard tail's diagnoses kept asking for "same-recipe GCC RTL / raw generated assembly": compiler_dump
    compiles as the judge does and returns GCC's assembly, maspsx's, and the pass dumps asked for; nothing recorded."""
    import pytest

    from decompnet import toolchains
    from decompnet.network import Network, NetworkError

    if not all(toolchains.installed(t) for t in ("gcc-2.7.2-psx", "maspsx", "binutils-mips")):
        pytest.skip("needs the PS1 GCC 2.7.2 toolchain")
    net = Network(tmp_path / "app.db")
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                         ("game", "Game", "", "mipsel", 0, str(tmp_path),
                          json.dumps({"kind": "rom", "compiler": {"toolchain": "gcc-2.7.2-psx", "flags": ["-O2", "-G8"]}}))).lastrowid
        db.execute("INSERT INTO functions(project_id,name,size,n_insns,status,unit,relocs) VALUES(?,?,?,?,?,?,?)",
                   (pid, "add3", 12, 3, "open", "", "[]"))
        db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                   "VALUES(?,?,?,?,?,?,?)", (pid, 1, "typedef int s32;", None, "", 0.0, ""))
    agent = net.authenticate(net.register_agent("a1", "you"))
    out = net.compiler_dump(agent, "add3", "s32 add3(s32 a) { return a + 3; }", "greg,dbr", "game")
    assert "compiles" in out.splitlines()[0]
    for part in ("### GCC's assembly", "### after the assembler step (maspsx)", "### pass greg", "### pass dbr"):
        assert part in out, part
    assert "addu" in out or "addiu" in out
    assert net.store.one("SELECT COUNT(*) AS n FROM attempts")["n"] == 0
    with pytest.raises(NetworkError, match="unknown pass"):
        net.compiler_dump(agent, "add3", "s32 add3(s32 a) { return a; }", "nonsense", "game")
