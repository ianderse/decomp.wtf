"""Persistent, evidence-scoped work plans for the hard tail of ROM matching.

A diagnosis is a hypothesis, not proof. Reviews never replace byte verification. Historical
attempts and lowest-score sources remain immutable; a reviewed starting point is a separate pointer.
"""
from __future__ import annotations

import hashlib
import json
import time
from functools import lru_cache
from pathlib import Path
from collections import Counter

LANES = {
    'stack': 'Investigate frame layout and spill placement; retain compiler dumps and compare one lifetime at a time.',
    'allocation': 'Use bounded, targeted declaration/lifetime/scheduling alternatives; preserve control flow.',
    'structural': 'Recover missing instructions, branch joins, types and call signatures before tuning registers.',
    'library': 'Compare shared library records and multiple callers; validate shared header changes against matches.',
    'linking': 'Inspect relocation targets, jump tables and encoded bytes before changing C structure.',
}


def digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


@lru_cache(maxsize=64)
def _file_digest(path, size, mtime):
    return digest(Path(path).read_bytes())


def compiler_builds(recipe):
    from . import toolchains
    from .project import toolchain_id
    tid = recipe.get('function_toolchain') or recipe.get('toolchain')
    if not tid:
        return {}
    result = {}
    try:
        folder = toolchains.path(toolchain_id(tid))
        files = [folder / n for n in ('gcc', 'cc1', 'cpp')]
        files += [toolchains.path('maspsx') / 'maspsx.py',
                  toolchains.path('binutils-mips') / 'mips-ps2-decompals-as']
        for p in files:
            if p.is_file():
                st = p.stat()
                result[str(p)] = _file_digest(str(p), st.st_size, st.st_mtime_ns)
    except (KeyError, ValueError):
        pass  # custom/non-PSX recipes still carry their full configured command
    return result


def environment(net, fn):
    proj = net.store.one('SELECT * FROM projects WHERE id=?', fn['project_id'])
    ctx = net._context(fn['project_id'], fn['unit'])
    cfg = json.loads(proj['config'])
    recipe = dict(cfg.get('compiler', {}))
    recipe['function_toolchain'] = net.fn_toolchain(fn)
    # Linking inputs matter even when a C compilation is unchanged.
    stamp = net.store.one('SELECT v FROM symbols_stamp WHERE project_id=?', fn['project_id'])
    details = {'header': digest(ctx['header']), 'context_version': ctx['version'], 'compiler': recipe,
               'target': digest(fn['target'] or b''), 'relocs': fn['relocs'],
               'tool_builds': compiler_builds(recipe),
               'symbols': stamp['v'] if stamp else 0, 'gp': cfg.get('gp'), 'regions': cfg.get('regions', [])}
    return digest(json.dumps(details, sort_keys=True)), details


def basis(net, fn):
    env, _ = environment(net, fn)
    review = net.store.one('SELECT MAX(id) id FROM candidate_reviews WHERE function_id=?', fn['id'])
    return digest(env + digest(fn['best_source'] or '') + str(review['id']))


def latest(net, fn):
    return net.store.one('SELECT * FROM function_diagnoses WHERE function_id=? ORDER BY id DESC LIMIT 1', fn['id'])


def eligible(net, fn, strategy='easiest'):
    # The templates lane is used by local-model helpers. The hard remainder belongs to strong sessions.
    if strategy == 'templates' and (fn['attempts'] >= 30 or fn['abandons'] >= 3):
        return False
    row = latest(net, fn)
    if not row:
        return strategy not in LANES
    current = row['basis'] == basis(net, fn)
    if row['exhausted'] and current:
        return False
    return strategy not in LANES or (current and row['category'] == strategy)


def record(net, agent, function, category, evidence, next_hypothesis, exhausted=False, project=None):
    from .network import NetworkError
    net._require_level(agent, 'Changing a function work plan')
    if category not in LANES:
        raise NetworkError('category must be one of: ' + ', '.join(LANES))
    if not evidence.strip() or not next_hypothesis.strip():
        raise NetworkError('Provide measured evidence and the next hypothesis or evidence needed to resume.')
    fn = net._function(function, project)
    env, details = environment(net, fn)
    with net.store.tx() as db:
        claim = db.execute('SELECT agent_id FROM claims WHERE function_id=?', (fn['id'],)).fetchone()
        if claim and claim['agent_id'] != agent.id:
            raise NetworkError('Another agent holds this function; do not change its work plan.')
        db.execute('INSERT INTO function_diagnoses(function_id,agent_id,category,evidence,next_hypothesis,exhausted,'
                   'basis,environment,details,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (fn['id'], agent.id, category, evidence.strip(), next_hypothesis.strip(), bool(exhausted),
                    basis(net, fn), env, json.dumps(details), time.time()))
    return f'Diagnosis saved: {category}; ' + ('waiting for new evidence (named claims remain available).' if exhausted
                                               else 'ready for a targeted claim.')


