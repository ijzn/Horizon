"""Multi-recipient isolation, durable deduplication and failure recovery."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.models import WeChatConfig
from src.services import wechat_subscribers as module
from src.services.wechat import WeChatDeliveryResult, WeChatDeliveryStatus, WeChatSession
from src.services.wechat_subscribers import SubscriberStore


CONFIG = WeChatConfig(enabled=True)


def run(coro):
    return asyncio.run(coro)


def bind(store, sid, user=None, context="context"):
    path = store.session_path(sid)
    path.parent.mkdir(exist_ok=True)
    WeChatSession(bot_token="fake-secret", user_id=user or sid, context_token=context).save(path)
    return path


def fake_transport(calls, results=None, delay=0):
    results = results or {}

    class FakeNotifier:
        def __init__(self, config, storage):
            self.path = Path(storage.data_dir) / "wechat_session.json"
            self.session = WeChatSession.load(self.path)

        async def send_daily_summary(self, summary, lang):
            if delay:
                await asyncio.sleep(delay)
            identity = self.session.user_id if self.session else "unbound"
            calls.append((identity, summary, lang))
            result = results.get(identity, WeChatDeliveryResult(WeChatDeliveryStatus.SUCCESS, chunks_sent=1))
            if isinstance(result, Exception):
                raise result
            return result

    return FakeNotifier


def test_legacy_owner_session_is_kept_and_new_sessions_are_isolated(tmp_path):
    legacy = tmp_path / "wechat_session.json"
    WeChatSession(bot_token="owner-secret", user_id="owner-user", context_token="old-context").save(legacy)
    before = legacy.read_bytes()
    store = SubscriberStore(tmp_path)
    friend = store.add("朋友 小李")
    assert [s.id for s in store.list()] == ["owner", friend.id]
    assert store.storage_for("owner").data_dir == tmp_path
    assert store.storage_for(friend.id).data_dir == tmp_path / "wechat_subscribers" / friend.id
    bind(store, friend.id)
    assert legacy.read_bytes() == before
    assert store.session_path(friend.id) != legacy
    descriptions = store.describe()
    assert descriptions[0]["ready"]
    assert descriptions[1]["session_present"]
    assert "secret" not in json.dumps(descriptions)
    assert "owner-user" not in json.dumps(descriptions)
    assert os.stat(store.root).st_mode & 0o777 == 0o700
    assert os.stat(store.roster_path).st_mode & 0o777 == 0o600
    assert os.stat(store.session_path(friend.id).parent).st_mode & 0o777 == 0o700


def test_pending_context_is_not_reported_ready(tmp_path):
    store = SubscriberStore(tmp_path)
    friend = store.add("待绑定")
    assert store.describe()[0]["ready"] is False
    bind(store, friend.id, context="")
    assert store.describe()[0]["session_present"]
    assert not store.describe()[0]["context_present"]
    assert not store.describe()[0]["ready"]


def test_broadcast_reuses_markdown_and_persists_dedup_per_actual_user(tmp_path):
    store = SubscriberStore(tmp_path)
    a, b = store.add("甲"), store.add("乙")
    bind(store, a.id)
    bind(store, b.id)
    calls = []
    transport = fake_transport(calls)
    first = run(store.broadcast("同一份日报", CONFIG, notifier_factory=transport))
    assert [r.status for r in first] == ["sent", "sent"]
    assert [r[1] for r in calls] == ["同一份日报", "同一份日报"]
    second = run(SubscriberStore(tmp_path).broadcast("同一份日报", CONFIG, notifier_factory=transport))
    assert [r.status for r in second] == ["duplicate", "duplicate"]
    assert len(calls) == 2
    assert os.stat(store.ledger_path).st_mode & 0o777 == 0o600
    raw = store.ledger_path.read_text()
    assert "fake-secret" not in raw and "同一份日报" not in raw
    assert a.id not in raw and b.id not in raw
    run(store.broadcast("同一份日报", CONFIG, force=True, notifier_factory=transport))
    assert len(calls) == 4
    run(store.broadcast("新的日报", CONFIG, notifier_factory=transport))
    assert len(calls) == 6


def test_failed_recipient_does_not_block_others_and_can_retry(tmp_path):
    store = SubscriberStore(tmp_path)
    a, b = store.add("失败"), store.add("成功")
    bind(store, a.id)
    bind(store, b.id)
    calls = []
    failure = WeChatDeliveryResult(WeChatDeliveryStatus.NETWORK_FAILURE, "secret detail")
    outcomes = run(store.broadcast("briefing", CONFIG, notifier_factory=fake_transport(calls, {a.id: failure})))
    assert [r.status for r in outcomes] == ["failed", "sent"]
    retry = run(store.broadcast("briefing", CONFIG, notifier_factory=fake_transport(calls)))
    assert [r.status for r in retry] == ["sent", "duplicate"]
    assert len(calls) == 3
    assert "secret detail" not in str(outcomes)


def test_partial_delivery_warning_and_exception_are_isolated(tmp_path):
    store = SubscriberStore(tmp_path)
    subscribers = [store.add(n) for n in ("部分", "异常", "成功")]
    for subscriber in subscribers:
        bind(store, subscriber.id)
    calls = []
    outcomes = run(store.broadcast("briefing", CONFIG, notifier_factory=fake_transport(calls, {
        subscribers[0].id: WeChatDeliveryResult(WeChatDeliveryStatus.PLATFORM_FAILURE, chunks_sent=1),
        subscribers[1].id: RuntimeError("token=never-print-this"),
    })))
    assert [r.status for r in outcomes] == ["failed", "failed", "sent"]
    assert outcomes[0].warning == "partial_delivery_retry_may_duplicate"
    assert "never-print-this" not in str(outcomes)
    assert len(calls) == 3


def test_disable_and_restore_preserve_session_and_delivery_history(tmp_path):
    store = SubscriberStore(tmp_path)
    friend = store.add("朋友")
    path = bind(store, friend.id)
    calls = []
    transport = fake_transport(calls)
    run(store.broadcast("briefing", CONFIG, notifier_factory=transport))
    store.remove(friend.id)
    assert path.exists()
    assert run(store.broadcast("briefing", CONFIG, notifier_factory=transport))[0].status == "disabled"
    store.set_enabled(friend.id, True)
    assert run(store.broadcast("briefing", CONFIG, notifier_factory=transport))[0].status == "duplicate"
    assert len(calls) == 1


@pytest.mark.parametrize("bad", ["../owner", "/tmp/a", "", "OWNER", "a" * 13, "12345678901g", "a/b", None])
def test_ids_are_validated_before_path_or_delivery_access(tmp_path, bad):
    store = SubscriberStore(tmp_path)
    with pytest.raises(ValueError):
        store.session_path(bad)
    with pytest.raises(ValueError):
        run(store.broadcast("briefing", CONFIG, subscriber_ids=[bad]))


def test_symlink_directory_and_session_cannot_escape(tmp_path):
    store = SubscriberStore(tmp_path / "data")
    friend = store.add("朋友")
    directory = store.root / friend.id
    directory.rmdir()
    directory.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        store.session_path(friend.id)


def test_duplicate_registration_and_rebinding_use_actual_wechat_identity(tmp_path):
    store = SubscriberStore(tmp_path)
    a, b = store.add("甲"), store.add("重复绑定")
    bind(store, a.id, "same-user")
    bind(store, b.id, "same-user")
    calls = []
    transport = fake_transport(calls)
    assert [r.status for r in run(store.broadcast("briefing", CONFIG, notifier_factory=transport))] == ["sent", "duplicate"]
    assert len(calls) == 1
    assert [r.status for r in run(store.broadcast("briefing", CONFIG, force=True, notifier_factory=transport))] == ["sent", "duplicate"]
    bind(store, b.id, "new-user")
    assert [r.status for r in run(store.broadcast("briefing", CONFIG, notifier_factory=transport))] == ["duplicate", "sent"]
    assert len(calls) == 3


def test_concurrent_manual_and_scheduled_calls_send_once(tmp_path):
    store = SubscriberStore(tmp_path)
    friend = store.add("朋友")
    bind(store, friend.id)
    calls = []
    transport = fake_transport(calls, delay=0.1)

    async def concurrent():
        return await asyncio.gather(
            store.broadcast("briefing", CONFIG, notifier_factory=transport),
            SubscriberStore(tmp_path).broadcast("briefing", CONFIG, notifier_factory=transport),
        )

    outcomes = run(concurrent())
    assert sorted(batch[0].status for batch in outcomes) == ["duplicate", "sent"]
    assert len(calls) == 1


def test_cancelling_lock_waiter_does_not_leave_a_background_lock(tmp_path):
    store = SubscriberStore(tmp_path)
    friend = store.add("朋友")
    bind(store, friend.id)
    calls = []
    transport = fake_transport(calls, delay=0.15)

    async def concurrent():
        holder = asyncio.create_task(store.broadcast("briefing", CONFIG, notifier_factory=transport))
        await asyncio.sleep(0.02)
        waiter = asyncio.create_task(SubscriberStore(tmp_path).broadcast("briefing", CONFIG, notifier_factory=transport))
        await asyncio.sleep(0.02)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await holder
        return await store.broadcast("new", CONFIG, notifier_factory=transport)

    assert run(concurrent())[0].sent
    assert len(calls) == 2


def test_ledger_write_failure_does_not_persist_false_success_on_later_recipient(tmp_path, monkeypatch):
    store = SubscriberStore(tmp_path)
    a, b = store.add("甲"), store.add("乙")
    bind(store, a.id)
    bind(store, b.id)
    actual_write = module._private_write
    writes = []

    def fail_once(path, value):
        writes.append(path)
        if len(writes) == 1:
            raise OSError("disk full")
        actual_write(path, value)

    monkeypatch.setattr(module, "_private_write", fail_once)
    outcomes = run(store.broadcast("briefing", CONFIG, notifier_factory=fake_transport([])))
    assert [r.status for r in outcomes] == ["failed", "sent"]
    assert outcomes[0].chunks_sent == 1
    assert outcomes[0].warning == "delivery_record_failed_retry_may_duplicate"
    assert len(json.loads(store.ledger_path.read_text())["sent"]) == 1


def test_unknown_ids_and_duplicate_requested_targets_send_nothing(tmp_path):
    store = SubscriberStore(tmp_path)
    with pytest.raises(KeyError):
        run(store.broadcast("briefing", CONFIG, subscriber_ids=["a" * 12]))
    with pytest.raises(ValueError):
        run(store.broadcast("briefing", CONFIG, subscriber_ids=["owner", "owner"]))
    with pytest.raises(ValueError):
        run(store.broadcast("   ", CONFIG))


def test_pipeline_adapter_fans_out_summary_and_failure_without_reanalysis(tmp_path, monkeypatch):
    from src.services.wechat_subscribers import SubscriberWeChatNotifier
    from src.storage.manager import StorageManager

    store = SubscriberStore(tmp_path)
    friend = store.add("朋友")
    bind(store, friend.id)
    calls = []
    notifier = fake_transport(calls)
    monkeypatch.setattr(module, "WeChatNotifier", lambda config, storage, **kwargs: notifier(config, storage))
    adapter = SubscriberWeChatNotifier(CONFIG, StorageManager(data_dir=str(tmp_path)))
    assert run(adapter.send_daily_summary("already generated", "zh"))[0].sent
    assert run(adapter.send_failure("2026-11-04", "upstream error"))[0].sent
    assert calls[0][1] == "already generated"
    assert "upstream error" in calls[1][1]


def test_login_binding_guard_skips_busy_person_without_blocking_others(tmp_path):
    store = SubscriberStore(tmp_path)
    a, b = store.add("正在扫码"), store.add("可以发送")
    bind(store, a.id)
    bind(store, b.id)
    calls = []
    transport = fake_transport(calls)

    async def during_login():
        async with store.binding_guard(a.id):
            return await SubscriberStore(tmp_path).broadcast("briefing", CONFIG, notifier_factory=transport)

    result = run(during_login())
    assert result[0].status == "failed"
    assert result[0].warning == "binding_in_progress"
    assert result[1].sent
    assert [call[0] for call in calls] == [b.id]
    retry = run(store.broadcast("briefing", CONFIG, notifier_factory=transport))
    assert [outcome.status for outcome in retry] == ["sent", "duplicate"]


def test_binding_guard_waits_for_active_delivery_and_releases_on_cancel(tmp_path):
    store = SubscriberStore(tmp_path)
    person = store.add("朋友")
    bind(store, person.id)
    entered = []
    calls = []

    async def binding():
        async with store.binding_guard(person.id):
            entered.append(True)

    async def concurrent():
        delivery = asyncio.create_task(store.broadcast("briefing", CONFIG, notifier_factory=fake_transport(calls, delay=0.1)))
        await asyncio.sleep(0.02)
        waiting = asyncio.create_task(binding())
        await asyncio.sleep(0.02)
        assert not entered
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        await delivery
        await binding()

    run(concurrent())
    assert entered == [True]


def test_zero_chunk_failure_allows_later_working_binding_of_same_user(tmp_path):
    store = SubscriberStore(tmp_path)
    old, current, alias = store.add("旧绑定"), store.add("当前绑定"), store.add("又一绑定")
    for subscriber in (old, current, alias):
        bind(store, subscriber.id, "same-user")
    calls = []

    class FirstBroken:
        def __init__(self, config, storage):
            self.sid = Path(storage.data_dir).name
            self.session = WeChatSession.load(Path(storage.data_dir) / "wechat_session.json")

        async def send_daily_summary(self, summary, lang):
            calls.append(self.sid)
            if self.sid == old.id:
                return WeChatDeliveryResult(WeChatDeliveryStatus.SKIPPED, chunks_sent=0)
            return WeChatDeliveryResult(WeChatDeliveryStatus.SUCCESS, chunks_sent=1)

    outcomes = run(store.broadcast("briefing", CONFIG, notifier_factory=FirstBroken))
    assert [result.status for result in outcomes] == ["failed", "sent", "duplicate"]
    assert calls == [old.id, current.id]
    assert len(json.loads(store.ledger_path.read_text())["sent"]) == 1


def test_partial_failure_blocks_later_alias_in_same_batch(tmp_path):
    store = SubscriberStore(tmp_path)
    first, alias = store.add("部分发送"), store.add("相同用户")
    for subscriber in (first, alias):
        bind(store, subscriber.id, "same-user")
    calls = []
    outcomes = run(store.broadcast("briefing", CONFIG, notifier_factory=fake_transport(calls, {
        "same-user": WeChatDeliveryResult(WeChatDeliveryStatus.PLATFORM_FAILURE, chunks_sent=1),
    })))
    assert [result.status for result in outcomes] == ["failed", "duplicate"]
    assert outcomes[0].warning == "partial_delivery_retry_may_duplicate"
    assert len(calls) == 1


def test_adapter_raises_after_fanout_when_one_person_fails(tmp_path, monkeypatch):
    from src.services.wechat_subscribers import SubscriberWeChatNotifier, WeChatPartialDeliveryError
    from src.storage.manager import StorageManager

    store = SubscriberStore(tmp_path)
    bad, good = store.add("失败"), store.add("成功")
    bind(store, bad.id)
    bind(store, good.id)
    calls = []
    notifier = fake_transport(calls, {bad.id: WeChatDeliveryResult(WeChatDeliveryStatus.NETWORK_FAILURE)})
    monkeypatch.setattr(module, "WeChatNotifier", lambda config, storage, **kwargs: notifier(config, storage))
    adapter = SubscriberWeChatNotifier(CONFIG, StorageManager(data_dir=str(tmp_path)))
    with pytest.raises(WeChatPartialDeliveryError) as caught:
        run(adapter.send_daily_summary("briefing", "zh"))
    assert [result.status for result in caught.value.outcomes] == ["failed", "sent"]
    assert len(calls) == 2


def test_adapter_failure_notification_does_not_raise_delivery_error(tmp_path, monkeypatch):
    from src.services.wechat_subscribers import SubscriberWeChatNotifier
    from src.storage.manager import StorageManager

    store = SubscriberStore(tmp_path)
    friend = store.add("失败")
    bind(store, friend.id)
    calls = []
    notifier = fake_transport(calls, {friend.id: WeChatDeliveryResult(WeChatDeliveryStatus.NETWORK_FAILURE)})
    monkeypatch.setattr(module, "WeChatNotifier", lambda config, storage, **kwargs: notifier(config, storage))
    adapter = SubscriberWeChatNotifier(CONFIG, StorageManager(data_dir=str(tmp_path)))
    outcomes = run(adapter.send_failure("2026-11-04", "original AI error"))
    assert outcomes[0].status == "failed"
    assert "original AI error" in calls[0][1]


def test_broken_failure_ledger_does_not_mask_original_generation_error(tmp_path):
    from src.services.wechat_subscribers import SubscriberWeChatNotifier
    from src.storage.manager import StorageManager

    store = SubscriberStore(tmp_path)
    store.ledger_path.write_text("not valid JSON")
    adapter = SubscriberWeChatNotifier(CONFIG, StorageManager(data_dir=str(tmp_path)))
    assert run(adapter.send_failure("2026-11-04", "original generation error")) == []


def cooldown_setup(tmp_path, monkeypatch, *, bot_id="dummy-bot", token="dummy-token"):
    store = SubscriberStore(tmp_path)
    friend = store.add("冷却测试")
    WeChatSession(bot_token=token, bot_id=bot_id, user_id="dummy-user", context_token="ctx-before").save(store.session_path(friend.id))
    clock = [1_800_000_000.0]
    monkeypatch.setattr(module, "_utc_timestamp", lambda: clock[0])
    return store, friend, clock


def rejected(chunks=0):
    return WeChatDeliveryResult(WeChatDeliveryStatus.PLATFORM_FAILURE, "prepare failed", chunks_sent=chunks, ret=-2)


def test_cooldown_persists_across_restart_force_and_new_inbound_context(tmp_path, monkeypatch):
    store, friend, clock = cooldown_setup(tmp_path, monkeypatch)
    calls = []
    result = run(store.broadcast("same briefing", CONFIG, notifier_factory=fake_transport(calls, {"dummy-user": rejected()})))
    assert result[0].status == "failed" and not result[0].sent and result[0].chunks_sent == 0
    ledger = json.loads(store.ledger_path.read_text())
    assert ledger["sent"] == {}
    assert len(ledger["send_cooldowns"]) == 2
    record = next(iter(ledger["send_cooldowns"].values()))
    assert record == {"retry_not_before": clock[0] + 3600, "reason": "ret_-2"}
    assert "dummy-bot" not in store.ledger_path.read_text() and "dummy-token" not in store.ledger_path.read_text()
    assert os.stat(store.ledger_path).st_mode & 0o777 == 0o600
    saved = WeChatSession.load(store.session_path(friend.id))
    saved.update_from_message({"message_type": 1, "from_user_id": "dummy-user", "context_token": "new-inbound"})
    saved.save(store.session_path(friend.id))
    clock[0] += 1200
    restored = SubscriberStore(tmp_path)
    result = run(restored.broadcast("same briefing", CONFIG, force=True, notifier_factory=fake_transport(calls)))
    assert result[0].status == "failed" and result[0].warning == "local_send_cooldown"
    assert not result[0].sent and result[0].chunks_sent == 0
    assert len(calls) == 1
    assert json.loads(store.ledger_path.read_text())["sent"] == {}


def test_cooldown_exact_boundary_and_rejection_extends_local_wait(tmp_path, monkeypatch):
    store, _, clock = cooldown_setup(tmp_path, monkeypatch)
    started = clock[0]
    calls = []
    transport = fake_transport(calls, {"dummy-user": rejected()})
    run(store.broadcast("briefing", CONFIG, notifier_factory=transport))
    clock[0] = started + 3599.999
    result = run(store.broadcast("briefing", CONFIG, notifier_factory=transport))
    assert result[0].warning == "local_send_cooldown" and len(calls) == 1
    clock[0] = started + 3600
    result = run(store.broadcast("briefing", CONFIG, notifier_factory=transport))
    assert result[0].status == "failed" and len(calls) == 2
    saved = next(iter(json.loads(store.ledger_path.read_text())["send_cooldowns"].values()))
    assert saved["retry_not_before"] == started + 7200
    clock[0] += 60
    run(store.broadcast("briefing", CONFIG, notifier_factory=fake_transport(calls)))
    assert len(calls) == 2
    clock[0] = started + 7200
    result = run(store.broadcast("briefing", CONFIG, notifier_factory=fake_transport(calls)))
    assert result[0].sent and len(calls) == 3
    assert run(store.broadcast("briefing", CONFIG, notifier_factory=fake_transport(calls)))[0].status == "duplicate"
    assert len(calls) == 3


def test_same_bot_alias_is_blocked_but_other_bot_still_receives(tmp_path, monkeypatch):
    store, first, _ = cooldown_setup(tmp_path, monkeypatch)
    alias, other = store.add("同bot第二注册"), store.add("另一个bot")
    # Different user IDs are deliberate: the cooldown belongs to the bot, not recipient.
    WeChatSession(bot_token="second-token", bot_id="dummy-bot", user_id="alias-user", context_token="ctx").save(store.session_path(alias.id))
    WeChatSession(bot_token="other-token", bot_id="other-bot", user_id="other-user", context_token="ctx").save(store.session_path(other.id))
    calls = []
    result = run(store.broadcast("briefing", CONFIG, subscriber_ids=[first.id, alias.id, other.id], notifier_factory=fake_transport(calls, {"dummy-user": rejected()})))
    assert [r.status for r in result] == ["failed", "failed", "sent"]
    assert result[1].warning == "local_send_cooldown"
    assert [call[0] for call in calls] == ["dummy-user", "other-user"]
    # A successful different bot must not discard the just-recorded cooldown map.
    assert json.loads(store.ledger_path.read_text())["send_cooldowns"]


def test_missing_bot_id_uses_same_token_hash_across_aliases(tmp_path, monkeypatch):
    store, first, _ = cooldown_setup(tmp_path, monkeypatch, bot_id="")
    alias = store.add("旧式会话")
    WeChatSession(bot_token="dummy-token", user_id="alias-user", context_token="ctx").save(store.session_path(alias.id))
    calls = []
    result = run(store.broadcast("briefing", CONFIG, subscriber_ids=[first.id, alias.id], notifier_factory=fake_transport(calls, {"dummy-user": rejected()})))
    assert result[1].warning == "local_send_cooldown" and len(calls) == 1


@pytest.mark.parametrize("failure", [
    WeChatDeliveryResult(WeChatDeliveryStatus.PLATFORM_FAILURE, "ret=-2 prepare failed"),
    WeChatDeliveryResult(WeChatDeliveryStatus.NETWORK_FAILURE, "ret=-2 prepare failed"),
    WeChatDeliveryResult(WeChatDeliveryStatus.PLATFORM_FAILURE, "other platform error", ret=-14),
])
def test_hint_text_or_other_return_code_does_not_create_cooldown(tmp_path, monkeypatch, failure):
    store, _, _ = cooldown_setup(tmp_path, monkeypatch)
    calls = []
    run(store.broadcast("briefing", CONFIG, notifier_factory=fake_transport(calls, {"dummy-user": failure})))
    result = run(store.broadcast("briefing", CONFIG, notifier_factory=fake_transport(calls)))
    assert result[0].sent and len(calls) == 2
    assert not json.loads(store.ledger_path.read_text())["send_cooldowns"]


def test_partial_minus_two_records_cooldown_without_marking_delivered(tmp_path, monkeypatch):
    store, _, _ = cooldown_setup(tmp_path, monkeypatch)
    calls = []
    result = run(store.broadcast("briefing", CONFIG, notifier_factory=fake_transport(calls, {"dummy-user": rejected(chunks=1)})))
    assert result[0].chunks_sent == 1 and not result[0].sent
    assert result[0].warning == "partial_delivery_retry_may_duplicate"
    assert json.loads(store.ledger_path.read_text())["sent"] == {}
    result = run(store.broadcast("briefing", CONFIG, force=True, notifier_factory=fake_transport(calls)))
    assert result[0].warning == "local_send_cooldown" and len(calls) == 1


def test_concurrent_broadcast_observes_first_calls_persisted_cooldown(tmp_path, monkeypatch):
    store, _, _ = cooldown_setup(tmp_path, monkeypatch)
    calls = []
    transport = fake_transport(calls, {"dummy-user": rejected()}, delay=0.01)

    async def both():
        return await asyncio.gather(store.broadcast("briefing", CONFIG, notifier_factory=transport), SubscriberStore(tmp_path).broadcast("briefing", CONFIG, notifier_factory=transport))

    results = run(both())
    assert len(calls) == 1
    assert all(not group[0].sent for group in results)
    assert sum(group[0].warning == "local_send_cooldown" for group in results) == 1



def test_legacy_alias_without_bot_id_shares_token_cooldown(tmp_path, monkeypatch):
    store, first, _ = cooldown_setup(tmp_path, monkeypatch)
    alias = store.add("缺少botid的旧注册")
    WeChatSession(bot_token="dummy-token", user_id="alias-user", context_token="ctx").save(store.session_path(alias.id))
    calls = []
    result = run(store.broadcast("briefing", CONFIG, subscriber_ids=[first.id, alias.id], notifier_factory=fake_transport(calls, {"dummy-user": rejected()})))
    assert result[1].warning == "local_send_cooldown" and len(calls) == 1


def test_partial_failure_alias_is_cooldown_failure_not_false_delivery_duplicate(tmp_path, monkeypatch):
    store, first, _ = cooldown_setup(tmp_path, monkeypatch)
    alias = store.add("同一接收者别名")
    WeChatSession(bot_token="dummy-token", bot_id="dummy-bot", user_id="dummy-user", context_token="ctx").save(store.session_path(alias.id))
    calls = []
    result = run(store.broadcast("briefing", CONFIG, subscriber_ids=[first.id, alias.id], notifier_factory=fake_transport(calls, {"dummy-user": rejected(chunks=1)})))
    assert result[0].status == "failed" and result[0].chunks_sent == 1
    assert result[1].status == "failed" and result[1].chunks_sent == 0
    assert result[1].warning == "local_send_cooldown" and not result[1].sent
    assert len(calls) == 1


def test_cooldown_write_failure_does_not_retry_partial_delivery_via_other_bot_alias(tmp_path, monkeypatch):
    store, first, _ = cooldown_setup(tmp_path, monkeypatch)
    alias = store.add("其他bot同接收者")
    WeChatSession(bot_token="other-token", bot_id="other-bot", user_id="dummy-user", context_token="ctx").save(store.session_path(alias.id))
    calls = []
    original = module._private_write

    def fail_ledger(path, value):
        if path == store.ledger_path:
            raise OSError("simulated persistence failure")
        return original(path, value)

    monkeypatch.setattr(module, "_private_write", fail_ledger)
    result = run(store.broadcast("briefing", CONFIG, subscriber_ids=[first.id, alias.id], notifier_factory=fake_transport(calls, {"dummy-user": rejected(chunks=1)})))
    assert result[0].status == "failed" and result[0].chunks_sent == 1
    assert result[1].status == "duplicate" and not result[1].sent
    assert len(calls) == 1
