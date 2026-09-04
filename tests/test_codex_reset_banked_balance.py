import httpx

from sentinel.codex_reset.engine import ResetMonitor
from sentinel.codex_reset.models import (
    BankedResetBalance,
    FetchedSource,
    ResetStage,
    ResetType,
)
from sentinel.codex_reset.notify import build_codex_reset_card
from sentinel.codex_reset.reference import banked_balance_from_rate_limits
from sentinel.codex_reset.store import ResetStore
from tests.test_codex_reset_engine import (
    FakeBroadcaster,
    FakeSource,
    _evidence,
    _settings,
)


def _balance(count: int, observed_at: int) -> BankedResetBalance:
    return BankedResetBalance(
        source_name="reference_account",
        available_count=count,
        observed_at=observed_at,
    )


def _fetched(*, at: int, count: int, announcement=None) -> FetchedSource:
    return FetchedSource(
        name="reference_account",
        family="local_reference",
        content_ts=at,
        evidence=[announcement] if announcement is not None else [],
        banked_balances=[_balance(count, at)],
    )


def test_official_available_count_is_parsed_without_credit_details():
    result = {
        "rateLimitResetCredits": {
            "availableCount": 2,
            "credits": None,
        }
    }
    balance = banked_balance_from_rate_limits(result, now_ts=1000)
    assert balance == _balance(2, 1000)


def test_missing_invalid_or_negative_available_count_is_unknown():
    for value in (None, True, -1, 1.5, "2"):
        result = {"rateLimitResetCredits": {"availableCount": value}}
        assert banked_balance_from_rate_limits(result, now_ts=1000) is None
    assert banked_balance_from_rate_limits({}, now_ts=1000) is None
    assert banked_balance_from_rate_limits({"rateLimitResetCredits": None}, now_ts=1000) is None


def test_balance_baseline_and_positive_change_require_two_equal_reads(tmp_path):
    store = ResetStore(str(tmp_path / "reset.db"))
    assert store.observe_banked_balance(_balance(2, 1000)) is None
    assert store.observe_banked_balance(_balance(3, 1060)) is None
    increase = store.observe_banked_balance(_balance(3, 1120))
    assert increase is not None
    assert increase.delta == 1
    assert increase.observed_at == 1060
    assert store.observe_banked_balance(_balance(3, 1180)) is None
    health = store.health(now_ts=1180, freshness_seconds=120)["banked_balance"]
    assert health["initialized"] is True
    assert health["change_pending_confirmation"] is False
    assert "stable_count" not in health
    store.close()


def test_decrease_is_not_arrival_and_later_increase_is_detected(tmp_path):
    store = ResetStore(str(tmp_path / "reset.db"))
    assert store.observe_banked_balance(_balance(3, 1000)) is None
    assert store.observe_banked_balance(_balance(2, 1060)) is None
    assert store.observe_banked_balance(_balance(2, 1120)) is None
    assert store.observe_banked_balance(_balance(3, 1180)) is None
    increase = store.observe_banked_balance(_balance(3, 1240))
    assert increase is not None and increase.delta == 1
    store.close()


def test_pending_balance_change_survives_restart(tmp_path):
    path = str(tmp_path / "reset.db")
    store = ResetStore(path)
    assert store.observe_banked_balance(_balance(4, 1000)) is None
    assert store.observe_banked_balance(_balance(5, 1060)) is None
    store.close()

    store = ResetStore(path)
    increase = store.observe_banked_balance(_balance(5, 1120))
    assert increase is not None and increase.delta == 1
    assert increase.observed_at == 1060
    store.close()


