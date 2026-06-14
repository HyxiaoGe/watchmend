# src/sentinel/panel/view.py
"""证据台 view-model 组装(纯函数)。从 Store + Settings 取数、做状态机/姿态推导,
返回 dict 供 jinja2 模板渲染。不碰 HTTP、不碰模板,可被 pytest 直接断言。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sentinel.config import Settings
from sentinel.findings import HYGIENE_RULES, RULE_NAMES, EventRecord
from sentinel.report import aggregate_window
from sentinel.store import Store

_REFRESH_SECONDS = 30
_DAY_SECONDS = 24 * 3600
# 自身/代理永不计入“在监容器”(与 scan_docker 同口径:按镜像子串识别,兼容 registry 前缀)
_SELF_IMAGE_SUBSTR = "watchmend"
_SOCKET_PROXY_IMAGE = "tecnativa/docker-socket-proxy"


def _hhmm(ts: int, tz: timezone) -> str:
    return datetime.fromtimestamp(ts, tz).strftime("%H:%M")


def _lifecycle(e: EventRecord, *, diag_active: bool = True) -> str:
    """事件生命周期标签(状态机可视化)。resolved 优先;open 的 scan_failed_* 单列。
    diag_active=诊断层此刻是否真在跑:为 False 时(LLM 关或已配置待重启)pending
    事件其实无人调查,显示 open 而非"调查中",避免面板误导。"""
    if e.status == "resolved":
        return "recovered"
    if e.rule.startswith("scan_failed_"):
        return "scan_failed"
    if e.diagnosis_status == "pending" and not diag_active:
        return "open"
    return {
        "pending": "investigating",
        "done": "diagnosed",
        "failed": "diagnosed_failed",
        "skipped": "open",
    }.get(e.diagnosis_status, "open")


def _summary_of(e: EventRecord) -> str | None:
    if not e.diagnosis_json:
        return None
    try:
        d = json.loads(e.diagnosis_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(d, dict):
        return d.get("summary") or d.get("root_cause")
    return None


def _event_view(e: EventRecord, tz: timezone, *, diag_active: bool = True) -> dict:
    return {
        "id": e.id,
        "ts_str": _hhmm(e.ts, tz),
        "rule": e.rule,
        "rule_label": RULE_NAMES.get(e.rule, e.rule),
        "subject": e.subject,
        "severity": e.severity,
        "lifecycle": _lifecycle(e, diag_active=diag_active),
        "is_scan_failure": e.rule.startswith("scan_failed_"),
        "diagnosis_status": e.diagnosis_status,
        "has_evidence": e.diagnosis_status == "done" and bool(e.diagnosis_tools_json),
        "summary": _summary_of(e),
        "resolved_str": _hhmm(e.resolved_ts, tz) if e.resolved_ts else None,
    }


def _docker_mode(settings: Settings) -> str:
    ep = settings.docker_endpoint
    if ep.startswith(("tcp://", "http://")):
        return "proxy"
    if ep.startswith("unix://"):
        return "raw"
    return "off"


def _channels(settings: Settings) -> list[str]:
    out: list[str] = []
    if settings.feishu_enabled:
        out.append("飞书")
    if settings.telegram_enabled:
        out.append("Telegram")
    if settings.ntfy_enabled:
        out.append("ntfy")
    if settings.webhook_enabled:
        out.append("webhook")
    return out


def _llm_posture(llm_config, settings: Settings, *, diag_registered: bool | None = None) -> dict:
    """LLM 姿态:优先 LLMConfig(llm.yaml/env 真源,鸭子类型 .current());
    无 config 时回退 settings.llm_*(向后兼容老调用/单测)。

    diag_registered=启动时诊断 job 是否注册(④ 条件注册:仅 config.enabled 时注册)。
    已配置(current() 非空)但 diag 未注册 → 是启动后才配的,诊断要重启一次才跑
    (pending_restart)。None=调用方未提供该信号,不下此结论(老调用/单测保持原状)。"""
    if llm_config is not None:
        profile = llm_config.current()
        configured = profile is not None
        return {
            "enabled": configured,
            "pending_restart": configured and diag_registered is False,
            "model": profile.model if configured else None,
        }
    enabled = bool(settings.llm_base_url and settings.llm_model)
    return {
        "enabled": enabled,
        "pending_restart": False,
        "model": settings.llm_model if enabled else None,
    }


def _env_redaction(events: list[EventRecord]) -> list[dict]:
    """best-effort:从已存证据链里 docker_inspect 项解析 len(Env),按 subject 聚合。
    output 可能被截断导致 JSON 解析失败 → 跳过该项(模板回退泛化文案)。不新增持久化。"""
    by_subject: dict[str, int] = {}
    for e in events:
        if not e.diagnosis_tools_json:
            continue
        try:
            calls = json.loads(e.diagnosis_tools_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict) or call.get("tool") != "docker_inspect":
                continue
            try:
                payload = json.loads(call.get("output") or "")
            except (json.JSONDecodeError, TypeError):
                continue
            env = payload.get("Env") if isinstance(payload, dict) else None
            if isinstance(env, list):
                by_subject[e.subject] = max(by_subject.get(e.subject, 0), len(env))
    return [{"subject": s, "count": c} for s, c in sorted(by_subject.items())]


async def _monitored_containers(docker, settings: Settings) -> int | None:
    """best-effort 实时计数(排除自身/proxy/exclude)。docker off 或 ps 失败 → None。"""
    if docker is None:
        return None
    try:
        rows = await docker.ps(all=False)
    except Exception:
        return None
    exclude = set(settings.docker_exclude_list)
    count = 0
    for row in rows:
        image = row.get("Image") or ""
        if _SELF_IMAGE_SUBSTR in image or _SOCKET_PROXY_IMAGE in image:
            continue
        names = [n.lstrip("/") for n in (row.get("Names") or [])]
        if names and names[0] in exclude:
            continue
        count += 1
    return count


def _hygiene_services(store: Store, *, now_ts: int) -> list[dict]:
    """探针 24h uptime/p95(复用 report.aggregate_window;服务名从样本派生)。"""
    samples = store.get_probe_samples_since(now_ts - _DAY_SECONDS)
    names = sorted({s.service for s in samples})
    out: list[dict] = []
    for st in aggregate_window(samples, names):
        uptime = round(st.ok_count / st.total * 100, 1) if st.total else None
        out.append({"service": st.service, "uptime_pct": uptime, "p95_ms": st.p95_ms})
    return out


async def build_overview(
    store: Store,
    settings: Settings,
    *,
    now: datetime,
    docker=None,
    llm_config=None,
    diag_registered: bool | None = None,
) -> dict:
    """总览 view-model。docker 为可选注入的 DockerClient,缺省 None → 容器计数降级 None。
    llm_config 为可选注入的 LLMConfig,缺省 None → 回退 settings.llm_*。
    diag_registered 为启动时诊断 job 是否注册(lifespan 注入),用于"已配置·待重启"判定。"""
    tz = (
        now.tzinfo
        if isinstance(now.tzinfo, timezone)
        else timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    )
    now_ts = int(now.timestamp())
    open_events = store.get_open_events()
    recoveries = store.get_resolved_since(now_ts - _DAY_SECONDS)
    docker_mode = _docker_mode(settings)
    llm = _llm_posture(llm_config, settings, diag_registered=diag_registered)
    # 诊断层此刻是否真在跑:已配置且未待重启。pending 事件的生命周期据此显示。
    diag_active = llm["enabled"] and not llm["pending_restart"]
    return {
        "now_str": now.strftime("%Y-%m-%d %H:%M"),
        "refresh_seconds": _REFRESH_SECONDS,
        "posture": {
            "monitored_containers": await _monitored_containers(docker, settings),
            "open_count": len(open_events),
            "resolved_24h": store.count_resolved_since(now_ts - _DAY_SECONDS),
            "llm": llm,
            "docker": {"mode": docker_mode, "read_only": True},
            "layers": {
                "prometheus": bool(settings.sentinel_prometheus_url),
                "loki": bool(settings.sentinel_loki_url),
                "docker": docker_mode != "off",
                "llm": llm["enabled"],
            },
            "channels": _channels(settings),
            "env_redaction": _env_redaction(open_events + recoveries),
        },
        "anomalies": [
            _event_view(e, tz, diag_active=diag_active)
            for e in open_events
            if e.rule not in HYGIENE_RULES
        ],
        "recoveries": [_event_view(e, tz, diag_active=diag_active) for e in recoveries],
        "hygiene": {
            "services": _hygiene_services(store, now_ts=now_ts),
            "hygiene_alerts": [
                _event_view(e, tz, diag_active=diag_active)
                for e in open_events
                if e.rule in HYGIENE_RULES
            ],
        },
    }


def _tool_calls_view(tools_json: str | None) -> list[dict]:
    if not tools_json:
        return []
    try:
        calls = json.loads(tools_json)
    except (json.JSONDecodeError, TypeError):
        return []
    out: list[dict] = []
    for c in calls if isinstance(calls, list) else []:
        if not isinstance(c, dict):
            continue
        out.append(
            {
                "tool": c.get("tool", "?"),
                "args_str": json.dumps(c.get("args") or {}, ensure_ascii=False),
                "output": c.get("output", ""),
                "ok": bool(c.get("ok")),
            }
        )
    return out


def build_event_detail(
    store: Store, settings: Settings, event_id: int, *, llm_config=None, diag_registered=None
) -> dict | None:
    """单事件详情 view-model。事件不存在 → None(路由据此 404)。
    settings 用于 llm_enabled 与时间显示时区(spec §6 签名补 settings)。
    llm_config 为可选注入的 LLMConfig,缺省 None → 回退 settings.llm_*。
    diag_registered 同 build_overview:控制 pending 事件是否显示"调查中"。"""
    e = store.get_event(event_id)
    if e is None:
        return None
    tz = timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    posture = _llm_posture(llm_config, settings, diag_registered=diag_registered)
    diag_active = posture["enabled"] and not posture["pending_restart"]
    ev = _event_view(e, tz, diag_active=diag_active)
    ev["detail"] = e.detail
    diagnosis = None
    if e.diagnosis_status == "done" and e.diagnosis_json:
        try:
            parsed = json.loads(e.diagnosis_json)
            diagnosis = parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, TypeError):
            diagnosis = None
    return {
        "event": ev,
        "llm_enabled": posture["enabled"],
        "diagnosis": diagnosis,
        "tool_calls": _tool_calls_view(e.diagnosis_tools_json),
    }