def classify(diff, problems=()):
    counts = Counter(m for m, _, _ in diff.rows if m != ' ')
    if problems or (not diff.bytes_match and not counts):
        return 'linking', counts
    if counts and set(counts) <= {'s'}:
        return 'stack', counts
    if counts and set(counts) <= {'r', 'o'}:
        return 'allocation', counts
    return 'structural', counts


def observe(net, fn, agent, diff, problems=()):
    """Only classify the incumbent, never overwrite a person's diagnosis with a worse trial."""
    current = net.store.one('SELECT * FROM functions WHERE id=?', fn['id'])
    if current['status'] != 'open' or current['best_score'] != diff.score:
        return
    old = latest(net, current)
    if old and old['basis'] == basis(net, current):
        return
    category, counts = classify(diff, problems)
    env, details = environment(net, current)
    with net.store.tx() as db:
        db.execute('INSERT INTO function_diagnoses(function_id,agent_id,category,evidence,next_hypothesis,exhausted,'
                   'basis,environment,details,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (fn['id'], agent.id, category, 'Measured diff markers: ' + json.dumps(counts) +
                    ('; linking: ' + '; '.join(problems) if problems else ''), LANES[category], 0,
                    basis(net, current), env, json.dumps(details), time.time()))


def review(net, agent, function, attempt_id, verdict, evidence, project=None):
    from .network import NetworkError
    net._require_level(agent, 'Reviewing a candidate')
    if verdict not in ('plausible', 'suspect') or not evidence.strip():
        raise NetworkError('verdict must be plausible or suspect, with explicit review evidence (not a score).')
    fn = net._function(function, project)
    a = net.store.one('SELECT * FROM attempts WHERE id=? AND function_id=?', attempt_id, fn['id'])
    if not a or not a['compiled']:
        raise NetworkError('Review an existing compiled attempt belonging to this function.')
    env, _ = environment(net, fn)
    proj = net.store.one('SELECT * FROM projects WHERE id=?', fn['project_id'])
    ctx = net._context(fn['project_id'], fn['unit'])
    result = net._compile(proj, ctx['header'], a['source'], fn)
    if not result.ok:
        raise NetworkError('Candidate no longer compiles under current inputs: ' + result.log)
    candidate, verdict_now, problems = net._evaluate(fn, proj, result.obj)
    if candidate is None:
        raise NetworkError('Compiled object does not contain the candidate function.')
    from . import scoring
    measured = scoring.diff(net._target_obj(fn, proj), candidate)
    score = 0 if verdict_now is True else max(1, measured.score) if verdict_now is False else measured.score
    if env != environment(net, net._function(function, project))[0]:
        raise NetworkError('Inputs changed during review; retry under stable inputs.')
    with net.store.tx() as db:
        db.execute('INSERT INTO candidate_reviews(function_id,attempt_id,source_hash,agent_id,verdict,evidence,'
                   'environment,measured_score,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                   (fn['id'], attempt_id, digest(a['source']), agent.id, verdict, evidence.strip(), env, score, time.time()))
    return f'Review recorded for attempt {attempt_id}: {verdict}. This is a source review, not proof of equivalence.'


def starting_point(net, fn):
    """Reviewed sources outrank the score incumbent. Suspect sources never seed machine searches."""
    rows = net.store.all('SELECT r.*,a.source,a.score,a.context_version FROM candidate_reviews r '
                         'JOIN attempts a ON a.id=r.attempt_id WHERE r.function_id=? ORDER BY r.id DESC', fn['id'])
    seen, plausible, suspect = set(), None, False
    for r in rows:
        if r['source_hash'] in seen:
            continue
        seen.add(r['source_hash'])
        if r['source_hash'] == digest(fn['best_source'] or ''):
            suspect = r['verdict'] == 'suspect'
        if plausible is None and r['verdict'] == 'plausible':
            plausible = r
    return plausible, suspect


def machine_source(net, fn):
    reviewed, suspect = starting_point(net, fn)
    if reviewed:
        if reviewed['environment'] != environment(net, fn)[0]:
            return None  # recompile/review under new inputs before automated search
        return dict(fn) | {'best_source': reviewed['source'], 'best_score': reviewed['measured_score']}
    return None if suspect else fn


def brief(net, fn):
    row = latest(net, fn)
    lines = []
    if row:
        stale = row['basis'] != basis(net, fn)
        lines += ['', '### Persistent diagnosis' + (' (inputs changed; revalidate)' if stale else ''),
                  f"Lane: {row['category']}. {LANES[row['category']]}", row['evidence'],
                  'Next hypothesis / resume condition: ' + row['next_hypothesis']]
        if row['exhausted'] and not stale:
            lines += ['Automatic rotation is paused. A named claim permits a new evidence-backed investigation.']
    lines += ['Historical attempts are not this claim\'s budget. Stop only on the current claim\'s stall signal; '
              'if no new hypothesis exists, record_diagnosis(exhausted=true) before release.']
    trials = net.store.all('SELECT id,hypothesis,state,score FROM hypothesis_trials WHERE function_id=? '
                           'ORDER BY id DESC LIMIT 8', fn['id'])
    if trials:
        lines += ['Recent measured hypotheses:'] + [f"- trial {r['id']}: {r['hypothesis']} [{r['state']}, score {r['score']}]"
                                                  for r in trials]
    return lines


def trial(net, agent, function, source, hypothesis, project=None):
    """One bounded hypothesis, exactly-once reservation per source and environment, even after interruption."""
    from .network import NetworkError
    if not hypothesis.strip():
        raise NetworkError('Name the single measured difference this candidate tests.')
    fn = net._function(function, project)
    env, details = environment(net, fn)
    source_hash = digest(source)
    with net.store.tx() as db:
        old = db.execute('SELECT * FROM hypothesis_trials WHERE function_id=? AND environment=? AND source_hash=?',
                         (fn['id'], env, source_hash)).fetchone()
        if old:
            db.execute('UPDATE hypothesis_trials SET duplicates=duplicates+1 WHERE id=?', (old['id'],))
            return f"Duplicate avoided: trial {old['id']} ({old['state']}); score {old['score']}. " + (old['result'] or
                    'Reserved or interrupted: inspect before rerunning; no completion was recorded.')
        claim = db.execute('SELECT agent_id FROM claims WHERE function_id=?', (fn['id'],)).fetchone()
        if not claim or claim['agent_id'] != agent.id:
            raise NetworkError('Claim this function before testing a new hypothesis.')
        cur = db.execute('INSERT INTO hypothesis_trials(function_id,agent_id,environment,source_hash,hypothesis,'
                         'details,source,state,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                         (fn['id'], agent.id, env, source_hash, hypothesis.strip(), json.dumps(details), source, 'running', time.time()))
        tid = cur.lastrowid
    try:
        result = net.compile_and_diff(agent, function, source, project)
        a = net.store.one('SELECT id,score FROM attempts WHERE function_id=? AND agent_id=? AND source=? '
                         'AND created_at>=? ORDER BY id DESC LIMIT 1', fn['id'], agent.id, source,
                         net.store.one('SELECT created_at FROM hypothesis_trials WHERE id=?', tid)['created_at'])
        with net.store.tx() as db:
            db.execute('UPDATE hypothesis_trials SET state=?,score=?,attempt_id=?,result=?,finished_at=? WHERE id=?',
                       ('done', a['score'] if a else None, a['id'] if a else None, result, time.time(), tid))
        return f'Trial {tid}: {hypothesis}\n{result}'
    except BaseException as exc:
        with net.store.tx() as db:
            db.execute('UPDATE hypothesis_trials SET state=?,result=?,finished_at=? WHERE id=?',
                       ('interrupted', type(exc).__name__ + ': ' + str(exc), time.time(), tid))
        raise


def inspect_candidate(net, function, attempt_id=0, project=None):
    from .network import NetworkError
    fn = net._function(function, project)
    if attempt_id:
        row = net.store.one('SELECT * FROM attempts WHERE id=? AND function_id=?', attempt_id, fn['id'])
        if not row:
            raise NetworkError('Attempt does not belong to this function.')
        return f"Attempt {row['id']}, score {row['score']}, context {row['context_version']}\n{row['source']}"
    rows = net.store.all('SELECT id,score,compiled,context_version,created_at FROM attempts WHERE function_id=? '
                         'ORDER BY id DESC LIMIT 20', fn['id'])
    return json.dumps([dict(r) for r in rows], indent=2)


def alternatives(net, agent, function, attempt_id, old, replacements, hypothesis, project=None):
    """Explicit, bounded single-region permutations, all forked from the same saved source."""
    from .network import NetworkError
    fn = net._function(function, project)
    a = net.store.one('SELECT * FROM attempts WHERE id=? AND function_id=?', attempt_id, fn['id'])
    if not a or not old or a['source'].count(old) != 1:
        raise NetworkError('Select an existing attempt and a nonempty region occurring exactly once.')
    if not 1 <= len(replacements) <= 8 or any(not isinstance(r, str) for r in replacements):
        raise NetworkError('Supply 1-8 explicit replacement strings; all other source remains fixed.')
    results = []
    for i, replacement in enumerate(replacements):
        if net._function(function, project)['status'] == 'matched':
            break
        results.append(trial(net, agent, function, a['source'].replace(old, replacement, 1),
                             f'{hypothesis} [alternative {i + 1}/{len(replacements)}]', project))
    return '\n\n'.join(results)


def require_hypothesis(net, fn, agent, source):
    from .network import NetworkError
    row = latest(net, fn)
    if fn['status'] != 'open' or not row or not row['exhausted'] or row['basis'] != basis(net, fn):
        return
    active = net.store.one("SELECT 1 FROM hypothesis_trials WHERE function_id=? AND agent_id=? AND environment=? "
                           "AND source_hash=? AND state='running'", fn['id'], agent.id,
                           environment(net, fn)[0], digest(source))
    if not active:
        raise NetworkError('This diagnosis is exhausted under unchanged inputs. Use test_hypothesis with a new '
                           'evidence-backed hypothesis, or record_diagnosis with new evidence before compiling.')
