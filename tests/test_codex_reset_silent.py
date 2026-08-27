import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from sentinel.codex_reset.engine import ResetMonitor
from sentinel.codex_reset.models import FetchedSource, ResetStage
from sentinel.codex_reset.notify import build_codex_reset_card
from sentinel.codex_reset.reference import evidence_from_rate_limits
from sentinel.codex_reset.sources import parse_reset_feed, parse_timeline, parse_timestamp
from tests.test_codex_reset_engine import FakeBroadcaster, FakeSource, _settings


def _payload():
    return json.loads(
        (Path(__file__).parent / "fixtures/codex_reset_silent_sample.json").read_text("utf-8")
    )


def _public():
    return parse_reset_feed(_payload()).evidence[0]


def _reference(start=None):
    start = start or parse_timestamp("2026-08-25T14:16:09Z")
    return evidence_from_rate_limits(
        {
            "rateLimitsByLimitId": {
                "codex": {
                    "primary": {"windowDurationMins": 10080, "resetsAt": start + 604800},
                }
            }
        },
        now_ts=start + 3600,
        min_window_minutes=10000,
        max_reset_age_seconds=86400,
    )


def _source(name, items):
    return FakeSource(
        name, name, [FetchedSource(name=name, family=name, content_ts=1787713000, evidence=items)]
    )


def test_real_silent_archive_is_confirmation_not_announcement():
    for parser in (parse_reset_feed, parse_timeline):
        result = parser(_payload())
        assert len(result.evidence) == 1
        e = result.evidence[0]
        assert e.signal_stage is ResetStage.CONFIRMED
        assert e.expected_end_ts is None
        assert e.explicit_completed
        assert e.canonical_hint == "x:2092311059197808936"
        assert e.observed_at == parse_timestamp("2026-08-25T22:15:00+08:00")


@pytest.mark.parametrize(
    "text",
    [
        "Tomorrow we will bring back the 5h limit for Plus accounts across ChatGPT Work and Codex.",
        "The reset should bring weekly usage back to 100% tomorrow.",
        "The reset did not bring weekly usage back to 100%.",
    ],
)
def test_policy_future_or_negated_restoration_is_not_completed(text):
    data = _payload()
    data["events"][0]["summary"] = text
    assert not any(e.explicit_completed for e in parse_timeline(data).evidence)


def test_same_tweet_mirrors_are_not_independent_confirmation():
    original = _public()
    mirror = replace(original, source_name="reset_html", source_family="codexreset_org")
    assert ResetMonitor._classify([original, mirror]) is None
    assert ResetMonitor._classify([original, mirror, _reference()]) is ResetStage.CONFIRMED
    assert ResetMonitor._classify([_reference()]) is None


@pytest.mark.parametrize("reverse", [False, True])
async def test_silent_confirmation_without_preview_is_sent_once_and_retryable(tmp_path, reverse):
    evidence = _public()
    sources = [_source("reset_feed", [evidence]), _source("reference_account", [_reference()])]
    if reverse:
        sources.reverse()
    broadcaster = FakeBroadcaster([0, 1])
    now = [1787713000]
    async with httpx.AsyncClient() as client:
        monitor = ResetMonitor(
            settings=_settings(tmp_path),
            client=client,
            broadcaster=broadcaster,
            sources=sources,
            clock=lambda: now[0],
            owner="silent",
        )
        await monitor.tick()
        event = monitor.store.get_event(evidence.canonical_hint)
        assert event.stage is ResetStage.CONFIRMED
        assert event.announced_ts is None
        assert monitor.store.delivery_status(event.canonical_id, ResetStage.CONFIRMED) == (
            "pending",
            1,
        )
        now[0] += 5
        await monitor.tick()
        assert len(broadcaster.sent) == 1
        now[0] += 5
        await monitor.tick()
        assert len(broadcaster.sent) == 2
        notification = broadcaster.sent[-1]
        assert notification.title == "Codex 静默重置已确认"
        card = build_codex_reset_card(
            notification.data["event"], ResetStage.CONFIRMED, now_str="now", utc_offset=8
        )
        body = card["card"]["elements"][0]["text"]["content"]
        assert "此前未发现预告" in body
        assert "本机共享 Codex 周额度窗口" in body
        assert "精确到账时刻" in body
        assert "预计截止" not in body
        monitor.close()
        # 重启后逐阶段回执仍阻止重复发送。
        monitor = ResetMonitor(
            settings=_settings(tmp_path),
            client=client,
            broadcaster=broadcaster,
            sources=sources,
            clock=lambda: now[0] + 60,
            owner="restarted",
        )
        await monitor.tick()
        assert len(broadcaster.sent) == 2
        monitor.close()


