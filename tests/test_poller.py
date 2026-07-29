# tests/test_poller.py
import time
from datetime import UTC, datetime

import pytest

from sentinel.models import Incident, IncidentStatus, Indicator, Snapshot
from sentinel.notify.message import Kind
from sentinel.poller import PollState, run_cycle, run_heartbeat, should_send_heartbeat
from sentinel.status_editor import StatusAnalysis
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


class RecordingBroadcaster:
    """记录收到的 Notification;fail=True 时返回 0(全渠道失败)。"""

    def __init__(self, fail=False):
        self.fail = fail
        self.sent = []

    async def send(self, n):
        self.sent.append(n)
        return 0 if self.fail else 1


class FakeEditor:
    model = "test-model"

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def analyze(self, snapshot, events):
        self.calls.append((snapshot, events))
        if self.error:
            raise self.error
        return self.result


def _analysis(decision="notify"):
    return StatusAnalysis(
        decision=decision,
        severity="info",
        headline="低影响状态更新",
        summary="官方状态发生变化。",
        impact_summary="暂无内部影响证据。",
        affected_services=[],
        evidence=["官方影响级别为 minor"],
        recommended_action="继续观察。",
        confidence=0.9,
    )


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
    store.put("anthropic", _snap("anthropic"))
    adapter = FakeAdapter("anthropic", _snap("anthropic", Indicator.MAJOR, [_inc("i1")]))
    bc = RecordingBroadcaster(fail=True)  # 全渠道失败
    state = PollState()

    await run_cycle([adapter], fetcher=None, store=store, broadcaster=bc, state=state)

    assert len(bc.sent) == 1  # 试图广播了
    assert store.get("anthropic").incidents == []  # 失败 → 不 commit


