import asyncio
from datetime import date,timedelta
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock
import httpx
import pytest
from src.services import briefing_publish as m

DAY='2026-11-07'
REPORT='# 工作实践\n\n公开原文与任务。'
ITEM={'category':'前端','title':'用AI审查组件变更','why_it_matters':'适合已有页面项目，帮助发现组件变更中的回归问题。','next_step':'拿一个小组件差异做审查，逐项核对文件行号和真实缺陷。','evidence_limit':'官方文档，尚未实测'}


def preview(root,**overrides):
    (root/'data').mkdir(exist_ok=True)
    result={'version':m.VERSION,'date':DAY,'report_sha':m.digest(REPORT),'items':[ITEM],**overrides}
    (root/'data/wechat-preview.json').write_text(json.dumps(result,ensure_ascii=False))
    return result


@pytest.mark.parametrize('change',[{'date':'2026-11-06'},{'version':0},{'report_sha':'a'*64},{'items':[]}])
def test_stale_or_invalid_preview_refused(tmp_path,change):
    preview(tmp_path,**change)
    with pytest.raises(ValueError):m.load_preview(tmp_path,DAY,REPORT)


def test_matching_cache_reuses_exact_message_and_keeps_code_off_wechat(tmp_path,monkeypatch):
    preview(tmp_path)
    factory=AsyncMock();monkeypatch.setattr(m,'create_ai_client',factory)
    result=asyncio.run(m.prepare_presentation(tmp_path,DAY,REPORT,None))
    factory.assert_not_called()
    assert '【前端】' in result['message'] and '本期未选：产品、运营、AI' in result['message']
    assert result['url'].endswith(m.digest(REPORT)[:12]+'.html')
    assert (tmp_path/'data/pages-site/index.html').exists()
    assert list((tmp_path/'data/pages-site').glob('*.json'))==[]


def test_real_prepare_path_with_fake_model_rejects_invented_categories(tmp_path,monkeypatch):
    (tmp_path/'data').mkdir()
    fake=type('Client',(),{'complete':AsyncMock(return_value=json.dumps({'items':[{**ITEM,'category':'投资'}]}))})()
    monkeypatch.setattr(m,'create_ai_client',lambda _:fake)
    with pytest.raises(ValueError):asyncio.run(m.prepare_presentation(tmp_path,DAY,REPORT,None))
    assert not (tmp_path/'data/wechat-preview.json').exists()


@pytest.mark.parametrize('name',['credentials.json','.env','unrelated.html','2026-11-07.html'])
def test_public_staging_rejects_any_unexpected_file(tmp_path,name):
    path=tmp_path/'data/public-briefings';path.mkdir(parents=True)
    (path/name).write_text('must not be uploaded')
    with pytest.raises(ValueError):m.stage_public_site(tmp_path,DAY)


def test_public_staging_keeps_recent_archive_and_refuses_symlink(tmp_path):
    path=tmp_path/'data/public-briefings';path.mkdir(parents=True)
    for age in (0,1,89,90):
        (path/f'{date.fromisoformat(DAY)-timedelta(days=age)}-aaaaaaaaaaaa.html').write_text('safe')
    output=m.stage_public_site(tmp_path,DAY)
    assert len(list(output.iterdir()))==3
    (path/'2026-11-07-bbbbbbbbbbbb.html').symlink_to(tmp_path/'data/wechat-preview.json')
    with pytest.raises(ValueError):m.stage_public_site(tmp_path,DAY)


@pytest.mark.parametrize('status,body,passes',[(200,b'expected',True),(404,b'expected',False),(200,b'old page',False)])
def test_public_link_requires_matching_content(tmp_path,monkeypatch,status,body,passes):
    data=preview(tmp_path)
    data['url']=m.detail_url(DAY,data['report_sha'])
    path=tmp_path/'data/public-briefings';path.mkdir()
    (path/f'{DAY}-{data["report_sha"][:12]}.html').write_bytes(b'expected')
    transport=httpx.MockTransport(lambda request:httpx.Response(status,content=body))
    original=httpx.AsyncClient
    monkeypatch.setattr(m.httpx,'AsyncClient',lambda **kw:original(transport=transport,**kw))
    call=m.verify_published_detail(tmp_path,data,attempts=1,pause=0)
    if passes:asyncio.run(call)
    else:
        with pytest.raises(RuntimeError,match='refusing to send'):asyncio.run(call)
