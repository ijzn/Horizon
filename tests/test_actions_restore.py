import base64
import importlib.util
import io
import json
from pathlib import Path
import zipfile
import pytest

spec = importlib.util.spec_from_file_location('actions_restore', Path(__file__).parents[1] / 'scripts/actions_restore.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(module, 'OUTPUT', tmp_path/'state.enc')
    monkeypatch.setenv('GH_REPO', 'ijzn/Horizon')
    monkeypatch.setenv('EXPECTED_WORKFLOW', '.github/workflows/personal-wechat.yml')
    monkeypatch.setenv('BOOTSTRAP', 'false')
    return module.OUTPUT


def test_missing_artifact_never_silently_uses_bootstrap(monkeypatch, tmp_path):
    out=setup(monkeypatch,tmp_path)
    monkeypatch.setenv('HORIZON_BOOTSTRAP_STATE', base64.b64encode(b'old').decode())
    monkeypatch.setattr(module,'api',lambda _: json.dumps({'artifacts':[]}).encode())
    with pytest.raises(RuntimeError,match='explicit manual bootstrap'):module.main()
    assert not out.exists()


def test_explicit_bootstrap_only(monkeypatch,tmp_path):
    out=setup(monkeypatch,tmp_path)
    monkeypatch.setenv('BOOTSTRAP','true')
    monkeypatch.setenv('HORIZON_BOOTSTRAP_STATE',base64.b64encode(b'encrypted').decode())
    module.main()
    assert out.read_bytes()==b'encrypted'
    assert out.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('expired,broken',[(True,False),(False,True)])
def test_newest_state_failures_do_not_fall_back(monkeypatch,tmp_path,expired,broken):
    out=setup(monkeypatch,tmp_path)
    def api(path):
        if '/actions/artifacts?' in path:return json.dumps({'artifacts':[{'id':2,'created_at':'2026-11-07T08:55:24Z','expired':expired,'workflow_run':{'id':8}},{'id':1,'created_at':'2026-11-07T08:32:56Z','expired':False,'workflow_run':{'id':7}}]}).encode()
        if '/actions/runs/' in path:return json.dumps({'path':'.github/workflows/personal-wechat.yml','head_branch':'main'}).encode()
        raise OSError('download failed')
    monkeypatch.setattr(module,'api',api)
    with pytest.raises((OSError,RuntimeError)):module.main()
    assert not out.exists()


def test_wrong_workflow_ignored_and_latest_valid_restored(monkeypatch,tmp_path):
    out=setup(monkeypatch,tmp_path)
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:z.writestr('horizon-state.enc',b'new-state')
    def api(path):
        if '/actions/artifacts?' in path:return json.dumps({'artifacts':[{'id':3,'created_at':'2026-11-07T08:55:24Z','expired':False,'workflow_run':{'id':9}},{'id':2,'created_at':'2026-11-07T08:32:56Z','expired':False,'workflow_run':{'id':8}}]}).encode()
        if path.endswith('/9'):return json.dumps({'path':'other.yml','head_branch':'main'}).encode()
        if path.endswith('/8'):return json.dumps({'path':'.github/workflows/personal-wechat.yml','head_branch':'main'}).encode()
        assert '/2/zip' in path
        return b.getvalue()
    monkeypatch.setattr(module,'api',api)
    module.main()
    assert out.read_bytes()==b'new-state'


def test_creation_time_wins_over_larger_older_id(monkeypatch,tmp_path):
    out=setup(monkeypatch,tmp_path)
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:z.writestr('horizon-state.enc',b'latest-by-time')
    def api(path):
        if '/actions/artifacts?' in path:return json.dumps({'artifacts':[
            {'id':13567586925,'created_at':'2026-11-07T08:32:56Z','expired':False,'workflow_run':{'id':1}},
            {'id':13567584889,'created_at':'2026-11-07T08:55:24Z','expired':False,'workflow_run':{'id':2}},
        ]}).encode()
        if '/actions/runs/' in path:return json.dumps({'path':'.github/workflows/personal-wechat.yml','head_branch':'main'}).encode()
        assert path.endswith('/13567584889/zip')
        return b.getvalue()
    monkeypatch.setattr(module,'api',api)
    module.main()
    assert out.read_bytes()==b'latest-by-time'
