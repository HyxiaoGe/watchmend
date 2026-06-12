# src/sentinel/scan_metrics.py
from __future__ import annotations

import math

from sentinel.config import Settings
from sentinel.findings import Finding
from sentinel.promql import PromClient

_DISK_QUERY = (
    '1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}'
)
# 工作集/限额,15m 窗口逐分钟取最小:整窗都在阈上才算"持续">90%
_MEM_QUERY = (
    'min_over_time((container_memory_working_set_bytes{name!=""}'
    ' / container_spec_memory_limit_bytes{name!=""})[15m:1m])'
)
_SWAP_QUERY = (
    "(node_memory_SwapTotal_bytes - node_memory_SwapFree_bytes) / node_memory_SwapTotal_bytes"
)


# 存储中间件兜底:探针层故意不探数据库类服务,由 exporter 的 up 指标(如 pg_up/redis_up)
# 兜底,清单经 SENTINEL_MIDDLEWARE_METRICS 配置(空=跳过本检查)。
# 不带 ==0 过滤:空结果要当数据源故障,值为 0 才是真挂。
def _middleware_query(metrics: list[str]) -> str:
    pattern = "|".join(metrics)
    return f'{{__name__=~"{pattern}"}}'


# cAdvisor 的 container_start_time_seconds 是创建时间,docker restart 后不变,changes() 抓不到;
# cgroup 重建会让 CPU 计数器归零,用 resets() 检测原地重启(已 live 验证)。
# 容器重建(CI 部署)产生新序列 resets=0 → 故意不报,避免每次部署都出卡。
_RESTART_QUERY = 'resets(container_cpu_usage_seconds_total{name!=""}[1h]) > 0'
_OOM_QUERY = 'increase(container_oom_events_total{name!=""}[1h]) > 0'
_STORM_THRESHOLD = 5  # 同轮重启容器数达到此值视为主机级事件,聚合成单卡防 30+ 卡风暴


async def _mandatory(prom: PromClient, query: str, what: str) -> list[tuple[dict, float]]:
    """该查询在本机必然有数据:空结果说明 exporter 静默(node-exporter/cAdvisor/
    各 exporter 挂了而 Prometheus 健康),必须当数据源故障抛出,不能被判"指标恢复正常"。"""
    rows = await prom.query(query)
    if not rows:
        raise RuntimeError(f"empty result for {what} (exporter down?)")
    return rows


async def run_metrics_scan(prom: PromClient, settings: Settings) -> list[Finding]:
    """Prometheus 指标规则:disk_usage / mem_pressure(容器+swap) / middleware_down /
    container_restart(+OOM)。任一查询失败或必有数据的查询返回空 → 整轮抛出,
    由调用方跳过本轮(不评估≠恢复)。"""
    findings: list[Finding] = []

    for _, value in await _mandatory(prom, _DISK_QUERY, "disk usage"):
        pct = value * 100
        if pct > settings.sentinel_disk_usage_pct:
            findings.append(
                Finding(
                    rule="disk_usage",
                    subject="/",
                    severity="critical",
                    detail=(
                        f"根分区使用率 {pct:.1f}%(阈值 {settings.sentinel_disk_usage_pct:.0f}%)"
                    ),
                    payload={"usage_pct": pct},
                )
            )

    for labels, value in await _mandatory(prom, _MEM_QUERY, "container mem"):
        if not math.isfinite(value):
            continue  # 无 limit 容器(limit=0):working_set/0=+Inf,临时 docker run 常见,不是内存压力
        pct = value * 100
        if pct > settings.sentinel_container_mem_pct:
            name = labels.get("name", "?")
            findings.append(
                Finding(
                    rule="mem_pressure",
                    subject=name,
                    severity="warning",
                    detail=(
                        f"容器 {name} 内存连续 15 分钟 ≥{pct:.1f}% limit"
                        f"(阈值 {settings.sentinel_container_mem_pct:.0f}%)"
                    ),
                    payload={"min_ratio_pct_15m": pct},
                    needs_diagnosis=True,
                )
            )

    for _, value in await _mandatory(prom, _SWAP_QUERY, "swap"):
        if not math.isfinite(value):
            continue  # 无 swap 主机:0/0=NaN,显式跳过(而非依赖 NaN 比较恒 False 的巧合)
        pct = value * 100
        if pct > settings.sentinel_swap_pct:
            findings.append(
                Finding(
                    rule="mem_pressure",
                    subject="swap",
                    severity="warning",
                    detail=(
                        f"宿主机 swap 使用率 {pct:.1f}%(阈值 {settings.sentinel_swap_pct:.0f}%)"
                    ),
                    payload={"swap_pct": pct},
                    needs_diagnosis=True,
                )
            )

    middleware_subjects = settings.middleware_subjects
    if middleware_subjects:
        query = _middleware_query(sorted(middleware_subjects))
        middleware_rows = await _mandatory(prom, query, "middleware up")
        seen_metrics = {labels.get("__name__") for labels, _ in middleware_rows}
        missing = middleware_subjects.keys() - seen_metrics
        if missing:
            # 各 exporter 是独立容器:单个静默时查询仍非空,必须按 metric 校验覆盖
            raise RuntimeError(f"middleware metrics missing: {sorted(missing)} (exporter down?)")
        for labels, value in middleware_rows:
            metric = labels.get("__name__", "?")
            if value == 0:
                subject = middleware_subjects.get(metric, metric)
                findings.append(
                    Finding(
                        rule="middleware_down",
                        subject=subject,
                        severity="critical",
                        detail=f"存储中间件 {subject} 健康指标 {metric}=0",
                        payload={"metric": metric},
                        needs_diagnosis=True,
                    )
                )

    restarted: dict[str, tuple[str, float]] = {}
    for labels, value in await prom.query(_RESTART_QUERY):
        restarted[labels.get("name", "?")] = ("重启", value)
    for labels, value in await prom.query(_OOM_QUERY):
        # OOM 比普通重启信息量大,覆盖;重启次数与 OOM 计数语义重叠,不另保留
        restarted[labels.get("name", "?")] = ("OOM", value)
    if len(restarted) >= _STORM_THRESHOLD:
        names = ", ".join(sorted(restarted))
        findings.append(
            Finding(
                rule="container_restart",
                subject="host",
                severity="critical",
                detail=f"近 1h {len(restarted)} 个容器同时重启(疑似宿主机重启): {names}",
                payload={
                    "containers": {
                        n: {"kind": k, "count": c} for n, (k, c) in sorted(restarted.items())
                    }
                },
                needs_diagnosis=True,
                point=True,
            )
        )
    else:
        for name, (kind, value) in sorted(restarted.items()):
            findings.append(
                Finding(
                    rule="container_restart",
                    subject=name,
                    severity="warning",
                    detail=f"容器 {name} 近 1h {kind} {value:.0f} 次",
                    payload={"kind": kind, "count": value},
                    needs_diagnosis=True,
                    point=True,
                )
            )
    return findings
