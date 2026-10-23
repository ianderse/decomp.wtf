import hashlib
import json
from pathlib import Path
import runpy

import pytest

from decompnet.network import Network
from decompnet.ps1_references import search, search_examples


@pytest.fixture
def net(tmp_path):
    net = Network(tmp_path / 'decomp.db')
    cfg = {'compiler': {'toolchain': 'gcc-2.7.2-psx', 'flags': ['-O2', '-G0']}}
    with net.store.tx() as db:
        db.execute('INSERT INTO projects(id,slug,root,config,arch) VALUES(1,?,?,?,?)',
                   ('ps1', str(tmp_path), json.dumps(cfg), 'mips'))
        db.execute('INSERT INTO context_versions(project_id,version,header) VALUES(1,2,?)', ('typedef int s32;',))
        for fid, name, status, source, unit in [
            (1, '_SsOpen', 'open', 'int _SsOpen(void) { return 0; }', 'libsnd'),
            (2, '_SsRead', 'matched', 'int _SsRead(void) { return 1; }', 'libsnd'),
            (3, 'clamp', 'matched', 'int clamp(int v) { return v < 0 ? 0 : v; }', 'game'),
        ]:
            db.execute('INSERT INTO functions(id,project_id,name,status,matched_source,asm,n_insns,unit,target,meta) '
                       'VALUES(?,1,?,?,?,?,?,?,?,?)',
                       (fid, name, status, source, f'{name}:\n jr $ra\n addiu $v0,$zero,1',
                        10 + fid, unit, bytes([fid]), json.dumps({'toolchain': 'gcc-2.7.2-psx@2.34'})))
        db.execute('INSERT INTO attempts(id,function_id,score,compiled,context_version,source) '
                   'SELECT 10,2,0,1,2,matched_source FROM functions WHERE id=2')
    yield net
    net.store.conn.close()
    net._pool.shutdown()


def test_local_search_excludes_unmatched_and_retains_provenance(net):
    result = search_examples(net, '_Ss', project='ps1')
    assert [x['name'] for x in result] == ['_SsRead']
    row = result[0]
    assert row['attempt_id'] == 10 and row['context_version'] == 2
    assert row['compiler']['aspsx'] == '2.34'
    assert row['compiler']['flags'] == ['-O2', '-G0']
    assert row['context_sha256'] == hashlib.sha256(b'typedef int s32;').hexdigest()
    assert 'not recompiled now' in row['verification']
    assert '_SsRead' in row['source'] and 'jr $ra' in row['assembly']
    assert net.store.one('SELECT COUNT(*) n FROM attempts')['n'] == 1
    assert net.store.one('SELECT COUNT(*) n FROM claims')['n'] == 0


def test_function_ranking_and_literal_queries(net):
    assert search_examples(net, function='_SsOpen')[0]['name'] == '_SsRead'
    assert search_examples(net, "' OR 1=1 --") == []
    assert 'No recorded PS1 match' in search(net, 'missing')
    assert len(search_examples(net, limit=100)) == 2


def write_index(tmp_path, **changes):
    source = tmp_path / 'sdk.c'
    source.write_text('void sdk_reference(void) {}')
    entry = {'repository': 'https://github.com/example/sdk', 'commit': 'a' * 40,
             'path': 'sdk.c', 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
             'license': 'MIT', 'symbol': 'sdk_reference', **changes}
    path = tmp_path / 'ps1-reference-index.json'
    path.write_text(json.dumps({'root': str(tmp_path), 'examples': [entry]}))
    return path


def test_external_source_requires_pinned_hash_license_and_commit(net, tmp_path):
    path = write_index(tmp_path)
    result = search_examples(net, 'sdk_reference', external_index=path)
    assert len(result) == 1 and result[0]['kind'] == 'external_candidate'
    assert 'UNVERIFIED FOR TARGET' in search(net, 'sdk_reference', external_index=path)
    (tmp_path / 'sdk.c').write_text('changed')
    assert search_examples(net, 'sdk_reference', external_index=path) == []
    for changes in ({'license': ''}, {'commit': 'main'}, {'sha256': 'bad'}, {'path': '../sdk.c'}):
        assert search_examples(net, 'sdk_reference', external_index=write_index(tmp_path, **changes)) == []