@pytest.mark.asyncio
async def test_commit_when_send_succeeds(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.put("anthropic", _snap("anthropic"))
    adapter = FakeAdapter("anthropic", _snap("anthropic", Indicator.MAJOR, [_inc("i1")]))
    bc = RecordingBroadcaster(fail=False)
    state = PollState()

    await run_cycle([adapter], fetcher=None, store=store, broadcaster=bc, state=state)

    assert bc.sent[0].kind is Kind.VENDOR_INCIDENT
    assert len(store.get("anthropic").incidents) == 1  # 已 commit


@pytest.mark.asyncio
async def test_no_events_commits_silently(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.put("anthropic", _snap("anthropic"))
    adapter = FakeAdapter("anthropic", _snap("anthropic"))
    bc = RecordingBroadcaster()
    await run_cycle([adapter], fetcher=None, store=store, broadcaster=bc, state=PollState())
    assert bc.sent == []


@pytest.mark.asyncio
async def test_one_adapter_error_does_not_block_others(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    good = FakeAdapter("github", _snap("github", Indicator.MAJOR, [_inc("g1")]))
    bad = FakeAdapter("openai", error=RuntimeError("network"))
    bc = RecordingBroadcaster()
    await run_cycle([bad, good], fetcher=None, store=store, broadcaster=bc, state=PollState())
    assert store.get("github") is not None


@pytest.mark.asyncio
async def test_meta_alert_fires_once_at_fail_threshold(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    bad = FakeAdapter("openai", error=RuntimeError("network"))
    bc = RecordingBroadcaster()
    state = PollState()
    for _ in range(5):
        await run_cycle(
            [bad], fetcher=None, store=store, broadcaster=bc, state=state, fail_threshold=3
        )
    meta = [n for n in bc.sent if "无法获取" in n.detail]
    assert len(meta) == 1  # 仅在第 3 次发一次


async def test_meta_alert_retries_until_delivered(tmp_path):
    # 阈值时全渠道失败,meta 卡不能丢:未送达就每轮重试,送达后才停(Codex P3)。
    store = Store(str(tmp_path / "s.db"))
    bad = FakeAdapter("openai", error=RuntimeError("network"))
    bc = RecordingBroadcaster(fail=True)  # 全渠道宕
    state = PollState()
    for _ in range(4):  # 1、2 计数<阈值不发;3 达阈值发(失败);4 仍未送达继续重试(失败)
        await run_cycle(
            [bad], fetcher=None, store=store, broadcaster=bc, state=state, fail_threshold=3
        )
    attempts = [n for n in bc.sent if "无法获取" in n.detail]
    assert len(attempts) == 2  # 第 3、4 轮都尝试了——没在阈值那轮之后把卡丢掉
    bc.fail = False  # 渠道恢复
    await run_cycle(
        [bad], fetcher=None, store=store, broadcaster=bc, state=state, fail_threshold=3
    )  # 第 5 轮:终于送达
    await run_cycle(
        [bad], fetcher=None, store=store, broadcaster=bc, state=state, fail_threshold=3
    )  # 第 6 轮:已送达 → 不再重复
    final = [n for n in bc.sent if "无法获取" in n.detail]
    assert len(final) == 3  # 送达一次后即止


async def test_meta_alert_rearms_after_recovery(tmp_path):
    # 送达后状态页恢复又再次失败到阈值:meta 应能再发一次(meta_sent 在成功抓取时清除)。
    store = Store(str(tmp_path / "s.db"))
    bad = FakeAdapter("openai", error=RuntimeError("network"))
    ok = FakeAdapter("openai", _snap("openai"))
    bc = RecordingBroadcaster()
    state = PollState()
    for _ in range(3):  # 达阈值发一次
        await run_cycle(
            [bad], fetcher=None, store=store, broadcaster=bc, state=state, fail_threshold=3
        )
    await run_cycle(
        [ok], fetcher=None, store=store, broadcaster=bc, state=state, fail_threshold=3
    )  # 抓取成功 → 清计数与 meta_sent
    for _ in range(3):  # 再次失败到阈值
        await run_cycle(
            [bad], fetcher=None, store=store, broadcaster=bc, state=state, fail_threshold=3
        )
    meta = [n for n in bc.sent if "无法获取" in n.detail]
    assert len(meta) == 2  # 恢复后重新武装,可再发


# ---- 心跳调度 ----


def _dt(hour, day=6):
    return datetime(2026, 6, day, hour, 0, tzinfo=UTC)


def test_should_send_heartbeat_before_hour_is_false():
    assert should_send_heartbeat(_dt(8), None, 9) is False


def test_should_send_heartbeat_after_hour_unsent_is_true():
    assert should_send_heartbeat(_dt(9), None, 9) is True
    assert should_send_heartbeat(_dt(14), "2026-06-05", 9) is True


def test_should_send_heartbeat_already_sent_today_is_false():
    assert should_send_heartbeat(_dt(10), "2026-06-06", 9) is False


@pytest.mark.asyncio
async def test_run_heartbeat_sends_then_records_and_dedups(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.put("anthropic", _snap("anthropic"))
    bc = RecordingBroadcaster()
    await run_heartbeat(
        ["anthropic"], store=store, broadcaster=bc, now_local=_dt(9), hour=9, interval=60
    )
    assert len(bc.sent) == 1
    assert bc.sent[0].kind is Kind.HEARTBEAT
    assert store.get_meta("heartbeat_last_date") == "2026-06-06"
    await run_heartbeat(
        ["anthropic"], store=store, broadcaster=bc, now_local=_dt(11), hour=9, interval=60
    )
    assert len(bc.sent) == 1


@pytest.mark.asyncio
async def test_run_heartbeat_failed_send_not_recorded(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.put("anthropic", _snap("anthropic"))
    bc = RecordingBroadcaster(fail=True)
    await run_heartbeat(
        ["anthropic"], store=store, broadcaster=bc, now_local=_dt(9), hour=9, interval=60
    )
    assert len(bc.sent) == 1  # 试图发了
    assert store.get_meta("heartbeat_last_date") is None  # 失败不记 → 下轮重试


async def test_gate_mode_suppresses_nonurgent_event_and_commits(tmp_path):
    old = Incident(
        key="i1",
        title="minor incident",
        status=IncidentStatus.INVESTIGATING,
        impact=Indicator.MINOR,
        url="https://x",
        started_at=None,
        updated_at=None,
        latest_update_id="u1",
    )
    new = Incident(
        key="i1",
        title="minor incident",
        status=IncidentStatus.MONITORING,
        impact=Indicator.MINOR,
        url="https://x",
        started_at=None,
        updated_at=None,
        latest_update_id="u2",
    )
    store = Store(str(tmp_path / "s.db"))
    store.put("cloudflare", _snap("cloudflare", Indicator.MINOR, [old]))
    bc = RecordingBroadcaster()
    await run_cycle(
        [FakeAdapter("cloudflare", _snap("cloudflare", Indicator.MINOR, [new]))],
        fetcher=None,
        store=store,
        broadcaster=bc,
        state=PollState(),
        status_editor=FakeEditor(_analysis("suppress")),
        editor_mode="gate",
    )
    assert bc.sent == []
    assert store.get("cloudflare").incidents[0].status is IncidentStatus.MONITORING
    assert store.get_recent_llm_analyses()[0]["mode"] == "gate"


async def test_gate_mode_cannot_suppress_major_event(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.put("openai", _snap("openai"))
    bc = RecordingBroadcaster()
    await run_cycle(
        [FakeAdapter("openai", _snap("openai", Indicator.MAJOR, [_inc("i1")]))],
        fetcher=None,
        store=store,
        broadcaster=bc,
        state=PollState(),
        status_editor=FakeEditor(_analysis("suppress")),
        editor_mode="gate",
    )
    assert len(bc.sent) == 1
    assert bc.sent[0].title == "openai · 低影响状态更新"


async def test_shadow_mode_keeps_original_notification(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.put("openai", _snap("openai"))
    bc = RecordingBroadcaster()
    await run_cycle(
        [FakeAdapter("openai", _snap("openai", Indicator.MAJOR, [_inc("i1")]))],
        fetcher=None,
        store=store,
        broadcaster=bc,
        state=PollState(),
        status_editor=FakeEditor(_analysis()),
        editor_mode="shadow",
    )
    assert len(bc.sent) == 1
    assert bc.sent[0].title == "openai 状态变更"
    assert store.get_recent_llm_analyses()[0]["mode"] == "shadow"


async def test_editor_failure_falls_back_to_original_notification(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    store.put("openai", _snap("openai"))
    bc = RecordingBroadcaster()
    await run_cycle(
        [FakeAdapter("openai", _snap("openai", Indicator.MAJOR, [_inc("i1")]))],
        fetcher=None,
        store=store,
        broadcaster=bc,
        state=PollState(),
        status_editor=FakeEditor(error=RuntimeError("editor down")),
        editor_mode="gate",
    )
    assert len(bc.sent) == 1
    assert bc.sent[0].title == "openai 状态变更"


async def test_minor_incident_obeys_provider_min_gap(tmp_path):
    minor = Incident(
        key="i1",
        title="minor",
        status=IncidentStatus.INVESTIGATING,
        impact=Indicator.MINOR,
        url="https://x",
        started_at=None,
        updated_at=None,
        latest_update_id="u1",
    )
    store = Store(str(tmp_path / "s.db"))
    store.put("openai", _snap("openai"))
    bc = RecordingBroadcaster()
    await run_cycle(
        [FakeAdapter("openai", _snap("openai", Indicator.MINOR, [minor]))],
        fetcher=None,
        store=store,
        broadcaster=bc,
        state=PollState(last_card_ts={"openai": time.monotonic()}),
        card_min_gap_s=600,
    )
    assert bc.sent == []
    assert store.get("openai").incidents == []
