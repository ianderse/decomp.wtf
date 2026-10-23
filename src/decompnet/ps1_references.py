"""Read-only PS1 examples. Recorded local matches and external hypotheses stay distinct.

External indexes contain metadata and paths, never executable commands. Upstream files
are read only after their pinned SHA-256 is checked; no network access occurs here.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import similarity, toolchains
from .project import with_toolchain

MAX_EXAMPLES = 5
MAX_FILE_BYTES = 256 * 1024


def _digest(data):
    return hashlib.sha256(data.encode() if isinstance(data, str) else data).hexdigest()


def _tokens(text):
    return list(dict.fromkeys(re.findall(r"[a-zA-Z_][a-zA-Z_0-9]*", text.lower())))[:8]


def _excerpt(text, terms, lines=36, chars=3000):
    rows = text.splitlines()
    hit = next((i for i, line in enumerate(rows) if any(t in line.lower() for t in terms)), 0)
    start = max(0, hit - 3)
    out = "\n".join(rows[start:start + lines])[:chars]
    return ("/* excerpt */\n" if start else "") + out + ("\n/* ... */" if start + lines < len(rows) or len(out) == chars else "")


def _recipe(net, fn, proj):
    configured = json.loads(proj['config']).get('compiler', {})
    spec = net.fn_toolchain(fn)
    recipe = with_toolchain(configured, spec) if spec and configured.get('toolchain') else dict(configured)
    tid = recipe.get('toolchain')
    flags = recipe.get('flags')
    if flags is None and tid:
        try:
            flags = toolchains.get(tid).flags
        except (KeyError, ValueError, toolchains.ToolchainError):
            flags = None
    return {'toolchain': spec or tid, 'flags': flags,
            'assembler': recipe.get('assembler', 'toolchain template' if tid else 'custom command'),
            'aspsx': recipe.get('aspsx', '2.77' if tid and '-psx' in tid and recipe.get('assembler') != 'gnu' else None),
            'command': recipe.get('cmd'), 'provenance': 'current stored recipe; historical executable hashes unavailable'}


def _default_index(net):
    workspace = getattr(net, 'workspace', None)
    if workspace:
        path = Path(workspace) / '.decomp' / 'ps1-reference-index.json'
        if path.is_file():
            return path
    # A plain Network also knows its database directory (normally workspace/.decomp).
    db = str(getattr(net.store, 'path', ':memory:'))
    if db != ':memory:' and not db.startswith('file:'):
        path = Path(db).resolve().parent / 'ps1-reference-index.json'
        if path.is_file():
            return path
    return None


def _external(index_path, terms):
    if index_path is None:
        return []
    path = Path(index_path)
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        return []
    try:
        index = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        return []
    entries = index.get('examples', []) if isinstance(index, dict) else []
    if not isinstance(entries, list):
        return []
    out = []
    for item in entries[:500]:
        if not isinstance(item, dict):
            continue
        required = ('repository', 'commit', 'path', 'sha256', 'license', 'symbol')
        if not all(isinstance(item.get(k), str) and item[k] for k in required):
            continue
        if not re.fullmatch(r'[a-fA-F0-9]{40}', item['commit']) or not re.fullmatch(r'[a-fA-F0-9]{64}', item['sha256']):
            continue
        haystack = ' '.join(str(item.get(k, '')) for k in ('symbol', 'path', 'tags', 'description')).lower()
        rank = sum(t in haystack for t in terms)
        if terms and not rank:
            continue
        # root is operator configured. Refuse path traversal out of that root.
        if not isinstance(index.get('root', str(path.parent)), str):
            continue
        root = Path(index.get('root', path.parent)).expanduser()
        if not root.is_absolute():
            root = path.parent / root
        source_path = (root / item['path']).resolve()
        if not source_path.is_relative_to(root.resolve()):
            continue
        try:
            if not source_path.is_file() or source_path.stat().st_size > MAX_FILE_BYTES:
                continue
            raw = source_path.read_bytes()
            if _digest(raw) != item['sha256']:
                continue
            source = raw.decode('utf-8')
        except (OSError, UnicodeError):
            continue
        definition = re.search(r'(?m)^\w[^;{}\n]*\b' + re.escape(item['symbol']) + r'\s*\([^;{}]*\)\s*\{', source)
        if definition:
            source = source[definition.start():]
        out.append({'kind': 'external_candidate', 'verification': 'UNVERIFIED FOR TARGET; pinned upstream source only',
                    'name': item['symbol'], 'repository': item['repository'], 'commit': item['commit'],
                    'path': item['path'], 'license': item['license'], 'sha256': item['sha256'],
                    'compiler': item.get('compiler', 'not recorded'),
                    'source': _excerpt(source, [item['symbol'].lower()]),
                    'assembly': None, 'rank': rank})
    return out


def search_examples(net, query='', project=None, function=None, limit=3, external_index=None):
    """Return up to five compact examples without changing claims, sources or scores.

    `function` ranks nearby matched functions by unit and size. `query` searches
    names, C and target assembly; an explicit query never falls back to unrelated C.
    The status is a historical coordinator record, not a fresh compiler replay.
    """
    limit = max(1, min(MAX_EXAMPLES, int(limit)))
    fn = net._function(function, project) if function else None
    proj = (net.store.one('SELECT * FROM projects WHERE id=?', fn['project_id']) if fn else
            net._project(project) if project else None)
    terms = _tokens(query)
    where = ["f.status='matched'", "COALESCE(f.matched_source, '') != ''", "COALESCE(f.asm, '') != ''",
             "(p.config LIKE '%psx%' OR p.config LIKE '%PS1%')"]
    args = []
    if proj:
        where.append('f.project_id=?')
        args.append(proj['id'])
    if fn:
        where.append('f.id != ?')
        args.append(fn['id'])
    if terms:
        # instr treats percent/underscore literally, unlike SQL LIKE patterns.
        where.append('(' + ' OR '.join('instr(lower(f.name || char(10) || f.unit || char(10) || '
                                      'f.matched_source || char(10) || f.asm), ?) > 0' for _ in terms) + ')')
        args.extend(terms)
    ordering = 'f.n_insns, f.id'
    if fn:
        ordering = '(f.unit=?) DESC, abs(f.n_insns-?), f.id'
        args.extend((fn['unit'], fn['n_insns'] or 0))
    candidates = net.store.all('SELECT f.*, p.slug FROM functions f JOIN projects p ON p.id=f.project_id WHERE '
                               + ' AND '.join(where) + ' ORDER BY ' + ordering + ' LIMIT 100', *args)
    patterns = {}
    wanted_recipe = _recipe(net, fn, proj) if fn else None
    if fn and fn['target']:
        patterns = dict(similarity.rank(
            similarity.mips_mnemonics(fn['target'], bool(proj['big_endian'])),
            {str(r['id']): similarity.mips_mnemonics(r['target'], bool(proj['big_endian']))
             for r in candidates if r['target']}, top=100, floor=0.4))
    examples = []
    for row in candidates:
        p = proj or net.store.one('SELECT * FROM projects WHERE id=?', row['project_id'])
        text = (row['name'] + '\n' + row['unit'] + '\n' + row['matched_source'] + '\n' + row['asm']).lower()
        rank = sum((6 if t in row['name'].lower() else 1) for t in terms if t in text)
        recipe = _recipe(net, row, p)
        same_recipe = recipe == wanted_recipe if fn else None
        pattern = patterns.get(str(row['id']), 0.0) if fn else None
        if fn:
            rank += 10 * same_recipe + 8 * pattern + 2 * (fn['unit'] == row['unit'])
            rank += 1 / (1 + abs((fn['n_insns'] or 0) - (row['n_insns'] or 0)))
        attempt = net.store.one('SELECT id,context_version,created_at FROM attempts WHERE function_id=? '
                                'AND score=0 AND compiled=1 AND source=? ORDER BY id DESC LIMIT 1',
                                row['id'], row['matched_source'])
        context = (net.store.one('SELECT version,header FROM context_versions WHERE project_id=? AND version=? '
                                 "AND unit IN (?, '') ORDER BY unit=? DESC LIMIT 1",
                                 row['project_id'], attempt['context_version'], row['unit'], row['unit']) if attempt else None)
        examples.append({'kind': 'local_recorded_match', 'verification': 'recorded byte match; not recompiled now',
                         'name': row['name'], 'project': row['slug'], 'unit': row['unit'],
                         'instructions': row['n_insns'], 'compiler': recipe,
                         'same_recipe': same_recipe, 'mnemonic_similarity': pattern,
                         'matched_at': row['matched_at'], 'attempt_id': attempt['id'] if attempt else None,
                         'context_version': context['version'] if context else None,
                         'context_sha256': _digest(context['header']) if context else None,
                         'provenance_note': None if attempt else 'matching attempt unavailable for current stored source',
                         'source_sha256': _digest(row['matched_source']),
                         'target_sha256': _digest(row['target']) if row['target'] is not None else None,
                         'source': _excerpt(row['matched_source'], terms),
                         'assembly': _excerpt(row['asm'], terms, lines=28, chars=2300), 'rank': rank})
    # Local recorded matches precede external suggestions even when lexical ranks differ.
    examples.sort(key=lambda e: (-e['rank'], e['name']))
    external = _external(external_index or _default_index(net), terms or _tokens(function or ''))
    external.sort(key=lambda e: (-e['rank'], e['name']))
    if external and examples and limit > 1:
        return examples[:limit - 1] + external[:1]
    if external and limit == 1 and terms and external[0]['name'].lower() == query.lower().strip():
        return external[:1]
    return (examples + external)[:limit]


def search(net, query='', project=None, function=None, limit=3, external_index=None):
    """Agent-facing rendering of search_examples()."""
    examples = search_examples(net, query, project, function, limit, external_index)
    if not examples:
        return 'No recorded PS1 match or hash-verified indexed external candidate found. Try a symbol, unit or assembly mnemonic.'
    out = ['PS1 references: local matches are historical records; external sources are hypotheses for this target.']
    for ex in examples:
        out += [f"\n{ex['name']} — {ex['verification']}"]
        if ex['kind'] == 'local_recorded_match':
            out += [f"{ex['project']}/{ex['unit']}; {ex['instructions']} instructions; attempt={ex['attempt_id']}; "
                    f"context={ex['context_version']}; source sha256={ex['source_sha256']}",
                    'Compiler: ' + json.dumps(ex['compiler'], sort_keys=True)]
            if ex['same_recipe'] is not None:
                out.append(f"Same current compiler recipe: {ex['same_recipe']}; mnemonic similarity: {ex['mnemonic_similarity']:.3f}")
            if ex['provenance_note']:
                out.append(ex['provenance_note'])
        else:
            out += [f"{ex['repository']} @ {ex['commit']} / {ex['path']}; license={ex['license']}; sha256={ex['sha256']}",
                    'Compiler: ' + json.dumps(ex['compiler'], sort_keys=True)]
        out += ['```c', ex['source'], '```']
        if ex['assembly']:
            out += ['Target assembly:', '```asm', ex['assembly'], '```']
    return '\n'.join(out)
