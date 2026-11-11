import json
from types import SimpleNamespace

import pytest

from decompnet import diagnosis, scoring
from decompnet.app.agents import tier_for
from decompnet.network import Network, NetworkError


@pytest.fixture
def setup(tmp_path):
    net = Network(tmp_path / 'work.db')
    with net.store.tx() as db:
        db.execute("INSERT INTO projects(id,slug,name,arch,root,config) VALUES(1,'game','Game','mips',?,?)",
                   (str(tmp_path), json.dumps({'compiler': {'flags': ['-O2']}})))
        db.execute("INSERT INTO context_versions(project_id,version,header,unit) VALUES(1,1,'typedef int s32;','')")
        for name, size in [('f', 40), ('g', 80)]:
            db.execute("INSERT INTO functions(project_id,name,size,n_insns,target,relocs,asm,best_source,best_score) "
                       "VALUES(1,?,?,?,?,'[]','nop','int f(void) { return 1; }',10)", (name, size, size//4, b'\0'*size))
    a = net.authenticate(net.register_agent('alice'))
    b = net.authenticate(net.register_agent('bob'))
    return net, a, b


def test_exhaustion_survives_restart_but_new_inputs_reopen(setup):
    net, a, b = setup
    diagnosis.record(net, a, 'f', 'stack', 'two stack offsets remain', 'new frame-layout evidence', True, 'game')
    reopened = Network(net.store.path)
    assert reopened.claim_row(b, 'game')['name'] == 'g'
    assert reopened.claim_row(a, 'game', 'f')['name'] == 'f'  # explicit investigation
    reopened.release(a, 'f', 'no new evidence', 'game')
    with reopened.store.tx() as db:
        db.execute("INSERT INTO context_versions(project_id,version,header,unit) VALUES(1,2,'typedef long s32;','')")
    fn = reopened._function('f', 'game')
    assert diagnosis.eligible(reopened, fn)
    assert not diagnosis.eligible(reopened, fn, 'stack')  # stale classification requires revalidation


def test_lanes_and_other_agents_claims(setup):
    net, a, b = setup
    diagnosis.record(net, a, 'f', 'allocation', 'two registers', 'shorten quotient lifetime', project='game')
    assert net.claim_row(a, 'game', strategy='stack') is None
    assert net.claim_row(a, 'game', strategy='allocation')['name'] == 'f'
    with pytest.raises(NetworkError, match='Another agent'):
        diagnosis.record(net, b, 'f', 'stack', 'guess', 'guess', project='game')


def test_duplicate_hypothesis_once_and_environment_change(setup, monkeypatch):
    net, a, _ = setup
    net.claim_row(a, 'game', 'f')
    calls = []
    def compile_(agent, function, source, project):
        calls.append(source)
        net._record(net._function(function, project), agent, 20, source, 1)
        return 'score 20'
    monkeypatch.setattr(net, 'compile_and_diff', compile_)
    one = diagnosis.trial(net, a, 'f', 'int f(){return 2;}', 'change immediate', 'game')
    two = diagnosis.trial(net, a, 'f', 'int f(){return 2;}', 'same source, different prose', 'game')
    assert 'Trial 1' in one and 'Duplicate avoided' in two and len(calls) == 1
    with net.store.tx() as db:
        db.execute("UPDATE projects SET config=? WHERE id=1", (json.dumps({'compiler': {'flags': ['-O1']}}),))
    diagnosis.trial(net, a, 'f', 'int f(){return 2;}', 'different compiler flags', 'game')
    assert len(calls) == 2


def test_interrupted_trial_is_retained_and_not_silently_retried(setup, monkeypatch):
    net, a, _ = setup
    net.claim_row(a, 'game', 'f')
    def fail(*args):
        raise RuntimeError('compiler process interrupted')
    monkeypatch.setattr(net, 'compile_and_diff', fail)
    with pytest.raises(RuntimeError):
        diagnosis.trial(net, a, 'f', 'source', 'test', 'game')
    assert 'interrupted' in diagnosis.trial(net, a, 'f', 'source', 'test', 'game')


def test_reviews_preserve_incumbent_and_do_not_imply_a_match(setup, monkeypatch):
    net, a, _ = setup
    fn = net._function('f', 'game')
    old = fn['best_source']
    net._record(fn, a, 10, old, 1)
    first = net.store.one('SELECT MAX(id) id FROM attempts')['id']
    net._record(fn, a, 40, 'int f(void) { return 3; }', 1)
    second = net.store.one('SELECT MAX(id) id FROM attempts')['id']
    monkeypatch.setattr(net, '_compile', lambda *a: SimpleNamespace(ok=True, obj=b''))
    monkeypatch.setattr(net, '_evaluate', lambda fn, proj, obj: (net._target_obj(fn, proj), False, []))
    diagnosis.review(net, a, 'f', first, 'suspect', 'uninitialized local read', 'game')
    assert diagnosis.machine_source(net, fn) is None
    diagnosis.review(net, a, 'f', second, 'plausible', 'all branches initialize the result; calls match', 'game')
    assert diagnosis.machine_source(net, fn)['best_source'] == 'int f(void) { return 3; }'
    current = net._function('f', 'game')
    assert current['best_source'] == old and current['status'] == 'open'
    assert 'SUSPECT' in net.get_function('f', 'game', a)
    assert 'Reviewed plausible' in net.get_function('f', 'game', a)
    with net.store.tx() as db:
        db.execute("INSERT INTO context_versions(project_id,version,header,unit) VALUES(1,2,'changed','')")
    assert diagnosis.machine_source(net, current) is None


@pytest.mark.parametrize('markers,problems,lane', [(['s','s'], [], 'stack'), (['r','o'], [], 'allocation'),
    (['r','<'], [], 'structural'), ([], [], 'linking'), (['r'], ['unresolved symbol'], 'linking')])
def test_classification_is_based_on_diff_not_percent(markers, problems, lane):
    d = scoring.DiffResult(2, 50000, [(m,'a','b') for m in markers], False)
    assert diagnosis.classify(d, problems)[0] == lane


def test_diagnosed_hard_work_uses_strong_tier():
    assert tier_for({'diagnosis_lane':'stack', 'best_score':2, 'n_insns':10})[2] == 'escalated'


def test_api_and_mcp_handlers_have_same_rom_tools():
    from decompnet.app import agents, tools
    assert {s['function']['name'] for s in agents.api_tool_schemas()} == set(tools.tool_names(False))
    assert 'test_hypothesis' not in {s['function']['name'] for s in agents.api_tool_schemas(True)}


def test_alternatives_are_bounded_and_use_original_source(setup, monkeypatch):
    net, a, _ = setup
    fn = net.claim_row(a, 'game', 'f')
    net._record(fn, a, 10, 'int f(){return 1;}', 1)
    aid = net.store.one('SELECT MAX(id) id FROM attempts')['id']
    seen = []
    monkeypatch.setattr(diagnosis, 'trial', lambda n,a,f,s,h,p: seen.append(s) or s)
    diagnosis.alternatives(net,a,'f',aid,'return 1;', ['return 2;', 'return 3;'], 'constant test','game')
    assert seen == ['int f(){return 2;}', 'int f(){return 3;}']
    with pytest.raises(NetworkError):
        diagnosis.alternatives(net,a,'f',aid,'return 1;', ['x'] * 9, 'test','game')


def test_concurrent_duplicate_does_not_start_second_compile(setup, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    net, a, _ = setup
    net.claim_row(a, 'game', 'f')
    started, finish = threading.Event(), threading.Event()
    def compile_(*args):
        started.set()
        assert finish.wait(5)
        return 'compile failed, retained'
    monkeypatch.setattr(net, 'compile_and_diff', compile_)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(diagnosis.trial, net, a, 'f', 'source', 'one test', 'game')
        assert started.wait(5)
        try:
            second = diagnosis.trial(net, a, 'f', 'source', 'same test', 'game')
            assert 'Duplicate avoided' in second and '(running)' in second
        finally:
            finish.set()
        first.result()
    assert net.store.one('SELECT COUNT(*) n FROM hypothesis_trials')['n'] == 1


def test_no_score_with_many_failures_is_still_strong():
    assert tier_for({'best_score':None, 'n_insns':20, 'attempts':40})[2] == 'escalated'


def test_new_review_reopens_an_exhausted_diagnosis(setup, monkeypatch):
    net, a, _ = setup
    fn=net._function('f','game')
    net._record(fn,a,10,fn['best_source'],1)
    aid=net.store.one('SELECT MAX(id) id FROM attempts')['id']
    diagnosis.record(net,a,'f','stack','stack offset','new evidence',True,'game')
    assert not diagnosis.eligible(net,fn)
    monkeypatch.setattr(net,'_compile',lambda *a: SimpleNamespace(ok=True,obj=b''))
    monkeypatch.setattr(net,'_evaluate',lambda f,p,o:(net._target_obj(f,p),False,[]))
    diagnosis.review(net,a,'f',aid,'plausible','initialization reviewed','game')
    assert diagnosis.eligible(net,fn)


def test_exhausted_named_claim_requires_explicit_hypothesis(setup, monkeypatch):
    net,a,_=setup
    diagnosis.record(net,a,'f','stack','offset remains','new source lifetime evidence',True,'game')
    fn=net.claim_row(a,'game','f')
    with pytest.raises(NetworkError,match='exhausted'):
        net.attempt(a,'f','int f(){return 1;}','game')
    def compile_(agent,function,source,project):
        diagnosis.require_hypothesis(net,fn,agent,source)
        return 'permitted explicit experiment'
    monkeypatch.setattr(net,'compile_and_diff',compile_)
    assert 'permitted' in diagnosis.trial(net,a,'f','int f(){return 1;}','measure alternate lifetime','game')


def test_diagnosis_workflow_over_real_mcp_http(setup, monkeypatch):
    import asyncio
    import socket
    import threading
    import time
    import httpx2
    import uvicorn
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from decompnet.app.tools import build_mcp

    net,a,_=setup
    monkeypatch.setattr(net,'resolve_function',lambda agent,slug,name:name,raising=False)
    monkeypatch.setattr(net,'fork_slug',lambda slug:None,raising=False)
    def compile_(agent,function,source,project):
        fn=net._function(function,project)
        diagnosis.require_hypothesis(net,fn,agent,source)
        net._record(fn,agent,20,source,1)
        return 'score 20'
    monkeypatch.setattr(net,'compile_and_diff',compile_)
    mcp=build_mcp(net,lambda ctx:(a,'game'))
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(host='127.0.0.1'),log_level='error'))
    thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
    async def run():
        async with httpx2.AsyncClient(timeout=10) as http:
            async with Client(streamable_http_client(f'http://127.0.0.1:{port}/mcp',http_client=http)) as client:
                async def call(name,args):
                    r=await client.call_tool(name,args)
                    assert not r.is_error, r
                    return ''.join(c.text for c in r.content if getattr(c,'text',None))
                await call('record_diagnosis',{'function':'f','category':'stack','evidence':'two offsets',
                                              'next_hypothesis':'new frame lifetime test','exhausted':True})
                await call('claim_function',{'function':'f'})
                args={'function':'f','source':'int f(){return 2;}','hypothesis':'one lifetime test'}
                assert 'score 20' in await call('test_hypothesis',args)
                assert 'Duplicate avoided' in await call('test_hypothesis',args)
                assert '"id"' in await call('inspect_candidate',{'function':'f'})
                await call('release_function',{'function':'f','notes':'bounded test complete'})
    try:
        deadline=time.monotonic()+5
        while not server.started:
            assert time.monotonic()<deadline
            time.sleep(.01)
        asyncio.run(run())
    finally:
        server.should_exit=True;thread.join(timeout=5);sock.close()
    assert not thread.is_alive()
    assert net.store.one('SELECT COUNT(*) n FROM claims')['n']==0


def test_completed_trial_replays_after_claim_release(setup, monkeypatch):
    net,a,_=setup
    net.claim_row(a,'game','f')
    monkeypatch.setattr(net,'compile_and_diff',lambda *a:'score 20')
    diagnosis.trial(net,a,'f','candidate','test','game')
    net.release(a,'f','finished','game')
    assert 'Duplicate avoided' in diagnosis.trial(net,a,'f','candidate','retry after lost response','game')


def test_local_template_lane_leaves_repeated_stalls_to_strong_agents(setup):
    net,a,_=setup
    with net.store.tx() as db:
        db.execute("UPDATE functions SET attempts=40 WHERE name='f'")
    assert net.claim_row(a,'game',strategy='templates')['name']=='g'
    assert diagnosis.eligible(net,net._function('f','game'),'strong')


def test_unrelated_regions_do_not_reopen_legacy_exhaustion(setup):
    net, a, _ = setup
    with net.store.tx() as db:
        db.execute('UPDATE functions SET vram=4096 WHERE name=\'f\'')
    diagnosis.record(net,a,'f','stack','measured spill','new compiler evidence',True,'game')
    row = diagnosis.latest(net,net._function('f','game'))
    # Legacy records included all regions. Its basis is retained unchanged.
    details = json.loads(row['details'])
    details['regions'] = [{'from':8192,'to':12288,'toolchain':'unrelated'}]
    env = diagnosis.digest(json.dumps(details,sort_keys=True))
    fn = net._function('f','game')
    old_basis = diagnosis.digest(env + diagnosis.digest(fn['best_source']) + 'None')
    with net.store.tx() as db:
        db.execute('UPDATE function_diagnoses SET details=?,environment=?,basis=? WHERE id=?',
                   (json.dumps(details),env,old_basis,row['id']))
        cfg = {'compiler':{'flags':['-O2']},'regions':[{'from':8192,'to':12288,'toolchain':'changed'}]}
        db.execute('UPDATE projects SET config=? WHERE id=1',(json.dumps(cfg),))
    assert not diagnosis.eligible(net,fn)
    assert diagnosis.invalidation_reasons(net,fn) == []
    cfg['regions'].append({'from':4096,'to':4100,'toolchain':'relevant'})
    with net.store.tx() as db:
        db.execute('UPDATE projects SET config=? WHERE id=1',(json.dumps(cfg),))
    assert diagnosis.invalidation_reasons(net,fn) == ['regions']
    assert 'Invalidated by: regions' in '\n'.join(diagnosis.brief(net,fn))


def test_incumbent_change_explained_and_budget_enforced(setup):
    from decompnet.runtime_workflow import require_budget, priorities
    from pathlib import Path
    net,a,_ = setup
    diagnosis.record(net,a,'f','stack','spill','investigate',True,'game')
    with net.store.tx() as db:
        db.execute("UPDATE functions SET best_source='new source' WHERE name='f'")
    fn = net._function('f','game')
    assert diagnosis.invalidation_reasons(net,fn) == ['incumbent_or_review']
    proj = net._project('game')
    (Path(proj['root'])/'runtime-workflow.json').write_text(json.dumps({'blockers':[{
        'function':'f','lane':'exact','milestone':'start','evidence':'run.log','next_hypothesis':'spill',
        'max_attempts':2,'attempts_at_start':0}]}))
    assert priorities(net,proj) == ['f']
    require_budget(net,fn)
    with net.store.tx() as db:
        db.execute("UPDATE functions SET attempts=2 WHERE name='f'")
    assert priorities(net,proj) == []
    with pytest.raises(NetworkError,match='budget exhausted'):
        require_budget(net,net._function('f','game'))


def test_permuters_search_exhausted_allocation_but_not_stack(setup):
    """An exhausted diagnosis keeps agents out, not the permuters: SLUS-01252's last 186 open functions waited on
    each other, agents with nothing to claim and the permuter given 2 functions a batch (2026-11-11)."""
    net, a, _ = setup
    diagnosis.record(net, a, 'f', 'allocation', 'two registers swapped', 'register lifetimes', True, 'game')
    diagnosis.record(net, a, 'g', 'stack', 'one frame offset', 'frame layout', True, 'game')
    f, g = net._function('f', 'game'), net._function('g', 'game')
    assert not diagnosis.eligible(net, f)  # agents wait for new evidence
    assert diagnosis.for_permuter(net, f) and not diagnosis.for_permuter(net, g)


def test_a_helper_learns_there_is_nothing_to_claim_without_claiming(setup):
    net, a, b = setup
    for name in ('f', 'g'):
        diagnosis.record(net, a, name, 'allocation', 'registers', 'a permuter run', True, 'game')
    assert not net.can_claim(b, 'game')
    with net.store.tx() as db:  # new evidence: a changed header reopens both
        db.execute("INSERT INTO context_versions(project_id,version,header,unit) VALUES(1,2,'typedef long s32;','')")
    assert net.can_claim(b, 'game')
    assert net.store.one('SELECT COUNT(*) AS n FROM claims')['n'] == 0  # only asked
