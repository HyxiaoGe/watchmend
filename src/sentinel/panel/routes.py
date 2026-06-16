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

from sentinel.config import Settings
from sentinel.panel import i18n, prefs, view

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    # 动态值 HTML 转义,原始日志作纯文本;徽标 .svg 模板亦纳入(纵深防御:未来若把
    # 服务名/事件 subject 接进徽标也不会破坏 SVG 结构)
    autoescape=select_autoescape(["html", "svg"]),
)


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

        t = i18n.make_translator(lang)

        qurl, tab_url = _nav_helpers(
            "/",
            lang=lang,
            theme=theme,
            window_days=window_days,
        )

        def eurl(event_id: int) -> str:
            # 事件详情链接携带当前 lang/theme/win,跳转后不丢上下文(issue #11 claim 4)
            return f"/event/{event_id}?" + urlencode(
                {"lang": lang, "theme": theme, "win": window_days}
            )

        html = _env.get_template("panel.html").render(
            **overview,
            t=t,
            lang=lang,
            theme=theme,
            rule_label=i18n.rule_label,
            qurl=qurl,
            tab_url=tab_url,
            eurl=eurl,
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
            llm_config=getattr(state, "llm_config", None),
            diag_registered=getattr(state, "diag_job_registered", None),
        )
        status = 200 if detail is not None else 404
        qurl, tab_url = _nav_helpers(
            f"/event/{event_id}", lang=lang, theme=theme, window_days=window_days
        )
        # 面包屑回到父标签「事件」,携带 lang/theme/win 不丢上下文(issue #11 claim 4)
        back_url = "/events?" + urlencode({"lang": lang, "theme": theme, "win": window_days})
        html = _env.get_template("event.html").render(
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
            active_tab="events",
            diag_lang=settings.sentinel_llm_lang,
        )  # 详情页不传 refresh_seconds → 不自动刷新(spec §3)
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
        qurl, tab_url = _nav_helpers("/services", lang=lang, theme=theme, window_days=window_days)

        def surl(name: str) -> str:
            return (
                "/service/"
                + quote(name, safe="")
                + "?"
                + urlencode({"lang": lang, "theme": theme, "win": window_days})
            )

        html = _env.get_template("services.html").render(
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

        html = _env.get_template("service.html").render(
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
            services_url="/services?" + prefs_qs,
            active_tab="services",
            diag_lang=settings.sentinel_llm_lang,
        )  # 详情页不传 refresh_seconds → 不自动刷新(spec §3)
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
            service_labels=getattr(state, "service_labels", None),
            llm_config=getattr(state, "llm_config", None),
            diag_registered=getattr(state, "diag_job_registered", None),
        )
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

        html = _env.get_template("events.html").render(
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
        qurl, tab_url = _nav_helpers("/hygiene", lang=lang, theme=theme, window_days=window_days)

        def eurl(event_id: int) -> str:
            return f"/event/{event_id}?" + urlencode(
                {"lang": lang, "theme": theme, "win": window_days}
            )

        html = _env.get_template("hygiene.html").render(
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
