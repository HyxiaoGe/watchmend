# src/sentinel/notify/message.py
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Severity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class Kind(StrEnum):
    ALERT = "alert"  # 内部巡检 finding(metrics/logs/docker/probe)
    RECOVERY = "recovery"  # open 事件恢复
    VENDOR_INCIDENT = "vendor_incident"  # 外部状态页变更
    HEARTBEAT = "heartbeat"  # 每日心跳
    REPORT = "report"  # 每日体检日报
    DIGEST = "digest"  # 非紧急事件分时摘要
    CODEX_TURN = "codex_turn"  # Codex 主回合完成（不等同于任务成功）
    CODEX_RESET = "codex_reset"  # Codex 共享额度 reset 四阶段
    DIAGNOSIS = "diagnosis"  # LLM 诊断结果
    SUMMARY = "summary"  # 日报 AI 总结


@dataclass
class Notification:
    """中立语义事件:业务层产出,各渠道各自渲染。

    title/detail/fields/severity 供文本渠道(Telegram/ntfy)与 webhook;
    data 携带 kind 专属结构化负载(domain 对象 + 渲染参数),供 FeishuChannel
    复刻现有卡片、供 webhook 序列化。link 留给③面板(当前多为空)。
    """

    kind: Kind
    severity: Severity
    title: str
    detail: str = ""
    fields: list[tuple[str, str]] = field(default_factory=list)
    subject: str = ""
    link: str | None = None
    ts: int = 0
    data: dict = field(default_factory=dict)