def test_default_index_and_missing_historical_attempt(net, tmp_path):
    write_index(tmp_path)
    assert search_examples(net, 'sdk_reference')[0]['kind'] == 'external_candidate'
    result = search_examples(net, 'clamp')[0]
    assert result['attempt_id'] is None
    assert 'unavailable' in result['provenance_note']


def test_non_ps1_matches_do_not_leak(net):
    with net.store.tx() as db:
        db.execute("UPDATE projects SET config=?", (json.dumps({'compiler': {'toolchain': 'gcc-2.7.2-kmc'}}),))
    assert search_examples(net) == []


def test_malformed_external_index_is_ignored(net, tmp_path):
    path = tmp_path / 'broken.json'
    for text in ('{', '[]', '{"examples": {}}'):
        path.write_text(text)
        assert search_examples(net, 'sdk_reference', external_index=path) == []


def test_same_recipe_preferred_and_external_slot_reserved(net, tmp_path):
    with net.store.tx() as db:
        db.execute("UPDATE functions SET meta=? WHERE id=3", (json.dumps({'toolchain': 'gcc-2.8.1-psx'}),))
    result = search_examples(net, function='_SsOpen')
    assert result[0]['name'] == '_SsRead' and result[0]['same_recipe'] is True
    assert result[1]['same_recipe'] is False
    path = write_index(tmp_path, symbol='int_reference', tags=['int', 'libsnd'])
    result = search_examples(net, 'int', limit=2, external_index=path)
    assert [r['kind'] for r in result] == ['local_recorded_match', 'external_candidate']


def test_workspace_index_and_invalid_root(net, tmp_path):
    folder = tmp_path / 'workspace' / '.decomp'
    folder.mkdir(parents=True)
    net.workspace = folder.parent
    path = write_index(folder)
    assert search_examples(net, 'sdk_reference')[0]['name'] == 'sdk_reference'
    data = json.loads(path.read_text())
    data['root'] = []
    path.write_text(json.dumps(data))
    assert search_examples(net, 'sdk_reference') == []


def test_external_alias_centers_definition_not_prototype(net, tmp_path):
    path = write_index(tmp_path, tags=['func_80160000'])
    raw = 'void sdk_reference(void);\n' + '/* unrelated */\n' * 50 + 'void sdk_reference(void) { return; }\n'
    (tmp_path / 'sdk.c').write_text(raw)
    data = json.loads(path.read_text())
    data['examples'][0]['sha256'] = hashlib.sha256(raw.encode()).hexdigest()
    path.write_text(json.dumps(data))
    row = search_examples(net, 'func_80160000', external_index=path)[0]
    assert row['source'].startswith('void sdk_reference(void) {')


def test_pilot_index_generation_checks_hashes_and_preserves_existing_index(tmp_path):
    module = runpy.run_path(str(Path(__file__).parents[1] / 'docs/plans/ps1-sdk-pilot.py'))
    repo, commit = 'sozud/psy-q-decomp', 'a' * 40
    folder = tmp_path / 'upstream' / repo
    folder.mkdir(parents=True)
    raw = 'void reference(void) {}\n'
    (folder / 'example.c').write_text(raw)
    catalog = {'repositories': [{'repository': repo, 'commit': commit, 'license': 'MIT',
                                'compiler_evidence': 'fixture'}], 'pilot': {'functions': []}}
    manifest = [{'repository': repo, 'commit': commit, 'path': 'example.c',
                 'sha256': hashlib.sha256(raw.encode()).hexdigest()}]
    index = module['build_index'](catalog, manifest, tmp_path)
    assert [e['symbol'] for e in index['examples']] == ['reference']
    path = tmp_path / 'index.json'
    module['write_new_index'](path, index)
    with pytest.raises(FileExistsError):
        module['write_new_index'](path, index)
    (folder / 'example.c').write_text('changed')
    with pytest.raises(ValueError, match='source hash changed'):
        module['build_index'](catalog, manifest, tmp_path)


def test_pilot_extracts_definition_and_rejects_asm_placeholder():
    module = runpy.run_path(str(Path(__file__).parents[1] / 'docs/plans/ps1-sdk-pilot.py'))
    source = 'void reference(s16);\nvoid reference(s16 value) { if (value) { return; } }\n'
    assert module['extract_function'](source, 'reference') == source.split('\n', 1)[1]
    with pytest.raises(ValueError, match='recovered C unavailable'):
        module['extract_function']('INCLUDE_ASM("path", reference);', 'reference')
