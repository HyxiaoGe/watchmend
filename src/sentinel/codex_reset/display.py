from __future__ import annotations

_DECISION_LABEL = {
    "ignore": "无关信号",
    "hint": "疑似预告",
    "announced": "明确预告",
}
_TYPE_LABEL = {
    "direct": "直接重置",
    "banked": "储备重置（Banked reset）",
    "unknown": "待确认",
}
_TIME_LABEL = {
    "during the day": "当天内",
    "during day": "当天内",
    "later today": "今天晚些时候",
    "today": "今天",
    "tomorrow": "明天",
    "within an hour": "一小时内",
    "within the next hour": "接下来一小时内",
    "next hour": "接下来一小时内",
}


def reset_type_label(value: object) -> str:
    raw = getattr(value, "value", value)
    return _TYPE_LABEL.get(str(raw or "unknown"), "待确认")


def semantic_time_label(value: str) -> str:
    normalized = " ".join(value.strip().lower().split())
    return _TIME_LABEL.get(normalized, value.strip() or "未提取")


def build_semantic_display(
    *, decision: str, confidence: float, reset_type: str, time_text: str, reason: str
) -> str:
    return "\n".join(
        [
            f"**模型判定**：{_DECISION_LABEL.get(decision, '待确认')}",
            f"**置信度**：{confidence:.2f}",
            f"**模型识别类型**：{reset_type_label(reset_type)}",
            f"**原文时间表达**：{semantic_time_label(time_text)}",
            f"**识别理由**：{reason}",
        ]
    )
