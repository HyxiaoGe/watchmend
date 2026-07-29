from __future__ import annotations

from sentinel.findings import Finding

# 服务不可用、监控失明、资源耗尽或容器故障必须立即通知。
_ALWAYS_IMMEDIATE = {
    "service_down",
    "middleware_down",
    "disk_usage",
    "log_error_spike",
    "scan_failed_prometheus",
    "scan_failed_loki",
    "scan_failed_docker",
    "container_down",
    "container_unhealthy",
    "container_oom",
    "container_crashloop",
}


def should_notify_immediately(finding: Finding) -> bool:
    """判定启用分时摘要后仍需实时广播的硬告警。"""
    if finding.rule in _ALWAYS_IMMEDIATE:
        return True
    if finding.rule == "container_restart":
        return finding.subject == "host" or finding.payload.get("kind") == "OOM"
    if finding.rule == "mem_pressure":
        return finding.severity == "critical"
    return False
