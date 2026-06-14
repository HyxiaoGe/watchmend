# src/sentinel/panel/i18n.py
"""证据台界面外壳 i18n（中/英）。零新依赖：纯 dict 目录 + format。
只翻"外壳"（表头/图例/状态名/规则名/按钮/轴/tooltip/页脚）；LLM 诊断正文与事件 detail
保持生成语言（见 spec §9.2）。lang 全程经 render context 注入，不重建 jinja Environment。"""

from __future__ import annotations

# 界面外壳静态串。缺某 key 时 make_translator 回退 zh→key（见下）。
MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        "hdr.title": "WatchMend 证据台",
        "hdr.refreshed": "刷新于 {ts}",
        "nav.lang": "语言",
        "nav.theme": "主题",
        "nav.window": "窗口",
        "nav.latest": "↻ 最新",
        "theme.dark": "深色",
        "theme.light": "浅色",
        "theme.system": "跟随系统",
        "banner.ok": "系统正常运行 · {probes} 探针在线 · {open} 开放异常 · "
        "近 {days} 天事件均已自动恢复",
        "sec.health": "组件健康",
        "sec.hostself": "宿主 & WatchMend 自身",
        "sec.events": "事件 · 均附 AI 诊断",
        "st.ok": "正常",
        "st.degraded": "性能下降",
        "st.partial": "异常",
        "st.down": "不可用",
        "st.nodata": "无数据",
        "tip.nodata": "这一天没有数据。",
        "tip.today": "进行中",
        "tip.today_nodata": "进行中 · 暂无样本",
        "axis.window": "{days} 天前",
        "axis.today": "今天",
        "host.engine": "探针引擎",
        "host.engine.live": "运行中",
        "host.engine.stale": "疑似停摆",
        "host.engine.unknown": "待确认",
        "host.channels": "通知渠道",
        "host.layers": "数据源层",
        "host.llm": "诊断 LLM",
        "host.llm.fallback": "fallback {model} 就绪",
        "host.llm.off": "未配置",
        "ev.page": "第 {page} / {total} 页 · 共 {count} 起",
        "ev.prev": "上一页",
        "ev.next": "下一页",
        "ev.empty": "暂无事件",
        "life.recovered": "已恢复",
        "life.investigating": "调查中",
        "life.diagnosed": "已诊断",
        "life.diagnosed_failed": "诊断失败",
        "life.open": "待处理",
        "life.scan_failed": "巡检失败",
        "sev.critical": "严重",
        "sev.warning": "警告",
        "footer.readonly": "只读 · localhost-only · env 值全遮蔽 · 建议命令永不自动执行",
    },
    "en": {
        "hdr.title": "WatchMend Evidence Panel",
        "hdr.refreshed": "Refreshed {ts}",
        "nav.lang": "Language",
        "nav.theme": "Theme",
        "nav.window": "Window",
        "nav.latest": "↻ Latest",
        "theme.dark": "Dark",
        "theme.light": "Light",
        "theme.system": "System",
        "banner.ok": "All systems operational · {probes} probes online · {open} open anomalies "
        "· events in the last {days} days auto-recovered",
        "sec.health": "Component Health",
        "sec.hostself": "Host & WatchMend itself",
        "sec.events": "Events · with AI diagnosis",
        "st.ok": "Operational",
        "st.degraded": "Degraded",
        "st.partial": "Partial outage",
        "st.down": "Major outage",
        "st.nodata": "No data",
        "tip.nodata": "No data exists for this day.",
        "tip.today": "In progress",
        "tip.today_nodata": "In progress · no samples yet",
        "axis.window": "{days} days ago",
        "axis.today": "Today",
        "host.engine": "Probe engine",
        "host.engine.live": "Running",
        "host.engine.stale": "Likely stalled",
        "host.engine.unknown": "Unknown",
        "host.channels": "Channels",
        "host.layers": "Data sources",
        "host.llm": "Diagnosis LLM",
        "host.llm.fallback": "fallback {model} ready",
        "host.llm.off": "Not configured",
        "ev.page": "Page {page} / {total} · {count} total",
        "ev.prev": "Prev",
        "ev.next": "Next",
        "ev.empty": "No events",
        "life.recovered": "Recovered",
        "life.investigating": "Investigating",
        "life.diagnosed": "Diagnosed",
        "life.diagnosed_failed": "Diagnosis failed",
        "life.open": "Open",
        "life.scan_failed": "Scan failed",
        "sev.critical": "Critical",
        "sev.warning": "Warning",
        "footer.readonly": "read-only · localhost-only · env values redacted · "
        "suggested commands never auto-run",
    },
}

# 规则名双语：面板层 i18n 单一来源，取代模板对 findings.RULE_NAMES 的直接引用。
# zh 与 findings.RULE_NAMES 对齐；findings 本身不动（其他用途不受影响）。
RULE_LABELS: dict[str, dict[str, str]] = {
    "service_down": {"zh": "服务异常", "en": "Service down"},
    "latency_degraded": {"zh": "延迟退化", "en": "Latency degraded"},
    "log_error_spike": {"zh": "错误日志激增", "en": "Error log spike"},
    "disk_usage": {"zh": "磁盘水位", "en": "Disk usage"},
    "disk_forecast": {"zh": "磁盘填满预测", "en": "Disk fill forecast"},
    "mem_pressure": {"zh": "内存压力", "en": "Memory pressure"},
    "middleware_down": {"zh": "中间件异常", "en": "Middleware down"},
    "container_restart": {"zh": "容器重启", "en": "Container restart"},
    "container_down": {"zh": "容器停止", "en": "Container down"},
    "container_unhealthy": {"zh": "容器健康检查异常", "en": "Container unhealthy"},
    "container_oom": {"zh": "容器 OOM", "en": "Container OOM"},
    "backup_stale": {"zh": "备份缺失", "en": "Backup stale"},
    "cert_expiry": {"zh": "证书临期", "en": "Cert expiring"},
    "scan_failed_prometheus": {"zh": "Prometheus 巡检失败", "en": "Prometheus scan failed"},
    "scan_failed_loki": {"zh": "Loki 巡检失败", "en": "Loki scan failed"},
    "scan_failed_docker": {"zh": "Docker 巡检失败", "en": "Docker scan failed"},
}

_SUPPORTED = ("zh", "en")
_DEFAULT_LANG = "zh"


def resolve_lang(query: str | None, cookie: str | None, accept_language: str | None) -> str:
    """query > cookie > Accept-Language（zh* → zh，否则 en）> 默认 zh。
    query/cookie 仅当显式为受支持语言时采用，否则落到下一优先级。"""
    for cand in (query, cookie):
        if cand and cand.strip().lower() in _SUPPORTED:
            return cand.strip().lower()
    if accept_language and accept_language.strip():
        return "zh" if accept_language.strip().lower().startswith("zh") else "en"
    return _DEFAULT_LANG


def make_translator(lang: str):
    """返回 t(key, **kw)：查 lang 表 → 缺失回退 zh 表 → 再缺回退 key 本身；有 kw 时 format。"""
    table = MESSAGES.get(lang, MESSAGES[_DEFAULT_LANG])
    zh = MESSAGES[_DEFAULT_LANG]

    def t(key: str, **kw) -> str:
        raw = table.get(key) or zh.get(key) or key
        return raw.format(**kw) if kw else raw

    return t


def rule_label(rule: str, lang: str) -> str:
    """规则名按 lang 解析；未知规则原样返回。"""
    entry = RULE_LABELS.get(rule)
    if entry is None:
        return rule
    return entry.get(lang) or entry.get(_DEFAULT_LANG) or rule
