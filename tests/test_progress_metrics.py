import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from decompnet.progress_metrics import format_report, measured_run, report
from decompnet.store import Store


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'metrics.db'
    store = Store(path)
    with store.tx() as db:
        db.execute("INSERT INTO projects(id,slug,name,root,config) VALUES(1,'game','Game','/unused','{}')")
        db.execute("INSERT INTO agents(id,name,token_hash) VALUES(1,'alice','alice'),(2,'bob','bob')")
        db.execute("INSERT INTO context_versions(project_id,version,header,unit) VALUES(1,1,'','')")
        for fid, name in [(1, 'partial'), (2, 'exact'), (3, 'zero_not_exact')]:
            db.execute("INSERT INTO functions(id,project_id,name,size,n_insns,target,relocs,best_score,best_source,best_agent_id) "
                       "VALUES(?,1,?,40,10,?,'[]',500,'original',1)", (fid, name, b'\0' * 40))
    yield path, store
    store.conn.close()


def attempt(store, fid, score, at, source='candidate', agent=1, compiled=True, context=1):
    with store.tx() as db:
        return db.execute('INSERT INTO attempts(function_id,agent_id,score,compiled,context_version,source,created_at) '
                          'VALUES(?,?,?,?,?,?,?)', (fid, agent, score, compiled, context, source, at)).lastrowid


def test_lower_penalties_first_candidate_errors_and_zero_are_not_matches(database):
    path, store = database
    attempt(store, 1, 500, 1)
    attempt(store, 1, 600, 11, source='worse')
    attempt(store, 1, 400, 12, source='better', agent=2)
    attempt(store, 1, 399, 13, source='tiny')
    attempt(store, 1, None, 14, compiled=False)
    attempt(store, 2, 300, 15)  # first compiled candidate is not a measured improvement
    attempt(store, 3, 0, 16)    # score zero does not establish byte identity
    attempt(store, 1, 100, 17, context=2)  # new context is a new baseline
    attempt(store, 1, 400, 18, context=None)
    attempt(store, 1, 200, 19, context=None)  # absent context provenance cannot establish comparable inputs
    attempt(store, 1, 1, 20)  # end is exclusive
    result = report(path, 'game', since=10, until=20)
    a = result['activity']
    assert a['attempts'] == 9 and a['compilation_errors'] == 1
    assert a['exact_functions'] == 0
    assert a['historical_partial_improvements'] == 1
    assert a['partial_improvements'][0]['score_drop'] == 100
    assert a['partial_improvements'][0]['agent_id'] == 2
    assert result['denominator']['agent_hours'] is None
    assert result['rates']['per_agent_hour']['exact_bytes'] is None
    assert 'Agent-hours unavailable' in format_report(result)


def test_explicit_intervals_union_per_agent_clip_window_and_scope_numerator(database):
    path, store = database
    attempt(store, 1, 500, 100, agent=1)
    attempt(store, 1, 400, 200, agent=1)
    attempt(store, 1, 300, 300, agent=2)
    with store.tx() as db:
        db.execute("UPDATE functions SET status='matched',matched_at=200,matched_agent_id=1 WHERE id=2")
    intervals = [{'agent_id': 1, 'start': -100, 'end': 900, 'source': 'trace-a'},
                 {'agent_id': 1, 'start': 500, 'end': 1800, 'source': 'trace-b'},
                 {'agent_id': 2, 'start': 900, 'end': 2700, 'source': 'trace-c'}]
    result = report(path, 'game', since=0, until=1800, activity_intervals=intervals)
    assert result['denominator']['agent_hours'] == .75
    assert not result['denominator']['covers_all_activity']  # bob's 300s submission is outside his intervals
    assert result['rates']['per_agent_hour']['exact_bytes'] is None
    assert result['rates']['per_measured_agent_hour']['exact_bytes'] == 40 / .75
    assert result['measured_activity']['historical_partial_improvements'] == 1
    assert result['activity']['historical_partial_improvements'] == 2
    with pytest.raises(ValueError, match='evidence source'):
        report(path, 'game', since=0, until=1, activity_intervals=[{'agent_id': 1, 'start': 0, 'end': 1}])
    assert report(path, 'game', since=0, until=0)['rates']['per_elapsed_hour']['exact_bytes'] is None


def test_baseline_retained_deltas_inputs_and_target_identity(database, tmp_path):
    path, store = database
    frozen = tmp_path / 'baseline.db'
    dest = sqlite3.connect(frozen)
    store.conn.backup(dest)
    dest.close()
    with store.tx() as db:
        db.execute("UPDATE functions SET best_score=200,best_agent_id=2 WHERE id=1")
        db.execute("UPDATE functions SET status='matched',matched_at=20,matched_agent_id=2 WHERE id=2")
        db.execute("UPDATE functions SET target=?,best_score=100 WHERE id=3", (b'x' * 40,))
        db.execute("UPDATE context_versions SET header='changed' WHERE project_id=1")
    result = report(path, 1, baseline_database=frozen, since=10, until=30)
    c = result['comparison']
    assert c['new_exact_functions'] == 1 and c['new_exact_bytes'] == 40
    assert c['new_exact'][0]['agent_id'] == 2
    assert c['partial_improvements'] == [{'function': 'partial', 'from_score': 500, 'to_score': 200,
                                         'score_drop': 300, 'agent_id': 2, 'same_inputs': False,
                                         'source_review': 'unreviewed'}]
    assert c['changed_targets_excluded'] == ['zero_not_exact']
    assert result['agents'][0]['name'] == 'bob'


