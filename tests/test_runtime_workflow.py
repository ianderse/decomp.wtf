import json
import sqlite3

import pytest

from decompnet.runtime_workflow import evaluate, freeze, run_scenario


def test_runtime_checks_never_turn_survival_into_visual_acceptance():
    s = {'min_vblanks':100, 'required_reads':['SG00.STG']}
    log = 'psx: 200 frames (vertical blanks)\ncd: CdRead 2 sectors (4096 bytes) from LBA 10 (SG00.STG +0x0)\n'
    assert evaluate(s,log,124,False,['screen.png'])['status'] == 'needs_visual_review'
    for code,forced,shots in [(139,False,['x']), (124,True,['x']), (124,False,[])]:
        assert evaluate(s,log,code,forced,shots)['status'] == 'failed'
    assert evaluate(s,log.replace('SG00.STG','OTHER.STG'),124,False,['x'])['status'] == 'failed'
    assert evaluate(s,log+'psx: segmentation fault at 0\n',124,False,['x'])['status'] == 'failed'


def test_snapshot_includes_wal_and_refuses_overwrite(tmp_path):
    source = tmp_path/'live.db'
    c=sqlite3.connect(source)
    c.execute('pragma journal_mode=wal')
    c.execute('create table evidence(value)')
    c.execute('insert into evidence values(42)')
    c.commit()
    target=tmp_path/'frozen.db'
    freeze(source,target)
    with sqlite3.connect(target) as frozen:
        assert frozen.execute('select value from evidence').fetchone()[0] == 42
    with pytest.raises(FileExistsError): freeze(source,target)
    c.close()


def test_actual_subprocess_isolated_card_and_raw_capture(tmp_path,monkeypatch):
    root=tmp_path/'game'
    port=root/'port'
    (port/'build').mkdir(parents=True)
    exe=port/'build'/'game'
    exe.write_text('#!/usr/bin/env python3\nimport os,pathlib\n'
                   'assert "PSX_MEMCARD" not in os.environ\n'
                   'pathlib.Path(os.environ["PSX_PORT_DUMP_SCREEN"]).write_bytes(b"capture")\n'
                   'print("psx: 120 frames (vertical blanks)")\n')
    exe.chmod(0o755)
    (port/'port.json').write_text('{}')
    (root/'runtime-workflow.json').write_text(json.dumps({'scenarios':{'title':{'seconds':1,'min_vblanks':100}}}))
    monkeypatch.setenv('PSX_MEMCARD','user-save.mcd')
    out=tmp_path/'evidence'
    r=run_scenario(root,'title',out)
    assert r['status']=='needs_visual_review'
    assert r['screenshots'] and (out/'inputs.json').exists()
    with pytest.raises(FileExistsError): run_scenario(root,'title',out)


def test_reviews_bound_to_capture_and_failed_checks_cannot_be_accepted(tmp_path):
    from decompnet.runtime_workflow import review_run,sha
    screen=tmp_path/'screen.png';screen.write_bytes(b'original')
    r={'status':'failed','screenshots':[{'path':'screen.png','sha256':sha(screen)}]}
    result=tmp_path/'result.json';result.write_text(json.dumps(r))
    with pytest.raises(ValueError,match='failed runtime'):review_run(tmp_path,'accepted','title visible','reviewer')
    r['status']='needs_visual_review';result.write_text(json.dumps(r))
    screen.write_bytes(b'changed')
    with pytest.raises(ValueError,match='Capture changed'):review_run(tmp_path,'accepted','title visible','reviewer')
    screen.write_bytes(b'original')
    assert review_run(tmp_path,'accepted','title visible','reviewer')['result_sha256']==sha(result)
    with pytest.raises(FileExistsError):review_run(tmp_path,'accepted','again','reviewer')


def test_export_scenarios_require_build():
    from decompnet.app.cli import main
    with pytest.raises(SystemExit) as error:main(['port','game','--scenarios'])
    assert error.value.code==2
