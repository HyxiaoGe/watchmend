# src/sentinel/panel/routes.py
"""证据台 SSR 路由(只读,localhost-only,沿用 127.0.0.1:8765 绑定,无新鉴权)。
GET / 总览、/services、/service/{name}、/events、/event/{id}、/hygiene、/badge.svg。
受 SENTINEL_PANEL_ENABLED 门控:关则不注册。
偏好(lang/theme/window)经 query>cookie>Accept-Language 解析,出现在 querystring 时写
cookie 以跨 30s meta 重载存活;ev_page/filter 为瞬时态只走 querystring。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from sentinel import __version__
from sentinel.config import Settings
from sentinel.panel import changelog, i18n, prefs, settings_view, view
from sentinel.update_check import is_newer

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    # 动态值 HTML 转义,原始日志作纯文本;徽标 .svg 模板亦纳入(纵深防御:未来若把
    # 服务名/事件 subject 接进徽标也不会破坏 SVG 结构)
    autoescape=select_autoescape(["html", "svg"]),
)
# SLO 看板 MTTR/时长 KPI 渲染:秒 → 紧凑人类可读(纯展示格式化,无新读)。
_env.globals["fmt_duration"] = view._fmt_duration


def _tz(settings: Settings) -> timezone:
    return timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))


def _nav_helpers(path: str, *, lang: str, theme: str, window_days: int, **transient):
    """跨页导航壳 URL 助手(spec §7:qurl 提为跨路由通用 helper)。
    qurl(**override):重建当前页 path,带 lang/theme/win + 该页瞬时态(transient)+ 覆盖项;
      None 值一律剔除(ev_page/filter 关闭即不出现在 querystring)。
    tab_url(p):构造他页 URL,只带 lang/theme/win —— 四标签主导航跨页跳转保留偏好。"""
    base: dict[str, object] = {"lang": lang, "theme": theme, "win": window_days, **transient}

    def qurl(**override) -> str:
        params = {**base, **override}
        clean = {k: v for k, v in params.items() if v is not None}
        return path + "?" + urlencode(clean) if clean else path

    def tab_url(p: str) -> str:
        return p + "?" + urlencode({"lang": lang, "theme": theme, "win": window_days})

    return qurl, tab_url


def _nav_context(store, lang: str) -> dict:
    """全局版本/更新态(每页注入 _base 头部)。只读 meta + 本地 changelog,绝不外呼;
    比对运行版本判断是否有新版;release_url 经 _safe_http_url 净化防 scheme 注入;
    releases = 全量版本块(供版本 chip 点击弹出的 :target 模态内联展示,当前版本高亮)。"""
    latest = store.get_meta("latest_known_version")
    available = bool(latest and is_newer(latest, __version__))
    return {
        "version": __version__,
        "update_available": available,
        # 未结事件数 = tab 标题的"状态灯"(平时安静,出事亮红);与 /badge.svg 同源(open_count)
        "incident_count": len(store.get_open_events()),
        "latest_version": latest if available else None,
        # 镜像标签去 v 前缀(更新命令用);仅有新版时才给
        "latest_tag": (latest[1:] if latest.startswith("v") else latest)
        if (available and latest)
        else None,
        "release_url": view._safe_http_url(store.get_meta("latest_release_url")),
        "releases": changelog.load_releases(lang),
    }


def _read_prefs(request: Request, settings: Settings) -> tuple[str, str, int]:
    """跨页统一的偏好解析(query > cookie > 配置默认/Accept-Language)。"""
    q = request.query_params
    c = request.cookies
    accept = request.headers.get("accept-language")
    lang = prefs.resolve_lang(
        q.get("lang"), c.get("wm_lang"), accept, default=settings.sentinel_panel_default_lang
    )
    theme = prefs.resolve_theme(
        q.get("theme"), c.get("wm_theme"), settings.sentinel_panel_default_theme
    )
    window_days = prefs.resolve_window(
        q.get("win"),
        c.get("wm_win"),
        history_days=settings.sentinel_panel_history_days,
        default=settings.sentinel_panel_default_window,
    )
    return lang, theme, window_days


def _resolve_refresh(request: Request, settings: Settings) -> int:
    """自动刷新间隔(秒)解析:浏览器偏好(query > wm_refresh cookie)优先,否则服务端默认。
    允许集 {0,15,30,60} 外/'default' 哨兵一律回落 server_default(prefs.resolve_refresh 兜底)。
    四个 live 页在 render 前用此覆盖 view 注入的 refresh_seconds,让浏览器偏好当次生效。"""
    return prefs.resolve_refresh(
        request.query_params.get("refresh"),
        request.cookies.get("wm_refresh"),
        settings.sentinel_panel_refresh_seconds,
    )


def _write_pref_cookies(resp: Response, request: Request, lang: str, theme: str, win: int) -> None:
    """仅对出现在 querystring 的偏好写 cookie(cookie 跨页兜底,querystring 当次生效)。"""
    q = request.query_params
    prefs.apply_pref_cookies(
        resp,
        lang=lang if q.get("lang") else None,
        theme=theme if q.get("theme") else None,
        window=win if q.get("win") else None,
    )


def register_panel_routes(app: FastAPI) -> None:
    # flag 在注册期决定(此刻 app.state.settings 尚未就绪);env 优先于 .env
    if not Settings().sentinel_panel_enabled:
        return

    @app.get("/", response_class=HTMLResponse)
    async def panel_index(request: Request) -> HTMLResponse:
        state = request.app.state
        settings: Settings = state.settings
        q = request.query_params
        c = request.cookies
        accept = request.headers.get("accept-language")

        lang = prefs.resolve_lang(
            q.get("lang"), c.get("wm_lang"), accept, default=settings.sentinel_panel_default_lang
        )
        theme = prefs.resolve_theme(
            q.get("theme"), c.get("wm_theme"), settings.sentinel_panel_default_theme
        )
        window_days = prefs.resolve_window(
            q.get("win"),
            c.get("wm_win"),
            history_days=settings.sentinel_panel_history_days,
            default=settings.sentinel_panel_default_window,
        )
        overview = await view.build_overview(
            state.store,
            settings,
            now=datetime.now(_tz(settings)),
            llm_config=getattr(state, "llm_config", None),
            diag_registered=getattr(state, "diag_job_registered", None),
            window_days=window_days,
            service_labels=getattr(state, "service_labels", None),
        )
        # 浏览器刷新偏好(query/wm_refresh cookie)覆盖服务端默认,render 前生效(spec /settings)
        overview["refresh_seconds"] = _resolve_refresh(request, settings)
        overview["latest_probe_ts"] = state.store.get_latest_probe_ts()

        t = i18n.make_translator(lang)

        qurl, tab_url = _nav_helpers(
            "/",
            lang=lang,
            theme=theme,
            window_days=window_days,
        )

        prefs_qs = urlencode({"lang": lang, "theme": theme, "win": window_days})

        def eurl(event_id: int) -> str:
            # 事件详情链接携带当前 lang/theme/win,跳转后不丢上下文(issue #11 claim 4)
            return f"/event/{event_id}?" + prefs_qs

        def surl(name: str) -> str:
            # 服务表整行点进 /service/{name};名称 quote 防特殊字符破坏路径,携带偏好
            return "/service/" + quote(name, safe="") + "?" + prefs_qs

        html = _env.get_template("panel.html").render(
            nav=_nav_context(state.store, lang),
            **overview,
            t=t,
            lang=lang,
            theme=theme,
            rule_label=i18n.rule_label,
            qurl=qurl,
            tab_url=tab_url,
            eurl=eurl,
            surl=surl,
            history_days=settings.sentinel_panel_history_days,
            active_tab="overview",
        )
        resp = HTMLResponse(html)
        # 仅对出现在 querystring 的偏好写 cookie(cookie 跨页兜底,querystring 当次生效)
        prefs.apply_pref_cookies(
            resp,
            lang=lang if q.get("lang") else None,
            theme=theme if q.get("theme") else None,
            window=window_days if q.get("win") else None,
        )
        return resp

    @app.get("/event/{event_id}", response_class=HTMLResponse)
    async def panel_event(event_id: int, request: Request) -> HTMLResponse:
        state = request.app.state
        settings: Settings = state.settings
        q = request.query_params
        c = request.cookies
        accept = request.headers.get("accept-language")

        lang = prefs.resolve_lang(
            q.get("lang"), c.get("wm_lang"), accept, default=settings.sentinel_panel_default_lang
        )
        theme = prefs.resolve_theme(
            q.get("theme"), c.get("wm_theme"), settings.sentinel_panel_default_theme
        )
        window_days = prefs.resolve_window(
            q.get("win"),
            c.get("wm_win"),
            history_days=settings.sentinel_panel_history_days,
            default=settings.sentinel_panel_default_window,
        )
        detail = view.build_event_detail(
            state.store,
            settings,
            event_id,
            window_days=window_days,
            llm_config=getattr(state, "llm_config", None),
            diag_registered=getattr(state, "diag_job_registered", None),
        )
        status = 200 if detail is not None else 404
        qurl, tab_url = _nav_helpers(
            f"/event/{event_id}", lang=lang, theme=theme, window_days=window_days
        )
        # 面包屑回到父标签「事件」,携带 lang/theme/win 不丢上下文(issue #11 claim 4)
        back_url = "/events?" + urlencode({"lang": lang, "theme": theme, "win": window_days})

        def surl(name: str) -> str:
            # 事件 subject → 服务详情(仅 view 标记 service_linkable 的真实服务在模板里用)
            return (
                "/service/"
                + quote(name, safe="")
                + "?"
                + urlencode({"lang": lang, "theme": theme, "win": window_days})
            )

        html = _env.get_template("event.html").render(
            nav=_nav_context(state.store, lang),
            detail=detail,
            t=i18n.make_translator(lang),
            lang=lang,
            theme=theme,
            window_days=window_days,
            history_days=settings.sentinel_panel_history_days,
            rule_label=i18n.rule_label,
            qurl=qurl,
            tab_url=tab_url,
            back_url=back_url,
            surl=surl,
            active_tab="events",
            diag_lang=settings.sentinel_llm_lang,
            refresh_seconds=_resolve_refresh(request, settings),
            latest_probe_ts=state.store.get_latest_probe_ts(),
            now_str=datetime.now(_tz(settings)).strftime("%Y-%m-%d %H:%M"),
        )
        resp = HTMLResponse(html, status_code=status)
        prefs.apply_pref_cookies(
            resp,
            lang=lang if q.get("lang") else None,
            theme=theme if q.get("theme") else None,
            window=window_days if q.get("win") else None,
        )
        return resp

    @app.get("/services", response_class=HTMLResponse)
    async def panel_services(request: Request) -> HTMLResponse:
        state = request.app.state
        settings: Settings = state.settings
        lang, theme, window_days = _read_prefs(request, settings)
        data = view.build_services_list(
            state.store,
            settings,
            now=datetime.now(_tz(settings)),
            window_days=window_days,
            service_labels=getattr(state, "service_labels", None),
        )
        data["refresh_seconds"] = _resolve_refresh(request, settings)  # 浏览器偏好覆盖,render 前
        data["latest_probe_ts"] = state.store.get_latest_probe_ts()
        qurl, tab_url = _nav_helpers("/services", lang=lang, theme=theme, window_days=window_days)

        def surl(name: str) -> str:
            return (
                "/service/"
                + quote(name, safe="")
                + "?"
                + urlencode({"lang": lang, "theme": theme, "win": window_days})
            )

        html = _env.get_template("services.html").render(
            nav=_nav_context(state.store, lang),
            **data,
            t=i18n.make_translator(lang),
            lang=lang,
            theme=theme,
            rule_label=i18n.rule_label,
            qurl=qurl,
            tab_url=tab_url,
            surl=surl,
            history_days=settings.sentinel_panel_history_days,
            active_tab="services",
        )
        resp = HTMLResponse(html)
        _write_pref_cookies(resp, request, lang, theme, window_days)
        return resp

    @app.get("/service/{name}", response_class=HTMLResponse)
    async def panel_service_detail(name: str, request: Request) -> HTMLResponse:
        state = request.app.state
        settings: Settings = state.settings
        q = request.query_params
        lang, theme, window_days = _read_prefs(request, settings)
        gran = "samples" if (q.get("gran") or "").strip().lower() == "samples" else "daily"
        page = prefs.resolve_page(q.get("ev_page"))
        detail = view.build_service_detail(
            state.store,
            settings,
            name,
            now=datetime.now(_tz(settings)),
            window_days=window_days,
            gran=gran,
            page=page,
            service_labels=getattr(state, "service_labels", None),
            llm_config=getattr(state, "llm_config", None),
            diag_registered=getattr(state, "diag_job_registered", None),
        )
        status = 200 if detail is not None else 404
        qurl, tab_url = _nav_helpers(
            "/service/" + quote(name, safe=""),
            lang=lang,
            theme=theme,
            window_days=window_days,
            gran="samples" if gran == "samples" else None,
            ev_page=detail["events"]["page"] if detail else None,
        )

        prefs_qs = urlencode({"lang": lang, "theme": theme, "win": window_days})

        def eurl(event_id: int) -> str:
            return f"/event/{event_id}?" + prefs_qs

        def events_url(subject: str) -> str:
            # 相关事件 → 事件流页按本服务筛选(同源同窗),带偏好
            return "/events?" + urlencode(
                {"subject": subject, "lang": lang, "theme": theme, "win": window_days}
            )

        html = _env.get_template("service.html").render(
            nav=_nav_context(state.store, lang),
            detail=detail,
            t=i18n.make_translator(lang),
            lang=lang,
            theme=theme,
            window_days=window_days,
            history_days=settings.sentinel_panel_history_days,
            rule_label=i18n.rule_label,
            qurl=qurl,
            tab_url=tab_url,
            eurl=eurl,
            events_url=events_url,
            services_url="/services?" + prefs_qs,
            active_tab="services",
            diag_lang=settings.sentinel_llm_lang,
            refresh_seconds=_resolve_refresh(request, settings),
            latest_probe_ts=state.store.get_latest_probe_ts(),
            now_str=datetime.now(_tz(settings)).strftime("%Y-%m-%d %H:%M"),
        )
        resp = HTMLResponse(html, status_code=status)
        _write_pref_cookies(resp, request, lang, theme, window_days)
        return resp

    @app.get("/events", response_class=HTMLResponse)
    async def panel_events(request: Request) -> HTMLResponse:
        state = request.app.state
        settings: Settings = state.settings
        q = request.query_params
        lang, theme, window_days = _read_prefs(request, settings)
        page = prefs.resolve_page(q.get("ev_page"))
        # 非法筛选值归一为 None(severity/status 仅接受白名单;subject 任意串透传)
        subject = (q.get("subject") or "").strip() or None
        sev_q = (q.get("severity") or "").strip().lower()
        severity = sev_q if sev_q in view._EVENT_SEVERITIES else None
        st_q = (q.get("status") or "").strip().lower()
        status = st_q if st_q in view._EVENT_STATUSES else None
        data = view.build_events_list(
            state.store,
            settings,
            now=datetime.now(_tz(settings)),
            page=page,
            subject=subject,
            severity=severity,
            status=status,
            window_days=window_days,
            service_labels=getattr(state, "service_labels", None),
            llm_config=getattr(state, "llm_config", None),
            diag_registered=getattr(state, "diag_job_registered", None),
        )
        data["refresh_seconds"] = _resolve_refresh(request, settings)  # 浏览器偏好覆盖,render 前
        data["latest_probe_ts"] = state.store.get_latest_probe_ts()
        qurl, tab_url = _nav_helpers(
            "/events",
            lang=lang,
            theme=theme,
            window_days=window_days,
            subject=subject,
            severity=severity,
            status=status,
            ev_page=data["events"]["page"],
        )

        def eurl(event_id: int) -> str:
            return f"/event/{event_id}?" + urlencode(
                {"lang": lang, "theme": theme, "win": window_days}
            )

        def surl(name: str) -> str:
            # 事件里的服务名 → 服务详情(仅 view 标记 service_linkable 的真实服务在模板里用)
            return (
                "/service/"
                + quote(name, safe="")
                + "?"
                + urlencode({"lang": lang, "theme": theme, "win": window_days})
            )

        html = _env.get_template("events.html").render(
            nav=_nav_context(state.store, lang),
            **data,
            t=i18n.make_translator(lang),
            lang=lang,
            theme=theme,
            window_days=window_days,
            history_days=settings.sentinel_panel_history_days,
            rule_label=i18n.rule_label,
            diag_lang=settings.sentinel_llm_lang,
            qurl=qurl,
            tab_url=tab_url,
            eurl=eurl,
            surl=surl,
            active_tab="events",
        )
        resp = HTMLResponse(html)
        _write_pref_cookies(resp, request, lang, theme, window_days)
        return resp

    @app.get("/hygiene", response_class=HTMLResponse)
    async def panel_hygiene(request: Request) -> HTMLResponse:
        state = request.app.state
        settings: Settings = state.settings
        lang, theme, window_days = _read_prefs(request, settings)
        data = await view.build_hygiene(
            state.store,
            settings,
            now=datetime.now(_tz(settings)),
            docker=getattr(state, "docker", None),
            llm_config=getattr(state, "llm_config", None),
            diag_registered=getattr(state, "diag_job_registered", None),
        )
        data["refresh_seconds"] = _resolve_refresh(request, settings)  # 浏览器偏好覆盖,render 前
        data["latest_probe_ts"] = state.store.get_latest_probe_ts()
        qurl, tab_url = _nav_helpers("/hygiene", lang=lang, theme=theme, window_days=window_days)

        def eurl(event_id: int) -> str:
            return f"/event/{event_id}?" + urlencode(
                {"lang": lang, "theme": theme, "win": window_days}
            )

        html = _env.get_template("hygiene.html").render(
            nav=_nav_context(state.store, lang),
            **data,
            t=i18n.make_translator(lang),
            lang=lang,
            theme=theme,
            window_days=window_days,
            history_days=settings.sentinel_panel_history_days,
            rule_label=i18n.rule_label,
            qurl=qurl,
            tab_url=tab_url,
            eurl=eurl,
            active_tab="hygiene",
        )
        resp = HTMLResponse(html)
        _write_pref_cookies(resp, request, lang, theme, window_days)
        return resp

    @app.get("/changelog", response_class=HTMLResponse)
    async def panel_changelog(request: Request) -> HTMLResponse:
        state = request.app.state
        settings: Settings = state.settings
        lang, theme, window_days = _read_prefs(request, settings)
        qurl, tab_url = _nav_helpers("/changelog", lang=lang, theme=theme, window_days=window_days)
        t = i18n.make_translator(lang)
        html = _env.get_template("changelog.html").render(
            nav=_nav_context(state.store, lang),
            lang=lang,
            theme=theme,
            t=t,
            qurl=qurl,
            tab_url=tab_url,
            active_tab="changelog",
            window_days=window_days,
            history_days=settings.sentinel_panel_history_days,
            releases=changelog.load_releases(lang),
            current_version=__version__,
        )
        resp = HTMLResponse(html)
        _write_pref_cookies(resp, request, lang, theme, window_days)
        return resp

    def _apply_settings_cookies(resp, request, lang, theme, win) -> None:
        """/settings 表单提交:按原始 querystring 值决定每项 SET 或 CLEAR(中性选项删 cookie)。
        无对应 querystring 项 → 不动该 cookie(普通页面行为)。"""
        q = request.query_params
        clear: list[str] = []
        set_lang = set_theme = set_refresh = set_win = None
        raw_lang = q.get("lang")
        if raw_lang is not None:
            if raw_lang.strip().lower() in ("zh", "en"):
                set_lang = raw_lang.strip().lower()
            else:  # "auto" / 非法 → 删 wm_lang 回落 Accept-Language
                clear.append("wm_lang")
        if q.get("theme") is not None:
            set_theme = theme  # theme 已被 _read_prefs 解析为具体值(system/dark/light)
        if q.get("win") is not None:
            set_win = win
        raw_refresh = q.get("refresh")
        if raw_refresh is not None:
            if raw_refresh.strip() in ("0", "15", "30", "60"):
                set_refresh = int(raw_refresh.strip())
            else:  # "default" / 非法 → 删 wm_refresh 回落服务端默认
                clear.append("wm_refresh")
        prefs.apply_pref_cookies(
            resp,
            lang=set_lang,
            theme=set_theme,
            window=set_win,
            refresh=set_refresh,
            clear=tuple(clear),
        )

    @app.get("/settings", response_class=HTMLResponse)
    async def panel_settings(request: Request) -> HTMLResponse:
        state = request.app.state
        settings: Settings = state.settings
        c = request.cookies
        q = request.query_params
        lang, theme, window_days = _read_prefs(request, settings)
        refresh_eff = prefs.resolve_refresh(
            q.get("refresh"), c.get("wm_refresh"), settings.sentinel_panel_refresh_seconds
        )
        display = settings_view.build_display_prefs(
            lang_eff=lang,
            lang_cookie=c.get("wm_lang"),
            theme_eff=theme,
            window_eff=window_days,
            history_days=settings.sentinel_panel_history_days,
            refresh_eff=refresh_eff,
            refresh_cookie=c.get("wm_refresh"),
            server_refresh=settings.sentinel_panel_refresh_seconds,
        )
        inventory = settings_view.build_config_inventory(
            settings, llm_config=getattr(state, "llm_config", None), lang=lang
        )
        qurl, tab_url = _nav_helpers("/settings", lang=lang, theme=theme, window_days=window_days)
        html = _env.get_template("settings.html").render(
            nav=_nav_context(state.store, lang),
            t=i18n.make_translator(lang),
            lang=lang,
            theme=theme,
            window_days=window_days,
            history_days=settings.sentinel_panel_history_days,
            qurl=qurl,
            tab_url=tab_url,
            active_tab="settings",
            display=display,
            inventory=inventory,
        )  # 不传 refresh_seconds → 设置页本身不自动刷新(spec §3)
        resp = HTMLResponse(html)
        _apply_settings_cookies(resp, request, lang, theme, window_days)
        return resp

    @app.get("/badge.svg")
    async def panel_badge(request: Request) -> Response:
        # 可嵌徽标(README/外部页 <img src> 引用)。只暴露 open_count——面板本就可见的聚合,
        # 无敏感面;受 SENTINEL_PANEL_ENABLED 同门控(register 关则整体不注册)。
        open_count = len(request.app.state.store.get_open_events())
        if open_count == 0:
            status_text, color = "operational", "#3fb950"
        else:
            status_text = f"{open_count} incident" + ("s" if open_count != 1 else "")
            color = "#f85149"
        left_w = 72
        right_w = int(len(status_text) * 6.5 + 20)
        total_w = left_w + right_w
        svg = _env.get_template("badge.svg").render(
            status_text=status_text,
            color=color,
            left_w=left_w,
            right_w=right_w,
            total_w=total_w,
            left_x=round(left_w / 2, 1),
            right_x=round(left_w + right_w / 2, 1),
        )
        return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "max-age=60"})
