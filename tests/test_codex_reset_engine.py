import httpx

from sentinel.codex_reset.engine import ResetMonitor
from sentinel.codex_reset.models import FetchedSource, ResetEvidence, ResetStage, ResetType
from sentinel.config import Settings


class FakeSource:
    def __init__(self, name: str, family: str, results: list):
        self.name = name
        self.family = family
        self.results = list(results)
        self.calls = 0

    async def fetch(self, fetcher):
        index = min(self.calls, len(self.results) - 1)
        self.calls += 1
        result = self.results[index]
        if isinstance(result, Exception):
            raise result
        return result


class FakeBroadcaster:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [1])
        self.sent = []

    async def send(self, notification):
        self.sent.append(notification)
        index = min(len(self.sent) - 1, len(self.outcomes) - 1)
        return self.outcomes[index]


def _settings(tmp_path, **overrides):
    return Settings(
        _env_file=None,
        sentinel_db_path=str(tmp_path / "reset.db"),
        sentinel_codex_reset_delay_grace_seconds=100,
        sentinel_codex_reset_retry_base_seconds=10,
        sentinel_codex_reset_retry_max_seconds=60,
        sentinel_codex_reset_notify_max_age_hours=24,
        **overrides,
    )


def _evidence(
    stage: ResetStage,
    *,
    family: str = "a",
    source: str | None = None,
    item: str = "1",
    observed: int = 1000,
    canonical: str = "x:1",
    local_reference: bool = False,
) -> ResetEvidence:
    announced = stage is ResetStage.ANNOUNCED
    return ResetEvidence(
        source_name=source or f"source-{family}",
        source_family=family,
        source_item_id=item,
        canonical_hint=canonical,
        signal_stage=stage,
        title="Codex reset",
        summary="official Codex reset signal",
        url=f"https://x.com/thsottiaux/status/{item}",
        observed_at=observed,
        reset_type=ResetType.DIRECT,
        expected_start_ts=observed if announced else None,
        expected_end_ts=observed + 50 if announced else None,
        official=True,
        explicit_completed=stage is ResetStage.CONFIRMED,
        local_reference=local_reference,
    )


def _fetched(name: str, family: str, *items: ResetEvidence, content_ts=1000):
    return FetchedSource(
        name=name,
        family=family,
        content_ts=content_ts,
        evidence=list(items),
    )


async def test_hint_announced_confirmed_upgrade_and_per_stage_delivery(tmp_path):
    now = [1000]
    a = FakeSource(
        "a",
        "a",
        [
            _fetched("a", "a", _evidence(ResetStage.HINT)),
            _fetched("a", "a", _evidence(ResetStage.ANNOUNCED, item="2", observed=1010)),
            _fetched(
                "a",
                "a",
                _evidence(ResetStage.CONFIRMED, item="3", observed=1060),
                content_ts=1060,
            ),
        ],
    )
    b = FakeSource(
        "b",
        "b",
        [
            _fetched("b", "b"),
            _fetched("b", "b"),
            _fetched(
                "b",
                "b",
                _evidence(ResetStage.CONFIRMED, family="b", item="4", observed=1061),
                content_ts=1061,
            ),
        ],
    )
    broadcaster = FakeBroadcaster()
    client = httpx.AsyncClient()
    monitor = ResetMonitor(
        settings=_settings(tmp_path),
        client=client,
        broadcaster=broadcaster,
        sources=[a, b],
        clock=lambda: now[0],
        owner="test",
    )

    await monitor.tick()
    now[0] = 1010
    await monitor.tick()
    now[0] = 1061
    await monitor.tick()

    event = monitor.store.get_event("x:1")
    assert event is not None and event.stage is ResetStage.CONFIRMED
    assert [notification.data["stage"] for notification in broadcaster.sent] == [
        ResetStage.HINT,
        ResetStage.ANNOUNCED,
        ResetStage.CONFIRMED,
    ]
    for stage in (ResetStage.HINT, ResetStage.ANNOUNCED, ResetStage.CONFIRMED):
        assert monitor.store.delivery_status("x:1", stage) == ("delivered", 0)
    monitor.close()
    await client.aclose()


