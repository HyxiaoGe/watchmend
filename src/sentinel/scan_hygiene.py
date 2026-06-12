# src/sentinel/scan_hygiene.py
from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from pathlib import Path

from sentinel.config import Settings
from sentinel.findings import Finding
from sentinel.promql import PromClient

logger = logging.getLogger("sentinel")

_FORECAST_QUERY = 'predict_linear(node_filesystem_avail_bytes{{mountpoint="/"}}[7d], {days}*86400)'


def check_backup(backup_dir: str, max_age_hours: int, *, now_ts: int) -> list[Finding] | None:
    """备份新鲜度:目录最新文件 mtime 超龄即告警。
    返回 None=目录缺失或完全为空 → 视为未启用备份监控(开箱即用:没挂备份目录的部署
    不应收到误报);目录里有文件但全是 0 字节/stat 失败 → 备份产物坏了,照常告警。"""
    path = Path(backup_dir)
    if not backup_dir or not path.is_dir():
        return None
    seen_any = False
    entries: list[tuple[float, Path]] = []
    # pg_dump 平铺写入本目录,不递归;轮转删除竞态下单文件 stat 失败忽略;0 字节=失败产物
    for p in path.glob("*"):
        seen_any = True
        try:
            st = p.stat()
        except OSError:
            continue
        if p.is_file() and st.st_size > 0:
            entries.append((st.st_mtime, p))
    if not seen_any:
        return None
    if not entries:
        return [
            Finding(
                rule="backup_stale",
                subject=path.name,
                severity="critical",
                detail=f"备份目录 {backup_dir} 有文件但无有效备份(全部 0 字节或不可读)",
            )
        ]
    mtime, newest = max(entries, key=lambda e: e[0])
    age_h = (now_ts - mtime) / 3600
    if age_h > max_age_hours:
        return [
            Finding(
                rule="backup_stale",
                subject=path.name,
                severity="critical",
                detail=f"最新备份 {newest.name} 已 {age_h:.1f} 小时(阈值 {max_age_hours}h)",
                payload={"newest": newest.name, "age_hours": age_h},
            )
        ]
    return []


async def check_disk_forecast(prom: PromClient, settings: Settings) -> list[Finding]:
    """线性外推:预计 N 天内可用空间归零则告警。空结果=node-exporter 静默,
    当数据源故障抛出(run_hygiene 会把 disk_forecast 剔出 scope,不评估≠恢复)。"""
    query = _FORECAST_QUERY.format(days=settings.sentinel_disk_forecast_days)
    rows = await prom.query(query)
    if not rows:
        raise RuntimeError("empty result for disk forecast (exporter down?)")
    findings: list[Finding] = []
    for _, value in rows:
        if value <= 0:
            findings.append(
                Finding(
                    rule="disk_forecast",
                    subject="/",
                    severity="warning",
                    detail=(
                        f"按近 7 天增速,根分区预计 {settings.sentinel_disk_forecast_days} 天内写满"
                    ),
                    payload={"predicted_avail_bytes": value},
                )
            )
    return findings


def _cert_expiry_ts(domain: str) -> float:
    ctx = ssl.create_default_context()
    with socket.create_connection((domain, 443), timeout=10) as sock:
        with ctx.wrap_socket(sock, server_hostname=domain) as tls:
            cert = tls.getpeercert()
    return ssl.cert_time_to_seconds(cert["notAfter"])


async def check_certs(
    domains: list[str], min_days: int, *, now_ts: int
) -> tuple[list[Finding], set[tuple[str, str]]]:
    """公网证书临期(CF 边缘证书自动续期,此为廉价兜底)。握手失败≠临期:记日志、
    该域名进 hold——open 的 cert_expiry 事件不被假恢复(探针走内网,公网 TLS 只有这道检查)。"""
    findings: list[Finding] = []
    hold: set[tuple[str, str]] = set()
    for domain in domains:
        try:
            expiry = await asyncio.to_thread(_cert_expiry_ts, domain)
        except Exception:
            logger.exception("cert check failed: %s", domain)
            hold.add(("cert_expiry", domain))
            continue
        days = (expiry - now_ts) / 86400
        if days < min_days:
            findings.append(
                Finding(
                    rule="cert_expiry",
                    subject=domain,
                    severity="warning",
                    detail=f"证书 {days:.1f} 天后过期(阈值 {min_days} 天)",
                    payload={"days_left": days},
                )
            )
    return findings, hold


async def run_hygiene(
    prom: PromClient, settings: Settings, *, now_ts: int
) -> tuple[list[Finding], set[str], set[tuple[str, str]]]:
    """日检三件套,随日报门控触发。返回 (findings, 实际完成评估的规则集, hold):
    某项检查失败时把它的规则剔出 scope;证书单域握手失败进 hold——两者都防"没查到"被误判"已恢复"。"""
    findings: list[Finding] = []
    evaluated: set[str] = set()
    try:
        backup_findings = check_backup(
            settings.sentinel_backup_dir,
            settings.sentinel_backup_max_age_hours,
            now_ts=now_ts,
        )
        if backup_findings is None:
            # 未启用≠已评估:不进 evaluated,open 的 backup_stale 事件不会被假恢复
            logger.warning(
                "backup dir %s 缺失或为空,跳过备份检查(视为未启用)",
                settings.sentinel_backup_dir,
            )
        else:
            findings += backup_findings
            evaluated.add("backup_stale")
    except Exception:
        logger.exception("backup check failed")
    if settings.sentinel_prometheus_url:  # URL 留空=指标层未接入,forecast 不评估
        try:
            findings += await check_disk_forecast(prom, settings)
            evaluated.add("disk_forecast")
        except Exception:
            logger.exception("disk forecast check failed")
    hold: set[tuple[str, str]] = set()
    if settings.cert_domains_list:
        cert_findings, hold = await check_certs(
            settings.cert_domains_list, settings.sentinel_cert_min_days, now_ts=now_ts
        )
        findings += cert_findings
        evaluated.add("cert_expiry")
    # 域名列表空=证书检查未启用(开箱默认):同 backup,不进 evaluated,不评估≠恢复
    return findings, evaluated, hold
