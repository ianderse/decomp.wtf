import hashlib
import json
import subprocess
from types import SimpleNamespace

import pytest

from decompnet import ps1_diagnostics as ps1
from decompnet.compiler import CompilerConfig
from decompnet.network import Network


def codes(report):
    return [w['code'] for w in report['warnings']]


def test_exact_declaration_and_matched_callee_arity_evidence():
    header = 'extern void f();\nextern int unrelated(int);'
    source = 'void caller(void) { f(); }'
    report = ps1.audit_call_boundaries(header, source, matched_callees={'f': 'void f(void *arg0) { }'})
    assert 'call_arity_disagreement' in codes(report)
    assert 'unspecified_parameters' in codes(report)
    warning = next(w for w in report['warnings'] if w['code'] == 'call_arity_disagreement')
    assert warning['evidence_origin'] == 'matched callee source'
    assert warning['declaration'] == 'void f(void *arg0) {'
    assert report['matched_comparisons'][0]['visible'] == ['extern void f();']
    assert not any(d['name'] == 'unrelated' for d in report['declarations'])
    assert 'No signature, equivalence, or ABI acceptance proof' in report['scope']


def test_native_port_dropped_argument_regression():
    # Reduced form of the func_80104228 -> func_800F5120 boundary documented in
    # commit 93e6bbf; matched bytes do not establish a valid C/host ABI signature.
    report = ps1.audit_call_boundaries('extern void func_800F5120();',
                                      'void func_80104228(void) { func_800F5120(); }',
                                      matched_callees={'func_800F5120': 'void func_800F5120(void *arg0) { }'})
    assert any(w['callee'] == 'func_800F5120' and w['code'] == 'call_arity_disagreement'
               for w in report['warnings'])


def test_lexical_calls_handle_strings_comments_and_nested_arguments():
    source = '''void caller(void) {
       /* fake(1, 2); */
       f("a,b)", nested(1, 2), (x, y));
       f("", nested(1, 2), 3); // fake()
    }'''
    header = 'extern void f(char *, int, int);\nextern int nested(int, int);'
    report = ps1.audit_call_boundaries(header, source)
    assert not report['warnings']
    assert [(c['name'], c['arity']) for c in report['calls']] == [('f', 3), ('nested', 2), ('f', 3), ('nested', 2)]


def test_void_unspecified_and_varargs_are_distinct():
    source = 'void caller(void) { unspecified(1,2); zero(1); variable(1,2,3); variable(); }'
    report = ps1.audit_call_boundaries('void unspecified(); void zero(void); int variable(int,...);', source)
    disagreements = [w for w in report['warnings'] if w['code'] == 'call_arity_disagreement']
    assert [(w['callee'], w['call']) for w in disagreements] == [('zero', 'zero(1)'), ('variable', 'variable()')]
    assert any(w['callee'] == 'unspecified' and w['code'] == 'unspecified_parameters' for w in report['warnings'])


def test_assembly_argument_setup_includes_delay_slot_without_claiming_arity():
    assembly = '''glabel caller
/* 0 80000000 00000000 */ addiu $a1, $zero, 1
/* 4 80000004 00000000 */ sw $v0, 0x10($sp)
/* 8 80000008 00000000 */ jal f
/* C 8000000C 00000000 */ addu $a0, $s0, $zero
'''
    report = ps1.audit_call_boundaries('void f();', 'void caller(void) { f(); }', assembly)
    warning = next(w for w in report['warnings'] if w['code'] == 'empty_call_with_argument_setup')
    assert warning['confidence'] == 'heuristic'
    evidence = warning['assembly'][0]
    assert evidence['argument_register_writes'] == ['addiu $a1, $zero, 1', 'addu $a0, $s0, $zero']
    assert evidence['possible_stack_argument_stores'] == ['sw $v0, 0x10($sp)']
    assert 'do not prove arity' in warning['message']


def test_assembly_window_stops_at_control_flow_and_return_spelling_is_heuristic():
    report = ps1.audit_call_boundaries('unsigned int f(void);', 'void caller(void) { f(); }',
                                      'addiu $a0,$zero,1\nbeq $s0,$s1,label\nnop\nlabel:\njal f\nnop',
                                      {'f': 'u32 f(void) { return 0; }'})
    assert report['assembly_calls'][0]['argument_register_writes'] == []
    assert 'empty_call_with_argument_setup' not in codes(report)
    warning = next(w for w in report['warnings'] if w['code'] == 'return_type_spelling_difference')
    assert warning['confidence'] == 'heuristic' and 'resolve typedefs' in warning['message']


def test_fingerprint_covers_compiler_helpers_and_assembler_package(tmp_path):
    tc, asm = tmp_path / 'compiler', tmp_path / 'maspsx'
    tc.mkdir(); asm.mkdir()
    (tc / 'gcc').write_bytes(b'driver')
    (tc / 'cc1').write_bytes(b'backend')
    (asm / 'maspsx.py').write_text('import helper')
    (asm / 'helper.py').write_text('original')
    cfg = CompilerConfig([str(tc / 'gcc')], ro_paths=(str(tc), str(asm)), sandbox='none')
    before = ps1.tool_fingerprints(cfg)
    assert before['files'][str(tc / 'cc1')]['sha256'] == hashlib.sha256(b'backend').hexdigest()
    (asm / 'helper.py').write_text('changed')
    after = ps1.tool_fingerprints(cfg)
    assert before['files'][str(asm / 'helper.py')] != after['files'][str(asm / 'helper.py')]


