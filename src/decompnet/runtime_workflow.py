"""Evidence-backed runtime priorities and immutable, repeatable port runs.

python -m decompnet.runtime_workflow --help
Project runtime-workflow.json is opt-in; no game data belongs in the repository.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time


def read_plan(root):
    path = Path(root) / 'runtime-workflow.json'
    if not path.exists():
        return {'blockers': [], 'scenarios': {}}
    plan = json.loads(path.read_text())
    names = set()
    for item in plan.get('blockers', []):
        if item['function'] in names or item['lane'] not in ('exact', 'port'):
            raise ValueError('Duplicate blocker or invalid lane')
        names.add(item['function'])
        if not item.get('evidence') or not item.get('next_hypothesis'):
            raise ValueError('Blockers require evidence and a next hypothesis')
        if item['lane'] == 'exact' and (item.get('max_attempts', 0) < 1 or item.get('attempts_at_start', -1) < 0):
            raise ValueError('Exact blockers require an explicit attempt baseline and positive budget')
    return plan


def priorities(net, proj):
    """Bounded priorities; exhaustion, claims, ranges and cooldown still apply upstream."""
    plan = read_plan(proj['root'])
    result = []
    for item in plan.get('blockers', []):
        if item['lane'] != 'exact' or item.get('state', 'active') != 'active':
            continue
        fn = net.store.one('SELECT * FROM functions WHERE project_id=? AND name=?', proj['id'], item['function'])
        if fn and fn['status'] == 'open' and fn['attempts'] - item['attempts_at_start'] < item['max_attempts']:
            result.append(item['function'])
    return result


def budget_exhausted(net, fn):
    proj = net.store.one('SELECT root FROM projects WHERE id=?', fn['project_id'])
    return any(item['function'] == fn['name'] and item['lane'] == 'exact'
               and item.get('state', 'active') == 'active'
               and fn['attempts'] - item['attempts_at_start'] >= item['max_attempts']
               for item in read_plan(proj['root']).get('blockers', []))


def automatic_paused(net, fn):
    proj = net.store.one('SELECT root FROM projects WHERE id=?', fn['project_id'])
    for item in read_plan(proj['root']).get('blockers', []):
        if item['function'] == fn['name'] and item['lane'] == 'exact':
            return item.get('state') == 'waiting' or (
                item.get('state', 'active') == 'active' and
                fn['attempts'] - item['attempts_at_start'] >= item['max_attempts'])
    return False


def require_budget(net, fn):
    from .network import NetworkError
    if budget_exhausted(net, fn):
        raise NetworkError('Runtime investigation budget exhausted. Preserve the incumbent and record the '
                           'outcome; a new evidence-backed budget in runtime-workflow.json is required.')


def brief(net, fn):
    proj = net.store.one('SELECT root FROM projects WHERE id=?', fn['project_id'])
    for item in read_plan(proj['root']).get('blockers', []):
        if item['function'] == fn['name']:
            return ['','### Runtime blocker', f"Milestone: {item['milestone']}; lane: {item['lane']}",
                    'Evidence: ' + item['evidence'], 'Next hypothesis: ' + item['next_hypothesis'],
                    f"Investigation budget: {item.get('max_attempts', 'n/a')} new attempts from "
                    f"attempt {item.get('attempts_at_start', 'n/a')}. Preserve the incumbent; record the outcome before release."]
    return []


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def freeze(database, destination):
    """SQLite backup includes committed WAL; never copy only the .db file."""
    destination = Path(destination)
    with destination.open('xb'):
        pass
    with sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
    return {'database': str(destination), 'sha256': sha(destination), 'captured_at': time.time()}


def evaluate(scenario, text, code, forced, screenshots):
    from .rom.export_port import summarize_run
    summary = summarize_run(text)
    failures = []
    frames = re.search(r'^psx: (\d+) frames \(vertical blanks\)', text, re.M)
    nframes = int(frames[1]) if frames else 0
    if forced or code not in (0, 124) or summary['fault'] or summary['stopped_at']:
        failures.append('fault, unexpected exit, or external watchdog termination')
    if nframes < scenario.get('min_vblanks', 1):
        failures.append('vblank milestone not reached')
    if (summary.get('mdec') or {}).get('frames', 0) < scenario.get('min_movie_frames', 0):
        failures.append('movie decode milestone not reached')
    for name in scenario.get('required_reads', []):
        if name not in summary['cd_reads']:
            failures.append('missing disc read: ' + name)
    for marker in scenario.get('required_log', []):
        if marker not in text:
            failures.append('missing log evidence: ' + marker)
    if not screenshots:
        failures.append('no screen capture')
    return {'status': 'failed' if failures else 'needs_visual_review', 'failures': failures,
            'vblanks': nframes, 'summary': summary,
            'acceptance': 'Automated transport/runtime checks only; inspect captures for the expected screen.'}


def run_scenario(root, name, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    plan = read_plan(root)
    scenario = plan['scenarios'][name]
    seconds = int(scenario['seconds'])
    if not 1 <= seconds <= 300:
        raise ValueError('Scenario duration must be 1..300 seconds')
    port = root / 'port'
    exe = port / 'build' / 'game'
    metadata = json.loads((port / 'port.json').read_text())
    output.mkdir(parents=True, exist_ok=False)
    (output / 'scenario.json').write_text(json.dumps(scenario, indent=2) + '\n')
    shutil.copy2(port / 'port.json', output / 'port.json')
    # Record all source/runtime/patch inputs and the actual binary. Build products other
    # than the executable are omitted; source files are hashed, never modified.
    files = [exe, port / 'build' / 'CMakeCache.txt']
    for folder in (root / 'source', port / 'patches', port / 'psx', port / 'gen', port / 'tools'):
        if folder.exists():
            files.extend(p for p in folder.rglob('*') if p.is_file() and '.git' not in p.parts)
    files += [p for p in (port / 'CMakeLists.txt', port / 'baserom.exe') if p.exists()]
    disc = Path(metadata['disc']) if metadata.get('disc') else None
    if disc:
        files.append(disc)
    identity = {str(p): sha(p) for p in files if p.is_file()}
    (output / 'inputs.json').write_text(json.dumps(identity, indent=2) + '\n')
    # Do not inherit an interactive user's memory card, pad script, trace or dump paths.
    env = {k:v for k,v in os.environ.items() if not k.startswith('PSX_')}
    env.update(PSX_HEADLESS='1', PSX_PORT_SOFT='1', PSX_TIMEOUT=str(seconds),
               PSX_PAD_SCRIPT=scenario.get('pad', ''), PSX_PORT_DUMP_SCREEN=str(output / 'screen.png'),
               PSX_PORT_DUMP_EVERY='300', PSX_AUDIO_WAV=str(output / 'audio.wav'))
    if disc:
        env['PSX_DISC'] = str(disc)
    if scenario.get('card') == 'fresh':
        env['PSX_MEMCARD'] = str(output / 'card.mcd')  # created only in this new evidence directory
    started = time.time()
    forced = False
    with (output / 'run.log').open('x') as log:
        proc = subprocess.Popen([str(exe)], cwd=exe.parent, env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            proc.wait(timeout=seconds + 5)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            forced = True
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    shots = sorted(output.glob('screen*.png'))
    result = evaluate(scenario, (output / 'run.log').read_text(errors='replace'), proc.returncode, forced, shots)
    result.update(scenario=name, started_at=started, finished_at=time.time(), exit=proc.returncode,
                  environment={k:v for k,v in env.items() if k.startswith('PSX_')},
                  input_manifest_sha256=sha(output / 'inputs.json'),
                  screenshots=[{'path':p.name, 'sha256':sha(p)} for p in shots])
    if sha(exe) != identity[str(exe)]:
        result['status'] = 'failed'
        result['failures'].append('executable changed during the run')
    (output / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def review_run(directory, verdict, evidence, reviewer):
    """Append a review bound to the immutable result and its captured images."""
    directory = Path(directory)
    result = json.loads((directory / 'result.json').read_text())
    if verdict == 'accepted' and result['status'] == 'failed':
        raise ValueError('A failed runtime check cannot be accepted; fix and rerun in a new directory')
    if not evidence.strip() or not reviewer.strip():
        raise ValueError('Review requires an identified reviewer and observed evidence')
    for image in result['screenshots']:
        if sha(directory / image['path']) != image['sha256']:
            raise ValueError('Capture changed since the run')
    review = {'verdict':verdict, 'evidence':evidence, 'reviewer':reviewer, 'at':time.time(),
              'result_sha256':sha(directory / 'result.json')}
    with (directory / 'review.json').open('x') as out:
        json.dump(review,out,indent=2)
    return review


def consolidated(database, project, output, baseline=None, since=None, runs=None):
    from .progress_metrics import report, format_report
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    snapshot = freeze(database, output / 'snapshot.db')
    result = report(output / 'snapshot.db', project, baseline_database=baseline, since=since)
    with sqlite3.connect(output / 'snapshot.db') as db:
        db.row_factory = sqlite3.Row
        p = db.execute('SELECT * FROM projects WHERE slug=?', (project,)).fetchone()
        plan = read_plan(p['root'])
        blockers = []
        for item in plan.get('blockers', []):
            f = db.execute('SELECT status,attempts,best_score FROM functions WHERE project_id=? AND name=?',
                           (p['id'], item['function'])).fetchone()
            blockers.append(dict(item, current=dict(f) if f else None,
                                 remaining_attempts=max(0,item.get('max_attempts',0) -
                                     ((f['attempts'] if f else 0)-item.get('attempts_at_start',0)))))
        diagnoses = []
        # A read-only facade avoids Store initialization/migrations on evidence.
        from types import SimpleNamespace
        from .network import Network
        from . import diagnosis
        def one(sql,*args): return db.execute(sql,args).fetchone()
        facade = SimpleNamespace(store=SimpleNamespace(one=one), fn_toolchain=Network.fn_toolchain,
            _context=lambda pid,unit: one('SELECT * FROM context_versions WHERE project_id=? AND unit IN (?,\'\') '
                                         'ORDER BY unit=? DESC,version DESC LIMIT 1',pid,unit,unit))
        for f in db.execute("SELECT * FROM functions WHERE project_id=? AND status='open'", (p['id'],)).fetchall():
            row = diagnosis.latest(facade, f)
            if row:
                reasons = diagnosis.invalidation_reasons(facade, f, row)
                diagnoses.append({'function':f['name'], 'diagnosis_id':row['id'], 'exhausted':bool(row['exhausted']),
                                  'invalidated_by':reasons})
    runtime = []
    if runs:
        for pending in sorted(Path(runs).glob('*/scenario.json')):
            if not (pending.parent / 'result.json').exists():
                runtime.append({'scenario':pending.parent.name, 'status':'interrupted_or_incomplete', 'evidence':str(pending)})
        for path in sorted(Path(runs).glob('*/result.json')):
            item = dict(json.loads(path.read_text()), evidence=str(path))
            review = path.parent / 'review.json'
            if review.exists():
                check = json.loads(review.read_text())
                if check['result_sha256'] == sha(path):
                    item['review'] = check
                    item['status'] = check['verdict'] if item['status'] != 'failed' else 'failed'
            runtime.append(item)
    result.update(snapshot=snapshot, runtime_blockers=blockers, diagnoses=diagnoses, runtime_scenarios=runtime)
    (output / 'report.json').write_text(json.dumps(result, indent=2) + '\n')
    from collections import Counter
    review_counts = Counter(p['source_review'] for p in result['activity']['partial_improvements'])
    lines = [format_report(result), 'Partial score drops by source review: ' + json.dumps(review_counts), '', 'Runtime blockers (database state, not runtime acceptance):']
    lines += [f"- {b['function']} [{b['lane']}]: {b['current']}; budget remaining {b['remaining_attempts']}; "
              f"next: {b['next_hypothesis']}" for b in blockers]
    lines += ['', 'Diagnosis invalidations:']
    lines += [f"- {d['function']}: {', '.join(d['invalidated_by'])}" for d in diagnoses if d['invalidated_by']]
    lines += ['', 'Runtime scenarios:'] + [f"- {r['scenario']}: {r['status']} ({r['evidence']})" for r in runtime]
    lines += ['', 'Background workers: ' + json.dumps(result['background_workers']),
              'Uninstrumented remote jobs and interrupted runs remain gaps; worker lifetime includes idle time.']
    (output / 'report.txt').write_text('\n'.join(lines) + '\n')
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    run = sub.add_parser('run')
    run.add_argument('root', type=Path)
    run.add_argument('scenario')
    run.add_argument('output', type=Path)
    review = sub.add_parser('review')
    review.add_argument('directory', type=Path)
    review.add_argument('verdict', choices=['accepted', 'rejected'])
    review.add_argument('--evidence', required=True)
    review.add_argument('--reviewer', required=True)
    rep = sub.add_parser('report')
    rep.add_argument('database', type=Path)
    rep.add_argument('project')
    rep.add_argument('output', type=Path)
    rep.add_argument('--baseline', type=Path)
    rep.add_argument('--since', type=float)
    rep.add_argument('--runs', type=Path)
    a = p.parse_args(argv)
    if a.command == 'review':
        print(json.dumps(review_run(a.directory,a.verdict,a.evidence,a.reviewer),indent=2))
        return 0
    if a.command == 'run':
        r = run_scenario(a.root, a.scenario, a.output)
        print(json.dumps(r, indent=2))
        return 1 if r['status'] == 'failed' else 0
    consolidated(a.database, a.project, a.output, a.baseline, a.since, a.runs)
    print(a.output / 'report.txt')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
