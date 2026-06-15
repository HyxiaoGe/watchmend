# src/sentinel/panel/routes.py
"""证据台 SSR 路由(只读,localhost-only,沿用 127.0.0.1:8765 绑定,无新鉴权)。
GET / 总览、GET /event/{id} 详情。受 SENTINEL_PANEL_ENABLED 门控:关则不注册。
偏好(lang/theme/window)经 query>cookie>Accept-Language 解析,出现在 querystring 时写
cookie 以跨 30s meta 重载存活;page/svc_all 为瞬时态只走 querystring。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from sentinel.config import Settings
from sentinel.panel import i18n, prefs, view

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),  # 所有动态值 HTML 转义,原始日志作纯文本
)

_TRUTHY = ("1", "true", "yes", "on")


def _tz(settings: Settings) -> timezone:
    return timezone(timedelta(hours=settings.sentinel_heartbeat_utc_offset))


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
        page = prefs.resolve_page(q.get("ev_page"))
        svc_all = (q.get("svc_all") or "").strip().lower() in _TRUTHY

        overview = await view.build_overview(
            state.store,
            settings,
            now=datetime.now(_tz(settings)),
            docker=getattr(state, "docker", None),
            llm_config=getattr(state, "llm_config", None),
            diag_registered=getattr(state, "diag_job_registered", None),
            window_days=window_days,
            page=page,
            service_labels=getattr(state, "service_labels", None),
        )

        t = i18n.make_translator(lang)

        def qurl(**override) -> str:
            params: dict[str, object] = {
                "lang": lang,
                "theme": theme,
                "win": window_days,
                "ev_page": overview["events"]["page"],  # 用钳后的真实页码
            }
            if svc_all:
                params["svc_all"] = 1
            params.update(override)
            clean = {k: v for k, v in params.items() if v is not None}
            return "?" + urlencode(clean)

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
            eurl=eurl,
            svc_all=svc_all,
            history_days=settings.sentinel_panel_history_days,
            services_cap=settings.sentinel_panel_services_cap,
            diag_lang=settings.sentinel_llm_lang,
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
        # 返回"最新"链接携带 lang/theme/win,跳回总览不丢上下文(issue #11 claim 4)
        back_url = "/?" + urlencode({"lang": lang, "theme": theme, "win": window_days})
        html = _env.get_template("event.html").render(
            detail=detail,
            t=i18n.make_translator(lang),
            lang=lang,
            theme=theme,
            rule_label=i18n.rule_label,
            back_url=back_url,
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
        svg = _env.get_template("badge.svg.j2").render(
            status_text=status_text,
            color=color,
            left_w=left_w,
            right_w=right_w,
            total_w=total_w,
            left_x=round(left_w / 2, 1),
            right_x=round(left_w + right_w / 2, 1),
        )
        return Response(
            svg, media_type="image/svg+xml", headers={"Cache-Control": "max-age=60"}
        )
