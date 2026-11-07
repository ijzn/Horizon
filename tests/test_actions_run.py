import hashlib
import importlib.util
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import pytest

spec=importlib.util.spec_from_file_location('actions_run',Path(__file__).parents[1]/'scripts/actions_run.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def setup(monkeypatch,tmp_path,stage,valid=False):
    monkeypatch.setattr(m,'ROOT',tmp_path);monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(m.sys,'argv',['actions_run.py',stage])
    (tmp_path/'profiles/personal-radar').mkdir(parents=True)
    (tmp_path/'profiles/personal-radar/config.public-web.json').write_text('{"ai":{"model":"PENDING_AUTH"}}')
    (tmp_path/'data/summaries').mkdir(parents=True)
    today=datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    report=tmp_path/f'data/summaries/horizon-{today}-zh.md'
    if valid:
        report.write_text('# Practice report')
        (tmp_path/'data/ready.json').write_text(json.dumps({'date':today,'sha256':hashlib.sha256(report.read_bytes()).hexdigest()}))
    return report,today


def test_verified_report_skips_model_generation(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path,'run',True);calls=[]
    monkeypatch.setattr(m.subprocess,'run',lambda args,**kw:calls.append(args[-1]))
    m.main();assert calls==['deliver']


def test_deliver_refuses_missing_report(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path,'deliver');calls=[]
    monkeypatch.setattr(m.subprocess,'run',lambda args,**kw:calls.append(args[-1]))
    with pytest.raises(RuntimeError,match='No verified report'):m.main()
    assert calls==[]


def test_hash_mismatch_regenerates_before_delivery(monkeypatch,tmp_path):
    report,today=setup(monkeypatch,tmp_path,'run',True);report.write_text('tampered');calls=[]
    def run(args,**kw):
        calls.append(args[-1])
        if args[-1]=='generate':
            report.write_text('fresh')
            (tmp_path/'data/ready.json').write_text(json.dumps({'date':today}))
    monkeypatch.setattr(m.subprocess,'run',run)
    m.main();assert calls==['generate','deliver']
    marker=json.loads((tmp_path/'data/ready.json').read_text());assert marker['sha256']==hashlib.sha256(b'fresh').hexdigest()


def test_generation_failure_does_not_deliver(monkeypatch,tmp_path):
    setup(monkeypatch,tmp_path,'run');calls=[]
    def fail(args,**kw):calls.append(args[-1]);raise RuntimeError('model failure')
    monkeypatch.setattr(m.subprocess,'run',fail)
    with pytest.raises(RuntimeError,match='model failure'):m.main()
    assert calls==['generate']
