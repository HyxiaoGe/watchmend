from dataclasses import replace

from sentinel.codex_reset.display import (
    build_semantic_display,
    reset_type_label,
    semantic_time_label,
)
from sentinel.codex_reset.models import ResetEvent, ResetStage, ResetType
from sentinel.codex_reset.notify import build_codex_reset_card


def test_semantic_values_are_localized_for_user_display():
    rendered = build_semantic_display(
        decision="announced",
        confidence=1.0,
        reset_type="banked",
        time_text="during the day",
        reason="官方作者明确表示将提供一次储备重置。",
    )
    assert "**模型判定**：明确预告" in rendered
    assert "**置信度**：1.00" in rendered
    assert "**模型识别类型**：储备重置（Banked reset）" in rendered
    assert "**原文时间表达**：当天内" in rendered
    assert "announced" not in rendered


def test_common_time_expressions_and_unknown_values_have_safe_labels():
    assert semantic_time_label("  Later   Today ") == "今天晚些时候"
    assert semantic_time_label("") == "未提取"
    assert reset_type_label("unknown") == "待确认"
    assert reset_type_label(None) == "待确认"


def test_regular_reset_card_uses_localized_banked_type():
    event = ResetEvent(
        canonical_id="x:1",
        stage=ResetStage.ANNOUNCED,
        reset_type=ResetType.BANKED,
        title="Codex reset",
        summary="Banked reset later today.",
        primary_url="https://x.com/thsottiaux/status/1",
        announced_ts=1000,
        expected_start_ts=1000,
        expected_end_ts=1100,
        confirmed_ts=None,
        first_seen_ts=1000,
        last_seen_ts=1000,
    )
    card = build_codex_reset_card(replace(event), ResetStage.ANNOUNCED, now_str="now", utc_offset=8)
    fields = card["card"]["elements"][0]["text"]["content"]
    assert "**类型**：储备重置（Banked reset）" in fields
