# src/sentinel/panel/view.py
"""证据台 view-model 组装(纯函数)。从 Store + Settings 取数、做状态机/姿态推导,
返回 dict 供 jinja2 模板渲染。不碰 HTTP、不碰模板,可被 pytest 直接断言。"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

from sentinel.config import Settings
from sentinel.findings import HYGIENE_RULES, EventRecord
from sentinel.report import aggregate_window, percentile
from sentinel.store import Store

_DAY_SECONDS = 24 * 3600
# 健康行排序权重：现态(今日格)最坏者优先，使 /services 列表顶端先露出需关注的服务。
# nodata 排最后(刚开始采样的服务不抢占注意力)。同权重内按服务名稳定排序。
_STATE_RANK = {"down": 0, "partial": 1, "degraded": 2, "ok": 3, "nodata": 4}
# 自身/代理永不计入“在监容器”(与 scan_docker 同口径:按镜像子串识别,兼容 registry 前缀)
_SELF_IMAGE_SUBSTR = "watchmend"
_SOCKET_PROXY_IMAGE = "tecnativa/docker-socket-proxy"
# 服务详情:延迟大图上打竖线注释的事件规则(与该服务延迟/可用直接相关)
_LATENCY_EVENT_RULES = frozenset({"latency_degraded", "service_down"})
_SAMPLE_WINDOW_SECONDS = 2 * _DAY_SECONDS  # 逐次延迟图窗口(近 48h)
_SAMPLE_CAP = 240  # 逐次图下采样上限(防 DOM 膨胀)
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


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


def _mini_sparkline_points(series: list[float | None], *, w: float = 140.0, h: float = 34.0) -> str:
    """单线迷你 sparkline 的 SVG path d(复用 _svg_line,只取折线、丢面积)。
    缺数据(全 None / <2 点)→ 空串,模板据此不渲染。"""
    return _svg_line(series, w=w, h=h)["line_d"]


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


def uptime_grade(pct: float | None, *, green: float, partial: float) -> str:
    """今日可用率阈值分级 → 阈值色 class。None(无数据)→ nodata;
    ≥green → ok(绿);≥partial → warn(琥珀);以下 → bad(红)。环中心大号与服务表共用。"""
    if pct is None:
        return "nodata"
    if pct >= green:
        return "ok"
    if pct >= partial:
        return "warn"
    return "bad"


def _window_mean(series: list[float | None], days: int) -> float | None:
    """趋势序列(左早右今)最近 `days` 天非 None 点的均值,round1。全 None/空 → None。"""
    window = series[-days:] if days > 0 else []
    vals = [v for v in window if v is not None]
    if not vals:
        return None
    return round(sum(vals) / len(vals), 1)


def _window_delta(series: list[float | None], days: int) -> float | None:
    """最近 `days` 窗口均值 − 紧邻的前一个等长窗口均值,round1。
    任一窗口无数据(无可比基线)→ None。用于 KPI 的 ▲/▼ 趋势箭头。"""
    cur = _window_mean(series, days)
    prior_slice = series[-2 * days : -days] if days > 0 else []
    prior_vals = [v for v in prior_slice if v is not None]
    if cur is None or not prior_vals:
        return None
    return round(cur - sum(prior_vals) / len(prior_vals), 1)


def _delta_dir(delta: float | None, eps: float = 0.05) -> str:
    """Δ 方向 class:None/在 ±eps 内 → flat;>eps → up;<-eps → down。"""
    if delta is None or abs(delta) <= eps:
        return "flat"
    return "up" if delta > 0 else "down"


_POINT_RULES = frozenset({"container_oom", "container_crashloop", "container_restart"})


def _is_real_incident(rule: str) -> bool:
    """「真实事件」= 可开→可恢复的状态型告警,排除 hygiene、巡检失败、即开即解的 point 事件。
    用于 MTTR 与 24h 净流的一致口径,避免每天例行触发的噪声污染信号。"""
    return (
        rule not in HYGIENE_RULES
        and not rule.startswith("scan_failed_")
        and rule not in _POINT_RULES
    )


def _mttr(resolved_events: list) -> dict:
    """MTTR(窗口内已恢复事件的平均/最差解决时长,秒)。
    仅计 resolved_ts > ts 的事件 → 天然排除 point 事件(resolved_ts==ts→时长 0,否则静默拉低均值)。
    返回 {mean_s, worst_s, n};n=0 → mean/worst None。"""
    durs = [e.resolved_ts - e.ts for e in resolved_events if e.resolved_ts and e.resolved_ts > e.ts]
    if not durs:
        return {"mean_s": None, "worst_s": None, "n": 0}
    return {"mean_s": round(sum(durs) / len(durs)), "worst_s": max(durs), "n": len(durs)}


def _net_flow(opened_events: list, resolved_events: list) -> dict:
    """24h 净流:窗口内新开 vs 已恢复的「真实事件」计数(对称口径)。
    opened 按 ts 落窗(调用方已过滤窗口);resolved 还须 resolved_ts>ts(真转移)。"""
    opened = sum(1 for e in opened_events if _is_real_incident(e.rule))
    resolved = sum(
        1
        for e in resolved_events
        if _is_real_incident(e.rule) and e.resolved_ts and e.resolved_ts > e.ts
    )
    return {"opened": opened, "resolved": resolved}


def _fmt_duration(s: float | None) -> str:
    """时长(秒)→ 紧凑人类可读(s/m/h/d)。None → "–"。"""
    if s is None:
        return "–"
    if s < 60:
        return f"{round(s)}s"
    if s < 3600:
        return f"{round(s / 60)}m"
    if s < _DAY_SECONDS:
        return f"{round(s / 3600)}h"
    return f"{round(s / _DAY_SECONDS)}d"


def _row_state(r: dict) -> str:
    """健康行现态 = 最后一日(今日)格状态;空 days → nodata。与 overall_ring 同口径。"""
    days = r.get("days") or []
    return days[-1]["state"] if days else "nodata"


_HYGIENE_BUCKET = {"backup_stale": "backup", "disk_forecast": "disk", "cert_expiry": "cert"}


def _hygiene_by_type(open_events: list) -> dict:
    """未结 hygiene 事件按类型(备份/磁盘/证书)计数 + 总数。三态卡明细仍留 /hygiene,本处仅计数。"""
    counts = {"backup": 0, "disk": 0, "cert": 0}
    for e in open_events:
        bucket = _HYGIENE_BUCKET.get(e.rule)
        if bucket:
            counts[bucket] += 1
    return {**counts, "total": sum(counts.values())}


def _report_freshness(date_str: str | None, today: date) -> dict:
    """日报新鲜度:把裸日期变判断。空/None → never;==今日 → today(0);否则 stale(N 天前)。"""
    if not date_str:
        return {"date": None, "freshness": "never", "days": None}
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        return {"date": date_str, "freshness": "never", "days": None}
    delta = (today - d).days
    return {"date": date_str, "freshness": "today" if delta == 0 else "stale", "days": delta}


def _top_roster(health: list[dict], cap: int) -> dict:
    """服务表:health 已 worst-first;取前 cap 行(每行展示名/三窗口可用率/p95/现态),
    overflow=剩余条数(模板渲「其余 N 项 → /services」)。"""
    rows = [
        {
            "name": r["service"],
            "label": r["label"],
            "state": _row_state(r),
            "p95_ms": r.get("p95_ms"),
            **_service_windows(r),
        }
        for r in health[:cap]
    ]
    return {"rows": rows, "overflow": max(0, len(health) - cap)}


def _open_by_severity(open_events: list) -> dict:
    """未结事件按严重度计数(severity ∈ {critical, warning})+ 总数。纯计数,事件已 fetch。"""
    crit = sum(1 for e in open_events if e.severity == "critical")
    warn = sum(1 for e in open_events if e.severity == "warning")
    return {"total": len(open_events), "critical": crit, "warning": warn}


def _service_state_counts(health: list[dict]) -> dict:
    """服务按现态四分 + nodata 计数(替代旧 rollup 的 problem 折叠,保留严重度梯度)。"""
    counts = {"total": len(health), "ok": 0, "degraded": 0, "partial": 0, "down": 0, "nodata": 0}
    for r in health:
        st = _row_state(r)
        if st in counts:
            counts[st] += 1
    return counts


def _service_windows(row: dict) -> dict:
    """单服务 SLO 看板三窗口可用率(今日/7d/30d),纯取健康行 days[].uptime_pct 求均值。
    无数据窗口 → None(模板渲 "–" 灰,永不染绿)。零新读。"""
    series = [d.get("uptime_pct") for d in (row.get("days") or [])]
    return {
        "d1": _window_mean(series, 1),
        "d7": _window_mean(series, 7),
        "d30": _window_mean(series, 30),
    }


def _when(ts: int, tz: timezone) -> str:
    """事件时间戳:始终带「月-日 时:分」(跨年再带年)。
    裸 HH:MM 在跨多日的事件流里无法分辨是哪天(过去几天的事件会被误读成今天),故统一带日期。"""
    dt = datetime.fromtimestamp(ts, tz)
    fmt = "%m-%d %H:%M" if dt.year == datetime.now(tz).year else "%Y-%m-%d %H:%M"
    return dt.strftime(fmt)


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


_CONFIDENCE_LEVELS = ("high", "medium", "low")
_CONFIDENCE_PCT = {"high": 92, "medium": 66, "low": 38}


def _confidence_of(e: EventRecord) -> str | None:
    """诊断置信度(high/medium/low)读自 diagnosis_json.confidence;缺/坏/未知 → None。"""
    if not e.diagnosis_json:
        return None
    try:
        d = json.loads(e.diagnosis_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(d, dict):
        c = d.get("confidence")
        if isinstance(c, str) and c.strip().lower() in _CONFIDENCE_LEVELS:
            return c.strip().lower()
    return None


def _tool_count_of(e: EventRecord) -> int:
    """取证步数 = diagnosis_tools_json 数组长度;缺/坏 → 0。"""
    if not e.diagnosis_tools_json:
        return 0
    try:
        calls = json.loads(e.diagnosis_tools_json)
    except (json.JSONDecodeError, TypeError):
        return 0
    return len(calls) if isinstance(calls, list) else 0


def _confidence_pct(level: str | None) -> int | None:
    """置信度 → 环 dasharray 百分比(high/medium/low → 92/66/38);未知/无 → None。"""
    return _CONFIDENCE_PCT.get(level) if level else None


def _event_view(e: EventRecord, tz: timezone, *, diag_active: bool = True) -> dict:
    detail_preview = _ANSI_ESCAPE_RE.sub("", e.detail).split("\n", 1)[0].strip()
    return {
        "id": e.id,
        "ts_str": _when(e.ts, tz),
        "rule": e.rule,
        "subject": e.subject,
        "severity": e.severity,
        "lifecycle": _lifecycle(e, diag_active=diag_active),
        "is_scan_failure": e.rule.startswith("scan_failed_"),
        "diagnosis_status": e.diagnosis_status,
        "has_evidence": e.diagnosis_status == "done" and bool(e.diagnosis_tools_json),
        "summary": _summary_of(e),
        "detail_preview": detail_preview[:240],
        "confidence": _confidence_of(e),  # §8.3 事件流卡第二行置信度色(总览不引用)
        "tool_count": _tool_count_of(e),  # §8.3 第三行取证步数
        "resolved_str": _when(e.resolved_ts, tz) if e.resolved_ts else None,
    }


def _event_filter_subject(e: EventRecord) -> str:
    """错误指纹仍以完整 subject 做冷却去重，但事件页按容器聚合筛选。"""
    if e.rule != "log_error_new":
        return e.subject
    try:
        payload = json.loads(e.payload_json)
    except (json.JSONDecodeError, TypeError):
        payload = {}
    if isinstance(payload, dict):
        container = payload.get("container")
        if isinstance(container, str) and container.strip():
            return container.strip()
    prefix, separator, _ = e.subject.partition(" · ")
    return prefix.strip() if separator and prefix.strip() else e.subject


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
    events = store.get_events_since(win_start_ts)
    ev_idx: dict[tuple[str, str], set[str]] = {}
    # 排序的“最近异常”同时覆盖：
    # 1. 单次失败探针（即使尚未达到连续失败告警阈值）；
    # 2. 与服务同 subject 的真实巡检事件（含已恢复与 point 事件）。
    # hygiene / scan_failed_* 是 WatchMend 自身巡检姿态，不应改变业务服务顺序。
    last_issue_ts = store.get_latest_failed_probe_ts_by_service(win_start_ts)
    for e in events:
        d = datetime.fromtimestamp(e.ts, tz).date().isoformat()
        ev_idx.setdefault((d, e.subject), set()).add(e.rule)
        if (
            e.subject in services
            and e.rule not in HYGIENE_RULES
            and not e.rule.startswith("scan_failed_")
        ):
            last_issue_ts[e.subject] = max(last_issue_ts.get(e.subject, 0), e.ts)
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
                p95 = st.p95_ms if st else None
                p50 = st.p50_ms if st else None
            else:
                row = daily_idx.get((svc, ds))
                uptime = (row.ok_count / row.total * 100) if (row and row.total) else None
                p95 = row.p95_ms if row else None
                p50 = row.p50_ms if row else None
            state = _day_state(uptime, ev_idx.get((ds, svc), frozenset()), settings)
            days.append(
                {
                    "date": ds,
                    "state": state,
                    "is_today": is_today,
                    "uptime_pct": round(uptime, 1) if uptime is not None else None,
                    # 逐日 p50/p95(服务页迷你 sparkline + 延迟大图复用)
                    "p95_ms": round(p95, 1) if p95 is not None else None,
                    "p50_ms": round(p50, 1) if p50 is not None else None,
                }
            )
        st = today_stats.get(svc)
        bars.append(
            {
                "service": svc,
                "label": (service_labels or {}).get(svc, svc),  # 显示名;无映射回退 name
                "uptime_pct": round(st.uptime_pct, 1) if (st and st.total) else None,
                "p95_ms": st.p95_ms if st else None,
                "last_issue_ts": last_issue_ts.get(svc),
                "days": days,
            }
        )
    # 现态最坏优先（今日格状态）；同状态按最近异常倒序，从未异常的服务最后；
    # 最后按服务名保证稳定顺序。总览 roster 与 /services 共用这份排序。
    # days 在 window_days<=0 的误配下会为空(range(0))；空则按 nodata 收尾，绝不 IndexError 崩整页。
    bars.sort(
        key=lambda b: (
            _STATE_RANK.get(b["days"][-1]["state"] if b["days"] else "nodata", 5),
            -(b["last_issue_ts"] or 0),
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
    宿主级 hygiene 告警另由 /hygiene 的本地三态卡(_local_hygiene)提供，这里不重算。
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


async def build_overview(
    store: Store,
    settings: Settings,
    *,
    now: datetime,
    llm_config=None,
    diag_registered: bool | None = None,
    window_days: int = 90,
    service_labels: dict[str, str] | None = None,
) -> dict:
    """总览 view-model(SLO 看板)。
    HERO:去重状态环(环=今日 4 态分布、中心=今日可用率带阈值色)+ 环旁 KPI
    (7/30/90d 均值+Δ、开放按严重度、24h 净流、MTTR)。
    再下:服务表(最差优先、今日/7d/30d 可用率,截断 cap、其余 → /services)、
    待关注(未结告警,非空才渲)、汇总(体检计数/日报新鲜度)。
    明细(事件流/三态体检卡/宿主姿态)交给子页,本页零重复。
    指标全为 view 层纯函数聚合现有 store 读(additive,无新表/列;新增读仅 resolved/events 窗口查询)。
    llm_config/diag_registered 仅供 header 的 LLM pill 姿态;完整姿态见 /hygiene。
    service_labels 为 name→显示名映射(services.yaml 的 label),缺省 None → 面板回退 name。"""
    tz = (
        now.tzinfo
        if isinstance(now.tzinfo, timezone)
        else timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    )
    now_ts = int(now.timestamp())
    open_events = store.get_open_events()
    llm = _llm_posture(llm_config, settings, diag_registered=diag_registered)
    # 诊断层此刻是否真在跑:已配置且未待重启。pending 事件的生命周期据此显示。
    diag_active = llm["enabled"] and not llm["pending_restart"]
    # 健康柱条供 HERO(整体环/可用率/趋势)+ 服务汇总计数派生;今日样本只拉一次。
    today_local = datetime.fromtimestamp(now_ts, tz).date()
    midnight_ts = int(
        datetime(today_local.year, today_local.month, today_local.day, tzinfo=tz).timestamp()
    )
    today_samples = store.get_probe_samples_since(midnight_ts)
    # SLO 窗口(7/30/90d)是与探针条 window 无关的滚动均值:固定按最大可用跨度(window 与
    # 保留史的较大者)建柱条,使 d90 真覆盖 90 天而非被 window=30 钳成 d30 的克隆。今日态量
    # (可用率/环/4 态计数/最差优先)只读 days[-1],与跨度无关,扩跨度不改其值。
    slo_span = max(window_days, settings.sentinel_panel_history_days)
    health = _service_health_bars(
        store,
        settings,
        now_ts=now_ts,
        tz=tz,
        window_days=slo_span,
        today_samples=today_samples,
        service_labels=service_labels,
    )
    # 窗口均值标量(7/30/90d)+ Δ vs 紧邻上一窗口,源自与逐日柱条同口径的整体趋势序列。
    series = _overall_trend_series(health)

    def _win(days: int) -> dict:
        # 序列不足整窗(如保留史 < 90d 时的 d90):不把截断窗口冒充完整窗口 → mean/Δ 皆 None。
        if len(series) < days:
            return {"mean": None, "delta": None, "dir": "flat"}
        delta = _window_delta(series, days)
        return {"mean": _window_mean(series, days), "delta": delta, "dir": _delta_dir(delta)}

    uptime_pct = overall_uptime_pct(health)
    # 事件态势:MTTR 取近 30d 已恢复(resolved_ts>ts 过滤排除 point 事件);
    # 24h 净流 = 近 24h 新开 vs 近 24h 已恢复的「真实事件」(对称口径)。事件稀疏,读廉价。
    resolved_30d = store.get_resolved_since(now_ts - 30 * _DAY_SECONDS, limit=1000)
    opened_24h = store.get_events_since(now_ts - _DAY_SECONDS)
    resolved_24h = [
        e for e in resolved_30d if e.resolved_ts and e.resolved_ts >= now_ts - _DAY_SECONDS
    ]
    hero = {
        "uptime_pct": uptime_pct,
        "uptime_grade": uptime_grade(
            uptime_pct,
            green=settings.sentinel_panel_green_uptime_pct,
            partial=settings.sentinel_panel_partial_uptime_pct,
        ),
        "ring": overall_ring(health),
        "services": _service_state_counts(health),
        "windows": {"d7": _win(7), "d30": _win(30), "d90": _win(90)},
        "open": _open_by_severity(open_events),
        "flow_24h": _net_flow(opened_24h, resolved_24h),
        "mttr": _mttr(resolved_30d),
    }
    # 待关注:当前未结的「告警」事件(非 hygiene),压缩成一行 → /event/{id}。模板仅在非空时渲染。
    # hygiene 类未结事件由汇总栏的「体检」计数承载,跳 /hygiene 看明细。
    labels = service_labels or {}
    needs_attention = []
    for e in open_events:
        if e.rule in HYGIENE_RULES:
            continue
        v = _event_view(e, tz, diag_active=diag_active)
        v["label"] = labels.get(e.subject, e.subject)  # 展示名;无映射回退 subject(与事件流同口径)
        needs_attention.append(v)
    return {
        "now_str": now.strftime("%Y-%m-%d %H:%M"),
        "refresh_seconds": settings.sentinel_panel_refresh_seconds,
        "window_days": window_days,
        # header 的 LLM pill 仅读 posture.llm;完整姿态(渠道/层/docker/容器)见 /hygiene。
        "posture": {"llm": llm},
        "hero": hero,
        "roster": _top_roster(health, settings.sentinel_panel_overview_roster_cap),
        "needs_attention": needs_attention,
        "rollup": {
            "hygiene": _hygiene_by_type(open_events),
            "report": _report_freshness(store.get_meta("daily_report_last_date"), today_local),
        },
    }


def build_services_list(
    store: Store,
    settings: Settings,
    *,
    now: datetime,
    window_days: int = 30,
    service_labels: dict[str, str] | None = None,
) -> dict:
    """服务列表 view-model(实用页 §8.2)。复用 _service_health_bars(已 worst-first 排序、
    含逐日 p95);每服务补现态 state + p95 迷你 sparkline。零新取数。"""
    tz = (
        now.tzinfo
        if isinstance(now.tzinfo, timezone)
        else timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    )
    now_ts = int(now.timestamp())
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
    for row in health:
        row["state"] = row["days"][-1]["state"] if row["days"] else "nodata"
        row["p95_pts"] = _mini_sparkline_points([d["p95_ms"] for d in row["days"]])
    return {
        "now_str": now.strftime("%Y-%m-%d %H:%M"),
        "refresh_seconds": settings.sentinel_panel_refresh_seconds,
        "window_days": window_days,
        "services": health,
    }


def _latency_chart(
    p50: list[float | None], p95: list[float | None], *, w: float = 600.0, h: float = 120.0
) -> dict:
    """p50/p95 双线共享 y 轴 + p50–p95 区间面积(spec §6 sparkline 详情大图)。
    共享 y 轴:两线同尺度才能读出"带宽=p50→p95 差"。ymin/ymax 取两序列窗口极值留 10% padding。
    <2 有效点 → 三者皆空串;含缺口的线自然断开,面积仅在两线均无缺口时填充。"""
    n = len(p50)
    allnums = [v for v in (list(p50) + list(p95)) if v is not None]
    if n < 2 or len(allnums) < 2:
        return {"p50_d": "", "p95_d": "", "band_d": ""}
    lo, hi = min(allnums), max(allnums)
    span = (hi - lo) or 1.0
    pad = span * 0.10
    lo -= pad
    hi += pad
    rng = (hi - lo) or 1.0

    def x_of(i: int) -> float:
        return i / (n - 1) * w

    def y_of(v: float) -> float:
        return h - (v - lo) / rng * h

    def path_of(series: list[float | None]) -> str:
        parts: list[str] = []
        gap = True
        for i, v in enumerate(series):
            if v is None:
                gap = True
                continue
            parts.append(f"{'M' if gap else 'L'}{x_of(i):.1f},{y_of(v):.1f}")
            gap = False
        return " ".join(parts)

    if None not in p50 and None not in p95:
        top = " ".join(f"{x_of(i):.1f},{y_of(v):.1f}" for i, v in enumerate(p95))
        bot = " ".join(f"{x_of(i):.1f},{y_of(v):.1f}" for i, v in reversed(list(enumerate(p50))))
        band_d = f"M{top} L{bot} Z"
    else:
        band_d = ""
    return {"p50_d": path_of(p50), "p95_d": path_of(p95), "band_d": band_d}


def _downsample(items: list, cap: int) -> list:
    """等间隔下采样到 ≤cap 个(防逐次图 DOM 膨胀)。"""
    n = len(items)
    if n <= cap:
        return items
    step = n / cap
    return [items[min(n - 1, int(i * step))] for i in range(cap)]


def _sample_latency_series(samples: list, *, cap: int = _SAMPLE_CAP) -> list[float | None]:
    """逐次延迟序列(升序、已下采样):ok 取 latency_ms,超时/失败 → None 断线。"""
    ordered = _downsample(sorted(samples, key=lambda s: s.ts), cap)
    return [s.latency_ms if (s.ok and s.latency_ms is not None) else None for s in ordered]


def _status_code_buckets(samples: list) -> list[dict]:
    """状态码分布(status_code is None → timeout 桶)。按出现次数降序、同数按码名。空 → []。"""
    counts: dict[str, int] = {}
    for s in samples:
        key = "timeout" if s.status_code is None else str(s.status_code)
        counts[key] = counts.get(key, 0) + 1
    total = sum(counts.values()) or 1
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"code": k, "count": v, "pct": round(v / total * 100, 1)} for k, v in items]


def _event_x_marks(
    events: list[EventRecord], *, start_ts: int, end_ts: int, tz: timezone, w: float = 600.0
) -> list[dict]:
    """事件 ts → 时序大图 x 像素(同 viewBox 叠竖线,可点进 /event/{id})。窗口外丢弃。"""
    span = (end_ts - start_ts) or 1
    out: list[dict] = []
    for e in events:
        if e.ts < start_ts or e.ts > end_ts:
            continue
        x = (e.ts - start_ts) / span * w
        out.append(
            {
                "id": e.id,
                "x": round(min(max(x, 0.0), w), 1),
                "severity": e.severity,
                "rule": e.rule,
                "ts_str": _when(e.ts, tz),
            }
        )
    return out


def _latency_compare(
    store: Store, settings: Settings, service: str, *, current_p95: float | None, before_date: str
) -> dict:
    """p95 三方对比(spec §8.2:必须复刻 probe_rules.py 阈值口径,否则误导)。
    baseline = get_recent_daily_p95s(service, before_date, limit=7) 均值(与引擎同口径);
    threshold = max(baseline*ratio, baseline+margin_ms)。无基线 → baseline/threshold=None。"""
    p95s = store.get_recent_daily_p95s(service, before_date=before_date, limit=7)
    if not p95s:
        return {
            "current_ms": round(current_p95) if current_p95 is not None else None,
            "baseline_ms": None,
            "threshold_ms": None,
            "fill_pct": None,
            "breached": None,
            "baseline_days": 0,
        }
    baseline = sum(p95s) / len(p95s)
    threshold = max(
        baseline * settings.sentinel_latency_ratio,
        baseline + settings.sentinel_latency_margin_ms,
    )
    fill = breached = None
    if current_p95 is not None and threshold:
        fill = round(min(current_p95 / threshold * 100, 100))
        breached = current_p95 > threshold
    return {
        "current_ms": round(current_p95) if current_p95 is not None else None,
        "baseline_ms": round(baseline),
        "threshold_ms": round(threshold),
        "fill_pct": fill,  # current 占阈值百分比(进度条宽度,封顶 100)
        "breached": breached,
        "baseline_days": len(p95s),
    }


def build_service_detail(
    store: Store,
    settings: Settings,
    name: str,
    *,
    now: datetime,
    window_days: int = 30,
    gran: str = "daily",
    page: int = 1,
    service_labels: dict[str, str] | None = None,
    llm_config=None,
    diag_registered: bool | None = None,
) -> dict | None:
    """服务详情 view-model(实用页 §8.2,主角=延迟时序大图)。
    完全无数据(无逐日行 + 近 24h 无样本 + 无相关事件)→ None(路由 404)。
    gran='samples' → 近 48h 逐次延迟(下采样);否则逐日 p50/p95。零新取数。"""
    tz = (
        now.tzinfo
        if isinstance(now.tzinfo, timezone)
        else timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    )
    now_ts = int(now.timestamp())
    today_local = datetime.fromtimestamp(now_ts, tz).date()
    midnight_ts = int(
        datetime(today_local.year, today_local.month, today_local.day, tzinfo=tz).timestamp()
    )
    posture = _llm_posture(llm_config, settings, diag_registered=diag_registered)
    diag_active = posture["enabled"] and not posture["pending_restart"]
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
    row = next((b for b in health if b["service"] == name), None)
    # 24h 滚动样本:当前 p95(24h)+ 状态码分布
    day_samples = [
        s for s in store.get_probe_samples_since(now_ts - _DAY_SECONDS) if s.service == name
    ]
    # 该服务相关事件(事件流窗口内 + 仍 open 的更早),ts 降序
    window_start = now_ts - settings.sentinel_event_feed_days * _DAY_SECONDS
    recent = [e for e in store.get_events_since(window_start) if e.subject == name]
    older_open = [e for e in store.get_open_events() if e.subject == name and e.ts < window_start]
    svc_events = sorted(older_open + recent, key=lambda e: e.ts, reverse=True)
    if row is None and not day_samples and not svc_events:
        return None
    label = (service_labels or {}).get(name, name)
    state = row["days"][-1]["state"] if (row and row["days"]) else "nodata"

    ok_lat_24h = [s.latency_ms for s in day_samples if s.ok and s.latency_ms is not None]
    current_p95 = percentile(ok_lat_24h, 95)
    compare = _latency_compare(
        store, settings, name, current_p95=current_p95, before_date=today_local.isoformat()
    )

    if gran == "samples":
        win_start = now_ts - _SAMPLE_WINDOW_SECONDS
        win_samples = sorted(
            (s for s in store.get_probe_samples_since(win_start) if s.service == name),
            key=lambda s: s.ts,
        )
        lat = _sample_latency_series(win_samples)
        chart = {
            "mode": "samples",
            "single_d": _svg_line(lat, w=600.0, h=120.0)["line_d"],
            "p50_d": "",
            "p95_d": "",
            "band_d": "",
        }
        x_start = win_samples[0].ts if win_samples else win_start
        x_end = win_samples[-1].ts if win_samples else now_ts
    else:
        days = row["days"] if row else []
        c = _latency_chart([d["p50_ms"] for d in days], [d["p95_ms"] for d in days])
        chart = {"mode": "daily", "single_d": "", **c}
        start_date = today_local - timedelta(days=window_days - 1)
        x_start = int(
            datetime(start_date.year, start_date.month, start_date.day, tzinfo=tz).timestamp()
        )
        x_end = now_ts

    ann_events = [e for e in svc_events if e.rule in _LATENCY_EVENT_RULES]
    marks = _event_x_marks(ann_events, start_ts=x_start, end_ts=x_end, tz=tz, w=600.0)
    status_codes = _status_code_buckets(day_samples)

    size = max(1, settings.sentinel_panel_page_size)
    total = len(svc_events)
    total_pages = max(1, (total + size - 1) // size)
    page = min(max(1, page), total_pages)
    start = (page - 1) * size
    ev_items = [
        _event_view(e, tz, diag_active=diag_active) for e in svc_events[start : start + size]
    ]

    return {
        "now_str": now.strftime("%Y-%m-%d %H:%M"),
        "service": name,
        "label": label,
        "state": state,
        "window_days": window_days,
        "gran": gran,
        "uptime_pct": row["uptime_pct"] if row else None,
        "p95_ms": row["p95_ms"] if row else None,
        "compare": compare,
        "chart": chart,
        "marks": marks,
        "status_codes": status_codes,
        "days": row["days"] if row else [],
        "posture": {"llm": posture},
        "events": {
            "items": ev_items,
            "page": page,
            "total_pages": total_pages,
            "total": total,
        },
    }


_EVENT_SEVERITIES = ("critical", "warning")
_EVENT_STATUSES = ("open", "recovered")


def _real_service_names(store: Store, *, now_ts: int, tz: timezone, window_days: int) -> set[str]:
    """真实探针服务集,且与 /service/{name} 在同一 window_days 下「渲染为 200」的判定对齐:
    probe_daily(最近 window_days 天) ∪ 近 24h 探针样本(= build_service_detail 的 day_samples 来源)。
    据此「可点 ⟹ 该 window 下 /service 必渲染 200」,绝不产生 404 死链(链接携带同一 win=window_days);
    host 指标(disk/mem)、状态页 provider、容器名等非探针 subject 不入这两表,恒为纯文本。
    复用既有读(get_probe_daily_since / get_probe_samples_since),零新表/列/查询方法。"""
    today_local = datetime.fromtimestamp(now_ts, tz).date()
    start_date = today_local - timedelta(days=max(1, window_days) - 1)
    daily = store.get_probe_daily_since(start_date.isoformat())
    recent_24h = store.get_probe_samples_since(now_ts - _DAY_SECONDS)
    return {r.service for r in daily} | {s.service for s in recent_24h}


def build_events_list(
    store: Store,
    settings: Settings,
    *,
    now: datetime,
    page: int = 1,
    subject: str | None = None,
    severity: str | None = None,
    status: str | None = None,
    window_days: int | None = None,
    service_labels: dict[str, str] | None = None,
    llm_config=None,
    diag_registered: bool | None = None,
) -> dict:
    """事件流列表 view-model(§8.3)。同源 _event_feed(近 N 天 + 仍 open 的更早),
    再对 subject/severity/status 做内存过滤(store 无对应过滤查询;事件稀疏,全量过滤廉价)。
    筛选纯 query 参数,零新查询;翻页须带筛选(路由 qurl 透传 transient)。
    status='recovered' 映射 EventRecord.status=='resolved';非法筛选值由路由先归一为 None。"""
    tz = (
        now.tzinfo
        if isinstance(now.tzinfo, timezone)
        else timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    )
    now_ts = int(now.timestamp())
    posture = _llm_posture(llm_config, settings, diag_registered=diag_registered)
    diag_active = posture["enabled"] and not posture["pending_restart"]
    window_start = now_ts - settings.sentinel_event_feed_days * _DAY_SECONDS
    recent = store.get_events_since(window_start)
    older_open = [e for e in store.get_open_events() if e.ts < window_start]
    feed = sorted(older_open + recent, key=lambda e: e.ts, reverse=True)
    labels = service_labels or {}
    # linkability 与服务页同窗:链接携带 win=wd,故按 wd 算「可点 ⟹ /service 渲染 200」
    wd = window_days if window_days is not None else settings.sentinel_panel_default_window
    real_services = _real_service_names(store, now_ts=now_ts, tz=tz, window_days=wd)
    subjects = sorted({_event_filter_subject(e) for e in feed})

    def keep(e: EventRecord) -> bool:
        if subject and e.subject != subject and _event_filter_subject(e) != subject:
            return False
        if severity and e.severity != severity:
            return False
        if status == "open" and e.status != "open":
            return False
        if status == "recovered" and e.status != "resolved":
            return False
        return True

    filtered = [e for e in feed if keep(e)]
    size = max(1, settings.sentinel_panel_page_size)
    total = len(filtered)
    total_pages = max(1, (total + size - 1) // size)
    page = min(max(1, page), total_pages)
    start = (page - 1) * size
    items: list[dict] = []
    for e in filtered[start : start + size]:
        v = _event_view(e, tz, diag_active=diag_active)
        filter_subject = _event_filter_subject(e)
        v["filter_subject"] = filter_subject
        v["label"] = labels.get(filter_subject, filter_subject)  # 错误指纹按容器展示
        v["service_linkable"] = e.subject in real_services  # 仅真实探针服务名可点回详情
        items.append(v)
    return {
        "now_str": now.strftime("%Y-%m-%d %H:%M"),
        "refresh_seconds": settings.sentinel_panel_refresh_seconds,
        "subjects": [{"name": s, "label": labels.get(s, s)} for s in subjects],
        "severities": list(_EVENT_SEVERITIES),
        "statuses": list(_EVENT_STATUSES),
        "filters": {"subject": subject, "severity": severity, "status": status},
        "events": {
            "items": items,
            "page": page,
            "total_pages": total_pages,
            "total": total,
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
    store: Store,
    settings: Settings,
    event_id: int,
    *,
    window_days: int | None = None,
    llm_config=None,
    diag_registered=None,
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
    ev["display_subject"] = _event_filter_subject(e)
    now_ts = int(datetime.now(tz).timestamp())
    # linkability 与服务页同窗(链接携带 win=wd):可点 ⟹ 该窗口下 /service 渲染 200,不产生死链
    wd = window_days if window_days is not None else settings.sentinel_panel_default_window
    ev["service_linkable"] = e.subject in _real_service_names(
        store, now_ts=now_ts, tz=tz, window_days=wd
    )  # 仅真实探针服务 subject 链回 /service/{name}
    diagnosis = None
    if e.diagnosis_status == "done" and e.diagnosis_json:
        try:
            parsed = json.loads(e.diagnosis_json)
            diagnosis = parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, TypeError):
            diagnosis = None
    conf_level = _confidence_of(e)  # 归一 high/medium/low(给 HERO 置信度环)
    return {
        "event": ev,
        "llm_enabled": posture["enabled"],
        "diagnosis": diagnosis,
        "confidence_level": conf_level,  # §8.3 HERO:环填充 + 标签
        "confidence_pct": _confidence_pct(conf_level),  # 92/66/38 给环 dasharray
        "tool_calls": _tool_calls_view(e.diagnosis_tools_json),
    }


# Indicator(上游 status snapshot)→ 面板 state 色;component status 同理(§8.4)。
_INDICATOR_STATE = {
    "none": "ok",
    "minor": "degraded",
    "major": "partial",
    "critical": "down",
    "unknown": "nodata",
}
_COMP_STATE = {
    "operational": "ok",
    "maintenance": "degraded",
    "degraded": "degraded",
    "partial_outage": "partial",
    "major_outage": "down",
    "unknown": "nodata",
}


def _worst_state(states: list[str]) -> str:
    """取最坏档(_STATE_RANK 越小越坏;空 → nodata)。"""
    if not states:
        return "nodata"
    return min(states, key=lambda s: _STATE_RANK.get(s, 4))


def _hygiene_payload(e: EventRecord) -> dict:
    try:
        d = json.loads(e.payload_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    return d if isinstance(d, dict) else {}


def _num(v) -> float | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _local_hygiene(store: Store, settings: Settings, *, tz: timezone) -> list[dict]:
    """本地体检三态卡(backup/disk/cert),事件驱动 + 配置门控(§8.4)。
    state:alert(有 open 事件,带数值)/ ok(已配置且无 open 事件)/ unevaluated(未配置)。
    未配置绝不画 ok ✓——避免"没在查"被误读成"健康"。无常驻 gauge 表,数值从 payload_json 解析。"""
    open_by_rule: dict[str, EventRecord] = {}
    for e in store.get_open_events():
        cur = open_by_rule.get(e.rule)
        if cur is None or e.ts > cur.ts:
            open_by_rule[e.rule] = e

    # backup:configured = backup_dir 非空(引擎对 backup 始终尝试,与 forecast/cert 的配置门控一致)
    be = open_by_rule.get("backup_stale")
    if be is not None:
        p = _hygiene_payload(be)
        age = _num(p.get("age_hours"))
        backup = {
            "key": "backup",
            "state": "alert",
            "event_id": be.id,
            "ts_str": _when(be.ts, tz),
            "values": {
                "age_hours": round(age, 1) if age is not None else None,
                "threshold_hours": settings.sentinel_backup_max_age_hours,
            },
        }
    else:
        backup = {
            "key": "backup",
            "state": "ok" if settings.sentinel_backup_dir else "unevaluated",
            "event_id": None,
            "ts_str": None,
            "values": {},
        }

    # disk:主 disk_forecast(预测),副 disk_usage(当前超阈水位);任一 open 即 alert
    disk_fc = open_by_rule.get("disk_forecast")
    disk_use = open_by_rule.get("disk_usage")
    primary = disk_fc or disk_use
    if primary is not None:
        disk_state = "alert"
    elif settings.sentinel_prometheus_url:
        disk_state = "ok"
    else:
        disk_state = "unevaluated"
    fc_p = _hygiene_payload(disk_fc) if disk_fc else {}
    use_p = _hygiene_payload(disk_use) if disk_use else {}
    avail = _num(fc_p.get("predicted_avail_bytes"))
    usage = _num(use_p.get("usage_pct"))
    disk = {
        "key": "disk",
        "state": disk_state,
        "event_id": primary.id if primary else None,
        "ts_str": _when(primary.ts, tz) if primary else None,
        "values": {
            "predicted_avail_gb": round(avail / 1e9, 1) if avail is not None else None,
            "forecast_days": settings.sentinel_disk_forecast_days,
            "usage_pct": round(usage, 1) if usage is not None else None,
            "usage_threshold_pct": round(settings.sentinel_disk_usage_pct),
        },
    }

    ce = open_by_rule.get("cert_expiry")
    if ce is not None:
        p = _hygiene_payload(ce)
        days = _num(p.get("days_left"))
        cert = {
            "key": "cert",
            "state": "alert",
            "event_id": ce.id,
            "ts_str": _when(ce.ts, tz),
            "values": {
                "days_left": round(days) if days is not None else None,
                "min_days": settings.sentinel_cert_min_days,
            },
        }
    else:
        cert = {
            "key": "cert",
            "state": "ok" if settings.cert_domains_list else "unevaluated",
            "event_id": None,
            "ts_str": None,
            "values": {},
        }
    return [backup, disk, cert]


def _safe_http_url(u: str | None) -> str:
    """外部来源 URL 的 scheme 白名单:仅放行 http(s),其余(javascript:/data: 等)抹成空。
    jinja autoescape 只转义引号/尖括号,不拦 URL scheme,故须在 view 层数据边界过滤,
    防上游被投毒的 incident.shortlink/status_url 落进 href 成可点击 XSS(零-JS 铁律)。"""
    if isinstance(u, str) and u.lower().startswith(("http://", "https://")):
        return u
    return ""


def _upstream_deps(store: Store, settings: Settings) -> list[dict]:
    """上游依赖健康(providers status snapshot,§8.4)。Indicator→state 映射;
    components + 未解决 incidents 供 <details> 展开;链到 status_url。Snapshot=None → nodata。
    cloudflare track_components=False → components 可能空,模板须容空。"""
    out: list[dict] = []
    for p in settings.providers_list:
        snap = store.get(p)
        if snap is None:
            out.append(
                {
                    "provider": p,
                    "name": p,
                    "state": "nodata",
                    "components": [],
                    "incidents": [],
                    "status_url": "",
                    "has_data": False,
                }
            )
            continue
        comps = [
            {"name": c.name, "state": _COMP_STATE.get(c.status.value, "nodata")}
            for c in snap.components
        ]
        incs = [
            {
                "title": i.title,
                "status": i.status.value,
                "url": _safe_http_url(i.url),
                "impact": _INDICATOR_STATE.get(i.impact.value, "nodata"),
            }
            for i in snap.incidents
            if i.status.value != "resolved"  # 只列未结
        ]
        out.append(
            {
                "provider": p,
                "name": snap.display_name,
                "state": _INDICATOR_STATE.get(snap.indicator.value, "nodata"),
                "components": comps,
                "incidents": incs,
                "status_url": _safe_http_url(snap.status_url),
                "has_data": True,
            }
        )
    return out


async def build_hygiene(
    store: Store,
    settings: Settings,
    *,
    now: datetime,
    docker=None,
    llm_config=None,
    diag_registered: bool | None = None,
) -> dict:
    """体检页 view-model(§8.4):本地三态卡 + 上游依赖 + 哨兵自身姿态 + 顶部体检 banner。
    banner pulse = 三块(本地/上游/自身)最坏档;零新表/新取数(事件驱动 + 现成 snapshot/meta)。"""
    tz = (
        now.tzinfo
        if isinstance(now.tzinfo, timezone)
        else timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))
    )
    now_ts = int(now.timestamp())
    llm = _llm_posture(llm_config, settings, diag_registered=diag_registered)
    local = _local_hygiene(store, settings, tz=tz)
    upstream = _upstream_deps(store, settings)
    latest_probe_ts = store.get_latest_probe_ts()
    host_self = _host_self(
        settings, latest_probe_ts=latest_probe_ts, now_ts=now_ts, llm_config=llm_config, llm=llm
    )
    docker_mode = _docker_mode(settings)
    open_events = store.get_open_events()
    layers = {
        "prometheus": bool(settings.sentinel_prometheus_url),
        "loki": bool(settings.sentinel_loki_url),
        "docker": docker_mode != "off",
        "llm": llm["enabled"],
        "editor": settings.editor_enabled,
    }
    editor = {
        "enabled": settings.editor_enabled,
        "mode": settings.sentinel_editor_mode,
        "model": settings.sentinel_editor_model or None,
    }
    posture = {
        "monitored_containers": await _monitored_containers(docker, settings),
        "open_count": len(open_events),
        "resolved_24h": store.count_resolved_since(now_ts - _DAY_SECONDS),
        "llm": llm,
        "editor": editor,
        "docker": {"mode": docker_mode, "read_only": True},
        "layers": layers,
        "channels": _channels(settings),
    }
    # banner:三块最坏档(alert→down / ok→ok / unevaluated→nodata 映射本地卡)
    local_states = [
        "down" if c["state"] == "alert" else ("ok" if c["state"] == "ok" else "nodata")
        for c in local
    ]
    local_worst = _worst_state(local_states)
    upstream_worst = _worst_state([u["state"] for u in upstream])
    engine = host_self["probe_engine_live"]
    self_state = "ok" if engine else ("degraded" if engine is False else "nodata")
    bad_up = [u for u in upstream if u["state"] in ("down", "partial", "degraded")]
    worst_up = min(bad_up, key=lambda u: _STATE_RANK.get(u["state"], 4))["name"] if bad_up else None
    banner = {
        "state": _worst_state([local_worst, upstream_worst, self_state]),
        "alert_count": sum(1 for c in local if c["state"] == "alert"),
        "evaluated_count": sum(1 for c in local if c["state"] != "unevaluated"),
        "local_total": len(local),
        "worst_upstream": worst_up,
        "upstream_any_data": any(u["has_data"] for u in upstream),  # 区分"全绿"vs"暂无数据"
        "layers_online": sum(1 for v in layers.values() if v),  # 兼容旧消费方
        "layers_enabled": sum(1 for v in layers.values() if v),
        "layers_disabled": sum(1 for v in layers.values() if not v),
        "layers_total": len(layers),
        "last_report_date": store.get_meta("daily_report_last_date"),
    }
    return {
        "now_str": now.strftime("%Y-%m-%d %H:%M"),
        "refresh_seconds": settings.sentinel_panel_refresh_seconds,
        "banner": banner,
        "local": local,
        "upstream": upstream,
        "posture": posture,
        "host_self": host_self,
    }
