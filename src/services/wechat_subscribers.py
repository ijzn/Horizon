"""Independent opt-in WeChat sessions and deduplicated multi-recipient delivery.

No credentials are stored in the roster or delivery ledger. The legacy session
remains in its original location and is exposed as the ``owner`` subscriber.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Callable
import uuid

from filelock import FileLock, Timeout

from ..models import WeChatConfig
from ..storage.manager import StorageManager
from .wechat import SESSION_FILE, WeChatNotifier, WeChatSession


_ID = re.compile(r"^(?:owner|[a-f0-9]{12})$")
# Conservative local backoff policy; not a Tencent TTL or recovery guarantee.
SEND_COOLDOWN_SECONDS = 60 * 60


def _utc_timestamp() -> float:
    return datetime.now(timezone.utc).timestamp()


def _bot_cooldown_keys(session) -> tuple[str, ...]:
    """Share a backoff across registrations of the same bot, never raw secrets."""
    keys = []
    for field in ("bot_id", "bot_token"):
        identity = getattr(session, field, None)
        if isinstance(identity, str) and identity:
            digest = hashlib.sha256((field + "\0" + identity).encode("utf-8")).hexdigest()
            keys.append("bot:" + digest)
    return tuple(keys)


@dataclass(frozen=True)
class Subscriber:
    id: str
    name: str
    enabled: bool = True
    is_owner: bool = False


@dataclass(frozen=True)
class SubscriberDelivery:
    id: str
    name: str
    status: str
    sent: bool = False
    chunks_sent: int = 0
    warning: str | None = None


class WeChatPartialDeliveryError(RuntimeError):
    """The pipeline finished fanout, but at least one subscriber failed."""

    def __init__(self, outcomes: list[SubscriberDelivery]):
        self.outcomes = outcomes
        failed = sum(result.status == "failed" for result in outcomes)
        super().__init__(f"WeChat delivery incomplete: {failed} subscriber(s) failed")


def _private_write(path: Path, value: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class SubscriberStore:
    """Roster for one Horizon data directory; IDs, not names, form paths."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.root = self.data_dir / "wechat_subscribers"
        if self.root.is_symlink():
            raise ValueError("Subscriber directory must not be a symlink")
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.root.chmod(0o700)
        self.roster_path = self.root / "subscribers.json"
        self.ledger_path = self.root / "delivery.json"

    @staticmethod
    def validate_id(subscriber_id: str) -> str:
        if not isinstance(subscriber_id, str) or not _ID.fullmatch(subscriber_id):
            raise ValueError("Invalid subscriber ID")
        return subscriber_id

    def _lock(self, name: str) -> FileLock:
        path = self.root / name
        if path.is_symlink():
            raise ValueError("Lock file must not be a symlink")
        return FileLock(str(path), mode=0o600, thread_local=False)

    @contextmanager
    def _roster_lock(self):
        with self._lock("roster.lock"):
            yield

    def _read(self, path: Path, default: dict) -> dict:
        if path.is_symlink():
            raise ValueError("Subscriber metadata must not be a symlink")
        if not path.exists():
            return default
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError("Invalid subscriber metadata")
        return value

    def _subscribers(self) -> list[Subscriber]:
        data = self._read(self.roster_path, {"version": 1, "subscribers": []})
        records = data.get("subscribers")
        if not isinstance(records, list):
            raise ValueError("Invalid subscriber roster")
        result = []
        seen = set()
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("Invalid subscriber record")
            sid = self.validate_id(record.get("id"))
            if sid in seen:
                raise ValueError("Duplicate subscriber ID")
            seen.add(sid)
            if not isinstance(record.get("name"), str) or not record["name"].strip():
                raise ValueError("Invalid subscriber name")
            if not isinstance(record.get("enabled", True), bool):
                raise ValueError("Invalid subscriber enabled state")
            result.append(Subscriber(sid, record["name"], record.get("enabled", True), sid == "owner"))
        if "owner" not in seen and (self.data_dir / SESSION_FILE).exists():
            result.insert(0, Subscriber("owner", "我自己", True, True))
        return result

    def list(self) -> list[Subscriber]:
        with self._roster_lock():
            return self._subscribers()

    def get(self, subscriber_id: str) -> Subscriber:
        self.validate_id(subscriber_id)
        for subscriber in self.list():
            if subscriber.id == subscriber_id:
                return subscriber
        raise KeyError(f"Unknown subscriber: {subscriber_id}")

    def _save_roster(self, subscribers: list[Subscriber]) -> None:
        _private_write(self.roster_path, {"version": 1, "subscribers": [asdict(s) for s in subscribers]})

    def add(self, name: str) -> Subscriber:
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError("Name must contain 1 to 80 characters")
        if any(ord(c) < 32 for c in name):
            raise ValueError("Name must not contain control characters")
        name = name.strip()
        with self._roster_lock():
            subscribers = self._subscribers()
            if any(s.name == name for s in subscribers):
                raise ValueError("A subscriber with this name already exists")
            sid = uuid.uuid4().hex[:12]
            while any(s.id == sid for s in subscribers):
                sid = uuid.uuid4().hex[:12]
            subscriber = Subscriber(sid, name)
            directory = self.root / sid
            directory.mkdir(mode=0o700)
            self._save_roster(subscribers + [subscriber])
            return subscriber

    def set_enabled(self, subscriber_id: str, enabled: bool) -> Subscriber:
        self.validate_id(subscriber_id)
        if not isinstance(enabled, bool):
            raise ValueError("Enabled must be a boolean")
        with self._roster_lock():
            subscribers = self._subscribers()
            for index, subscriber in enumerate(subscribers):
                if subscriber.id == subscriber_id:
                    updated = Subscriber(subscriber.id, subscriber.name, enabled, subscriber.is_owner)
                    subscribers[index] = updated
                    self._save_roster(subscribers)
                    return updated
        raise KeyError(f"Unknown subscriber: {subscriber_id}")

    def remove(self, subscriber_id: str) -> Subscriber:
        """Disable delivery while retaining session/history for explicit restoration."""
        return self.set_enabled(subscriber_id, False)

    def session_path(self, subscriber_id: str) -> Path:
        self.get(subscriber_id)
        directory = self.data_dir if subscriber_id == "owner" else self.root / subscriber_id
        if directory.is_symlink() or directory.resolve().parent != self.root and subscriber_id != "owner":
            raise ValueError("Subscriber directory escapes its root")
        path = directory / SESSION_FILE
        if path.is_symlink():
            raise ValueError("Subscriber session must not be a symlink")
        return path

    def storage_for(self, subscriber_id: str) -> StorageManager:
        path = self.session_path(subscriber_id)
        if not path.parent.exists():
            path.parent.mkdir(mode=0o700)
        if subscriber_id != "owner":
            path.parent.chmod(0o700)
        return StorageManager(data_dir=str(path.parent), config_path=str(self.data_dir / "config.json"))

    def describe(self) -> list[dict]:
        """Expose only roster fields and whether a session file exists."""
        descriptions = []
        for subscriber in self.list():
            path = self.session_path(subscriber.id)
            session = None
            try:
                session = WeChatSession.load(path)
            except (OSError, ValueError, TypeError):
                pass
            descriptions.append({
                **asdict(subscriber), "session_present": path.exists(),
                "ready": bool(session and session.ready),
                "context_present": bool(session and session.context_token),
            })
        return descriptions

    @asynccontextmanager
    async def binding_guard(self, subscriber_id: str):
        """Serialize login/refresh against delivery for this subscriber only."""
        self.get(subscriber_id)
        lock = self._lock(f"binding-{subscriber_id}.lock")
        while True:
            try:
                lock.acquire(timeout=0)
                break
            except Timeout:
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            lock.release()

    async def broadcast(
        self,
        summary: str,
        config: WeChatConfig,
        *,
        subscriber_ids: list[str] | None = None,
        lang: str = "zh",
        force: bool = False,
        notifier_factory: Callable | None = None,
    ) -> list[SubscriberDelivery]:
        """Serialize sends and persist successes; retry failures only on a later call.

        This cannot guarantee exactly-once delivery: a process can die after the
        server accepts a message but before its success is committed locally.
        """
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("Cannot send an empty briefing")
        if subscriber_ids is not None:
            for sid in subscriber_ids:
                self.validate_id(sid)
            if len(set(subscriber_ids)) != len(subscriber_ids):
                raise ValueError("Duplicate requested subscriber IDs")
        digest = hashlib.sha256((lang + "\0" + summary).encode("utf-8")).hexdigest()
        lock = self._lock("delivery.lock")
        # A bounded wait avoids leaked background acquisition after cancellation.
        while True:
            try:
                lock.acquire(timeout=0)
                break
            except Timeout:
                await asyncio.sleep(0.05)
        try:
            subscribers = self.list()
            if subscriber_ids is not None:
                by_id = {s.id: s for s in subscribers}
                missing = set(subscriber_ids) - by_id.keys()
                if missing:
                    raise KeyError("Unknown subscriber ID")
                subscribers = [by_id[sid] for sid in subscriber_ids]
            ledger = self._read(self.ledger_path, {"version": 1, "sent": {}})
            if not isinstance(ledger.get("sent"), dict):
                raise ValueError("Invalid delivery ledger")
            cooldowns = ledger.get("send_cooldowns", {})
            if not isinstance(cooldowns, dict):
                raise ValueError("Invalid send cooldown ledger")
            for key, record in cooldowns.items():
                until = record.get("retry_not_before") if isinstance(record, dict) else None
                if (
                    not isinstance(key, str) or not re.fullmatch(r"bot:[a-f0-9]{64}", key)
                    or type(until) not in (int, float) or not math.isfinite(until) or until <= 0
                    or record.get("reason") != "ret_-2"
                ):
                    raise ValueError("Invalid send cooldown record")
            ledger = {**ledger, "send_cooldowns": cooldowns}
            outcomes = []
            attempted_targets = set()
            factory = notifier_factory or WeChatNotifier
            for subscriber in subscribers:
                sid = subscriber.id
                if not subscriber.enabled or not config.enabled:
                    outcomes.append(SubscriberDelivery(sid, subscriber.name, "disabled"))
                    continue
                binding = self._lock(f"binding-{sid}.lock")
                try:
                    binding.acquire(timeout=0)
                except Timeout:
                    outcomes.append(SubscriberDelivery(sid, subscriber.name, "failed", warning="binding_in_progress"))
                    continue
                chunks = 0
                try:
                    notifier = factory(config, self.storage_for(sid))
                    # One WeChat user can accidentally register twice. Dedupe by
                    # the actual recipient, and allow a rebound ID's new user to
                    # receive a briefing already delivered to its previous user.
                    session = getattr(notifier, "session", None)
                    identity = getattr(session, "user_id", None)
                    target = ("user:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()) if identity else "subscriber:" + sid
                    prior = ledger["sent"].get(target, {})
                    if not isinstance(prior, dict):
                        raise ValueError("Invalid subscriber delivery history")
                    if digest in prior and not force:
                        outcomes.append(SubscriberDelivery(sid, subscriber.name, "duplicate"))
                        continue
                    bot_keys = _bot_cooldown_keys(session)
                    if any(
                        ledger["send_cooldowns"].get(key, {}).get("retry_not_before", 0) > _utc_timestamp()
                        for key in bot_keys
                    ):
                        outcomes.append(SubscriberDelivery(sid, subscriber.name, "failed", warning="local_send_cooldown"))
                        continue
                    if target in attempted_targets:
                        outcomes.append(SubscriberDelivery(sid, subscriber.name, "duplicate"))
                        continue
                    result = await notifier.send_daily_summary(summary, lang)
                    chunks = result.chunks_sent
                    # An invalid old registration must not suppress a later
                    # working registration of the same user. After any actual
                    # transmission, however, avoid re-sending through an alias.
                    if result.sent or chunks:
                        attempted_targets.add(target)
                    # Only a structured platform return code starts this local
                    # policy. New inbound contexts and --force do not clear it.
                    if getattr(result, "ret", None) == -2 and not result.sent and bot_keys:
                        ledger = {
                            **ledger,
                            "send_cooldowns": {
                                **ledger["send_cooldowns"],
                                **{
                                    key: {
                                        "retry_not_before": _utc_timestamp() + SEND_COOLDOWN_SECONDS,
                                        "reason": "ret_-2",
                                    }
                                    for key in bot_keys
                                },
                            },
                        }
                        _private_write(self.ledger_path, ledger)
                    if not result.sent:
                        warning = "partial_delivery_retry_may_duplicate" if chunks else "delivery_failed"
                        outcomes.append(SubscriberDelivery(sid, subscriber.name, "failed", chunks_sent=chunks, warning=warning))
                        continue
                    updated = {**ledger, "version": 1, "sent": dict(ledger["sent"])}
                    updated["sent"][target] = {**prior, digest: datetime.now(timezone.utc).isoformat()}
                    _private_write(self.ledger_path, updated)
                    ledger = updated
                    outcomes.append(SubscriberDelivery(sid, subscriber.name, "sent", True, chunks))
                except Exception:
                    # Do not propagate raw exception details: transports can include secrets.
                    warning = "delivery_record_failed_retry_may_duplicate" if chunks else "delivery_or_recording_failed"
                    outcomes.append(SubscriberDelivery(sid, subscriber.name, "failed", chunks_sent=chunks, warning=warning))
                finally:
                    binding.release()
            return outcomes
        finally:
            lock.release()


class SubscriberWeChatNotifier:
    """Drop-in pipeline adapter: generate once, fan out to the saved roster."""

    def __init__(self, config: WeChatConfig, storage: StorageManager, console=None, icons=None):
        self.config = config
        self.store = SubscriberStore(storage.data_dir)
        self.console = console
        self.icons = icons

    def _notifier(self, config: WeChatConfig, storage: StorageManager):
        return WeChatNotifier(config, storage, console=self.console, icons=self.icons)

    async def send_daily_summary(self, summary: str, lang: str):
        outcomes = await self.store.broadcast(
            summary, self.config, lang=lang, notifier_factory=self._notifier,
        )
        if self.console is not None:
            sent = sum(result.sent for result in outcomes)
            failed = sum(result.status == "failed" for result in outcomes)
            skipped = len(outcomes) - sent - failed
            self.console.print(f"WeChat subscribers: {sent} sent, {failed} failed, {skipped} skipped.")
            if not outcomes:
                self.console.print("No WeChat subscribers. Bind the owner or add a subscriber first.")
        if any(result.status == "failed" for result in outcomes):
            raise WeChatPartialDeliveryError(outcomes)
        return outcomes

    async def send_failure(self, date: str, error_message: str):
        try:
            return await self.store.broadcast(
                f"**Horizon generation failed** ({date})\n\n{error_message}",
                self.config, lang=(self.config.languages or ["zh"])[0],
                notifier_factory=self._notifier,
            )
        except Exception:
            # A broken roster/ledger must not hide the original generation error.
            if self.console is not None:
                self.console.print("WeChat failure notification could not be delivered.")
            return []
