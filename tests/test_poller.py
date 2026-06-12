# tests/test_poller.py
from datetime import UTC, datetime

import pytest

from sentinel.models import Incident, IncidentStatus, Indicator, Snapshot
from sentinel.poller import PollState, run_cycle, run_heartbeat, should_send_heartbeat
from sentinel.store import Store


class FakeAdapter:
    def __init__(self, provider, snapshot=None, error=None):
        self.provider = provider
        self.display_name = provider
        self.track_components = True
        self._snapshot = snapshot
        self._error = error

    async def fetch(self, fetcher):
        if self._error:
            raise self._error
        return self._snapshot


class RecordingFeishu:
    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    async def send(self, card):
        self.sent.append(card)
        if self.fail:
            from sentinel.feishu.client import FeishuError

            raise FeishuError("boom")


def _snap(provider, indicator=Indicator.NONE, incidents=None):
    return Snapshot(
        provider=provider,
        display_name=provider,
        indicator=indicator,
        status_url="https://x",
        components=[],
        incidents=incidents or [],
        fetched_at="2026-06-06T00:00:00Z",
    )


def _inc(key):
    return Incident(
        key=key,
        title=key,
        status=IncidentStatus.INVESTIGATING,
        impact=Indicator.MAJOR,
        url="https://x",
        started_at=None,
        updated_at=None,
        latest_update_id="u1",
    )


@pytest.mark.asyncio
async def test_commit_only_after_successful_send(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.put("anthropic", _snap("anthropic"))  # 上次:无事件
    adapter = FakeAdapter("anthropic", _snap("anthropic", Indicator.MAJOR, [_inc("i1")]))
    feishu = RecordingFeishu(fail=True)  # 发送失败
    state = PollState()

    await run_cycle([adapter], fetcher=None, store=store, feishu=feishu, state=state)

    assert len(feishu.sent) == 1  # 试图发了
    # 发送失败 -> 不 commit -> store 仍是旧的(无事件)
    assert store.get("anthropic").incidents == []


@pytest.mark.asyncio
async def test_commit_when_send_succeeds(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.put("anthropic", _snap("anthropic"))
    adapter = FakeAdapter("anthropic", _snap("anthropic", Indicator.MAJOR, [_inc("i1")]))
    feishu = RecordingFeishu(fail=False)
    state = PollState()

    await run_cycle([adapter], fetcher=None, store=store, feishu=feishu, state=state)

    assert len(store.get("anthropic").incidents) == 1  # 已 commit


@pytest.mark.asyncio
async def test_no_events_commits_silently(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.put("anthropic", _snap("anthropic"))
    adapter = FakeAdapter("anthropic", _snap("anthropic"))  # 还是全绿,无翻转
    feishu = RecordingFeishu()
    await run_cycle([adapter], fetcher=None, store=store, feishu=feishu, state=PollState())
    assert feishu.sent == []  # 无事件不发


@pytest.mark.asyncio
async def test_one_adapter_error_does_not_block_others(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    good = FakeAdapter("github", _snap("github", Indicator.MAJOR, [_inc("g1")]))
    bad = FakeAdapter("openai", error=RuntimeError("network"))
    feishu = RecordingFeishu()
    await run_cycle([bad, good], fetcher=None, store=store, feishu=feishu, state=PollState())
    # good 仍被处理并 commit
    assert store.get("github") is not None


@pytest.mark.asyncio
async def test_meta_alert_fires_once_at_fail_threshold(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    bad = FakeAdapter("openai", error=RuntimeError("network"))
    feishu = RecordingFeishu()
    state = PollState()
    for _ in range(5):
        await run_cycle(
            [bad], fetcher=None, store=store, feishu=feishu, state=state, fail_threshold=3
        )
    meta = [c for c in feishu.sent if "无法获取" in c["card"]["elements"][0]["text"]["content"]]
    assert len(meta) == 1  # 仅在第 3 次发一次


# ---- 心跳调度 ----


def _dt(hour, day=6):
    return datetime(2026, 6, day, hour, 0, tzinfo=UTC)


def test_should_send_heartbeat_before_hour_is_false():
    assert should_send_heartbeat(_dt(8), None, 9) is False


def test_should_send_heartbeat_after_hour_unsent_is_true():
    assert should_send_heartbeat(_dt(9), None, 9) is True
    assert should_send_heartbeat(_dt(14), "2026-06-05", 9) is True  # 昨天发过,今天到点该发


def test_should_send_heartbeat_already_sent_today_is_false():
    assert should_send_heartbeat(_dt(10), "2026-06-06", 9) is False


@pytest.mark.asyncio
async def test_run_heartbeat_sends_then_records_and_dedups(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.put("anthropic", _snap("anthropic"))
    feishu = RecordingFeishu()
    await run_heartbeat(
        ["anthropic"], store=store, feishu=feishu, now_local=_dt(9), hour=9, interval=60
    )
    assert len(feishu.sent) == 1
    assert store.get_meta("heartbeat_last_date") == "2026-06-06"
    # 同日再调不重发
    await run_heartbeat(
        ["anthropic"], store=store, feishu=feishu, now_local=_dt(11), hour=9, interval=60
    )
    assert len(feishu.sent) == 1


@pytest.mark.asyncio
async def test_run_heartbeat_failed_send_not_recorded(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.put("anthropic", _snap("anthropic"))
    feishu = RecordingFeishu(fail=True)
    await run_heartbeat(
        ["anthropic"], store=store, feishu=feishu, now_local=_dt(9), hour=9, interval=60
    )
    assert len(feishu.sent) == 1  # 试图发了
    assert store.get_meta("heartbeat_last_date") is None  # 失败不记 → 下轮重试
