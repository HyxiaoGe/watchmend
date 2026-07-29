# src/sentinel/engine.py
from __future__ import annotations

import json
import logging

from sentinel.findings import Finding
from sentinel.notification_policy import should_notify_immediately
from sentinel.notify.build import alert_notification, recovery_notification
from sentinel.store import Store

logger = logging.getLogger("sentinel")


async def apply_findings(
    findings: list[Finding],
    *,
    scope: frozenset[str] | set[str],
    store: Store,
    broadcaster,
    now_ts: int,
    now_str: str,
    cooldown_seconds: int,
    hold: set[tuple[str, str]] | frozenset[tuple[str, str]] = frozenset(),
    max_new_sends: int | None = None,
    defer_nonurgent: bool = False,
) -> None:
    """事件状态机。findings 必须是本轮 scope 内规则的全部命中:
    新命中(无 open、过冷却)→ 广播事件再落库;open 但未命中 → 广播恢复再置 resolved;
    点事件落库即 resolved,无恢复,靠冷却去重。
    scope 只能包含本轮真正评估过的规则——数据源失败时调用方必须把对应规则排除;
    hold 是规则在 scope 内、但个别 (rule, subject) 本轮被跳过评估的豁免名单
    (如服务挂掉时跳过其延迟评估)——两者共同保证"不评估≠恢复"。
    send-then-commit:广播到任一渠道成功(返回 ≥1)才落库;全渠道失败(0)只记日志不落库,
    下轮重评估自动重试,且不阻塞同轮其他事件。
    发送成功但落库失败同样只记日志跳过(下轮可能重发,但库里绝不会有没发出去的事件)。
    max_new_sends 限制单轮接纳的新事件数，恢复不受限；用于错误指纹冷启动背压。
    defer_nonurgent=True 时，非硬告警进入分时摘要，默认 False 保持既有实时行为。
    非重入:同一规则族必须由单个串行循环调用,跨族 scope 互不相交时并发安全。"""
    open_by_key = {(e.rule, e.subject): e for e in store.get_open_events() if e.rule in scope}
    firing: set[tuple[str, str]] = set()
    admitted = 0
    immediate_sent = 0
    capped = 0
    for f in findings:
        key = (f.rule, f.subject)
        if key in firing:
            continue  # 同轮重复命中:只处理第一条
        firing.add(key)
        if key in open_by_key:
            continue  # 已 open:不重发
        if store.get_cooldown_until(f.rule, f.subject) > now_ts:
            continue
        if max_new_sends is not None and admitted >= max_new_sends:
            capped += 1
            continue
        immediate = not defer_nonurgent or should_notify_immediately(f)
        if immediate:
            if await broadcaster.send(alert_notification(f, now_ts=now_ts, now_str=now_str)) < 1:
                logger.warning("alert not delivered to any channel: %s/%s", f.rule, f.subject)
                continue
            immediate_sent += 1
        else:
            store.enqueue_digest(f, now_ts=now_ts, state="observed")
        admitted += 1
        try:
            event_id = store.insert_event(
                ts=now_ts,
                rule=f.rule,
                subject=f.subject,
                severity=f.severity,
                status="resolved" if f.point else "open",
                detail=f.detail,
                payload_json=json.dumps(f.payload, ensure_ascii=False),
                diagnosis_status="pending" if f.needs_diagnosis and immediate else "skipped",
                cooldown_until=now_ts + cooldown_seconds,
                resolved_ts=now_ts if f.point else None,
                notified=immediate,
            )
        except Exception:
            logger.exception("event insert failed: %s/%s", f.rule, f.subject)
            continue
        if f.point:
            logger.info("point event recorded: id=%s %s/%s", event_id, f.rule, f.subject)
        else:
            logger.info("event opened: id=%s %s/%s", event_id, f.rule, f.subject)
    if capped:
        logger.warning(
            "max_new_sends=%s 达上限,本轮接纳 %d 个（实时广播 %d 个）、延后 %d 个命中",
            max_new_sends,
            admitted,
            immediate_sent,
            capped,
        )
    for key, event in open_by_key.items():
        if key in firing or key in hold:
            continue
        if event.notified:
            if (
                await broadcaster.send(recovery_notification(event, now_ts=now_ts, now_str=now_str))
                < 1
            ):
                logger.warning("recovery not delivered: %s/%s", event.rule, event.subject)
                continue
        else:
            store.resolve_deferred_digest(event, now_ts=now_ts)
        store.resolve_event(event.id, resolved_ts=now_ts)
        logger.info("event resolved: %s/%s", event.rule, event.subject)
