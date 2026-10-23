"""Bounded SLUS hard-tail pilot against a disposable DB copy, through real MCP/HTTP.
Usage: python docs/plans/slus-tail-pilot.py BASELINE_DB OUTPUT_DIR
Never opens the live DB for writing. Keeps all inputs/results and compiler intermediate dumps.
"""
import asyncio
import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from decompnet import diagnosis, scoring, toolchains
from decompnet.app.engine import RomNetwork
from decompnet.app.tools import build_mcp
from decompnet.compiler import block_comments
from decompnet.project import compiler_config, with_toolchain
from decompnet.psx_gte import header_path

NAMES = ['func_800E4384', 'func_800E6C90', 'func_80118998', 'func_80161C0C',
         'func_80161770', 'func_800F64A4', 'func_801672F0', 'func_80149820',
         'func_8010D420', 'func_8016BFEC', 'func_8016DAE0', 'func_8016F558']


def trace(net, proj, fn, source, out):
    out.mkdir(parents=True)
    cfg = json.loads(proj['config'])
    if net.fn_toolchain(fn):
        cfg['compiler'] = with_toolchain(cfg['compiler'], net.fn_toolchain(fn))
    cc = compiler_config(Path(proj['root']), cfg)
    header = net._context(proj['id'], fn['unit'])['header']
    if not cc.line_comments:
        header, source = block_comments(header), block_comments(source)
    (out/'context.h').write_text(header)
    shutil.copyfile(header_path(), out/'psx_gte.h')
    (out/'input.c').write_text('#include "psx_gte.h"\n#include "context.h"\n'+source)
    argv = [a.replace('{src}', str(out/'input.c')).replace('{obj}', str(out/'candidate.o'))
            .replace('{include}', str(out)) for a in cc.cmd] + ['-da']
    (out/'command.json').write_text(json.dumps(argv, indent=2))
    r = subprocess.run(argv, cwd=out, capture_output=True, text=True, errors='replace', timeout=40)
    (out/'stdout.txt').write_text(r.stdout); (out/'stderr.txt').write_text(r.stderr)
    result = {'returncode': r.returncode, 'files': sorted(p.name for p in out.iterdir())}
    if r.returncode == 0:
        candidate, verdict, problems = net._evaluate(fn, proj, (out/'candidate.o').read_bytes())
        d = scoring.diff(net._target_obj(fn, proj), candidate)
        result.update(score=0 if verdict else max(1,d.score), problems=problems)
        (out/'diff.txt').write_text(d.render(max_rows=2000))
    (out/'result.json').write_text(json.dumps(result,indent=2))
    return result


