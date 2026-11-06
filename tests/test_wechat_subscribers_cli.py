"""Offline user-flow checks: isolated QR binding and manual fan-out."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.models import WeChatConfig
from src.services import wechat_cli
from src.services import wechat_subscribers_cli as cli
from src.services.wechat_subscribers import SubscriberStore, SubscriberDelivery


def invoke(monkeypatch, data, *args):
    monkeypatch.setattr('sys.argv', ['horizon-wechat-users', '-d', str(data), *args])
    monkeypatch.setattr(cli, 'load_dotenv', lambda: None)
    return cli.main()


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setattr(cli, 'configured_wechat', lambda *a: WeChatConfig(enabled=True, languages=['zh']))


def test_independent_friend_login_preserves_owner_and_restores_qr_renderer(monkeypatch, tmp_path, config, capsys):
    owner = tmp_path / 'wechat_session.json'
    original_owner = '{"bot_token":"owner-secret","user_id":"owner-user","context_token":"owner-context"}'
    owner.write_text(original_owner)
    store = SubscriberStore(tmp_path)
    friend = store.add('朋友甲')
    renderer = wechat_cli._render_qr
    seen = []

    async def login(config, storage, force):
        seen.append((Path(storage.data_dir), force))
        wechat_cli._render_qr('https://example.com/opaque-qr-binding')
        Path(storage.data_dir, 'wechat_session.json').write_text('{"bot_token":"friend-secret"}')

    monkeypatch.setattr(wechat_cli, '_run_login', login)
    invoke(monkeypatch, tmp_path, 'login', friend.id)
    expected = tmp_path / 'wechat_subscribers' / friend.id
    assert seen == [(expected.resolve(), False)]
    assert owner.read_text() == original_owner
    images = list(expected.glob('login-*.png'))
    assert len(images) == 1 and images[0].read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
    assert images[0].stat().st_mode & 0o777 == 0o600
    assert wechat_cli._render_qr is renderer
    output = capsys.readouterr().out
    assert 'owner-secret' not in output and 'opaque-qr-binding' not in output


def test_list_redacts_tokens_context_and_platform_user(monkeypatch, tmp_path, capsys):
    (tmp_path / 'wechat_session.json').write_text(json.dumps({'bot_token':'secret-token', 'user_id':'private-wechat-id', 'context_token':'secret-context'}))
    invoke(monkeypatch, tmp_path, 'list', '--json')
    output = capsys.readouterr().out
    rows = json.loads(output)
    assert rows[0]['id'] == 'owner' and rows[0]['ready'] is True
    assert all(secret not in output for secret in ('secret-token', 'private-wechat-id', 'secret-context'))


def test_manual_preview_needs_no_ai_config_or_network(monkeypatch, tmp_path, capsys):
    store = SubscriberStore(tmp_path)
    reader = store.add('朋友乙')
    report = tmp_path / 'chosen.md'
    report.write_text('# 手选简报\n\n一条可直接转发的内容。')

    def forbidden(*a, **kw):
        raise AssertionError('Preview must not require credentials or contact a service')

    monkeypatch.setattr(cli, 'configured_wechat', forbidden)
    monkeypatch.setattr(cli, 'WeChatNotifier', forbidden)
    invoke(monkeypatch, tmp_path, 'send', '--file', str(report), '--to', reader.id, '--dry-run')
    output = capsys.readouterr().out
    assert '朋友乙' in output and '手选简报' in output and '未发送' in output
    assert not store.ledger_path.exists()


def test_manual_send_existing_file_forwards_targets_without_ai(monkeypatch, tmp_path, config, capsys):
    import src.ai.client
    monkeypatch.setattr(src.ai.client, 'create_ai_client', lambda *a, **kw: pytest.fail('Manual delivery must not initialize AI'))
    store = SubscriberStore(tmp_path)
    friend = store.add('朋友丙')
    report = tmp_path / 'old-briefing.md'
    report.write_text('# 之前生成的简报')
    calls = []

    async def broadcast(self, summary, config, **kwargs):
        calls.append((summary, kwargs))
        return [SubscriberDelivery(friend.id, friend.name, 'sent', True, 1)]

    monkeypatch.setattr(SubscriberStore, 'broadcast', broadcast)
    invoke(monkeypatch, tmp_path, 'send', '--file', str(report), '--to', friend.id, '--force')
    assert calls == [('# 之前生成的简报', {'subscriber_ids':[friend.id], 'lang':'zh', 'force':True})]
    assert 'sent' in capsys.readouterr().out


def test_partial_failure_has_nonzero_exit_and_keeps_other_results(monkeypatch, tmp_path, config, capsys):
    store = SubscriberStore(tmp_path)
    first, second = store.add('甲'), store.add('乙')

    async def broadcast(*a, **kw):
        return [SubscriberDelivery(first.id, first.name, 'sent', True, 1), SubscriberDelivery(second.id, second.name, 'failed', False, 1, 'partial_delivery_retry_may_duplicate')]

    monkeypatch.setattr(SubscriberStore, 'broadcast', broadcast)
    with pytest.raises(SystemExit) as exc:
        invoke(monkeypatch, tmp_path, 'test')
    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert 'sent' in output and 'failed' in output and 'partial_delivery' in output


def test_disabled_reader_cannot_be_rebound(monkeypatch, tmp_path, config):
    store = SubscriberStore(tmp_path)
    friend = store.add('暂停订阅')
    store.remove(friend.id)
    monkeypatch.setattr(wechat_cli, '_run_login', lambda *a: pytest.fail('Disabled reader must not be rebound'))
    with pytest.raises(SystemExit) as exc:
        invoke(monkeypatch, tmp_path, 'login', friend.id)
    assert exc.value.code == 1


def test_binding_error_does_not_leak_secret_and_restores_renderer(monkeypatch, tmp_path, config, capsys):
    friend = SubscriberStore(tmp_path).add('失败绑定')
    original = wechat_cli._render_qr

    async def failed(*a):
        raise RuntimeError('sensitive-login-token')

    monkeypatch.setattr(wechat_cli, '_run_login', failed)
    with pytest.raises(SystemExit):
        invoke(monkeypatch, tmp_path, 'login', friend.id)
    assert wechat_cli._render_qr is original
    assert 'sensitive-login-token' not in capsys.readouterr().err
