# src/sentinel/findings.py
from __future__ import annotations

from dataclasses import dataclass, field

# 规则名 → 卡片显示名(事件卡/恢复卡/日报共用)
RULE_NAMES = {
    "service_down": "服务异常",
    "latency_degraded": "延迟退化",
    "log_error_spike": "错误日志激增",
    "disk_usage": "磁盘水位",
    "disk_forecast": "磁盘填满预测",
    "mem_pressure": "内存压力",
    "middleware_down": "中间件异常",
    "container_restart": "容器重启",
    "backup_stale": "备份缺失",
    "cert_expiry": "证书临期",
    "scan_failed_prometheus": "Prometheus 巡检失败",
    "scan_failed_loki": "Loki 巡检失败",
    "container_down": "容器停止",
    "container_unhealthy": "容器健康检查异常",
    "container_oom": "容器 OOM",
    "scan_failed_docker": "Docker 巡检失败",
}

# 各评估入口"成功评估时"覆盖的规则集 → apply_findings 的 scope。
# scan_failed_* 不在其中:它们由 tick 层按连续失败计数单独并入 scope。
PROBE_RULES = frozenset({"service_down", "latency_degraded"})
METRICS_RULES = frozenset({"disk_usage", "mem_pressure", "container_restart", "middleware_down"})
LOG_RULES = frozenset({"log_error_spike"})
HYGIENE_RULES = frozenset({"backup_stale", "disk_forecast", "cert_expiry"})
# Docker 巡检规则集 → docker_tick 成功评估时并入 scope。
# scan_failed_docker 不在其中:由 tick 层按连续失败计数单独并入。
DOCKER_RULES = frozenset({"container_down", "container_unhealthy", "container_oom"})
# 仅状态型规则进 OPEN 集(可恢复);container_oom 是 point 事件,落库即 resolved,不在此。
DOCKER_OPEN_RULES = frozenset({"container_down", "container_unhealthy"})


@dataclass
class Finding:
    """一次规则评估的命中。engine 据此开事件并发卡;同轮未命中的 open 事件被判恢复。"""

    rule: str
    subject: str
    severity: str  # "critical" | "warning"
    detail: str  # 卡片正文(含触发值 vs 阈值/基线)
    payload: dict = field(default_factory=dict)  # 原始证据,Phase 3 诊断输入
    needs_diagnosis: bool = False  # True → diagnosis_status=pending(Phase 3 拉取)
    point: bool = False  # 点事件(容器重启):落库即 resolved,无恢复卡,靠冷却去重


@dataclass
class EventRecord:
    """events 表一行。字段顺序必须与 store._EVENT_COLS 的 SELECT 列序一致。"""

    id: int
    ts: int
    rule: str
    subject: str
    severity: str
    status: str  # open / resolved
    detail: str
    payload_json: str
    diagnosis_status: str  # pending / done / failed / skipped
    diagnosis_json: str | None
    cooldown_until: int
    resolved_ts: int | None
