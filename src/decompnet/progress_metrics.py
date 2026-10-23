"""Read-only evidence for useful output, with an honest time denominator.

Never instantiate Store here: opening Store can migrate or repair a live database.
Scores are mismatch penalties (lower is better), not percentages or matched bytes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import time
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def measured_run(net, slug, agent_id, session, model=None, effort=None):
    """Persist run bounds around a runner; preserves interrupted/open runs as gaps.

    This writer is deliberately separate from report(), which remains read-only.
    Duration is occupied agent session time, including tools and waits.
    """
    try:
        project = net.store.one('SELECT id FROM projects WHERE slug=?', slug)
        if project is None:
            raise ValueError(f'Unknown project: {slug}')
    except Exception:
        logging.getLogger(__name__).warning('Agent run timing unavailable for %s; runner will continue', session, exc_info=True)
        yield
        return
    detail = json.dumps({'run_id': str(uuid.uuid4()), 'session': session, 'model': model, 'effort': effort})
    def event(kind):
        try:
            with net.store.tx() as db:
                db.execute('INSERT INTO events(project_id,agent_id,kind,detail,created_at) VALUES(?,?,?,?,?)',
                           (project['id'], agent_id, kind, detail, time.time()))
        except Exception:
            # Telemetry must not interrupt useful work or replace the runner's exception.
            # A missing finish leaves an open run; a missing start leaves an orphan finish.
            logging.getLogger(__name__).warning('Could not persist %s for %s; timing has a gap', kind, session, exc_info=True)
    event('agent_run_started')
    try:
        yield
    finally:
        event('agent_run_finished')


@contextmanager
def _reader(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')  # one consistent committed WAL snapshot
        yield conn
    finally:
        conn.close()


def _rows(db, sql, args=()):
    return [dict(r) for r in db.execute(sql, args)]


def _optional(db, table, pid):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return None
    return _rows(db, f'SELECT t.* FROM {table} t JOIN functions f ON f.id=t.function_id WHERE f.project_id=?', (pid,))


def _snapshot(db, project, *, history=True):
    key = 'id' if isinstance(project, int) else 'slug'
    p = db.execute(f'SELECT id,slug,name,config FROM projects WHERE {key}=?', (project,)).fetchone()
    if not p:
        raise ValueError(f'Unknown project: {project}')
    pid = p['id']
    functions = _rows(db, 'SELECT id,name,size,n_insns,status,best_score,best_source,best_agent_id,'
                      'matched_agent_id,matched_at,target,fingerprint,relocs,unit,meta,vram FROM functions WHERE project_id=?', (pid,))
    contexts = _rows(db, 'SELECT unit,version,header FROM context_versions WHERE project_id=? ORDER BY version', (pid,))
    headers = {r['unit']: r['header'] for r in contexts}
    stamp = db.execute('SELECT v FROM symbols_stamp WHERE project_id=?', (pid,)).fetchone()
    for f in functions:
        # Full target identity, not only a name or an address which may have been reused.
        f['target_hash'] = hashlib.sha256((f.pop('target') or b'') + (f['fingerprint'] or '').encode()).hexdigest()
        f['source_hash'] = hashlib.sha256((f.pop('best_source') or '').encode()).hexdigest()
        header = headers.get(f['unit'], headers.get('', ''))
        f['inputs'] = hashlib.sha256(json.dumps([p['config'], header, f['relocs'], f['meta'], f['vram'],
                                                stamp['v'] if stamp else 0], sort_keys=True).encode()).hexdigest()
    attempts = []
    if history:
        for row in db.execute('SELECT a.id,a.function_id,a.agent_id,a.score,a.compiled,a.context_version,'
                              'a.created_at,a.source FROM attempts a JOIN functions f ON f.id=a.function_id '
                              'WHERE f.project_id=? ORDER BY a.created_at,a.id', (pid,)):
            a = dict(row)
            a['source_hash'] = hashlib.sha256((a.pop('source') or '').encode()).hexdigest()
            attempts.append(a)
    events = _rows(db, 'SELECT id,kind,agent_id,detail,created_at FROM events WHERE project_id=? ORDER BY id', (pid,)) if history else []
    return dict(project=dict(p), functions=functions, attempts=attempts, events=events,
                reviews=_optional(db, 'candidate_reviews', pid), trials=_optional(db, 'hypothesis_trials', pid),
                agents={r['id']: r['name'] for r in _rows(db, 'SELECT id,name FROM agents')})


def _hours(intervals, start, end):
    """Sum the union per agent; parallel agents count separately, overlaps do not."""
    if intervals is None:
        return None, []
    by_agent, sources = defaultdict(list), set()
    for r in intervals:
        if r.get('agent_id') is None or not str(r.get('source', '')).strip():
            raise ValueError('Activity intervals require agent_id and an evidence source')
        a, b = float(r['start']), float(r['end'])
        if not math.isfinite(a) or not math.isfinite(b) or b < a:
            raise ValueError('Activity intervals require finite start <= end')
        a, b = max(start, a), min(end, b)
        if b > a:
            sources.add(r['source'])
            by_agent[r['agent_id']].append((a, b))
    seconds = 0
    for pairs in by_agent.values():
        left, right = None, None
        for a, b in sorted(pairs):
            if right is not None and a <= right:
                right = max(right, b)
            else:
                if right is not None:
                    seconds += right - left
                left, right = a, b
        if right is not None:
            seconds += right - left
    return seconds / 3600, sorted(sources)


def _run_intervals(events, start, end):
    starts, finished, intervals, gaps = {}, set(), [], []
    for e in events:
        if e['kind'] not in ('agent_run_started', 'agent_run_finished'):
            continue
        try:
            run = json.loads(e['detail'])['run_id']
        except (ValueError, TypeError, KeyError):
            if e['created_at'] is not None and start <= e['created_at'] < end:
                gaps.append({'event_id': e['id'], 'reason': 'invalid run detail'})
            continue
        if e['kind'] == 'agent_run_started':
            if run in starts:
                if e['created_at'] is not None and start <= e['created_at'] < end:
                    gaps.append({'event_id': e['id'], 'reason': 'duplicate run start'})
                continue
            starts[run] = e
        else:
            first = starts.get(run)
            if first is None or run in finished or first['agent_id'] != e['agent_id'] or e['agent_id'] is None:
                if e['created_at'] is not None and start <= e['created_at'] < end:
                    gaps.append({'event_id': e['id'], 'reason': 'unpaired or invalid run finish'})
                continue
            if first['created_at'] is None or e['created_at'] is None or e['created_at'] < first['created_at']:
                if e['created_at'] is not None and start <= e['created_at'] < end:
                    gaps.append({'event_id': e['id'], 'reason': 'invalid run timestamps'})
                continue
            finished.add(run)
            intervals.append({'agent_id': e['agent_id'], 'start': first['created_at'], 'end': e['created_at'],
                              'source': f"events:{first['id']}-{e['id']} run:{run}"})
    gaps.extend({'event_id': e['id'], 'reason': 'open run; interrupted or still running'}
                for run, e in starts.items() if run not in finished and e['created_at'] is not None
                and start <= e['created_at'] < end)
    return intervals, gaps


def report(database, project, *, baseline_database=None, since=None, until=None,
           minimum_score_drop=100, activity_intervals=None):
    """Return JSON-ready metrics without writing either database.

    ``since <= timestamp < until`` bounds activity; default is all observed history.
    Baseline comparison describes the two stored snapshots, independently of that
    activity window. An explicit baseline timestamp is not inferred from file mtime.
    Activity intervals must come from independently trustworthy session telemetry;
    a claim lease, agent last_seen, or time between submissions is insufficient.
    """
    end = time.time() if until is None else float(until)
    if minimum_score_drop <= 0 or not math.isfinite(float(minimum_score_drop)):
        raise ValueError('minimum_score_drop must be positive and finite')
    with _reader(database) as db:
        cur = _snapshot(db, project)
    baseline = None
    if baseline_database is not None:
        with _reader(baseline_database) as db:
            baseline = _snapshot(db, cur['project']['slug'], history=False)
    timestamps = [r['created_at'] for r in cur['attempts'] + cur['events'] if r['created_at'] is not None]
    start = float(since) if since is not None else min(timestamps, default=end)
    if not math.isfinite(start) or not math.isfinite(end) or start > end:
        raise ValueError('Window requires finite since <= until')
    inside = lambda r: r.get('created_at') is not None and start <= r['created_at'] < end
    attempts = [a for a in cur['attempts'] if inside(a)]
    funcs = {f['id']: f for f in cur['functions']}
    matched = [f for f in cur['functions'] if f['status'] == 'matched']
    new_matches = [f for f in matched if f['matched_at'] is not None and start <= f['matched_at'] < end]
    agent_counts = defaultdict(Counter)
    for f in new_matches:
        agent_counts[f['matched_agent_id']]['exact_functions'] += 1
        agent_counts[f['matched_agent_id']]['exact_bytes'] += f['size'] or 0
    # A zero score alone never upgrades an attempt to exact-match credit.
    best, seen_sources = {}, set()
    partials, duplicate_sources = [], 0
    for a in cur['attempts']:
        if a['created_at'] is None or a['created_at'] >= end:
            continue
        key = (a['function_id'], a['context_version'])
        source_key = (*key, a['source_hash'])
        if inside(a):
            duplicate_sources += source_key in seen_sources
            agent_counts[a['agent_id']]['attempts'] += 1
            agent_counts[a['agent_id']]['compilation_errors'] += not bool(a['compiled'])
        seen_sources.add(source_key)
        if not a['compiled'] or a['score'] is None or a['score'] < 0:
            continue
        old = best.get(key)
        if a['context_version'] is not None and old is not None and 0 < a['score'] < old and \
                old - a['score'] >= minimum_score_drop and inside(a):
            partials.append({'function': funcs[a['function_id']]['name'], 'attempt_id': a['id'],
                             'agent_id': a['agent_id'], 'context_version': a['context_version'],
                             'from_score': old, 'to_score': a['score'], 'score_drop': old - a['score']})
            agent_counts[a['agent_id']]['historical_partial_improvements'] += 1
        best[key] = min(old, a['score']) if old is not None else a['score']
    reviews = cur['reviews']
    window_reviews = [r for r in reviews or [] if inside(r)]
    attempt_by_id = {a['id']: a for a in cur['attempts']}
    regressions = [dict(review_id=r['id'], attempt_id=r['attempt_id'], function=funcs[r['function_id']]['name'],
                        original_score=attempt_by_id[r['attempt_id']]['score'], measured_score=r['measured_score'])
                   for r in window_reviews if r.get('measured_score') is not None and
                   r['attempt_id'] in attempt_by_id and attempt_by_id[r['attempt_id']]['score'] is not None and
                   r['measured_score'] > attempt_by_id[r['attempt_id']]['score']]
    latest_reviews = {}
    for r in sorted(reviews or [], key=lambda r: r['id']):
        latest_reviews[r['function_id'], r['source_hash']] = r
    for row in partials:
        a = attempt_by_id[row['attempt_id']]
        row['source_review'] = latest_reviews.get((a['function_id'], a['source_hash']), {}).get('verdict', 'unreviewed')
    suspect_incumbents = [f['name'] for f in cur['functions'] if
                          latest_reviews.get((f['id'], f['source_hash']), {}).get('verdict') == 'suspect']
    trials = cur['trials']
    duplicate_total = sum(r['duplicates'] for r in trials or [])
    duplicate_delta = None
    comparison = None
    if baseline is not None:
        before = {f['name']: f for f in baseline['functions']}
        after = {f['name']: f for f in cur['functions']}
        retained, lost, improved, worsened, changed = [], [], [], [], []
        for name in sorted(before.keys() & after.keys()):
            a, b = before[name], after[name]
            if a['target_hash'] != b['target_hash'] or a['size'] != b['size']:
                changed.append(name)
                continue
            if a['status'] != 'matched' and b['status'] == 'matched':
                retained.append({'function': name, 'bytes': b['size'] or 0, 'agent_id': b['matched_agent_id']})
            if a['status'] == 'matched' and b['status'] != 'matched':
                lost.append(name)
            if a['best_score'] is not None and b['best_score'] is not None and b['status'] != 'matched':
                drop = a['best_score'] - b['best_score']
                row = {'function': name, 'from_score': a['best_score'], 'to_score': b['best_score'],
                       'score_drop': drop, 'agent_id': b['best_agent_id'], 'same_inputs': a['inputs'] == b['inputs'],
                       'source_review': latest_reviews.get((b['id'], b['source_hash']), {}).get('verdict', 'unreviewed')}
                if drop >= minimum_score_drop and b['best_score'] > 0:
                    improved.append(row)
                elif drop < 0:
                    worsened.append(row)
        comparison = {'basis': 'stored snapshots; not a recompile or causal attribution',
                      'new_exact_functions': len(retained), 'new_exact_bytes': sum(f['bytes'] for f in retained),
                      'new_exact': retained, 'lost_exact': lost, 'partial_improvements': improved,
                      'score_regressions': worsened, 'changed_targets_excluded': changed,
                      'added_functions': sorted(after.keys() - before.keys()),
                      'removed_functions': sorted(before.keys() - after.keys())}
        if trials is not None and baseline['trials'] is not None:
            old = {(r['function_id'], r['environment'], r['source_hash']): r['duplicates'] for r in baseline['trials']}
            # Counters have no retry timestamps. Delta spans snapshots, never the requested window.
            duplicate_delta = sum(max(0, r['duplicates'] - old.get((r['function_id'], r['environment'], r['source_hash']), 0))
                                  for r in trials)
    run_intervals, run_gaps = _run_intervals(cur['events'], start, end)
    intervals = activity_intervals if activity_intervals is not None else run_intervals
    # No historical telemetry means unknown, not zero agent-hours.
    hours, sources = _hours(intervals if intervals or activity_intervals is not None else None, start, end)
    covered = lambda aid, at: at is not None and any(r['agent_id'] == aid and r['start'] <= at < r['end']
                                                   for r in intervals)
    measured_matches = [f for f in new_matches if covered(f['matched_agent_id'], f['matched_at'])]
    measured_partials = [r for r in partials if covered(r['agent_id'], attempt_by_id[r['attempt_id']]['created_at'])]
    full_coverage = hours is not None and all(covered(a['agent_id'], a['created_at']) for a in attempts) and \
                    all(covered(f['matched_agent_id'], f['matched_at']) for f in new_matches) and not run_gaps
    elapsed = (end - start) / 3600
    counts = {'exact_functions': len(new_matches), 'exact_bytes': sum(f['size'] or 0 for f in new_matches),
              'historical_partial_improvements': len(partials)}
    rates = lambda denominator: {k: v / denominator if denominator else None for k, v in counts.items()}
    measured_counts = {'exact_functions': len(measured_matches), 'exact_bytes': sum(f['size'] or 0 for f in measured_matches),
                       'historical_partial_improvements': len(measured_partials)}
    return {'schema_version': 1, 'minimum_score_drop': minimum_score_drop,
            'project': {k: cur['project'][k] for k in ('id', 'slug', 'name')},
            'sources': {'database': str(Path(database).resolve()),
                        'baseline_database': str(Path(baseline_database).resolve()) if baseline_database else None,
                        'read_only': True, 'snapshot_at': time.time()},
            'window': {'since': start, 'until': end, 'elapsed_hours': elapsed, 'bounds': '[since, until)'},
            'denominator': {'agent_hours': hours, 'evidence_sources': sources,
                            'covers_all_activity': full_coverage, 'run_gaps': run_gaps,
                            'basis': 'explicit activity intervals' if activity_intervals is not None else
                            'paired agent run events (session time including tools/waits)' if run_intervals else
                            'unavailable: database records claims and submissions, not active session durations'},
            'current': {'functions': len(funcs), 'exact_functions': len(matched),
                        'exact_bytes': sum(f['size'] or 0 for f in matched), 'suspect_incumbents': suspect_incumbents},
            'activity': {**counts, 'attempts': len(attempts),
                         'compilation_errors': sum(not bool(a['compiled']) for a in attempts),
                         'compiled_without_score': sum(bool(a['compiled']) and a['score'] is None for a in attempts),
                         'duplicate_sources_same_context': duplicate_sources,
                         'new_hypotheses': sum(inside(r) for r in trials or []) if trials is not None else None,
                         'duplicate_hypotheses_all_time': duplicate_total if trials is not None else None,
                         'duplicate_hypotheses_since_baseline': duplicate_delta,
                         'hypothesis_states': dict(Counter(r['state'] for r in trials or [] if inside(r))),
                         'suspect_reviews': sum(r['verdict'] == 'suspect' for r in window_reviews),
                         'suspect_partial_improvements': sum(r['source_review'] == 'suspect' for r in partials),
                         'review_score_regressions': regressions, 'partial_improvements': partials},
            'measured_activity': measured_counts,
            'rates': {'per_elapsed_hour': rates(elapsed), 'per_agent_hour': rates(hours if full_coverage else None),
                      'per_measured_agent_hour': {k: v / hours if hours else None for k, v in measured_counts.items()}},
            'agents': [{'id': aid, 'name': cur['agents'].get(aid, 'unknown'), **values}
                       for aid, values in sorted(agent_counts.items(), key=lambda r: str(r[0]))],
            'comparison': comparison,
            'limitations': ['Exact totals use stored matched status; this report does not recompile.',
                            'Historical partials compare positive compiled penalties within a context version; '
                            'target/compiler/symbol changes are not fully recorded per attempt.',
                            'Partial score drops are not matched bytes, semantic correctness, or independent functions.',
                            'Duplicate source retries share function/context; full historical compiler identity is unavailable.',
                            'Hypothesis duplicate counters have no timestamps or requesting-agent attribution.',
                            'Baseline is unavailable; retained snapshot improvements cannot be measured.' if baseline is None else
                            'Snapshot score deltas with same_inputs=false need revalidation before improvement credit.']}


def format_report(result):
    """Compact agent-facing summary; full evidence remains in the JSON report."""
    a, c, d = result['activity'], result['current'], result['denominator']
    lines = [f"{result['project']['slug']}: stored exact {c['exact_functions']} functions / {c['exact_bytes']} bytes.",
             f"Window {result['window']['elapsed_hours']:.2f} elapsed hours: +{a['exact_functions']} exact functions / "
             f"{a['exact_bytes']} bytes; {a['historical_partial_improvements']} same-context partial score drops "
             f"(at least {result['minimum_score_drop']}); "
             f"{a['attempts']} attempts / {a['compilation_errors']} compilation errors.",
             f"Repeated sources: {a['duplicate_sources_same_context']}; suspect reviews: {a['suspect_reviews']}; "
             f"review score regressions: {len(a['review_score_regressions'])}."]
    lines.append(f"New hypotheses: {a['new_hypotheses']}; duplicate hypothesis requests (all time): "
                 f"{a['duplicate_hypotheses_all_time']} (retry timestamps/requesting agents unavailable).")
    if d['agent_hours'] is None:
        lines.append('Agent-hours unavailable; elapsed time is not agent time. No per-agent-hour rate reported.')
    else:
        rate = result['rates']['per_measured_agent_hour']['exact_bytes']
        lines.append(f"Measured session-hours: {d['agent_hours']:.3f}; covered exact bytes/session-hour: "
                     + (f'{rate:.2f}.' if rate is not None else 'unavailable (zero duration).'))
        if not d['covers_all_activity']:
            lines.append('Overall agent-hour rate unavailable: some activity/run bounds are unmeasured.')
    comparison = result['comparison']
    if comparison is None:
        lines.append('No frozen baseline: retained snapshot improvements/regressions unknown.')
    else:
        lines.append(f"Baseline: +{comparison['new_exact_functions']} retained exact functions / "
                     f"{comparison['new_exact_bytes']} bytes; {len(comparison['partial_improvements'])} partial drops "
                     f"(check same_inputs); {len(comparison['lost_exact'])} lost exact matches.")
    lines.append('Scores are penalties, lower is better; partials are not exact bytes or semantic proof.')
    return '\n'.join(lines)
