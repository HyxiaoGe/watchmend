# src/sentinel/engine.py
from __future__ import annotations

import json
import logging

from sentinel.feishu.cards import build_event_card, build_recovery_card
from sentinel.findings import Finding
from sentinel.store import Store

logger = logging.getLogger("sentinel")


async def apply_findings(
    findings: list[Finding],
    *,
    scope: frozenset[str] | set[str],
    store: Store,
    feishu,
    now_ts: int,
    now_str: str,
    cooldown_seconds: int,
    hold: set[tuple[str, str]] | frozenset[tuple[str, str]] = frozenset(),
) -> None:
    """事件状态机。findings 必须是本轮 scope 内规则的全部命中:
    新命中(无 open、过冷却)→ 发事件卡再落库;open 但未命中 → 发恢复卡再置 resolved;
    点事件落库即 resolved,无恢复卡,靠冷却去重。
    scope 只能包含本轮真正评估过的规则——数据源失败时调用方必须把对应规则排除;
    hold 是规则在 scope 内、但个别 (rule, subject) 本轮被跳过评估的豁免名单
    (如服务挂掉时跳过其延迟评估)——两者共同保证"不评估≠恢复"。
    单事件发送失败只记日志不落库(send-then-commit),下轮重评估自动重试,
    且不阻塞同轮其他事件。
    发卡成功但落库失败同样只记日志跳过(下轮可能重发卡,但库里绝不会有没发出去的事件)。
    非重入:同一规则族必须由单个串行循环调用,跨族 scope 互不相交时并发安全。"""
    open_by_key = {(e.rule, e.subject): e for e in store.get_open_events() if e.rule in scope}
    firing: set[tuple[str, str]] = set()
    for f in findings:
        key = (f.rule, f.subject)
        if key in firing:
            continue  # 同轮重复命中(如 PromQL 重复 series):只处理第一条
        firing.add(key)
        if key in open_by_key:
            continue  # 已 open:不重发
        if store.get_cooldown_until(f.rule, f.subject) > now_ts:
            continue
        try:
            await feishu.send(build_event_card(f, now_str=now_str))
        except Exception:
            logger.exception("event card send failed: %s/%s", f.rule, f.subject)
            continue
        try:
            event_id = store.insert_event(
                ts=now_ts,
                rule=f.rule,
                subject=f.subject,
                severity=f.severity,
                status="resolved" if f.point else "open",
                detail=f.detail,
                payload_json=json.dumps(f.payload, ensure_ascii=False),
                diagnosis_status="pending" if f.needs_diagnosis else "skipped",
                cooldown_until=now_ts + cooldown_seconds,
                resolved_ts=now_ts if f.point else None,
            )
        except Exception:
            logger.exception("event insert failed: %s/%s", f.rule, f.subject)
            continue
        if f.point:
            logger.info("point event recorded: id=%s %s/%s", event_id, f.rule, f.subject)
        else:
            logger.info("event opened: id=%s %s/%s", event_id, f.rule, f.subject)
    for key, event in open_by_key.items():
        if key in firing or key in hold:
            continue
        try:
            await feishu.send(build_recovery_card(event, now_ts=now_ts, now_str=now_str))
        except Exception:
            logger.exception("recovery card send failed: %s/%s", event.rule, event.subject)
            continue
        store.resolve_event(event.id, resolved_ts=now_ts)
        logger.info("event resolved: %s/%s", event.rule, event.subject)
