from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sentinel.findings import RULE_NAMES, DigestItem

_WINDOW_SECONDS = 600
_MAX_GROUPS = 3
_SHANGHAI = timezone(timedelta(hours=8))
_MODEL_ERRORS = (
    "apiconnectionerror",
    "midstreamfallbackerror",
    "modelroundunavailableerror",
    "internalservererror",
)
_FRIENDLY_NAMES = {
    "fusion-api": "Fusion",
    "litellm-proxy": "LiteLLM",
    "agent-tools-gateway": "工具网关",
}


@dataclass(frozen=True)
class DigestSummary:
    heading: str
    body: str
    problem_groups: int

    @property
    def text(self) -> str:
        return "\n".join(part for part in [self.heading, self.body] if part)


@dataclass
class _Group:
    key: tuple[str, ...]
    label: str
    severity: str
    state: str
    first_ts: int
    last_ts: int
    sources: set[str] = field(default_factory=set)
    records: int = 0


def _payload(item: DigestItem) -> dict:
    try:
        value = json.loads(item.payload_json)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _safe_label(value: str) -> str:
    value = re.sub(
        r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
        "[标识]",
        value,
    )
    value = " ".join(value.split()).replace("<", "&lt;").replace(">", "&gt;")
    return value if len(value) <= 48 else value[:47] + "…"


def _source(item: DigestItem, payload: dict) -> str:
    candidate = payload.get("container")
    if not isinstance(candidate, str):
        candidate = item.subject.split(" · ", 1)[0]
    # 日志标题可能本身是 JSON；只从明确的服务名生成展示来源。
    if not re.fullmatch(r"[\w.-]{1,80}", candidate):
        return "未知服务"
    return candidate


def _describe(item: DigestItem, payload: dict) -> tuple[str, tuple[str, ...], str]:
    if item.rule != "log_error_new":
        label = RULE_NAMES.get(item.rule, "巡检异常")
        if item.rule == "container_restart":
            if item.subject == "host":
                label = "主机重启"
            elif str(payload.get("kind", "")).upper() == "OOM":
                label = "内存耗尽后重启"
            elif isinstance(payload.get("count"), (int, float)) and payload["count"] > 1:
                label = "重复重启"
        return label, (item.rule, item.subject), _safe_label(item.subject)

    source = _source(item, payload)
    signature = payload.get("signature")
    signature = signature if isinstance(signature, str) else item.subject
    text = item.detail + "\n" + signature
    lower = text.lower()
    if source in {"fusion-api", "litellm-proxy"} and any(x in lower for x in _MODEL_ERRORS):
        return "模型调用失败", ("model-call",), source
    if source == "agent-tools-gateway":
        if any(x in lower for x in ["remoteprotocolerror", "incomplete chunked read"]):
            return "网关流式响应中断", ("gateway-stream",), source
        if "exception in asgi application" in lower:
            return "网关应用异常", ("gateway-asgi",), source
    if "graceful shutdown exceeded" in lower:
        return "服务退出超时", ("shutdown", source), source
    if "does not exist" in lower and any(x in lower for x in ["relation ", "column "]):
        return "数据库结构异常", ("database-schema", source), source
    if any(x in lower for x in ["readtimeout", "connecttimeout", "timeouterror"]):
        return "请求超时", ("request-timeout", source), source
    exceptions = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)\b", text)
    label = "应用错误" + (f"（{exceptions[-1][:32]}）" if exceptions else "")
    # 未识别原因保留独立指纹，不能把同一服务的所有错误合成一个问题。
    return label, ("unclassified-log", source, signature), source


def summarize_digest(items: list[DigestItem]) -> DigestSummary:
    """只归类展示，不修改原始记录；记录数不是故障次数或受影响请求数。"""
    groups: list[_Group] = []
    latest: dict[tuple[str, ...], _Group] = {}
    restarts = shutdowns = 0
    for item in sorted(items, key=lambda x: (x.last_ts, x.id)):
        payload = _payload(item)
        if (
            item.rule == "container_restart"
            and item.severity != "critical"
            and item.subject != "host"
            and str(payload.get("kind", "")).upper() != "OOM"
            and type(payload.get("count")) in {int, float}
            and payload.get("count") == 1
            and item.state == "observed"
        ):
            restarts += 1
            continue
        if (
            item.rule == "log_error_new"
            and item.severity != "critical"
            and item.state == "observed"
            and "graceful shutdown exceeded" in item.detail.lower()
            and re.search(r"\bCancel\s+0\s+running\s+task", item.detail, re.IGNORECASE)
        ):
            shutdowns += 1
            continue
        label, category, source = _describe(item, payload)
        key = (*category, item.severity, item.state)
        group = latest.get(key)
        if group is None or item.last_ts - group.first_ts > _WINDOW_SECONDS:
            group = _Group(key, label, item.severity, item.state, item.last_ts, item.last_ts)
            groups.append(group)
            latest[key] = group
        group.last_ts = max(group.last_ts, item.last_ts)
        group.sources.add(source)
        group.records += 1

    groups.sort(
        key=lambda x: (
            x.state == "resolved",
            {"critical": 0, "warning": 1, "info": 2}.get(x.severity, 1),
            -x.last_ts,
            x.label,
        )
    )
    lines = []
    for group in groups[:_MAX_GROUPS]:
        restored = group.state == "resolved"
        marker = "✅" if restored else "🔴" if group.severity == "critical" else "🟠"
        sources = sorted(_FRIENDLY_NAMES.get(x, x) for x in group.sources)
        source_text = "、".join(_safe_label(x) for x in sources[:2])
        if len(sources) > 2:
            source_text += f"等{len(sources)}项"
        state = "（已恢复）" if restored else ""
        time_str = datetime.fromtimestamp(group.last_ts, _SHANGHAI).strftime("%H:%M")
        lines.append(
            f"{marker} {group.label}{state} · {source_text}"
            f" · {group.records} 条相关记录 · {time_str}"
        )
    if len(groups) > _MAX_GROUPS:
        lines.append(f"另有 {len(groups) - _MAX_GROUPS} 组问题")
    changes = []
    if restarts:
        changes.append(f"重启记录 {restarts} 条")
    if shutdowns:
        changes.append(f"退出超时记录 {shutdowns} 条")
    if changes:
        lines.append("运行变更：" + " · ".join(changes))
    if groups:
        heading = f"{len(groups)} 组问题 · {sum(g.records for g in groups)} 条相关记录"
    else:
        heading = "本窗口仅有运行记录" if changes else "本窗口无新增记录"
    return DigestSummary(heading, "\n".join(lines), len(groups))
