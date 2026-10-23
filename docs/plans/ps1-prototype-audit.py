"""Read-only, bounded call audit retaining header/source/assembly evidence.

Usage: .venv/bin/python docs/plans/ps1-prototype-audit.py DATABASE NEW_OUTPUT_DIR
  [--project-id 2] [--function func_80104228] (repeat --function up to 12 times)

Opens SQLite in mode=ro with query_only, in one read snapshot. Does not construct
Network/Store (which can initialize/migrate the database), compile, or rewrite C.
"""
import argparse
import json
import sqlite3
from pathlib import Path

from decompnet.ps1_diagnostics import audit_call_boundaries, _calls


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--project-id', type=int, default=2)
    parser.add_argument('--function', action='append')
    args = parser.parse_args()
    names = args.function or ['func_80104228', 'func_800E4384', 'func_800E6C90', 'func_80149820']
    if not 1 <= len(names) <= 12:
        parser.error('Select 1-12 named functions.')
    args.output.mkdir(parents=True, exist_ok=False)
    db = sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    db.execute('BEGIN')
    reports = []
    for name in names:
        fn = db.execute('SELECT * FROM functions WHERE project_id=? AND name=?', (args.project_id, name)).fetchone()
        if fn is None:
            reports.append({'function': name, 'state': 'not_found'})
            continue
        unit = fn['unit'] or ''
        ctx = db.execute('SELECT * FROM context_versions WHERE project_id=? AND unit IN (?, ?) '
                         'ORDER BY (unit=?) DESC, version DESC LIMIT 1',
                         (args.project_id, unit, '', unit)).fetchone()
        source = fn['matched_source'] or fn['best_source'] or fn['seed'] or ''
        callees = sorted({c['name'] for c in _calls(source)})
        matched = {}
        if callees:
            placeholders = ','.join('?' for _ in callees)
            rows = db.execute(f"SELECT name,matched_source FROM functions WHERE project_id=? AND status='matched' "
                              f'AND name IN ({placeholders})', (args.project_id, *callees))
            matched = {r['name']: r['matched_source'] for r in rows if r['matched_source']}
        header = ctx['header'] if ctx else ''
        report = {'function': name, 'function_id': fn['id'], 'status': fn['status'],
                  'context_version': ctx['version'] if ctx else None,
                  'source_kind': 'matched' if fn['matched_source'] else 'best' if fn['best_source'] else 'seed',
                  **audit_call_boundaries(header, source, fn['asm'] or '', matched)}
        # These are derived copies. Originals remain untouched.
        folder = args.output / str(fn['id'])
        folder.mkdir()
        for filename, value in [('context.h', header), ('input.c', source), ('target.asm', fn['asm'] or ''),
                                ('matched-callees.json', json.dumps(matched, indent=2)),
                                ('audit.json', json.dumps(report, indent=2))]:
            (folder / filename).write_text(value)
        reports.append(report)
    db.rollback()
    db.close()
    result = {'database': str(args.database.resolve()), 'project_id': args.project_id,
              'scope': 'Read-only snapshot; lexical warnings are not proof of invalid/matched C or ABI.',
              'reports': reports}
    (args.output / 'report.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps([{'function': r['function'], 'warnings': len(r.get('warnings', [])),
                       'state': r.get('state', 'audited')} for r in reports], indent=2))


if __name__ == '__main__':
    main()
