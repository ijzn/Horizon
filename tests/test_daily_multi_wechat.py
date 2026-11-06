"""Manual/09:00 entry must not skip friends added after an earlier delivery."""
import asyncio
import importlib.util
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src.models import WeChatConfig
from src.services.wechat_subscribers import SubscriberDelivery


@pytest.fixture
def daily(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # ensure the import's cwd mutation is restored
    module_path = Path(__file__).resolve().parents[1] / 'daily-local.py'
    spec = importlib.util.spec_from_file_location('daily_multi_test', module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    config = SimpleNamespace(wechat=WeChatConfig(enabled=True, languages=['zh']))
    monkeypatch.setattr(module, 'StorageManager', lambda: SimpleNamespace(load_config=lambda: config))
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
    (tmp_path / 'data/summaries').mkdir(parents=True)
    (tmp_path / 'data/ready.json').write_text(json.dumps({'date':today}))
    (tmp_path / f'data/summaries/horizon-{today}-zh.md').write_text('# 同一份日报')
    return module, today


def test_existing_channel_ledger_does_not_skip_new_friend(monkeypatch, tmp_path, daily):
    module, today = daily
    (tmp_path / f'data/delivery-{today}.json').write_text('{"wechat":"sent","feishu":"sent"}')
    calls = []

    class Store:
        def __init__(self, directory):
            assert directory == tmp_path / 'data'

        async def broadcast(self, summary, config, **kw):
            assert kw['subscriber_ids'] == ['owner']
            calls.append(summary)
            return [SubscriberDelivery('owner', '自己', 'duplicate'), SubscriberDelivery('a' * 12, '新朋友', 'sent', True, 1)]

    monkeypatch.setattr(module, 'SubscriberStore', Store)
    asyncio.run(module.main('deliver'))
    assert calls == ['# 同一份日报']


def test_failed_recipient_is_reported_after_others_succeed(monkeypatch, daily):
    module, _ = daily

    class Store:
        def __init__(self, directory):
            pass

        async def broadcast(self, *a, **kw):
            return [SubscriberDelivery('owner', '自己', 'sent', True, 1), SubscriberDelivery('a' * 12, '朋友', 'failed')]

    monkeypatch.setattr(module, 'SubscriberStore', Store)
    with pytest.raises(RuntimeError, match='Some WeChat readers'):
        asyncio.run(module.main('deliver'))


def test_pipeline_partial_delivery_does_not_broadcast_another_error_to_everyone(monkeypatch):
    from io import StringIO
    from unittest.mock import AsyncMock
    from rich.console import Console
    from src.orchestrator import HorizonOrchestrator
    from src.services.wechat_subscribers import WeChatPartialDeliveryError

    orchestrator = object.__new__(HorizonOrchestrator)
    orchestrator.console = Console(file=StringIO())
    orchestrator.email_manager = None
    orchestrator.webhook_notifier = None
    orchestrator.config = SimpleNamespace(email=None, collection=SimpleNamespace(time_window_hours=24))
    notify_failure = AsyncMock()
    orchestrator.wechat_notifier = SimpleNamespace(send_failure=notify_failure)
    error = WeChatPartialDeliveryError([SubscriberDelivery('owner', '自己', 'sent', True, 1), SubscriberDelivery('a' * 12, '朋友', 'failed')])

    async def partial(_):
        raise error

    monkeypatch.setattr(orchestrator, 'fetch_all_sources', partial)
    with pytest.raises(WeChatPartialDeliveryError):
        asyncio.run(orchestrator.run())
    notify_failure.assert_not_awaited()
    assert 'completed successfully' not in orchestrator.console.file.getvalue()
