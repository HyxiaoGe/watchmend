# src/sentinel/api.py
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from sentinel.codex_hooks import CodexHookEvent
from sentinel.llm_driver import cap_tool_outputs
from sentinel.notify.build import (
    codex_turn_notification,
    diagnosis_notification,
    summary_notification,
)
from sentinel.report import build_daily_stats

logger = logging.getLogger("sentinel")

# token 头一律用 Annotated[...] = None 风格:写 Header(default=None) 默认参数会触发
# ruff B008;此定义必须放在全部 import 之后,夹在 import 中间会触发 E402。
_TokenHeader = Annotated[str | None, Header(alias="X-Sentinel-Token")]
_CodexTokenHeader = Annotated[str | None, Header(alias="X-WatchMend-Token")]


class DiagnosisIn(BaseModel):
    status: Literal["done", "failed", "skipped"]
    diagnosis: dict = Field(default_factory=dict)
    # 子项目③:可选证据链回填(host 编排路径)。省略=不动该列;显式 [] 或 null=清空。
    tools: list[dict] | None = None


class SummaryIn(BaseModel):
    text: str


class CodexTurnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=300)
    thread_id: str = Field(min_length=1, max_length=128)
    turn_id: str = Field(min_length=1, max_length=128)
    project: str = Field(min_length=1, max_length=160)
    cwd: str = Field(min_length=1, max_length=1024)
    task_summary: str = Field(min_length=1, max_length=800)
    result_summary: str = Field(min_length=1, max_length=2000)


def _now_local(request: Request) -> datetime:
    offset = request.app.state.settings.sentinel_heartbeat_utc_offset
    return datetime.now(timezone(timedelta(hours=offset)))


def _check_token(request: Request, token: str | None) -> None:
    expected = request.app.state.settings.sentinel_diag_token
    if expected and token != expected:
        raise HTTPException(status_code=401, detail="bad or missing X-Sentinel-Token")


def _check_codex_token(request: Request, token: str | None) -> None:
    expected = request.app.state.settings.sentinel_codex_ingest_token
    if not expected:
        # 未配置即关闭入口，不退化成匿名写 API，也不向探测者暴露功能已安装。
        raise HTTPException(status_code=404, detail="not found")
    if token is None or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="bad or missing X-WatchMend-Token")


def register_routes(app: FastAPI) -> None:
    """Phase 3 编排 API。依赖经 app.state 注入(store/settings/patrol_broadcaster/services),
    仅宿主机编排脚本调用；编排与 Codex 写端点分别使用独立 token 鉴权。"""

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
        set_kwargs = {"status": body.status, "diagnosis_json": diagnosis_json}
        if "tools" in body.model_fields_set:  # 显式提供(含 [] / null)才动该列;省略=不动
            capped = cap_tool_outputs(body.tools) if body.tools else None
            set_kwargs["tools_json"] = json.dumps(capped, ensure_ascii=False) if capped else None
        store.set_diagnosis(event_id, **set_kwargs)
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

    @app.post("/notifications/codex")
    async def post_codex_notification(
        body: CodexTurnIn,
        request: Request,
        x_watchmend_token: _CodexTokenHeader = None,
    ) -> dict:
        _check_codex_token(request, x_watchmend_token)
        now_local = _now_local(request)
        now_ts = int(now_local.timestamp())
        store = request.app.state.store
        event_key = f"{body.thread_id}:{body.turn_id}"
        if body.event_id != event_key:
            raise HTTPException(status_code=422, detail="event_id must match thread_id:turn_id")
        receipt_hash = hashlib.sha256(event_key.encode("utf-8")).hexdigest()
        receipt = store.get_notification_receipt("codex", receipt_hash)
        if receipt is not None:
            return {"ok": True, "delivered_count": receipt[1], "duplicate": True}

        retention_days = request.app.state.settings.sentinel_codex_receipt_retention_days
        store.prune_notification_receipts(before_ts=now_ts - retention_days * 86400)
        n = codex_turn_notification(
            project=body.project,
            cwd=body.cwd,
            task_summary=body.task_summary,
            result_summary=body.result_summary,
            thread_id=body.thread_id,
            turn_id=body.turn_id,
            now_ts=now_ts,
            now_str=now_local.strftime("%Y-%m-%d %H:%M:%S"),
        )
        delivered_count = await request.app.state.patrol_broadcaster.send(n)
        if delivered_count < 1:
            raise HTTPException(status_code=503, detail="all notification channels failed")
        store.record_notification_receipt(
            "codex",
            receipt_hash,
            delivered_ts=now_ts,
            delivered_count=delivered_count,
        )
        return {"ok": True, "delivered_count": delivered_count, "duplicate": False}

    @app.post("/notifications/codex/hooks", status_code=202)
    async def post_codex_hook(
        body: CodexHookEvent,
        request: Request,
        x_watchmend_token: _CodexTokenHeader = None,
    ) -> dict:
        _check_codex_token(request, x_watchmend_token)
        result = request.app.state.codex_hook_manager.ingest(body)
        return {"ok": True, **result}