async def test_pending_reference_survives_restart_before_public_archive(tmp_path):
    broadcaster = FakeBroadcaster()
    stable_reference = replace(
        _reference(),
        summary="本机参考账号连续两次只读观察到同一共享 Codex七日额度窗口起点。",
    )
    async with httpx.AsyncClient() as client:
        monitor = ResetMonitor(
            settings=_settings(tmp_path),
            client=client,
            broadcaster=broadcaster,
            sources=[_source("reference_account", [stable_reference])],
            clock=lambda: 1787713000,
            owner="early",
        )
        await monitor.tick()
        assert broadcaster.sent == []
        monitor.close()
        monitor = ResetMonitor(
            settings=_settings(tmp_path),
            client=client,
            broadcaster=broadcaster,
            sources=[_source("reset_feed", [_public()])],
            clock=lambda: 1787713060,
            owner="late",
        )
        await monitor.tick()
        assert len(broadcaster.sent) == 1
        assert broadcaster.sent[0].title == "Codex 静默重置已确认"
        assert broadcaster.sent[0].subject == _public().canonical_hint
        monitor.close()


async def test_ambiguous_public_events_do_not_claim_one_reference_window(tmp_path):
    first = _public()
    second = replace(
        first,
        source_item_id="other",
        canonical_hint="x:other",
        url="https://x.com/thsottiaux/status/other",
        observed_at=first.observed_at + 1200,
    )
    async with httpx.AsyncClient() as client:
        broadcaster = FakeBroadcaster()
        monitor = ResetMonitor(
            settings=_settings(tmp_path),
            client=client,
            broadcaster=broadcaster,
            sources=[
                _source("reset_feed", [first, second]),
                _source("reference_account", [_reference()]),
            ],
            clock=lambda: 1787713000,
            owner="ambiguous",
        )
        await monitor.tick()
        assert broadcaster.sent == []
        monitor.close()


async def test_silent_public_confirmation_with_reference_failure_waits_safely(tmp_path):
    broadcaster = FakeBroadcaster()
    async with httpx.AsyncClient() as client:
        monitor = ResetMonitor(
            settings=_settings(tmp_path),
            client=client,
            broadcaster=broadcaster,
            sources=[
                _source("reset_feed", [_public()]),
                FakeSource("reference_account", "local_reference", [TimeoutError()]),
            ],
            clock=lambda: 1787713000,
            owner="failed-reference",
        )
        await monitor.tick()
        assert broadcaster.sent == []
        health = monitor.health()
        assert (
            next(s for s in health["sources"] if s["name"] == "reference_account")[
                "consecutive_failures"
            ]
            == 1
        )
        monitor.close()


async def test_later_reply_does_not_block_clearly_matching_reset_window(tmp_path):
    first = _public()
    later = replace(
        first,
        source_name="reset_html",
        source_family="codexreset_org",
        source_item_id="2092316228497063958",
        canonical_hint="x:2092316228497063958",
        url="https://x.com/thsottiaux/status/2092316228497063958",
        observed_at=1787682036,
    )
    broadcaster = FakeBroadcaster()
    async with httpx.AsyncClient() as client:
        monitor = ResetMonitor(
            settings=_settings(tmp_path),
            client=client,
            broadcaster=broadcaster,
            sources=[_source("reset_feed", [first]), _source("reference_account", [_reference()])],
            clock=lambda: 1787713000,
            owner="later-reply",
        )
        monitor.store.put_evidence(later.canonical_hint, later, now_ts=1787712000)
        await monitor.tick()
        assert [n.subject for n in broadcaster.sent] == [first.canonical_hint]
        monitor.close()


async def test_reference_outside_event_window_does_not_confirm(tmp_path):
    async with httpx.AsyncClient() as client:
        broadcaster = FakeBroadcaster()
        monitor = ResetMonitor(
            settings=_settings(tmp_path),
            client=client,
            broadcaster=broadcaster,
            sources=[
                _source("reset_feed", [_public()]),
                _source("reference_account", [_reference(1787712000)]),
            ],
            clock=lambda: 1787713000,
            owner="unrelated",
        )
        await monitor.tick()
        assert broadcaster.sent == []
        monitor.close()


def test_unused_model_window_cannot_override_shared_codex_window():
    start = parse_timestamp("2026-08-25T14:16:09Z")
    data = {
        "rateLimitsByLimitId": {
            "codex": {"primary": {"windowDurationMins": 10080, "resetsAt": start + 604800}},
            "codex_bengalfox": {
                "secondary": {"windowDurationMins": 10080, "resetsAt": start + 604800 + 3600},
            },
        }
    }
    params = dict(now_ts=start + 3601, min_window_minutes=10000, max_reset_age_seconds=86400)
    assert evidence_from_rate_limits(data, **params).observed_at == start
    del data["rateLimitsByLimitId"]["codex"]
    assert evidence_from_rate_limits(data, **params) is None
