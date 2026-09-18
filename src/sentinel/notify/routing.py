"""Feishu 通知路由：按语义优先级对通知进行分类和前缀标记。

P0(立刻看)：连续健康失败/绿→红、配额告警(Codex ≤10%)、上游 major、部署/CI 失败阻塞发布。
P1(要验收)：Codex 长任务完成/阻塞待人工、确认卡、"请重新验证 PR"。
P2(流水账)：定时摘要、全绿卫生、上游 minor/恢复、成功合并/部署、信息性 Diun 摘要、软 cron 失败。

当前阶段：仅逻辑分类和可选标题前缀，不改变 webhook 目标。
"""

from __future__ import annotations

from sentinel.notify.message import Kind, Notification, Severity


class Priority:
    """通知优先级常量"""

    P0 = "p0"  # 立刻看 - 需要立即关注的严重问题
    P1 = "p1"  # 要验收 - 需要人工确认或验收的事项
    P2 = "p2"  # 流水账 - 信息性通知和成功消息


# 优先级标题前缀映射
_PRIORITY_PREFIXES = {
    Priority.P0: "[P0]",
    Priority.P1: "[验收]",
    Priority.P2: "[流水]",
}


def _classify_alert(n: Notification) -> str:
    """内部巡检 finding 分类：critical → P0, 其他 → P1"""
    return Priority.P0 if n.severity == Severity.CRITICAL else Priority.P1


def _classify_recovery(n: Notification) -> str:
    """恢复通知：总是 P2（信息性）"""
    return Priority.P2


def _classify_vendor_incident(n: Notification) -> str:
    """外部状态页变更分类：critical → P0, 其他 → P2"""
    return Priority.P0 if n.severity == Severity.CRITICAL else Priority.P2


def _classify_heartbeat(n: Notification) -> str:
    """心跳日报分类：warning → P0（存在异常）, 其他 → P2"""
    return Priority.P0 if n.severity == Severity.WARNING else Priority.P2


def _classify_report(n: Notification) -> str:
    """内部体检日报分类：critical → P0, warning → P1, 其他 → P2"""
    if n.severity == Severity.CRITICAL:
        return Priority.P0
    if n.severity == Severity.WARNING:
        return Priority.P1
    return Priority.P2


def _classify_digest(n: Notification) -> str:
    """巡检摘要：总是 P2（定时汇总）"""
    return Priority.P2


def _classify_codex_turn(n: Notification) -> str:
    """Codex 回合通知分类：
    - approval_required, input_required, execution_failed → P1（需人工介入）
    - long_turn_complete → P1（长任务完成需验收）
    - turn_complete → P2（普通完成）
    """
    category = n.data.get("category", "turn_complete")
    if category in {"approval_required", "input_required", "execution_failed"}:
        return Priority.P1
    if category == "long_turn_complete":
        return Priority.P1
    return Priority.P2


def _classify_codex_reset(n: Notification) -> str:
    """Codex 额度 reset 通知分类：
    - 预告/确认/延迟 → P1（需要人工关注额度状态）
    - 其他 → P2
    """
    stage = n.data.get("stage", "")
    if stage in {"forecast", "confirmed", "delayed"}:
        return Priority.P1
    return Priority.P2


def _classify_diagnosis(n: Notification) -> str:
    """诊断结果：P2（辅助信息，非紧急）"""
    return Priority.P2


def _classify_summary(n: Notification) -> str:
    """AI 总结：P2（辅助信息）"""
    return Priority.P2


# Kind → 分类函数映射
_CLASSIFIERS: dict[Kind, callable] = {
    Kind.ALERT: _classify_alert,
    Kind.RECOVERY: _classify_recovery,
    Kind.VENDOR_INCIDENT: _classify_vendor_incident,
    Kind.HEARTBEAT: _classify_heartbeat,
    Kind.REPORT: _classify_report,
    Kind.DIGEST: _classify_digest,
    Kind.CODEX_TURN: _classify_codex_turn,
    Kind.CODEX_RESET: _classify_codex_reset,
    Kind.DIAGNOSIS: _classify_diagnosis,
    Kind.SUMMARY: _classify_summary,
}


def classify_notification(n: Notification) -> str:
    """对通知进行优先级分类。

    Args:
        n: 通知对象

    Returns:
        优先级字符串：Priority.P0 / P1 / P2

    Raises:
        ValueError: 未知的通知类型
    """
    classifier = _CLASSIFIERS.get(n.kind)
    if classifier is None:
        # 防御性默认：未知类型默认 P1（既不静默也不过度告警）
        return Priority.P1
    return classifier(n)


def apply_title_prefix(title: str, priority: str) -> str:
    """为标题添加优先级前缀。

    Args:
        title: 原始标题
        priority: 优先级（Priority.P0/P1/P2）

    Returns:
        带前缀的标题（如果 priority 有对应前缀）；否则返回原标题
    """
    prefix = _PRIORITY_PREFIXES.get(priority)
    if prefix:
        return f"{prefix} {title}"
    return title
