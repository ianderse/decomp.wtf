"""Overlays that share addresses: names are per project, so where two overlays have a function at one address one of
them carries the overlay's name (ovl_b_func_80100080), and the symbol table has one name per address. An agent briefed
on an overlay's function sees each address as its own overlay has it (RomNetwork.unit_label); the export and the port
flag C that names another overlay's function there (export_port.cross_overlay_refs). No compiler needed."""

import json
import time

from decompnet.app.engine import RomNetwork
from decompnet.rom import export_port as ep

A, B = "ovl_a", "ovl_b"
BASE = 0x80100000
# (unit, name, vram, size, relocs [(offset, type, address)], asm)
FUNCS = [
    ("main", "main_func", 0x80010000, 0x20, [], "jr $ra"),
    (A, "func_80100000", BASE, 0x40, [], "jr $ra"),
    (A, "func_80100040", BASE + 0x40, 0x40, [(0, 4, BASE + 0x80), (8, 5, BASE + 0x100), (16, 4, BASE + 0xA0), (24, 4, 0x80010000)],
     "/* 0 80100040 0 */ jal func_80100080\n/* 8 80100048 0 */ lui $a0, %hi(func_80100100)\n"
     "/* 10 80100050 0 */ jal func_801000A0\n/* 18 80100058 0 */ jal func_80010000"),
    (A, "func_80100080", BASE + 0x80, 0x40, [], "jr $ra"),
    (B, "ovl_b_func_80100000", BASE, 0x80, [(0, 4, BASE + 0x80)], "/* 0 80100000 0 */ jal ovl_b_func_80100080"),
    (B, "ovl_b_func_80100080", BASE + 0x80, 0x20, [], "jr $ra"),
    (B, "func_801000A0", BASE + 0xA0, 0x20, [], "jr $ra"),
    (B, "func_80100100", BASE + 0x100, 0x40, [], "jr $ra"),
]


def _net(tmp_path):
    net = RomNetwork(tmp_path / "app.db", tmp_path)
    cfg = {"kind": "rom", "platform": "psx", "compiler": {"toolchain": "gcc-2.7.2-psx", "flags": []},
           "segments": [{"name": "main", "rom_start": 0x800, "rom_end": 0x10800, "vram": 0x80010000},
                        {"name": A, "rom_start": 0x10800, "rom_end": 0x11800, "vram": BASE},
                        {"name": B, "rom_start": 0x11800, "rom_end": 0x12800, "vram": BASE}]}
    with net.store.tx() as db:
        pid = db.execute("INSERT INTO projects(slug,name,description,arch,big_endian,root,config) VALUES(?,?,?,?,?,?,?)",
                         ("game", "Game", "", "mips", 0, str(tmp_path), json.dumps(cfg))).lastrowid
        db.execute("INSERT INTO context_versions(project_id,version,header,agent_id,message,created_at,unit) "
                   "VALUES(?,?,?,?,?,?,?)", (pid, 1, "", None, "", time.time(), ""))
        sources = {"ovl_b_func_80100080": "void ovl_b_func_80100080(int x, int y) {}",
                   "func_80100080": "void func_80100080(int x) {}"}
        for unit, name, vram, size, relocs, asm in FUNCS:
            src = sources.get(name)
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,vram,status,matched_source,relocs,rom_offset,"
                       "asm,target,unit) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (pid, name, size, size // 4, vram, "matched" if src else "open", src, json.dumps(relocs), 0,
                        asm, bytes(size), unit))
        # as the import leaves it: one name per address, the last overlay's (SymbolTable.add replaces)
        for unit, name, vram, size, _, _ in FUNCS:
            if (unit, vram) not in ((A, BASE), (A, BASE + 0x80)):
                db.execute("INSERT INTO symbols(project_id,name,address,size,kind,preview) VALUES(?,?,?,?,?,?)",
                           (pid, name, vram, size, "func", None))
    return net, net.project_row("game")


