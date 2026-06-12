# src/sentinel/probe_rules.py
from __future__ import annotations

from sentinel.config import Settings
from sentinel.findings import Finding
from sentinel.models import ProbeSample
from sentinel.report import percentile
from sentinel.store import Store


def evaluate_probe_rules(
    store: Store,
    services: list[str],
    settings: Settings,
    *,
    now_ts: int,
    date_str: str,
) -> tuple[list[Finding], set[tuple[str, str]]]:
    """探针时序规则:service_down(近 1h 窗口内最后连续 N 次失败;Kuma 已发即时告警,
    此事件供诊断)+ latency_degraded(近 1h p95 对七日基线)。每个探针周期评估一次。
    返回 (findings, hold):hold 是本轮被跳过评估的 (rule, subject)——服务挂掉或
    样本不足时延迟没被评估,engine 不得把 open 的延迟事件误判恢复。
    连败只看 1h 窗口:跨停机的陈旧失败样本不算"连续"。"""
    findings: list[Finding] = []
    hold: set[tuple[str, str]] = set()
    streak = settings.sentinel_probe_fail_streak
    # 1h 窗口与 streak×probe_interval 耦合:默认 3×300s=900s 留 4 倍余量;
    # 若把 probe_interval 调到 >1200s,窗口凑不满 streak 条,连败判定会静默失效
    window = store.get_probe_samples_since(now_ts - 3600)
    by_service: dict[str, list[ProbeSample]] = {}
    for s in window:
        by_service.setdefault(s.service, []).append(s)

    for name in services:
        samples = by_service.get(name, [])
        recent = samples[-streak:]
        if len(recent) == streak and all(not s.ok for s in recent):
            codes = [s.status_code for s in recent]
            codes_text = ",".join(str(c) if c is not None else "超时/连接失败" for c in codes)
            findings.append(
                Finding(
                    rule="service_down",
                    subject=name,
                    severity="critical",
                    detail=f"连续 {streak} 次探针失败(状态码 {codes_text})",
                    payload={
                        "status_codes": codes,
                        "last_ts": recent[-1].ts,
                        "first_fail_ts": recent[0].ts,
                    },
                    needs_diagnosis=True,
                )
            )
            hold.add(("latency_degraded", name))
            continue  # 服务已挂,跳过延迟评估(hold 防误判恢复)

        if not samples or not samples[-1].ok:
            # 末样本失败/无样本:连败未满但无恢复证据(如 sentinel 自身停机后重启,
            # 窗口内失败样本不足 streak)——hold 防止仍挂着的服务被误判恢复
            hold.add(("service_down", name))

        latencies = [s.latency_ms for s in samples if s.ok and s.latency_ms is not None]
        if len(latencies) < settings.sentinel_latency_min_samples:
            hold.add(("latency_degraded", name))
            continue
        p95s = store.get_recent_daily_p95s(name, before_date=date_str)
        if not p95s:
            continue  # 无基线(新服务/首日):从未有基线就不可能有 open 延迟事件,无需 hold
        baseline = sum(p95s) / len(p95s)
        p95_1h = percentile(latencies, 95)
        threshold = max(
            baseline * settings.sentinel_latency_ratio,
            baseline + settings.sentinel_latency_margin_ms,
        )
        if p95_1h is not None and p95_1h > threshold:
            findings.append(
                Finding(
                    rule="latency_degraded",
                    subject=name,
                    severity="warning",
                    detail=(
                        f"近 1h p95 {p95_1h:.0f}ms,近{len(p95s)}日基线 {baseline:.0f}ms,"
                        f"阈值 {threshold:.0f}ms(样本 {len(latencies)})"
                    ),
                    payload={
                        "p95_1h_ms": p95_1h,
                        "baseline_ms": baseline,
                        "baseline_days": len(p95s),
                        "threshold_ms": threshold,
                        "samples": len(latencies),
                    },
                    needs_diagnosis=True,
                )
            )
    return findings, hold