async def test_delayed_event_can_still_upgrade_to_confirmed(tmp_path):
    now = [1000]
    announcement = _evidence(ResetStage.ANNOUNCED, item="2", observed=1000)
    source_a = FakeSource(
        "a",
        "a",
        [
            _fetched("a", "a", announcement),
            _fetched("a", "a", announcement, content_ts=1200),
            _fetched(
                "a",
                "a",
                _evidence(ResetStage.CONFIRMED, item="3", observed=1210),
                content_ts=1210,
            ),
        ],
    )
    source_b = FakeSource(
        "b",
        "b",
        [
            _fetched("b", "b"),
            _fetched("b", "b", content_ts=1200),
            _fetched(
                "b",
                "b",
                _evidence(ResetStage.CONFIRMED, family="b", item="4", observed=1210),
                content_ts=1210,
            ),
        ],
    )
    broadcaster = FakeBroadcaster()
    client = httpx.AsyncClient()
    monitor = ResetMonitor(
        settings=_settings(tmp_path),
        client=client,
        broadcaster=broadcaster,
        sources=[source_a, source_b],
        clock=lambda: now[0],
        owner="test",
    )

    await monitor.tick()
    now[0] = 1200
    await monitor.tick()
    assert monitor.store.get_event("x:1").stage is ResetStage.DELAYED
    now[0] = 1210
    await monitor.tick()

    assert monitor.store.get_event("x:1").stage is ResetStage.CONFIRMED
    assert [notification.data["stage"] for notification in broadcaster.sent] == [
        ResetStage.ANNOUNCED,
        ResetStage.DELAYED,
        ResetStage.CONFIRMED,
    ]
    monitor.close()
    await client.aclose()


async def test_public_confirmation_requires_two_families(tmp_path):
    now = [1000]
    first = _evidence(ResetStage.CONFIRMED, observed=1000)
    second = _evidence(ResetStage.CONFIRMED, family="b", item="2", observed=1001)
    source_a = FakeSource("a", "a", [_fetched("a", "a", first)])
    source_b = FakeSource(
        "b",
        "b",
        [_fetched("b", "b"), _fetched("b", "b", second, content_ts=1001)],
    )
    broadcaster = FakeBroadcaster()
    client = httpx.AsyncClient()
    monitor = ResetMonitor(
        settings=_settings(tmp_path),
        client=client,
        broadcaster=broadcaster,
        sources=[source_a, source_b],
        clock=lambda: now[0],
        owner="test",
    )

    await monitor.tick()
    assert monitor.store.get_event("x:1") is None
    now[0] = 1001
    await monitor.tick()
    assert monitor.store.get_event("x:1").stage is ResetStage.CONFIRMED
    assert len(broadcaster.sent) == 1
    monitor.close()
    await client.aclose()


async def test_local_reference_can_confirm_without_second_public_family(tmp_path):
    source = FakeSource(
        "local",
        "local",
        [
            _fetched(
                "local",
                "local",
                _evidence(ResetStage.CONFIRMED, local_reference=True),
            )
        ],
    )
    broadcaster = FakeBroadcaster()
    client = httpx.AsyncClient()
    monitor = ResetMonitor(
        settings=_settings(tmp_path),
        client=client,
        broadcaster=broadcaster,
        sources=[source],
        clock=lambda: 1000,
        owner="test",
    )
    await monitor.tick()
    assert monitor.store.get_event("x:1").stage is ResetStage.CONFIRMED
    monitor.close()
    await client.aclose()


async def test_delivery_failure_uses_exponential_retry_and_then_commits(tmp_path):
    now = [1000]
    hint = _evidence(ResetStage.HINT)
    source = FakeSource("a", "a", [_fetched("a", "a", hint)])
    broadcaster = FakeBroadcaster([0, 0, 1])
    client = httpx.AsyncClient()
    monitor = ResetMonitor(
        settings=_settings(tmp_path),
        client=client,
        broadcaster=broadcaster,
        sources=[source],
        clock=lambda: now[0],
        owner="test",
    )

    await monitor.tick()
    assert monitor.store.delivery_status("x:1", ResetStage.HINT) == ("pending", 1)
    now[0] = 1009
    await monitor.tick()
    assert len(broadcaster.sent) == 1
    now[0] = 1010
    await monitor.tick()
    assert monitor.store.delivery_status("x:1", ResetStage.HINT) == ("pending", 2)
    now[0] = 1029
    await monitor.tick()
    assert len(broadcaster.sent) == 2
    now[0] = 1030
    await monitor.tick()
    assert monitor.store.delivery_status("x:1", ResetStage.HINT) == ("delivered", 2)
    monitor.close()
    await client.aclose()


async def test_one_source_failure_does_not_abort_tick_or_poison_health(tmp_path):
    now = [1000]
    source_a = FakeSource(
        "a",
        "a",
        [_fetched("a", "a", content_ts=1000), RuntimeError("offline")],
    )
    source_b = FakeSource(
        "b",
        "b",
        [_fetched("b", "b", content_ts=1000), _fetched("b", "b", content_ts=1010)],
    )
    client = httpx.AsyncClient()
    monitor = ResetMonitor(
        settings=_settings(tmp_path, sentinel_codex_reset_freshness_seconds=60),
        client=client,
        broadcaster=FakeBroadcaster(),
        sources=[source_a, source_b],
        clock=lambda: now[0],
        owner="test",
    )
    await monitor.tick()
    assert monitor.health()["status"] == "ok"
    now[0] = 1010
    await monitor.tick()
    health = monitor.health()
    assert health["status"] == "ok"
    assert (
        next(item for item in health["sources"] if item["name"] == "a")["consecutive_failures"] == 1
    )
    monitor.close()
    await client.aclose()