def test_microprobes_are_fixed_bounded_and_keep_real_runner_outcomes(tmp_path, monkeypatch):
    calls = []
    def runner(cfg, work, argv, **kwargs):
        calls.append((cfg, work, argv, kwargs))
        assert (work / 'input.c').is_file() and (work / 'include/context.h').read_text() == ''
        if work.name == 'stack_plain':
            raise subprocess.TimeoutExpired(argv, 1)
        if work.name == 'stack_reserve':
            return SimpleNamespace(returncode=1, stdout='', stderr='compiler error')
        (work / 'output.o').write_bytes(b'object')
        (work / 'output.o.s').write_text('.frame $sp,48,$31\n' + work.name + '\nmfhi $8\n')
        (work / 'output.o.ms').write_text('assembled\n' + work.name + '\n')
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    monkeypatch.setattr(ps1.compiler, 'run_in', runner)
    from decompnet.elfobj import FunctionObj
    monkeypatch.setattr('decompnet.elfobj.extract_function', lambda obj, name:
                        FunctionObj(name, 'mips', False, bytes.fromhex('0800e00300000000')))
    cfg = CompilerConfig(['compiler', '{src}', '-o', '{obj}', '-I{include}'], sandbox='none')
    out = tmp_path / 'artifacts'
    report = ps1.run_microprobes(out, {'test': cfg})
    assert len(calls) == 4
    assert [r['state'] for r in report['results']] == ['timeout', 'failed', 'ok', 'ok']
    assert json.loads((out / 'manifest.json').read_text())['requested_compiles'] == 4
    assert len(report['differences']) == 6
    assert any(r['state'] == 'unavailable' for r in report['differences'])
    assert any(r['state'] == 'different' and (out / r['diff']).read_text() for r in report['differences'])
    assert report['results'][2]['final_code']['state'] == 'ok'
    assert (out / 'test/grid_plain/probe.bin').read_bytes() == bytes.fromhex('0800e00300000000')
    assert 'jr $ra' in (out / 'test/grid_plain/probe.disasm').read_text()
    assert next(r for r in report['differences'] if r['stage'] == 'disasm' and r['state'] == 'same')['code_and_relocations_equal']
    assert 'no original target' in report['scope']
    with pytest.raises(FileExistsError):
        ps1.run_microprobes(out, {'test': cfg})
    assert len(calls) == 4  # an interrupted/existing artifact tree is never silently rerun
    ps1.analyze_microprobes(out)
    assert len(calls) == 4  # derived final-code inspection reuses immutable compile artifacts


@pytest.mark.parametrize('lanes', [{}, {'../escape': CompilerConfig(['unused'])}])
def test_microprobes_reject_invalid_bounds_before_creating_artifacts(tmp_path, lanes):
    with pytest.raises(ValueError):
        ps1.run_microprobes(tmp_path / 'out', lanes)
    assert not (tmp_path / 'out').exists()


def test_network_wrapper_is_read_only_and_uses_matched_source(tmp_path):
    net = Network(tmp_path / 'test.db')
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(id,slug,name,arch,root,config) VALUES(1,'game','Game','mips',?,'{}')", (str(tmp_path),))
        db.execute("INSERT INTO context_versions(project_id,version,header,unit) VALUES(1,1,'void callee();','')")
        db.execute("INSERT INTO functions(project_id,name,status,matched_source) VALUES(1,'callee','matched',?)",
                   ('void callee(int arg) { }',))
        db.execute("INSERT INTO functions(project_id,name,status,matched_source,best_source) "
                   "VALUES(1,'caller','matched',?,'ignored')", ('void caller(void) { callee(); }',))
    changes = net.store.conn.total_changes
    report = json.loads(ps1.audit_function(net, 'caller', 'game'))
    assert report['source_kind'] == 'matched' and report['context_version'] == 1
    assert 'call_arity_disagreement' in codes(report)
    assert net.store.conn.total_changes == changes
    assert net.store.one('SELECT COUNT(*) n FROM attempts')['n'] == 0


def test_network_wrapper_bounds_have_explicit_omission_counts():
    names = [f'callee{i}' for i in range(45)]
    net = SimpleNamespace(
        _function=lambda *args: {'project_id': 1, 'unit': '', 'name': 'caller',
                                'matched_source': None, 'best_source': 'void caller(void) {' +
                                ''.join(f'{n}();' for n in names) + '}', 'seed': None, 'asm': ''},
        _context=lambda *args: {'version': 1, 'header': ''.join(f'void {n}();' for n in names)},
        store=SimpleNamespace(all=lambda *args: [{'name': n, 'matched_source': f'void {n}(int value) {{ }}'} for n in names]))
    report = json.loads(ps1.audit_function(net, 'caller'))
    assert report['counts']['warnings'] == 90 and len(report['warnings']) == 40
    assert report['omitted']['warnings'] == 50
    assert report['counts']['callees'] == 45 and len(report['callees']) == 30
    assert report['omitted']['callees'] == 15
    assert report['omitted']['declarations'] == 15
    assert report['omitted']['matched_comparisons'] == 15


def test_saved_successful_compiler_exit_can_have_failed_object_inspection(tmp_path):
    out = tmp_path / 'saved'
    work = out / 'lane/grid_plain'
    work.mkdir(parents=True)
    (work / 'output.o').write_bytes(b'not an ELF')
    (out / 'manifest.json').write_text(json.dumps({'lanes': {'lane': {}}, 'sources': ps1.microcases(), 'scope': 'no target'}))
    (out / 'results.json').write_text(json.dumps([{'lane': 'lane', 'case': 'grid_plain', 'state': 'ok'}]))
    report = ps1.analyze_microprobes(out)
    assert report['results'][0]['state'] == 'ok'
    assert report['results'][0]['final_code']['state'] == 'inspection_failed'