def test_duplicates_review_regressions_and_missing_optional_tables(database):
    path, store = database
    aid = attempt(store, 1, 500, 1)
    attempt(store, 1, 500, 10)
    attempt(store, 1, 500, 11, context=2)  # changed context is not the same retry
    with store.tx() as db:
        db.execute("UPDATE functions SET best_source='candidate' WHERE id=1")
        db.execute('INSERT INTO candidate_reviews(function_id,attempt_id,source_hash,agent_id,verdict,evidence,'
                   'environment,measured_score,created_at) VALUES(1,?,?,2,\'suspect\',\'uninitialized\',\'env\',700,12)',
                   (aid, hashlib.sha256(b'candidate').hexdigest()))
        db.execute('INSERT INTO hypothesis_trials(function_id,agent_id,environment,source_hash,hypothesis,details,'
                   'state,duplicates,created_at) VALUES(1,1,\'env\',\'hash\',\'test\',\'{}\',\'interrupted\',3,1)')
    result = report(path, 'game', since=10, until=20)
    assert result['activity']['duplicate_sources_same_context'] == 1
    assert result['activity']['duplicate_hypotheses_all_time'] == 3
    assert result['activity']['duplicate_hypotheses_since_baseline'] is None
    assert result['activity']['new_hypotheses'] == 0  # old counter cannot be attributed to this window
    assert result['activity']['suspect_reviews'] == 1
    assert result['activity']['review_score_regressions'][0]['measured_score'] == 700
    assert result['current']['suspect_incumbents'] == ['partial']
    with store.tx() as db:
        db.execute('DROP TABLE candidate_reviews')
        db.execute('DROP TABLE hypothesis_trials')
    assert report(path, 'game', since=0, until=20)['activity']['new_hypotheses'] is None


def test_read_only_report_does_not_repair_or_migrate(database, tmp_path):
    path, store = database
    with store.tx() as db:
        db.execute('CREATE TABLE unrelated_evidence(value TEXT)')
        db.execute("INSERT INTO unrelated_evidence VALUES('preserve')")
    store.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    schema = store.conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    report(path, 'game', since=0, until=1)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert store.conn.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == schema
    missing = tmp_path / 'does-not-exist.db'
    with pytest.raises(sqlite3.OperationalError):
        report(missing, 'game')
    assert not missing.exists()


def test_run_events_record_finish_on_exception_and_scope_closed_runs(database, monkeypatch):
    path, store = database
    net = SimpleNamespace(store=store)
    times = iter([100, 3700])
    monkeypatch.setattr('decompnet.progress_metrics.time.time', lambda: next(times))
    with pytest.raises(RuntimeError):
        with measured_run(net, 'game', 1, 'session-1', 'model', 'high'):
            attempt(store, 1, 500, 1000)
            raise RuntimeError('runner failed')
    events = store.all('SELECT * FROM events ORDER BY id')
    assert [e['kind'] for e in events] == ['agent_run_started', 'agent_run_finished']
    assert json.loads(events[0]['detail'])['run_id'] == json.loads(events[1]['detail'])['run_id']
    monkeypatch.setattr('decompnet.progress_metrics.time.time', lambda: 7200)
    result = report(path, 'game', since=0, until=7200)
    assert result['denominator']['agent_hours'] == 1
    assert result['denominator']['covers_all_activity']
    with store.tx() as db:
        db.execute("INSERT INTO events(project_id,agent_id,kind,detail,created_at) VALUES(1,2,'agent_run_started',?,2000)",
                   (json.dumps({'run_id': 'interrupted'}),))
    result = report(path, 'game', since=0, until=7200)
    assert result['denominator']['run_gaps'][0]['reason'].startswith('open run')
    assert not result['denominator']['covers_all_activity']


def test_old_open_run_does_not_block_later_closed_run(database):
    path, store = database
    with store.tx() as db:
        for kind, run, at in [('agent_run_started', 'old', 1),
                              ('agent_run_started', 'closed', 100), ('agent_run_finished', 'closed', 3700)]:
            db.execute('INSERT INTO events(project_id,agent_id,kind,detail,created_at) VALUES(1,1,?,?,?)',
                       (kind, json.dumps({'run_id': run}), at))
    attempt(store, 1, 500, 200)
    result = report(path, 'game', since=100, until=4000)
    assert result['denominator']['agent_hours'] == 1
    assert result['denominator']['run_gaps'] == []
    assert result['denominator']['covers_all_activity']


def test_telemetry_failure_warns_and_preserves_runner_exception(database, monkeypatch, caplog):
    _, store = database
    def broken():
        raise sqlite3.OperationalError('timing write failed')
    monkeypatch.setattr(store, 'tx', broken)
    with pytest.raises(RuntimeError, match='original runner error'):
        with measured_run(SimpleNamespace(store=store), 'game', 1, 'session'):
            raise RuntimeError('original runner error')
    assert 'timing has a gap' in caplog.text
    assert 'agent_run_started' in caplog.text and 'agent_run_finished' in caplog.text
