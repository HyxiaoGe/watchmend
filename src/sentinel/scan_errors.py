"""从 Loki 提取错误级别日志，按归一化指纹生成一次性事件。"""

from __future__ import annotations

import logging
import re

from sentinel.config import Settings
from sentinel.findings import Finding
from sentinel.logql import LokiClient
from sentinel.scan_logs import _ERROR_LEVEL_RE

logger = logging.getLogger("sentinel")

_ERROR_LEVEL = re.compile(_ERROR_LEVEL_RE)
_CRITICAL_LEVEL = re.compile(
    r"\bCRITICAL\b|\bFATAL\b|\bPANIC\b"
    r"|level=(?:critical|fatal|panic)"
    r'|"level":\s*"(?:critical|fatal|panic)"'
    r"|\[(?:crit|alert|emerg)\]"
)
_NORMALIZERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\x1b\[[0-9;]*[A-Za-z]"), ""),
    (
        re.compile(
            r"^\[[^\]]*\b(?:ERROR|CRITICAL|FATAL|PANIC|WARNING|INFO|DEBUG)\b[^\]]*\]\s*",
            re.I,
        ),
        "",
    ),
    (re.compile(r"^(?:ERROR|CRITICAL|FATAL|PANIC|WARNING|INFO|DEBUG):\s*", re.I), ""),
    (re.compile(r"0x[0-9a-fA-F]+"), "0xADDR"),
    (
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "UUID",
    ),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[.,]?\d*"), "TS"),
    (re.compile(r"\b\d{1,2}:\d{2}:\d{2}(?:[.,]\d+)?\b"), "TIME"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "IP"),
    (re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{8,}\b"), "HEX"),
    (
        re.compile(r"\b(video|task|job|user|request|trace)[ =:]+[\w-]{6,}", re.I),
        r"\1 X",
    ),
    (re.compile(r"\d+"), "N"),
]
_MAX_SIG_LEN = 200


def normalize(line: str) -> str:
    """剥除日志前缀并掩盖高基数变量，得到稳定去重键。"""
    normalized = line.strip()
    for pattern, replacement in _NORMALIZERS:
        normalized = pattern.sub(replacement, normalized)
    return re.sub(r"\s+", " ", normalized).strip()[:_MAX_SIG_LEN]


async def run_error_scan(loki: LokiClient, settings: Settings, *, now_ts: int) -> list[Finding]:
    """查询最近窗口的错误日志，按容器和指纹聚合为 point 事件。"""
    start_ts = now_ts - settings.sentinel_error_window_minutes * 60
    selector = settings.loki_stream_selector(settings.sentinel_error_containers)
    query = f"{selector} |~ `{_ERROR_LEVEL_RE}`"
    lines = await loki.query_range(
        query,
        start_ts=start_ts,
        end_ts=now_ts,
        limit=settings.sentinel_error_query_limit,
    )

    groups: dict[tuple[str, str], dict] = {}
    ignore = settings.error_ignore_list
    for labels, line in lines:
        container = labels.get("container", "?")
        if container == settings.sentinel_self_container or not _ERROR_LEVEL.search(line):
            continue
        signature = normalize(line)
        if not signature:
            logger.debug("error scan dropped empty signature: %s", line[:120])
            continue
        if any(pattern in signature.lower() for pattern in ignore):
            continue
        severity = "critical" if _CRITICAL_LEVEL.search(line) else "warning"
        key = (container, signature)
        group = groups.get(key)
        if group is None:
            groups[key] = {"count": 1, "sample": line.strip(), "severity": severity}
        else:
            group["count"] += 1
            if severity == "critical":
                group["severity"] = "critical"

    findings: list[Finding] = []
    for (container, signature), group in groups.items():
        findings.append(
            Finding(
                rule="log_error_new",
                subject=f"{container} · {signature}",
                severity=group["severity"],
                detail=(
                    f"{group['sample'][:500]}\n\n"
                    f"近 {settings.sentinel_error_window_minutes} 分钟内出现 {group['count']} 次"
                ),
                payload={
                    "container": container,
                    "signature": signature,
                    "count": group["count"],
                },
                point=True,
            )
        )
    findings.sort(key=lambda finding: (finding.severity != "critical", -finding.payload["count"]))
    return findings
