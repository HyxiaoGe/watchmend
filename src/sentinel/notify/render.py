# src/sentinel/notify/render.py
from __future__ import annotations

from sentinel.notify.message import Kind, Notification, Severity

# 跨渠道严重度 → ntfy 优先级(1-5)。飞书头色按 kind,不走此表(见 FeishuChannel)。
NTFY_PRIORITY = {Severity.CRITICAL: 5, Severity.WARNING: 4, Severity.INFO: 3}

_LEAD_BY_SEVERITY = {Severity.CRITICAL: "🔴", Severity.WARNING: "🟠", Severity.INFO: "🟢"}

# ntfy tags 用 emoji shortcode,客户端渲染成图标
_NTFY_SEVERITY_TAG = {
    Severity.CRITICAL: "rotating_light",
    Severity.WARNING: "warning",
    Severity.INFO: "information_source",
}
_NTFY_KIND_TAG = {
    Kind.CODEX_TURN: "computer",
    Kind.RECOVERY: "white_check_mark",
    Kind.REPORT: "clipboard",
    Kind.DIGEST: "memo",
    Kind.HEARTBEAT: "heartbeat",
    Kind.DIAGNOSIS: "brain",
    Kind.SUMMARY: "memo",
}


def lead_emoji(n: Notification) -> str:
    """标题前导 emoji:恢复永远 ✅,其余按严重度(spec §5)。"""
    if n.kind == Kind.RECOVERY:
        return "✅"
    return _LEAD_BY_SEVERITY[n.severity]


def ntfy_tags(n: Notification) -> list[str]:
    tags = [_NTFY_SEVERITY_TAG[n.severity]]
    kind_tag = _NTFY_KIND_TAG.get(n.kind)
    if kind_tag:
        tags.append(kind_tag)
    return tags


def body_text(n: Notification) -> str:
    """detail + fields → 纯文本正文(未转义;Telegram/ntfy 共用)。"""
    parts: list[str] = []
    if n.detail:
        parts.append(n.detail)
    parts.extend(f"{label}:{value}" for label, value in n.fields)
    return "\n".join(parts)
