import json
from pathlib import Path

import httpx

from sentinel.codex_reset.engine import ResetMonitor
from sentinel.codex_reset.models import FetchedSource, ResetStage
from sentinel.codex_reset.sources import parse_reset_feed, parse_timeline, parse_timestamp
from tests.test_codex_reset_engine import FakeBroadcaster, FakeSource, _settings


def _payload():
    return json.loads(
        (Path(__file__).parent / "fixtures/codex_reset_tease_completion_sample.json").read_text(
            "utf-8"
        )
    )


def _tease_only():
    data = _payload()
    data["events"] = []
    data["tweets"] = data["tweets"][:1]
    return parse_reset_feed(data)


def _completion_only():
    data = _payload()
    data["tweets"] = data["tweets"][1:]
    return parse_reset_feed(data)


def test_real_feed_tease_and_completion_contract():
    tease = _tease_only().evidence[0]
    assert tease.source_item_id == "2092862554632826968"
    assert tease.signal_stage is ResetStage.HINT
    assert tease.reset_type.value == "direct"
    assert tease.observed_at == parse_timestamp("2026-08-27T14:31:31+08:00")

    for parsed in (parse_reset_feed(_payload()), parse_timeline(_payload())):
        completed = next(e for e in parsed.evidence if e.source_item_id == "2093014447833116908")
        assert completed.signal_stage is ResetStage.CONFIRMED
        assert completed.explicit_completed is True
        assert completed.title == "Live radar feed"
        assert ResetMonitor._classify([completed]) is ResetStage.CONFIRMED


async def test_tease_then_different_post_completion_keeps_one_canonical_event(tmp_path):
    tease = _tease_only()
    completed = parse_reset_feed(_payload())
    source = FakeSource("reset_feed", "codexreset", [tease, completed, completed])
    broadcaster = FakeBroadcaster()
    now = [parse_timestamp("2026-08-27T06:32:00Z")]
    async with httpx.AsyncClient() as client:
        monitor = ResetMonitor(
            settings=_settings(tmp_path),
            client=client,
            broadcaster=broadcaster,
            sources=[source],
            clock=lambda: now[0],
            owner="tease-complete",
        )
        await monitor.tick()
        now[0] = parse_timestamp("2026-08-27T16:36:00Z")
        await monitor.tick()
        await monitor.tick()

        assert [(n.subject, n.data["stage"]) for n in broadcaster.sent] == [
            ("x:2092862554632826968", ResetStage.HINT),
            ("x:2092862554632826968", ResetStage.CONFIRMED),
        ]
        event = monitor.store.get_event("x:2092862554632826968")
        assert event.stage is ResetStage.CONFIRMED
        assert event.primary_url.endswith("/2093014447833116908")
        assert monitor.store.get_event("x:2093014447833116908") is None
        monitor.close()


async def test_late_tease_is_attached_to_existing_completion_without_reverse_notification(tmp_path):
    completed = _completion_only()
    tease_and_completed = FetchedSource(
        name="reset_feed",
        family="codexreset",
        content_ts=completed.content_ts,
        evidence=[*_tease_only().evidence, *completed.evidence],
        intent_candidates=completed.intent_candidates,
    )
    source = FakeSource(
        "reset_feed", "codexreset", [completed, tease_and_completed, tease_and_completed]
    )
    broadcaster = FakeBroadcaster()
    now = [parse_timestamp("2026-08-27T16:36:00Z")]
    async with httpx.AsyncClient() as client:
        monitor = ResetMonitor(
            settings=_settings(tmp_path),
            client=client,
            broadcaster=broadcaster,
            sources=[source],
            clock=lambda: now[0],
            owner="late-tease",
        )
        await monitor.tick()
        now[0] += 60
        await monitor.tick()
        await monitor.tick()
        assert [(n.subject, n.data["stage"]) for n in broadcaster.sent] == [
            ("x:2093014447833116908", ResetStage.CONFIRMED)
        ]
        evidence = monitor.store.evidence_for("x:2093014447833116908")
        assert any(e.signal_stage is ResetStage.HINT for e in evidence)
        monitor.close()


async def test_moving_reference_window_never_becomes_confirmation(tmp_path):
    from sentinel.codex_reset.reference import ReferenceRateLimitSource

    source = ReferenceRateLimitSource(
        cli_path="unused",
        codex_home=str(tmp_path),
        runtime_home=str(tmp_path / "runtime"),
        timeout_seconds=5,
        min_window_minutes=10000,
        max_reset_age_seconds=86400,
    )
    starts = iter([1000, 1060, 1120])
    source._validate_home = lambda: None
    source._read_rate_limits = lambda: None

    async def moving():
        start = next(starts)
        return {
            "rateLimitsByLimitId": {
                "codex": {"primary": {"windowDurationMins": 10080, "resetsAt": start + 604800}}
            }
        }

    source._read_rate_limits = moving
    source._clock = lambda: 1200
    for _ in range(3):
        assert (await source.fetch(None)).evidence == []


async def test_stable_reference_requires_two_consecutive_reads(tmp_path):
    from sentinel.codex_reset.reference import ReferenceRateLimitSource

    source = ReferenceRateLimitSource(
        cli_path="unused",
        codex_home=str(tmp_path),
        runtime_home=str(tmp_path / "runtime"),
        timeout_seconds=5,
        min_window_minutes=10000,
        max_reset_age_seconds=86400,
        clock=lambda: 1200,
    )
    source._validate_home = lambda: None

    async def stable():
        return {
            "rateLimitsByLimitId": {
                "codex": {"primary": {"windowDurationMins": 10080, "resetsAt": 1000 + 604800}}
            }
        }

    source._read_rate_limits = stable
    assert (await source.fetch(None)).evidence == []
    evidence = (await source.fetch(None)).evidence
    assert len(evidence) == 1
    assert "连续两次" in evidence[0].summary
