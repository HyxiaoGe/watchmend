import json
from pathlib import Path

from sentinel.codex_reset.models import ResetStage, ResetType
from sentinel.codex_reset.sources import (
    parse_radar_current,
    parse_radar_rss,
    parse_reset_feed,
    parse_reset_html,
    parse_timeline,
    parse_timestamp,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_real_timeline_contract_classifies_hint_announced_and_confirmed():
    result = parse_timeline(_json("codex_reset_timeline_sample.json"))
    by_id = {item.source_item_id: item for item in result.evidence}
    assert by_id["2086189414292865249"].signal_stage is ResetStage.HINT
    announced = by_id["2087706104814023111"]
    assert announced.signal_stage is ResetStage.ANNOUNCED
    assert announced.reset_type is ResetType.DIRECT
    assert announced.expected_end_ts == parse_timestamp("2026-08-13T02:01:37.000Z")
    assert by_id["2086972802457063486"].signal_stage is ResetStage.CONFIRMED


def test_official_banked_credit_during_day_is_reliable_announcement():
    data = _json("codex_reset_banked_announcement_sample.json")
    result = parse_timeline(data)
    feed_announced = parse_reset_feed(data).evidence[0]
    assert feed_announced.source_name == "reset_feed"
    assert feed_announced.signal_stage is ResetStage.ANNOUNCED
    tweet_only = _json("codex_reset_banked_announcement_sample.json")
    tweet_only["events"] = []
    tweet_announcement = parse_reset_feed(tweet_only).evidence[0]
    assert tweet_announcement.source_item_id == "2090766694897619318"
    assert tweet_announcement.signal_stage is ResetStage.ANNOUNCED
    assert tweet_announcement.reset_type is ResetType.BANKED
    assert len(result.evidence) == 1
    announced = result.evidence[0]
    assert announced.source_item_id == "2090766694897619318"
    assert announced.signal_stage is ResetStage.ANNOUNCED
    assert announced.reset_type is ResetType.BANKED
    assert announced.official is True
    assert announced.expected_start_ts == parse_timestamp("2026-08-21T11:43:19.000Z")
    assert announced.expected_end_ts == announced.expected_start_ts + 86400


def test_generic_credit_event_is_not_mistaken_for_banked_reset():
    data = _json("codex_reset_banked_announcement_sample.json")
    event = data["events"][0]
    event["reset_kind"] = None
    event["summary"] = "20M active users celebration credits during the day."
    assert parse_timeline(data).evidence == []
    data["events"] = []
    data["tweets"][0]["kind"] = None
    data["tweets"][0]["text"] = "20M active users celebration credits during the day."
    assert parse_reset_feed(data).evidence == []


def test_announced_requires_official_window_type_and_link():
    data = _json("codex_reset_timeline_sample.json")
    event = data["events"][0]
    event["official_window"] = None
    result = parse_timeline(data)
    item = next(e for e in result.evidence if e.source_item_id == event["id"])
    assert item.signal_stage is ResetStage.HINT


def test_feed_uses_contextual_event_id_but_does_not_trust_remote_reference_as_local():
    result = parse_reset_feed(_json("codex_reset_feed_sample.json"))
    by_id = {item.source_item_id: item for item in result.evidence}
    assert by_id["2087706104814023111"].canonical_hint == "x:2087423996115681767"
    confirmed = by_id["2086972802457063486"]
    assert confirmed.canonical_hint == "x:2086189414292865249"
    assert confirmed.signal_stage is ResetStage.CONFIRMED
    assert confirmed.local_reference is False


def test_radar_current_probability_is_not_an_event_but_official_window_is_hint():
    result = parse_radar_current(_json("codex_reset_radar_current_sample.json"))
    assert len(result.evidence) == 1
    assert result.evidence[0].signal_stage is ResetStage.HINT
    assert result.content_ts == parse_timestamp("2026-07-22T06:57:20.626603+08:00")


def test_real_rss_contract_parses_announcement_and_completion():
    text = (FIXTURES / "codex_reset_rss_sample.xml").read_text(encoding="utf-8")
    result = parse_radar_rss(text)
    assert [item.signal_stage for item in result.evidence] == [
        ResetStage.ANNOUNCED,
        ResetStage.CONFIRMED,
    ]


def test_html_is_confirmation_only_cross_check():
    text = (FIXTURES / "codex_reset_html_sample.html").read_text(encoding="utf-8")
    result = parse_reset_html(text)
    assert len(result.evidence) == 1
    assert result.evidence[0].source_family == "codexreset_org"
    assert result.evidence[0].signal_stage is ResetStage.CONFIRMED


def test_time_conversion_normalizes_offset_and_zulu():
    assert parse_timestamp("2026-08-13T09:01:37+08:00") == parse_timestamp("2026-08-13T01:01:37Z")
