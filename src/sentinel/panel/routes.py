# src/sentinel/panel/routes.py
"""证据台 SSR 路由(只读,localhost-only,沿用 127.0.0.1:8765 绑定,无新鉴权)。
GET / 总览、GET /event/{id} 详情。受 SENTINEL_PANEL_ENABLED 门控:关则不注册。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from sentinel.config import Settings
from sentinel.panel import view

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(_TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),  # 所有动态值 HTML 转义,原始日志作纯文本
)


def register_panel_routes(app: FastAPI) -> None:
    # flag 在注册期决定(此刻 app.state.settings 尚未就绪);env 优先于 .env
    if not Settings().sentinel_panel_enabled:
        return

    @app.get("/", response_class=HTMLResponse)
    async def panel_index(request: Request) -> HTMLResponse:
        state = request.app.state
        tz = timezone(timedelta(hours=state.settings.sentinel_heartbeat_utc_offset))
        overview = await view.build_overview(
            state.store,
            state.settings,
            now=datetime.now(tz),
            docker=getattr(state, "docker", None),
            llm_config=getattr(state, "llm_config", None),
        )
        return HTMLResponse(_env.get_template("panel.html").render(**overview))

    @app.get("/event/{event_id}", response_class=HTMLResponse)
    async def panel_event(event_id: int, request: Request) -> HTMLResponse:
        state = request.app.state
        detail = view.build_event_detail(
            state.store, state.settings, event_id, llm_config=getattr(state, "llm_config", None)
        )
        status = 200 if detail is not None else 404
        return HTMLResponse(
            _env.get_template("event.html").render(detail=detail), status_code=status
        )
