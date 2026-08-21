from dataclasses import replace

from sentinel.codex_reset.models import ResetEvent, ResetStage, ResetType
from sentinel.codex_reset.notify import build_codex_reset_card, codex_reset_notification
from sentinel.notify.feishu_channel import render_card
from sentinel.notify.message import Kind


def _event(stage: ResetStage) -> ResetEvent:
    return ResetEvent(
        canonical_id="x:1",
        stage=stage,
        reset_type=ResetType.DIRECT,
        title="Codex reset",
        summary="官方公开信号",
        primary_url="https://x.com/thsottiaux/status/1",
        announced_ts=1000,
        expected_start_ts=1100,
        expected_end_ts=1200,
        confirmed_ts=1250 if stage is ResetStage.CONFIRMED else None,
        first_seen_ts=1000,
        last_seen_ts=1250,
        evidence_count=2,
        source_families=("a", "b"),
    )


def test_four_stage_cards_are_clear_and_use_distinct_colors():
    expected = {
        ResetStage.HINT: "blue",
        ResetStage.ANNOUNCED: "orange",
        ResetStage.CONFIRMED: "green",
        ResetStage.DELAYED: "red",
    }
    for stage, color in expected.items():
        card = build_codex_reset_card(_event(stage), stage, now_str="now", utc_offset=8)
        assert card["msg_type"] == "interactive"
        assert card["card"]["header"]["template"] == color
        assert "Codex 重置" in card["card"]["header"]["title"]["content"]
        body = card["card"]["elements"][0]["text"]["content"]
        assert "确认依据" in body
        assert "2 条 / 2 个来源族" in body
        assert "usedPercent" not in body
        assert "额度百分比" not in body


def test_banked_announcement_card_uses_official_approximate_time_wording():
    event = replace(_event(ResetStage.ANNOUNCED), reset_type=ResetType.BANKED)
    card = build_codex_reset_card(event, ResetStage.ANNOUNCED, now_str="now", utc_offset=8)
    body = card["card"]["elements"][0]["text"]["content"]
    assert "官方称当天内（以原帖表述为准）" in body
    assert "预计截止" not in body
    notification = codex_reset_notification(
        event, ResetStage.ANNOUNCED, now_ts=1300, now_str="now", utc_offset=8
    )
    assert ("预计时间", "官方称当天内（以原帖表述为准）") in notification.fields


def test_reset_notification_reuses_feishu_channel_renderer():
    event = _event(ResetStage.ANNOUNCED)
    notification = codex_reset_notification(
        event,
        ResetStage.ANNOUNCED,
        now_ts=1300,
        now_str="now",
        utc_offset=8,
    )
    assert notification.kind is Kind.CODEX_RESET
    assert render_card(notification) == build_codex_reset_card(
        event, ResetStage.ANNOUNCED, now_str="now", utc_offset=8
    )
