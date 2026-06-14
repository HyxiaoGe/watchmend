# src/sentinel/api.py
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from sentinel.notify.build import diagnosis_notification, summary_notification
from sentinel.report import build_daily_stats

logger = logging.getLogger("sentinel")

# token 头一律用 Annotated[...] = None 风格:写 Header(default=None) 默认参数会触发
# ruff B008;此定义必须放在全部 import 之后,夹在 import 中间会触发 E402。
_TokenHeader = Annotated[str | None, Header(alias="X-Sentinel-Token")]


class DiagnosisIn(BaseModel):
    status: Literal["done", "failed", "skipped"]
    diagnosis: dict = Field(default_factory=dict)
    tools: list[dict] | None = None  # 子项目③:可选证据链回填(host 编排路径);缺省=不动该列


class SummaryIn(BaseModel):
    text: str


def _now_local(request: Request) -> datetime:
    offset = request.app.state.settings.sentinel_heartbeat_utc_offset
    return datetime.now(timezone(timedelta(hours=offset)))


def _check_token(request: Request, token: str | None) -> None:
    expected = request.app.state.settings.sentinel_diag_token
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="bad or missing X-Sentinel-Token")


def register_routes(app: FastAPI) -> None:
    """Phase 3 编排 API。依赖经 app.state 注入(store/settings/patrol_broadcaster/services),
    仅宿主机编排脚本调用;写端点按 settings.sentinel_diag_token 鉴权。"""

    # 两个 GET 必须 async def:同步 def 会被 FastAPI 丢进线程池,而 Store 的 sqlite
    # 连接建在主线程 → sqlite3.ProgrammingError(评审实测复现)。事件循环内同步调
    # store 与库内既有口径一致。
    @app.get("/events/pending")
    async def events_pending(request: Request) -> dict:
        events = request.app.state.store.get_pending_diagnosis_events()
        return {"events": [asdict(e) for e in events]}

    @app.post("/events/{event_id}/diagnosis")
    async def post_diagnosis(
        event_id: int,
        body: DiagnosisIn,
        request: Request,
        x_sentinel_token: _TokenHeader = None,
    ) -> dict:
        _check_token(request, x_sentinel_token)
        store = request.app.state.store
        event = store.get_event(event_id)
        if event is None:
            raise HTTPException(status_code=404, detail="event not found")
        diagnosis_json = json.dumps(body.diagnosis, ensure_ascii=False) if body.diagnosis else None
        tools_json = json.dumps(body.tools, ensure_ascii=False) if body.tools else None
        store.set_diagnosis(
            event_id, status=body.status, diagnosis_json=diagnosis_json, tools_json=tools_json
        )
        card_sent = False
        if body.status == "done" and body.diagnosis:
            now_local = _now_local(request)
            n = diagnosis_notification(
                event,
                body.diagnosis,
                now_ts=int(now_local.timestamp()),
                now_str=now_local.strftime("%Y-%m-%d %H:%M:%S"),
            )
            card_sent = await request.app.state.patrol_broadcaster.send(n) >= 1
        return {"ok": True, "card_sent": card_sent}

    @app.get("/report/daily-data")
    async def daily_data(request: Request) -> dict:
        now_local = _now_local(request)
        now_ts = int(now_local.timestamp())
        date_str = now_local.date().isoformat()
        store = request.app.state.store
        stats = build_daily_stats(
            store, request.app.state.services, now_ts=now_ts, date_str=date_str
        )
        return {
            "date": date_str,
            "services": [asdict(s) for s in stats],
            "open_events": [asdict(e) for e in store.get_open_events()],
            "resolved_24h": store.count_resolved_since(now_ts - 24 * 3600),
        }

    @app.post("/report/summary")
    async def post_summary(
        body: SummaryIn,
        request: Request,
        x_sentinel_token: _TokenHeader = None,
    ) -> dict:
        _check_token(request, x_sentinel_token)
        now_local = _now_local(request)
        n = summary_notification(
            body.text,
            date_str=now_local.date().isoformat(),
            now_ts=int(now_local.timestamp()),
            now_str=now_local.strftime("%Y-%m-%d %H:%M:%S"),
        )
        await request.app.state.patrol_broadcaster.send(n)
        return {"ok": True}