def _fn(net, name):
    return net.store.one("SELECT * FROM functions WHERE name=?", name)


def test_the_brief_names_an_address_as_the_functions_own_overlay_has_it(tmp_path):
    net, proj = _net(tmp_path)
    fn = _fn(net, "func_80100040")
    asm = net._asm_text(fn, proj)
    assert "jal func_80100080" in asm and "ovl_b_func_80100080" not in asm  # its overlay's own function there
    assert "%hi(D_80100100)" in asm  # past ovl_a's last function: its data, not ovl_b's func_80100100
    assert "jal func_801000A0" in asm  # inside ovl_a's func_80100080: an automatic name, nobody's function
    assert "jal main_func" in asm  # the resident program's names as before
    # the project-wide view (no unit) is what it was: the symbol table's one name per address
    assert "ovl_b_func_80100080" in net.rename_tokens(proj, fn["asm"])
    notes = net.overlay_notes(proj, fn)
    assert len(notes) == 2
    assert any(n.startswith("D_80100100: no function of ovl_a starts at 0x80100100") and "past its last function" in n
               and "ovl_b's func_80100100" in n for n in notes), notes
    assert any(n.startswith("func_801000A0:") and "inside its func_80100080 (+0x20)" in n for n in notes), notes
    # the other overlay keeps its own names
    other = _fn(net, "ovl_b_func_80100000")
    assert "jal ovl_b_func_80100080" in net._asm_text(other, proj)
    assert net.overlay_notes(proj, other) == []


def test_calls_callers_and_signatures_are_the_overlays_own(tmp_path):
    net, proj = _net(tmp_path)
    view = net.overlay_view(proj)
    assert set(view["shared"]) == {A, B}
    calls, callers, used = net._unit_refs(proj, _fn(net, "func_80100080"), view)
    assert callers == [BASE + 0x40]  # ovl_a's func_80100040; not ovl_b's function at 0x80100000, which calls ovl_b's
    calls, _, used = net._unit_refs(proj, _fn(net, "func_80100040"), view)
    assert calls == [0x80010000, BASE + 0x80, BASE + 0xA0, BASE + 0x100]
    sigs = net._callee_signatures(proj, _fn(net, "func_80100040"), calls)
    assert sigs == ["void func_80100080(int x);"]  # not ovl_b's two-parameter function at the same address
    lines = "\n".join(net._extra_brief(_fn(net, "func_80100040"), proj))
    assert "calls: main_func, func_80100080, func_801000A0, D_80100100" in lines
    assert "### Overlay addresses" in lines


def test_a_view_is_rebuilt_after_a_rename(tmp_path):
    net, proj = _net(tmp_path)
    assert net.overlay_view(proj)["at"][(A, BASE + 0x80)] == "func_80100080"
    with net.store.tx() as db:
        db.execute("UPDATE functions SET name='draw' WHERE name='func_80100080'")
        db.execute("UPDATE symbols_stamp SET v=v+1")
        db.execute("INSERT INTO functions(project_id,name,size,n_insns,vram,status,relocs,rom_offset,asm,target,unit) "
                   "SELECT id,'func_801000C0',4,1,?,'open','[]',0,'',X'00000000',? FROM projects", (BASE + 0xC0, A))
    assert net.overlay_view(proj)["at"][(A, BASE + 0x80)] == "draw"
    assert "jal draw" in net._asm_text(_fn(net, "func_80100040"), proj)


def test_a_game_without_overlays_sharing_addresses_is_named_as_before(tmp_path):
    net, proj = _net(tmp_path)
    cfg = json.loads(proj["config"])
    cfg["segments"][2]["vram"] = 0x80200000  # ovl_b elsewhere: nothing shares an address
    with net.store.tx() as db:
        db.execute("UPDATE projects SET config=?", (json.dumps(cfg),))
    proj = net.project_row("game")
    assert net.overlay_view(proj) is None
    fn = _fn(net, "func_80100040")
    assert net._asm_text(fn, proj) == net.rename_tokens(proj, fn["asm"])


