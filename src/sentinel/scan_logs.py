# src/sentinel/scan_logs.py
from __future__ import annotations

from sentinel.config import Settings
from sentinel.findings import Finding
from sentinel.logql import LokiClient

_ERROR_QUERY = (
    'sum by (container) (count_over_time({container=~".+"}'
    ' |~ "(?i)error|exception|traceback" [15m]))'
)
_BASELINE_DAYS = 7


async def run_log_scan(loki: LokiClient, settings: Settings, *, now_ts: int) -> list[Finding]:
    """错误日志激增:当前 15m 错误数 vs 近 7 天同时段均值(同一查询在 now-1d..now-7d
    重放,现查 Loki 不落库;Loki 留存 30d 足够)。当前窗口无错误时只发 1 个查询即返回。
    任一查询失败整轮抛出,由调用方跳过本轮。"""
    current = {
        labels.get("container", "?"): value
        for labels, value in await loki.query(_ERROR_QUERY, at_ts=now_ts)
    }
    if not current:
        return []
    if all(v <= settings.sentinel_log_spike_min for v in current.values()):
        return []  # 全部低于绝对下限:下限是 AND 条件,基线不可能翻转结果,省 7 个基线查询
    totals: dict[str, float] = {}
    for day in range(1, _BASELINE_DAYS + 1):
        for labels, value in await loki.query(_ERROR_QUERY, at_ts=now_ts - day * 86400):
            c = labels.get("container", "?")
            totals[c] = totals.get(c, 0.0) + value
    findings: list[Finding] = []
    for container, count in sorted(current.items()):
        baseline = totals.get(container, 0.0) / _BASELINE_DAYS  # 缺数据的天按 0 计
        if count > settings.sentinel_log_spike_min and (
            count > baseline * settings.sentinel_log_spike_ratio
        ):
            findings.append(
                Finding(
                    rule="log_error_spike",
                    subject=container,
                    severity="warning",
                    detail=(
                        f"近 15 分钟错误日志 {count:.0f} 条,七日同时段均值 {baseline:.1f} 条"
                        f"(阈值 >{settings.sentinel_log_spike_min} 且"
                        f" >{settings.sentinel_log_spike_ratio:g}× 基线)"
                    ),
                    payload={"count": count, "baseline": baseline},
                    needs_diagnosis=True,
                )
            )
    return findings