async def test_banked_increase_upgrades_recent_announcement_and_sends_arrival_card(tmp_path):
    announcement = _evidence(
        ResetStage.ANNOUNCED,
        item="banked-announcement",
        observed=1000,
        reset_type=ResetType.BANKED,
        canonical="x:banked-announcement",
    )
    source = FakeSource(
        "reference_account",
        "local_reference",
        [
            _fetched(at=1000, count=2, announcement=announcement),
            _fetched(at=1060, count=3, announcement=announcement),
            _fetched(at=1120, count=3, announcement=announcement),
        ],
    )
    now = [1000]
    broadcaster = FakeBroadcaster()
    async with httpx.AsyncClient() as client:
        monitor = ResetMonitor(
            settings=_settings(tmp_path),
            client=client,
            broadcaster=broadcaster,
            sources=[source],
            clock=lambda: now[0],
            owner="banked-arrival",
        )
        await monitor.tick()
        now[0] = 1060
        await monitor.tick()
        assert [item.data["stage"] for item in broadcaster.sent] == [ResetStage.ANNOUNCED]
        now[0] = 1120
        await monitor.tick()

        event = monitor.store.get_event("x:banked-announcement")
        assert event is not None and event.stage is ResetStage.CONFIRMED
        assert [item.title for item in broadcaster.sent] == [
            "Codex 重置预告",
            "Codex Banked reset 已到账",
        ]
        notification = broadcaster.sent[-1]
        assert "数量增加 1 次" in notification.detail
        card = build_codex_reset_card(
            notification.data["event"],
            ResetStage.CONFIRMED,
            now_str="now",
            utc_offset=8,
        )
        body = card["card"]["elements"][0]["text"]["content"]
        assert "到账范围**：本机参考账号" in body
        assert "预计截止" not in body
        monitor.close()


async def test_each_later_banked_increase_gets_a_distinct_confirmed_event(tmp_path):
    announcement = _evidence(
        ResetStage.ANNOUNCED,
        item="daily-banked",
        observed=1000,
        reset_type=ResetType.BANKED,
        canonical="x:daily-banked",
    )
    source = FakeSource(
        "reference_account",
        "local_reference",
        [
            _fetched(at=1000, count=1, announcement=announcement),
            _fetched(at=1060, count=2, announcement=announcement),
            _fetched(at=1120, count=2, announcement=announcement),
            _fetched(at=1180, count=3, announcement=announcement),
            _fetched(at=1240, count=3, announcement=announcement),
        ],
    )
    now = [1000]
    broadcaster = FakeBroadcaster()
    async with httpx.AsyncClient() as client:
        monitor = ResetMonitor(
            settings=_settings(tmp_path),
            client=client,
            broadcaster=broadcaster,
            sources=[source],
            clock=lambda: now[0],
            owner="daily-banked",
        )
        for timestamp in (1000, 1060, 1120, 1180, 1240):
            now[0] = timestamp
            await monitor.tick()

        arrivals = [item for item in broadcaster.sent if item.data["stage"] is ResetStage.CONFIRMED]
        assert len(arrivals) == 2
        assert arrivals[0].subject == "x:daily-banked"
        assert arrivals[1].subject.startswith("local-banked:")
        assert arrivals[1].link == announcement.url
        monitor.close()


async def test_unannounced_banked_increase_is_account_scoped_not_silent_direct(tmp_path):
    source = FakeSource(
        "reference_account",
        "local_reference",
        [
            _fetched(at=1000, count=0),
            _fetched(at=1060, count=1),
            _fetched(at=1120, count=1),
        ],
    )
    now = [1000]
    broadcaster = FakeBroadcaster()
    async with httpx.AsyncClient() as client:
        monitor = ResetMonitor(
            settings=_settings(tmp_path),
            client=client,
            broadcaster=broadcaster,
            sources=[source],
            clock=lambda: now[0],
            owner="unannounced-banked",
        )
        for timestamp in (1000, 1060, 1120):
            now[0] = timestamp
            await monitor.tick()

        assert len(broadcaster.sent) == 1
        assert broadcaster.sent[0].title == "Codex Banked reset 已到账"
        assert broadcaster.sent[0].link is None
        monitor.close()