# ------------------------------------------------------------------------------------------- export / port

def _build():
    fns = [{"name": n, "seg": u, "vram": v, "rom": 0, "asm": f"asm/{u}/{n}.s"} for u, n, v, _, _, _ in FUNCS]
    c = {"func_80100040": "src/ovl_a/func_80100040.c", "main_func": "src/main/main_func.c",
         "ovl_b_func_80100080": "src/ovl_b/ovl_b_func_80100080.c"}
    for f in fns:
        if f["name"] in c:
            f["c"] = c[f["name"]]
    segs = [{"name": "main", "kind": "code", "start": 0x800, "end": 0x10800, "vram": 0x80010000},
            {"name": A, "kind": "code", "start": 0x10800, "end": 0x11800, "vram": BASE},
            {"name": B, "kind": "code", "start": 0x11800, "end": 0x12800, "vram": BASE}]
    return {"functions": fns, "segments": segs}


SOURCES = {
    "src/ovl_a/func_80100040.c": """void ovl_b_func_80100080(int);
void func_80100100(void);
extern char func_801000A0[];
void ovl_b_func_80100000(void);  /* declared, never used */
void func_80100040(void) {
    ovl_b_func_80100080(1);   /* ovl_a's own func_80100080 is what runs at that address */
    *(int *)((char *)func_80100100 + 4) = func_801000A0[2];  /* ovl_a's data there, by ovl_b's names */
    main_func();
}
""",
    "src/main/main_func.c": "void ovl_b_func_80100080(int);\nvoid main_func(void) { ovl_b_func_80100080(0); }\n",
    "src/ovl_b/ovl_b_func_80100080.c": "void ovl_b_func_80100080(int x) { func_80100100(); }\n",
}


def test_the_export_and_the_port_flag_another_overlays_function_named_at_a_shared_address():
    refs = {r["name"]: r for r in ep.cross_overlay_refs(_build(), SOURCES)}
    assert set(refs) == {"ovl_b_func_80100080", "func_80100100", "func_801000A0", "ovl_b_func_80100000"}
    call = refs["ovl_b_func_80100080"]
    assert (call["caller"], call["seg"], call["use"], call["own"], call["links"], call["error"]) == \
        ("func_80100040", A, "call", "func_80100080", "matched C", True)
    data = refs["func_80100100"]  # called by name elsewhere (ovl_b), so the port has a stub for it: host code
    assert (data["use"], data["own"], data["links"], data["error"]) == ("address", None, "stub", True)
    assert data["here"] == "after func_80100080, its last function (data)"
    # still assembly and nothing calls it: an absolute symbol at its PS1 address, the right value
    assert (refs["func_801000A0"]["use"], refs["func_801000A0"]["links"], refs["func_801000A0"]["error"]) == \
        ("address", "its address", False)
    assert (refs["ovl_b_func_80100000"]["use"], refs["ovl_b_func_80100000"]["error"]) == ("declared", False)
    md = ep.cross_overlay_md(list(refs.values()))
    assert "**2 errors**" in md and "| `func_80100040` | ovl_a | `ovl_b_func_80100080` | ovl_b | call |" in md


def test_named_by_its_own_overlay_nothing_is_flagged():
    fixed = dict(SOURCES)
    fixed["src/ovl_a/func_80100040.c"] = ("void func_80100080(int);\nextern char D_80100100[];\n"
                                          "void func_80100040(void) { func_80100080(1); D_80100100[4] = 0; }\n")
    assert ep.cross_overlay_refs(_build(), fixed) == []
    assert "No matched C of an overlay names" in ep.cross_overlay_md([])
    b = _build()
    b["segments"][2]["vram"] = 0x80200000  # no shared addresses at all
    assert ep.shared_segments(b) == {} and ep.cross_overlay_refs(b, SOURCES) == []
