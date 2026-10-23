#!/usr/bin/env python3
"""Frozen five-function source-assisted libsnd experiment; never opens live DB writable.

Run from repo with .venv/bin/python docs/plans/ps1-sdk-pilot.py --source-db ...
--output NEW_DIRECTORY [--references-dir EXISTING_FETCH_DIRECTORY]. Source metadata
is ../ps1-references/sources.json. Output is append-only: existing output is refused.
No agent, claim, source, score, symbol or compiler setting is adopted into the live DB.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
import urllib.request

from decompnet import diagnosis, scoring
from decompnet.app.engine import RomNetwork

CATALOG = Path(__file__).resolve().parents[1] / 'ps1-references' / 'sources.json'
PROTOTYPES = '''extern SeqStruct *D_801FAD50[];
s32 func_8016D768(s16, s16);
void func_8016BE84(s16, s16, s32, s32);
void func_8016BFEC(s16, s16, u8);
void func_8016BF80(s16, s16, u8);
void func_8016D370(s16, s16);
void func_8016D410(s16, s16, u8);
void func_8016F2BC(s32, s16, u8, u8);
'''


def sha(raw):
    return hashlib.sha256(raw.encode() if isinstance(raw, str) else raw).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def fetch_references(catalog, root):
    """Fetch only pinned libsnd C/header files and provenance; preserve raw bytes."""
    jobs = []
    for repo in catalog['repositories']:
        name, commit = repo['repository'], repo['commit']
        url = f'https://api.github.com/repos/{name}/git/trees/{commit}?recursive=1'
        tree = json.load(urllib.request.urlopen(url, timeout=60))
        folder = root / 'upstream' / name
        folder.mkdir(parents=True, exist_ok=True)
        save(folder / 'tree.json', tree)
        for ent in tree['tree']:
            p = ent['path']
            if ent['type'] == 'blob' and (p.startswith(repo['source_prefix']) or p in repo['evidence_paths']):
                jobs.append((name, commit, p, folder))

    def fetch(job):
        repo, commit, p, folder = job
        raw = urllib.request.urlopen(f'https://raw.githubusercontent.com/{repo}/{commit}/{p}', timeout=60).read()
        out = folder / p
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(raw)
        return {'repository': repo, 'commit': commit, 'path': p, 'sha256': sha(raw), 'bytes': len(raw)}

    with ThreadPoolExecutor(max_workers=8) as pool:
        entries = list(pool.map(fetch, jobs))
    save(root / 'upstream-manifest.json', entries)
    return entries


def extract_function(source, symbol):
    pattern = rf'(?m)^(?:void|s32)\s+{re.escape(symbol)}\([^;]*?\)\s*\{{'
    match = re.search(pattern, source)
    if not match:
        raise ValueError(f'{symbol}: recovered C unavailable (may be INCLUDE_ASM)')
    depth = 1
    end = match.end()
    while end < len(source) and depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    if depth:
        raise ValueError(f'{symbol}: unbalanced source')
    return source[match.start():end] + '\n'


def build_index(catalog, manifest, refs):
    """Metadata only; verify every local source against the pinned fetch manifest."""
    repos = {r['repository']: r for r in catalog['repositories']}
    examples = []
    for record in manifest:
        if not record['path'].endswith('.c'):
            continue
        repo = repos[record['repository']]
        if record['commit'] != repo['commit']:
            raise ValueError('index source commit differs from frozen catalog')
        root = (refs / 'upstream' / repo['repository']).resolve()
        source_path = (root / record['path']).resolve()
        if not source_path.is_relative_to(root):
            raise ValueError('source manifest escapes repository root')
        raw = source_path.read_bytes()
        if sha(raw) != record['sha256']:
            raise ValueError(f"source hash changed: {record['path']}")
        source = raw.decode('utf-8')
        if repo['repository'] == 'Xeeynamo/sotn-decomp' and not source.startswith('// SPDX-License-Identifier: MIT'):
            continue
        symbols = re.findall(r'(?m)^\w[^;{}\n]*\b([A-Za-z_]\w*)\s*\([^;{}]*\)\s*\{', source)
        for symbol in symbols:
            aliases = [x['target'] for x in catalog['pilot']['functions'] if x['symbol'] == symbol]
            examples.append({'repository': 'https://github.com/' + repo['repository'], 'commit': record['commit'],
                             'path': repo['repository'] + '/' + record['path'], 'sha256': record['sha256'],
                             'license': repo['license'], 'symbol': symbol, 'tags': ['libsnd', *aliases],
                             'description': 'Pinned upstream C; target mapping is a hypothesis. No target byte-match claim.',
                             'compiler': {'upstream_evidence': repo['compiler_evidence'],
                                          'target_status': 'unverified; see five-function pilot results separately'}})
    return {'schema': 1, 'root': str((refs / 'upstream').resolve()), 'examples': examples,
            'catalog_sha256': sha(json.dumps(catalog, sort_keys=True))}


def write_new_index(path, index):
    """Do not overwrite operator configuration; an existing index needs an explicit merge."""
    encoded = json.dumps(index, indent=2) + '\n'
    if len(encoded.encode()) > 256 * 1024:
        raise ValueError('index exceeds retrieval reader size bound')
    with Path(path).open('x', encoding='utf-8') as output:
        output.write(encoded)


def evaluate(net, proj, fn, header, source, folder, label):
    (folder / f'{label}.c').write_text(source)
    start = time.monotonic()
    try:
        result = net._compile(proj, header, source, fn)
        (folder / f'{label}.compiler.txt').write_text(result.log)
        out = {'compiled': result.ok, 'source_sha256': sha(source)}
        if result.ok:
            (folder / f'{label}.o').write_bytes(result.obj)
            candidate, exact, problems = net._evaluate(fn, proj, result.obj)
            out['link_problems'] = problems
            if candidate is not None:
                diff = scoring.diff(net._target_obj(fn, proj), candidate)
                out.update(score=diff.score if exact is None else 0 if exact else max(1, diff.score),
                           exact_byte_match=bool(exact if exact is not None else diff.bytes_match),
                           candidate_instructions=len(candidate.code) // 4)
                (folder / f'{label}.asm').write_text(scoring.asm_text(candidate))
                (folder / f'{label}.diff.txt').write_text(diff.render())
    except Exception as error:
        out = {'compiled': False, 'error': str(error), 'source_sha256': sha(source)}
        (folder / f'{label}.error.txt').write_text(str(error))
    out['seconds'] = time.monotonic() - start
    return out


def run(source_db, output, references=None, index_output=None):
    output = Path(output).resolve()
    source_db = Path(source_db).resolve()
    # Refusing reuse keeps failed candidates, controls and the denominator frozen.
    output.mkdir(parents=True, exist_ok=False)
    catalog = json.loads(CATALOG.read_text())
    save(output / 'sources.json', catalog)
    refs = Path(references).resolve() if references else output
    manifest = (json.loads((refs / 'upstream-manifest.json').read_text()) if references else
                fetch_references(catalog, refs))
    save(output / 'upstream-manifest.json', manifest)
    selected = catalog['pilot']['functions']
    if not 5 <= len(selected) <= 10:
        raise ValueError('pilot budget must remain between five and ten functions')
    repo = catalog['pilot']['source_repository']
    source_path = catalog['pilot']['source_path']
    entry = next(r for r in manifest if r['repository'] == repo and r['path'] == source_path)
    pin = next(r for r in catalog['repositories'] if r['repository'] == repo)
    if entry['commit'] != pin['commit']:
        raise ValueError('reference commit does not match frozen catalog')
    raw = (refs / 'upstream' / repo / source_path).read_bytes()
    if sha(raw) != entry['sha256'] or not raw.startswith(b'// SPDX-License-Identifier: MIT'):
        raise ValueError('reference bytes or selected file license changed')
    snapshot = output / 'snapshot.db'
    with sqlite3.connect(source_db.as_uri() + '?mode=ro', uri=True) as live:
        live.execute('PRAGMA query_only=ON')
        with sqlite3.connect(snapshot) as copy:
            live.backup(copy)
    snapshot_hash = sha(snapshot.read_bytes())
    net = RomNetwork(snapshot, output)
    proj = net._project('slus-01252')
    start_counts = net.store.one('SELECT COUNT(*) n, SUM(attempts) attempts FROM functions')
    results = []
    save(output / 'plan.json', {'source_db': str(source_db), 'read_only_source': True,
                               'snapshot_sha256_before_store_init': snapshot_hash, 'selected': selected,
                               'budget': catalog['pilot']['budget'], 'reference': entry})
    try:
        for item in selected:
            fn = net._function(item['target'], proj['slug'])
            folder = output / fn['name']
            folder.mkdir()
            ctx = net._context(fn['project_id'], fn['unit'])
            header = ctx['header']
            (folder / 'context.h').write_text(header)
            (folder / 'target.asm').write_text(fn['asm'])
            env, details = diagnosis.environment(net, fn)
            save(folder / 'environment.json', {'environment': env, **details})
            record = {'function': fn['name'], 'symbol_hypothesis': item['symbol'], 'mapping_evidence': item['evidence'],
                      'status_at_snapshot': fn['status'], 'historical_best_score': fn['best_score'],
                      'target_sha256': sha(fn['target']), 'context_version': ctx['version'],
                      'function_toolchain': net.fn_toolchain(fn), 'reference': entry}
            # One incumbent control and one source-assisted candidate, no automatic alternatives.
            record['incumbent'] = evaluate(net, proj, fn, header, fn['best_source'] or '', folder, 'incumbent')
            source = extract_function(raw.decode(), item['symbol'])
            (folder / 'upstream-function.c').write_text('// SPDX-License-Identifier: MIT\n' + source)
            names = catalog['pilot']['identifier_map']
            source = re.sub(r'\b[A-Za-z_]\w*\b', lambda m: names.get(m[0], m[0]), source)
            record['candidate'] = evaluate(net, proj, fn, header,
                '// SPDX-License-Identifier: MIT\n/* Adapted from pinned source; see environment and sources.json. */\n' + PROTOTYPES + source,
                folder, 'candidate')
            record['improved'] = (record['candidate'].get('score') is not None and
                                  record['incumbent'].get('score') is not None and
                                  record['candidate']['score'] < record['incumbent']['score'])
            results.append(record)
            save(folder / 'result.json', record)
            print(fn['name'], 'control', record['incumbent'].get('score'),
                  'candidate', record['candidate'].get('score'), flush=True)
        end_counts = net.store.one('SELECT COUNT(*) n, SUM(attempts) attempts FROM functions')
        if tuple(start_counts) != tuple(end_counts):
            raise RuntimeError('unexpected attempt/function mutation even on copy')
        report = {'functions': len(results), 'candidate_budget': len(selected),
                  'candidate_compiled': sum(r['candidate']['compiled'] for r in results),
                  'candidate_exact_matches': sum(r['candidate'].get('exact_byte_match', False) for r in results),
                  'improved': sum(r['improved'] for r in results), 'results': results,
                  'live_mutations': False, 'adopted_candidates': 0,
                  'next_boundary': 'Review evidence and exact-match/replay candidates before any live adoption; no source tuning in this pilot.'}
        index = build_index(catalog, manifest, refs)
        write_new_index(output / 'ps1-reference-index.json', index)
        report['indexed_examples'] = len(index['examples'])
        save(output / 'report.json', report)
        if index_output:
            write_new_index(index_output, index)
        return report
    finally:
        net._pool.shutdown()
        net.store.conn.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-db', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--references-dir', type=Path)
    parser.add_argument('--index-output', type=Path, help='Optional NEW index path; existing operator configuration is refused')
    args = parser.parse_args()
    report = run(args.source_db, args.output, args.references_dir, args.index_output)
    print(json.dumps({k: v for k, v in report.items() if k != 'results'}, indent=2))
