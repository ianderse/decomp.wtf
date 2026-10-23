"""Classify every open incumbent without recording attempts or modifying the source database.
Usage: python docs/plans/slus-tail-inventory.py BASELINE_DB OUTPUT_DIR
"""
import json
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from decompnet import diagnosis, scoring
from decompnet.app.engine import RomNetwork

baseline, out = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
out.mkdir(parents=True, exist_ok=False)
with sqlite3.connect(f'file:{baseline}?mode=ro', uri=True) as src, sqlite3.connect(out/'inventory.db') as dst:
    src.backup(dst)
net = RomNetwork(out/'inventory.db', out)
proj = net._project('slus-01252')
rows = net.store.all("SELECT * FROM functions WHERE project_id=? AND status='open' ORDER BY id", proj['id'])
started = time.time()

def one(fn):
    result = {'id':fn['id'],'name':fn['name'],'source_hash':diagnosis.digest(fn['best_source'] or ''),
              'environment':diagnosis.environment(net,fn)[0], 'bytes':fn['size'],
              'attempts':fn['attempts'],'abandons':fn['abandons'],'old_score':fn['best_score']}
    try:
        header=net._context(proj['id'],fn['unit'])['header']
        res=net._compile(proj,header,fn['best_source'],fn)
        if not res.ok:
            return result | {'error':res.log}
        candidate,verdict,problems=net._evaluate(fn,proj,res.obj)
        if candidate is None: return result | {'error':'missing function in object'}
        d=scoring.diff(net._target_obj(fn,proj),candidate)
        d=scoring.DiffResult(0 if verdict else max(1,d.score),d.max_score,d.rows,bool(verdict))
        lane,counts=diagnosis.classify(d,problems)
        if 0x8016A000 <= fn['vram'] < 0x80172000: lane='library'
        result.update(category=lane,counts=dict(counts),score=d.score,matched=d.bytes_match,problems=problems)
        (out/(fn['name']+'.diff.txt')).write_text(d.render(max_rows=2000))
    except Exception as e:
        result['error']=type(e).__name__+': '+str(e)
    return result

with ThreadPoolExecutor(max_workers=4) as pool:
    measured=list(pool.map(one,rows))
result={'started_at':started,'elapsed':time.time()-started,'functions':len(rows),
        'lanes':dict(Counter(r.get('category','error') for r in measured)), 'rows':measured}
(out/'inventory.json').write_text(json.dumps(result,indent=2))
print(json.dumps({k:v for k,v in result.items() if k!='rows'}))
