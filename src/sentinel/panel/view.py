# src/sentinel/panel/view.py
"""证据台 view-model 组装(纯函数)。从 Store + Settings 取数、做状态机/姿态推导,
返回 dict 供 jinja2 模板渲染。不碰 HTTP、不碰模板,可被 pytest 直接断言。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from sentinel.config import Settings
from sentinel.findings import HYGIENE_RULES, EventRecord
from sentinel.report import aggregate_window
from sentinel.store import Store

_REFRESH_SECONDS = 30
_DAY_SECONDS = 24 * 3600
# 健康行排序权重：现态(今日格)最坏者优先，使被 services_cap 截断后仍先露出需关注的服务。
# nodata 排最后(刚开始采样的服务不抢占注意力)。同权重内按服务名稳定排序。
_STATE_RANK = {"down": 0, "partial": 1, "degraded": 2, "ok": 3, "nodata": 4}
# 自身/代理永不计入“在监容器”(与 scan_docker 同口径:按镜像子串识别,兼容 registry 前缀)
_SELF_IMAGE_SUBSTR = "watchmend"
_SOCKET_PROXY_IMAGE = "tecnativa/docker-socket-proxy"


def _svg_line(values: list[float | None], *, w: float, h: float, pad_frac: float = 0.08) -> dict:
    """把一条可能含 None 缺口的序列归一成 SVG 路径(零 JS 可视化基元)。
    返回 {"line_d", "area_d"}:line_d 在 None 处用 M 断开折线;
    area_d 仅当序列无缺口时自底边闭合填充,否则为空串。
    y 轴留 pad_frac 余量,避免 99%+ 的微小波动被压成直线。"""
    nums = [v for v in values if v is not None]
    n = len(values)
    if n < 2 or len(nums) < 2:
        return {"line_d": "", "area_d": ""}
    lo, hi = min(nums), max(nums)
    span = (hi - lo) or 1.0
    pad = span * pad_frac
    lo -= pad
    hi += pad
    rng = (hi - lo) or 1.0

    def x_of(i: int) -> float:
        return i / (n - 1) * w

    def y_of(v: float) -> float:
        return h - (v - lo) / rng * h

    parts: list[str] = []
    gap = True
    for i, v in enumerate(values):
        if v is None:
            gap = True
            continue
        parts.append(f"{'M' if gap else 'L'}{x_of(i):.1f},{y_of(v):.1f}")
        gap = False
    line_d = " ".join(parts)

    if None in values:
        area_d = ""
    else:
        seg = " ".join(f"{x_of(i):.1f},{y_of(v):.1f}" for i, v in enumerate(values))
        area_d = f"M{x_of(0):.1f},{h:.1f} L{seg} L{x_of(n - 1):.1f},{h:.1f} Z"
    return {"line_d": line_d, "area_d": area_d}


def overall_uptime_pct(health: list[dict]) -> float | None:
    """各服务今日 uptime 等权均值,排除 nodata(uptime_pct is None)。
    全 nodata / 空 → None。英雄区中心大号数字(标签 "今日可用率")。
    刻意取今日而非窗口:大号数字渲染在状态环正中,而环(overall_ring)反映今日态,
    二者须同口径才不自相矛盾(否则"历史好、今天坏"时会出现绿数字嵌在红环里);
    窗口历史由下方趋势线与逐日柱条承载。"""
    vals = [r["uptime_pct"] for r in health if r.get("uptime_pct") is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 1)


def overall_ring(health: list[dict]) -> dict:
    """按各服务今日格状态计数归一成 {ok,degraded,partial,down} 占比(%),
    分母为服务总数;nodata 不计入四段(留作状态环底色余量)。空 → 全 0。"""
    counts = {"ok": 0, "degraded": 0, "partial": 0, "down": 0}
    for r in health:
        days = r.get("days") or []
        state = days[-1]["state"] if days else "nodata"
        if state in counts:
            counts[state] += 1
    total = len(health) or 1
    return {k: round(counts[k] / total * 100, 2) for k in counts}


def _days_clean(latest_event_ts: int | None, now_ts: int) -> int | None:
    """距今最后一次事件的整天数(下取整,钳 ≥0)。latest_event_ts=None(从无事件)→ None。
    英雄区"连续 N 天 0 事件"正向里程碑;调用方传入全表最新事件 ts。"""
    if latest_event_ts is None:
        return None
    return max(0, (now_ts - latest_event_ts) // _DAY_SECONDS)


def _overall_trend_series(health: list[dict]) -> list[float | None]:
    """逐日把各服务 days[i].uptime_pct 等权均值,得整体可用率时间序列(左早右今)。
    某天全服务无数据 → 该点 None(由 _svg_line 断开)。"""
    if not health:
        return []
    n = len(health[0].get("days") or [])
    series: list[float | None] = []
    for i in range(n):
        vals = [
            r["days"][i]["uptime_pct"]
            for r in health
            if i < len(r.get("days") or []) and r["days"][i]["uptime_pct"] is not None
        ]
        series.append(round(sum(vals) / len(vals), 2) if vals else None)
    return series


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


def _hygiene_services(
    store: Store, *, now_ts: int, service_labels: dict[str, str] | None = None
) -> list[dict]:
    """探针 24h uptime/p95(复用 report.aggregate_window;服务名从样本派生)。"""
    samples = store.get_probe_samples_since(now_ts - _DAY_SECONDS)
    names = sorted({s.service for s in samples})
    out: list[dict] = []
    for st in aggregate_window(samples, names):
        uptime = round(st.ok_count / st.total * 100, 1) if st.total else None
        out.append(
            {
                "service": st.service,
                "label": (service_labels or {}).get(st.service, st.service),  # 显示名回退 name
                "uptime_pct": uptime,
                "p95_ms": st.p95_ms,
            }
        )
    return out


def _day_state(
    uptime_pct: float | None, rules: frozenset[str] | set[str], settings: Settings
) -> str:
    """单（服务×天）状态。nodata 优先于一切；其余取最坏 down>partial>degraded>ok。
    uptime is None（total==0）一律 nodata，绝不当 0%→down。"""
    if uptime_pct is None:
        return "nodata"
    down_event = bool({"service_down", "container_down"} & rules)
    if down_event or uptime_pct < settings.sentinel_panel_red_uptime_pct:
        return "down"
    if uptime_pct < settings.sentinel_panel_partial_uptime_pct or "container_unhealthy" in rules:
        return "partial"
    if {"latency_degraded", "mem_pressure"} & rules:
        return "degraded"
    return "ok"


def _service_health_bars(
    store: Store,
    settings: Settings,
    *,
    now_ts: int,
    tz: timezone,
    window_days: int,
    today_samples: list,
    service_labels: dict[str, str] | None = None,
) -> list[dict]:
    """每服务一行 N 天逐日健康柱条。
    返回 [{service, uptime_pct, p95_ms, days:[{date, state, is_today, uptime_pct}]}]。
    days 左=最早、右=今天；历史取 probe_daily，今天用实时聚合；缺行/零样本 → nodata。
    服务集从数据派生（probe_daily 服务 ∪ 今日样本服务），不解析 services.yaml。
    事件按 (date_local, subject) 一次性索引，逐格 O(1) 命中。"""
    today_local = datetime.fromtimestamp(now_ts, tz).date()
    start_date = today_local - timedelta(days=window_days - 1)
    daily = store.get_probe_daily_since(start_date.isoformat())
    services = sorted({r.service for r in daily} | {s.service for s in today_samples})
    today_stats = {st.service: st for st in aggregate_window(today_samples, services)}
    # 当天事件索引：date_local 用同一 tz 换算，与 probe_daily.date 同口径。
    # 查询窗对齐 start_date 本地午夜(与逐日格同口径),不用 now_ts-N*86400
    # ——后者偏移半天、会多捞一截不展示日的事件,徒增噪声。
    win_start_ts = int(
        datetime(start_date.year, start_date.month, start_date.day, tzinfo=tz).timestamp()
    )
    ev_idx: dict[tuple[str, str], set[str]] = {}
    for e in store.get_events_since(win_start_ts):
        d = datetime.fromtimestamp(e.ts, tz).date().isoformat()
        ev_idx.setdefault((d, e.subject), set()).add(e.rule)
    daily_idx = {(r.service, r.date): r for r in daily}
    bars: list[dict] = []
    for svc in services:
        days: list[dict] = []
        for i in range(window_days):
            day = start_date + timedelta(days=i)
            ds = day.isoformat()
            is_today = day == today_local
            if is_today:
                st = today_stats.get(svc)
                uptime = st.uptime_pct if (st and st.total) else None
            else:
                row = daily_idx.get((svc, ds))
                uptime = (row.ok_count / row.total * 100) if (row and row.total) else None
            state = _day_state(uptime, ev_idx.get((ds, svc), frozenset()), settings)
            days.append(
                {
                    "date": ds,
                    "state": state,
                    "is_today": is_today,
                    "uptime_pct": round(uptime, 1) if uptime is not None else None,
                }
            )
        st = today_stats.get(svc)
        bars.append(
            {
                "service": svc,
                "label": (service_labels or {}).get(svc, svc),  # 显示名;无映射回退 name
                "uptime_pct": round(st.uptime_pct, 1) if (st and st.total) else None,
                "p95_ms": st.p95_ms if st else None,
                "days": days,
            }
        )
    # 现态最坏优先（今日格状态），同权重内按服务名；保证 services_cap 截断后先露出问题服务。
    # days 在 window_days<=0 的误配下会为空(range(0))；空则按 nodata 收尾，绝不 IndexError 崩整页。
    bars.sort(
        key=lambda b: (
            _STATE_RANK.get(b["days"][-1]["state"] if b["days"] else "nodata", 5),
            b["service"],
        )
    )
    return bars


def _host_self(
    settings: Settings,
    *,
    latest_probe_ts: int | None,
    now_ts: int,
    llm_config,
    llm: dict,
) -> dict:
    """宿主 & 自身行的新增自省块：探针引擎活性 + LLM active/fallback。
    宿主级 hygiene 告警另由 build_overview 的 hygiene.hygiene_alerts 提供，这里不重算。
    llm 为 _llm_posture 结果（env 回退路径用其 model；llm_config 在时给 active+fallback）。"""
    if latest_probe_ts is None:
        engine_live: bool | None = None
    else:
        engine_live = (now_ts - latest_probe_ts) < 2 * settings.sentinel_probe_interval
    active = fallback = None
    if llm_config is not None:
        cur = llm_config.current()
        fb = llm_config.fallback()
        active = cur.model if cur else None
        fallback = fb.model if fb else None
    else:
        active = llm.get("model")
    return {
        "probe_engine_live": engine_live,
        "llm": {"active": active, "fallback": fallback, "fallback_ready": fallback is not None},
    }


def _event_feed(
    store: Store,
    settings: Settings,
    *,
    now_ts: int,
    tz: timezone,
    open_events: list,
    page: int,
    diag_active: bool,
) -> dict:
    """事件流：近 N 天发生的事件 + 仍 open 的更早事件，ts 降序、服务端切片分页。
    page 越上界钳到末页；空流 total_pages=1、items=[]。"""
    window_start = now_ts - settings.sentinel_event_feed_days * _DAY_SECONDS
    recent = store.get_events_since(window_start)  # ts >= start, desc
    older_open = [e for e in open_events if e.ts < window_start]
    feed = sorted(older_open + recent, key=lambda e: e.ts, reverse=True)
    size = max(1, settings.sentinel_panel_page_size)  # 防 0/负配置触发除零 → 钳到 ≥1
    total = len(feed)
    total_pages = max(1, (total + size - 1) // size)
    page = min(max(1, page), total_pages)
    start = (page - 1) * size
    items = [_event_view(e, tz, diag_active=diag_active) for e in feed[start : start + size]]
    return {
        "items": items,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "page_size": size,
    }


async def build_overview(
    store: Store,
    settings: Settings,
    *,
    now: datetime,
    docker=None,
    llm_config=None,
    diag_registered: bool | None = None,
    window_days: int = 90,
    page: int = 1,
    service_labels: dict[str, str] | None = None,
) -> dict:
    """总览 view-model。docker 为可选注入的 DockerClient,缺省 None → 容器计数降级 None。
    llm_config 为可选注入的 LLMConfig,缺省 None → 回退 settings.llm_*。
    diag_registered 为启动时诊断 job 是否注册(lifespan 注入),用于"已配置·待重启"判定。
    service_labels 为 name→显示名映射(services.yaml 的 label),缺省 None → 面板回退 name。"""
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
    # 健康柱条 + 宿主自身行：今日样本只拉一次，健康柱条与引擎活性共用。
    today_local = datetime.fromtimestamp(now_ts, tz).date()
    midnight_ts = int(
        datetime(today_local.year, today_local.month, today_local.day, tzinfo=tz).timestamp()
    )
    today_samples = store.get_probe_samples_since(midnight_ts)
    health = _service_health_bars(
        store,
        settings,
        now_ts=now_ts,
        tz=tz,
        window_days=window_days,
        today_samples=today_samples,
        service_labels=service_labels,
    )
    # 英雄区数据(纯函数派生,零新取数):每服务迷你趋势线 + 整体指标。
    for row in health:
        row["mini_pts"] = _svg_line([d["uptime_pct"] for d in row["days"]], w=120.0, h=40.0)[
            "line_d"
        ]
    latest_events = store.get_events_since(0, limit=1)  # 全表最新一条,事件稀疏 → 廉价
    latest_event_ts = latest_events[0].ts if latest_events else None
    hero = {
        "uptime_pct": overall_uptime_pct(health),
        "ring": overall_ring(health),
        "days_clean": _days_clean(latest_event_ts, now_ts),
        "trend": _svg_line(_overall_trend_series(health), w=300.0, h=64.0),
    }
    # 引擎活性看全表最新样本(不限今日):午夜后最新样本可能落在昨日,
    # 复用 today_samples 会把"刚探测过"误判成 unknown。
    latest_probe_ts = store.get_latest_probe_ts()
    host_self = _host_self(
        settings, latest_probe_ts=latest_probe_ts, now_ts=now_ts, llm_config=llm_config, llm=llm
    )
    events = _event_feed(
        store,
        settings,
        now_ts=now_ts,
        tz=tz,
        open_events=open_events,
        page=page,
        diag_active=diag_active,
    )
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
            "services": _hygiene_services(store, now_ts=now_ts, service_labels=service_labels),
            "hygiene_alerts": [
                _event_view(e, tz, diag_active=diag_active)
                for e in open_events
                if e.rule in HYGIENE_RULES
            ],
        },
        "window_days": window_days,
        "health": health,
        "hero": hero,
        "host_self": host_self,
        "events": events,
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