async def main(baseline, out):
    resume = out.exists()
    out.mkdir(parents=True, exist_ok=True)
    if not resume:
        db = sqlite3.connect(out/'pilot.db')
        with sqlite3.connect(f'file:{baseline}?mode=ro', uri=True) as src:
            src.backup(db)
        db.close()
    net = RomNetwork(out/'pilot.db', out)
    net.on_match = lambda *a: None  # no twin search: this pilot's fixed denominator is twelve
    net._alternates_on = lambda *a: False  # one exact current recipe per hypothesis
    with net.store.tx() as db:
        db.execute('DELETE FROM claims')  # only the disposable snapshot
    agent, token = net.local_agent('tail-pilot')
    def auth(ctx):
        return net.authenticate((ctx.headers or {}).get('authorization','').removeprefix('Bearer ')), 'slus-01252'
    mcp = build_mcp(net, auth)
    app = mcp.streamable_http_app(host='127.0.0.1')
    sock = socket.socket(); sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app,log_level='error'))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]),daemon=True); thread.start()
    deadline=time.monotonic()+15
    while not server.started:
        if time.monotonic()>deadline: raise RuntimeError('HTTP server startup timeout')
        await asyncio.sleep(.02)
    log=json.loads((out/'transport.json').read_text()) if resume else []
    rows=[]; proj=net._project('slus-01252')
    completed={x['args']['function'] for x in log if x['tool']=='release_function' and not x['error']}
    baseconn=sqlite3.connect(f'file:{baseline}?mode=ro',uri=True);baseconn.row_factory=sqlite3.Row
    manifest={'functions':NAMES,'max_unique_tool_trials':24,'max_duplicate_replays':12,
              'trace_compiles':4,'baseline':str(baseline),'started_at':time.time()}
    if not resume: (out/'manifest.json').write_text(json.dumps(manifest,indent=2))
    try:
        async with httpx2.AsyncClient(headers={'Authorization':f'Bearer {token}'},timeout=90) as http:
            async with Client(streamable_http_client(f'http://127.0.0.1:{port}/mcp',http_client=http)) as client:
                async def call(name,args):
                    r=await client.call_tool(name,args)
                    text=''.join(c.text for c in r.content if getattr(c,'text',None))
                    log.append({'tool':name,'args':args,'error':r.is_error,'result':text})
                    (out/'transport.json').write_text(json.dumps(log,indent=2))
                    if r.is_error: raise RuntimeError(text)
                    return text
                available={t.name for t in (await client.list_tools()).tools}
                assert {'test_hypothesis','record_diagnosis','review_candidate','test_alternatives'}<=available
                for name in NAMES:
                    fn=baseconn.execute('SELECT * FROM functions WHERE project_id=2 AND name=?',(name,)).fetchone(); source=fn['best_source']
                    if name in completed:
                        current=net._function(name,'slus-01252'); lane=diagnosis.latest(net,current)['category']
                        rows.append({'function':name,'lane':lane,'before':fn['best_score'],'after':current['best_score'],
                                     'bytes':fn['size'],'matched':current['status']=='matched'})
                        continue
                    await call('claim_function',{'function':name})
                    await call('test_hypothesis',{'function':name,'source':source,'hypothesis':'Reproduce saved incumbent under frozen current inputs'})
                    await call('test_hypothesis',{'function':name,'source':source,'hypothesis':'Deliberate duplicate: verify no recompilation'})
                    latest=diagnosis.latest(net,net._function(name,'slus-01252'))
                    lane=latest['category'] if latest else 'structural'
                    aid=net.store.one('SELECT MAX(id) id FROM attempts WHERE function_id=?',fn['id'])['id']
                    # One bounded intervention per function. These probes do not assert original-source types.
                    if name=='func_800E4384':
                        old='volatile s32 reserved[2];'; replacement='volatile s32 reserved[2] __attribute__((aligned(8)));'
                        hypothesis='Test explicit eight-byte alignment of the existing reserve without changing its size'
                    elif name=='func_80161C0C':
                        old='s32 pad[2];'; replacement='s32 pad[2] __attribute__((aligned(8)));'
                        hypothesis='Test alignment of existing stack reserve without changing its size'
                    elif name=='func_8016F558':
                        old='s32 index,i,j,mask,offset,stride,count,count2,limit,bit,one;'
                        replacement='s32 index,i,j,mask,offset,stride,count,count2,limit,bit; register s32 one;'
                        hypothesis='Test explicit register lifetime hint for clear-loop constant one'
                    elif name=='func_800E6C90':
                        old='(i % 17)'; replacement='(i - (i / 17) * 17)'
                        hypothesis='Test quotient sharing by expressing remainder from the same signed quotient'
                    elif name=='func_80149820':
                        clean=net.store.one('SELECT source FROM attempts WHERE id=141255 AND function_id=?',fn['id'])
                        if not clean: raise RuntimeError('Frozen reviewed candidate 141255 missing')
                        old=source; replacement=clean['source']; lane='structural'
                        hypothesis='Reproduce the documented candidate with initialized joins and distinct x/y products'
                    else:
                        import re
                        # Declaration-order intervention only, with the exact region retained in the transport record.
                        matches=list(re.finditer(r'(?m)^([ \t]+)(s32|u32|s16|u16|u8) ([A-Za-z_]\w*);$',source))
                        if not matches: raise RuntimeError(f'No simple declaration probe for {name}; stop instead of inventing one')
                        m=matches[0]; old=m.group(); replacement=m[1]+'register '+m[2]+' '+m[3]+';'
                        hypothesis='Test register declaration for '+m[3]+'; no semantic or original-source inference'
                    await call('test_alternatives',{'function':name,'attempt_id':aid,'old':old,
                               'replacements':[replacement],'hypothesis':hypothesis})
                    if 0x8016A000 <= fn['vram'] < 0x80172000: lane='library'
                    after=net._function(name,'slus-01252')
                    await call('record_diagnosis',{'function':name,'category':lane,
                        'evidence':f'Frozen baseline score {fn["best_score"]}; after one bounded probe best {after["best_score"]}. See retained hypothesis trials.',
                        'next_hypothesis':diagnosis.LANES[lane], 'exhausted':False})
                    rows.append({'function':name,'lane':lane,'before':fn['best_score'],'after':after['best_score'],
                                 'bytes':fn['size'],'matched':after['status']=='matched'})
                    if after['status']=='open':
                        await call('release_function',{'function':name,'notes':'Bounded pilot complete; measurements retained in hypothesis trials. One probe does not exhaust this function.'})
                    (out/'progress.json').write_text(json.dumps(rows,indent=2))
                    print(json.dumps(rows[-1]),flush=True)
        # Diagnostic compiles preserve GCC's RTL passes and exact assembly pipeline.
        for name in NAMES[:2]:
            fn=net._function(name,'slus-01252')
            original=sqlite3.connect(f'file:{baseline}?mode=ro',uri=True).execute('SELECT best_source FROM functions WHERE id=?',(fn['id'],)).fetchone()[0]
            trace(net,proj,fn,original,out/'traces'/name/'baseline')
            variant=(original.replace('volatile s32 reserved[2];','',1) if name==NAMES[0]
                     else original.replace('(i % 17)','(i - (i / 17) * 17)',1))
            trace(net,proj,fn,variant,out/'traces'/name/'probe')
        result={'rows':rows,'matched_bytes':sum(r['bytes'] for r in rows if r['matched']),
                'improved_functions':sum(r['after']<r['before'] for r in rows),
                'unique_trials':net.store.one('SELECT COUNT(*) n FROM hypothesis_trials')['n'],
                'duplicates_avoided':net.store.one('SELECT SUM(duplicates) n FROM hypothesis_trials')['n'],
                'claims_left':net.store.one('SELECT COUNT(*) n FROM claims WHERE agent_id=?',agent.id)['n']}
        (out/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True)
    finally:
        server.should_exit=True;thread.join(timeout=10);sock.close()
        (out/'shutdown.json').write_text(json.dumps({'server_stopped':not thread.is_alive()}))


if __name__=='__main__':
    asyncio.run(main(Path(sys.argv[1]).resolve(),Path(sys.argv[2]).resolve()))
