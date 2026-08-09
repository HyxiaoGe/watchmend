"""Codex 生命周期 Hook 的延迟通知状态机。

Hook 请求只改变内存状态；正文不落库。候选通知在宽限期内可被后续用户输入、审批后的
工具执行或会话结束取消。WatchMend 重启会丢弃尚未发送的候选，符合旁路通知 fail-open
原则，也避免重启后补发已经失去上下文的旧消息。
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from sentinel.notify.build import codex_turn_notification

HookEventName = Literal[
    "UserPromptSubmit",
    "PermissionRequest",
    "PostToolUse",
    "Stop",
    "SessionEnd",
]
CodexCategory = Literal[
    "approval_required",
    "input_required",
    "execution_failed",
    "long_turn_complete",
]


class CodexHookEvent(BaseModel):
    """本机 Hook 客户端收窄、脱敏后的稳定协议。"""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=400)
    event_name: HookEventName
    session_id: str = Field(min_length=1, max_length=128)
    turn_id: str | None = Field(default=None, max_length=128)
    project: str = Field(min_length=1, max_length=160)
    cwd: str = Field(min_length=1, max_length=1024)
    task_summary: str | None = Field(default=None, max_length=800)
    result_summary: str | None = Field(default=None, max_length=2000)
    tool_name: str | None = Field(default=None, max_length=256)
    tool_use_id: str | None = Field(default=None, max_length=256)
    tool_fingerprint: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    stop_hook_active: bool = False


@dataclass(frozen=True)
class _TurnState:
    started_at: float
    project: str
    cwd: str
    task_summary: str


@dataclass
class _Pending:
    event_id: str
    session_id: str
    turn_id: str
    category: CodexCategory
    project: str
    cwd: str
    task_summary: str
    result_summary: str
    due_at: float
    attempts: int = 0


_INPUT_REQUIRED = re.compile(
    r"(?:请|需要你|麻烦你)(?:先)?(?:提供|确认|选择|输入|补充|回复|批准|授权|检查|处理)"
    r"|(?:please|need you to)\s+(?:provide|confirm|choose|select|enter|reply|approve|"
    r"authorize|check|fix)",
    re.IGNORECASE,
)
_USER_REJECTED = re.compile(
    r"(?:你|用户).{0,8}(?:拒绝|取消).{0,8}(?:审批|授权|执行)"
    r"|(?:approval|permission|tool call).{0,12}(?:denied|rejected|cancelled)",
    re.IGNORECASE,
)
_EXECUTION_FAILED = re.compile(
    r"(?:执行|部署|构建|测试|命令|任务).{0,12}(?:失败|出错|未通过)"
    r"|(?:无法|未能)(?:继续|完成|连接|部署|执行)"
    r"|(?:execution|deploy(?:ment)?|build|test|command|task).{0,16}(?:failed|error)"
    r"|(?:cannot|unable to)\s+(?:continue|complete|connect|deploy|run)",
    re.IGNORECASE,
)


def _stop_category(message: str, elapsed: float | None, threshold: int) -> CodexCategory | None:
    if _USER_REJECTED.search(message):
        return None
    if _INPUT_REQUIRED.search(message):
        return "input_required"
    if _EXECUTION_FAILED.search(message):
        return "execution_failed"
    if elapsed is not None and elapsed >= threshold:
        return "long_turn_complete"
    return None


class CodexHookNotificationManager:
    """维护 Codex 主线程待通知候选，并在到期后经现有广播渠道发送。"""

    def __init__(
        self,
        *,
        store,
        broadcaster,
        grace_seconds: int,
        long_turn_seconds: int,
        utc_offset: int,
        receipt_retention_days: int = 30,
        clock=time.time,
    ) -> None:
        self._store = store
        self._broadcaster = broadcaster
        self._grace_seconds = grace_seconds
        self._long_turn_seconds = long_turn_seconds
        self._receipt_retention_days = receipt_retention_days
        self._utc_offset = utc_offset
        self._clock = clock
        self._turns: dict[tuple[str, str], _TurnState] = {}
        self._pending: dict[str, _Pending] = {}
        self._inflight: dict[str, _Pending] = {}
        self._seen: dict[str, float] = {}

    @property
    def pending_count(self) -> int:
        return len(self._pending) + len(self._inflight)

    def _mark_seen(self, event_id: str, now: float) -> bool:
        if event_id in self._seen:
            return False
        self._seen[event_id] = now
        if len(self._seen) > 2_000:
            cutoff = now - 86_400
            self._seen = {key: ts for key, ts in self._seen.items() if ts >= cutoff}
        return True

    def _cancel(self, *, session_id: str, turn_id: str | None = None, category=None) -> int:
        pending_keys = [
            key
            for key, item in self._pending.items()
            if item.session_id == session_id
            and (turn_id is None or item.turn_id == turn_id)
            and (category is None or item.category == category)
        ]
        inflight_keys = [
            key
            for key, item in self._inflight.items()
            if item.session_id == session_id
            and (turn_id is None or item.turn_id == turn_id)
            and (category is None or item.category == category)
        ]
        for key in pending_keys:
            self._pending.pop(key, None)
        for key in inflight_keys:
            self._inflight.pop(key, None)
        return len(pending_keys) + len(inflight_keys)

    def _queue(
        self,
        event: CodexHookEvent,
        *,
        category: CodexCategory,
        now: float,
        task_summary: str,
        result_summary: str,
    ) -> dict:
        turn_id = event.turn_id or "session"
        self._pending[event.event_id] = _Pending(
            event_id=event.event_id,
            session_id=event.session_id,
            turn_id=turn_id,
            category=category,
            project=event.project,
            cwd=event.cwd,
            task_summary=task_summary,
            result_summary=result_summary,
            due_at=now + self._grace_seconds,
        )
        return {"action": "queued", "category": category}

    def ingest(self, event: CodexHookEvent) -> dict:
        """接收一个 Hook 事件；同步完成入队/取消，不在 Hook 请求中发送外部消息。"""
        now = self._clock()
        if not self._mark_seen(event.event_id, now):
            return {"action": "duplicate"}

        if event.event_name == "UserPromptSubmit":
            cancelled = self._cancel(session_id=event.session_id)
            if event.turn_id:
                self._turns[(event.session_id, event.turn_id)] = _TurnState(
                    started_at=now,
                    project=event.project,
                    cwd=event.cwd,
                    task_summary=event.task_summary or "Codex 任务",
                )
            return {"action": "cancelled", "count": cancelled}

        if event.event_name == "PostToolUse":
            cancelled = self._cancel(
                session_id=event.session_id,
                turn_id=event.turn_id,
                category="approval_required",
            )
            return {"action": "cancelled", "count": cancelled}

        if event.event_name == "SessionEnd":
            cancelled = self._cancel(session_id=event.session_id)
            self._turns = {
                key: state for key, state in self._turns.items() if key[0] != event.session_id
            }
            return {"action": "cancelled", "count": cancelled}

        if event.event_name == "PermissionRequest":
            return self._queue(
                event,
                category="approval_required",
                now=now,
                task_summary=self._task_summary(event),
                result_summary=event.result_summary or "Codex 正在等待审批。",
            )

        if event.event_name == "Stop":
            if event.stop_hook_active:
                return {"action": "ignored", "reason": "continued_stop"}
            if event.turn_id:
                self._cancel(
                    session_id=event.session_id,
                    turn_id=event.turn_id,
                    category="approval_required",
                )
            state = self._turns.pop((event.session_id, event.turn_id or ""), None)
            elapsed = now - state.started_at if state is not None else None
            result = event.result_summary or "Codex 回合已结束。"
            category = _stop_category(result, elapsed, self._long_turn_seconds)
            if category is None:
                reason = "short_success" if elapsed is not None else "not_actionable"
                return {"action": "ignored", "reason": reason}
            return self._queue(
                event,
                category=category,
                now=now,
                task_summary=state.task_summary if state else self._task_summary(event),
                result_summary=result,
            )

        return {"action": "ignored", "reason": "unsupported"}

    def _task_summary(self, event: CodexHookEvent) -> str:
        if event.task_summary:
            return event.task_summary
        if event.turn_id:
            state = self._turns.get((event.session_id, event.turn_id))
            if state is not None:
                return state.task_summary
        return "Codex 任务"

    async def dispatch_due(self) -> int:
        """发送已过宽限期的候选；成功回执落库，正文始终不落库。"""
        now = self._clock()
        due = sorted(
            (item for item in self._pending.values() if item.due_at <= now),
            key=lambda item: (item.due_at, item.event_id),
        )
        if not due:
            return 0
        self._store.prune_notification_receipts(
            before_ts=int(now) - self._receipt_retention_days * 86_400
        )
        sent = 0
        for item in due:
            current = self._pending.pop(item.event_id, None)
            if current is None:
                continue
            self._inflight[item.event_id] = item
            receipt_key = f"{item.event_id}:{item.category}"
            receipt_hash = hashlib.sha256(receipt_key.encode("utf-8")).hexdigest()
            if self._store.get_notification_receipt("codex-hook", receipt_hash) is not None:
                self._inflight.pop(item.event_id, None)
                continue
            now_local = datetime.fromtimestamp(now, tz=timezone(timedelta(hours=self._utc_offset)))
            notification = codex_turn_notification(
                project=item.project,
                cwd=item.cwd,
                task_summary=item.task_summary,
                result_summary=item.result_summary,
                thread_id=item.session_id,
                turn_id=item.turn_id,
                now_ts=int(now),
                now_str=now_local.strftime("%Y-%m-%d %H:%M:%S"),
                category=item.category,
            )
            delivered = await self._broadcaster.send(notification)
            was_cancelled = self._inflight.pop(item.event_id, None) is None
            if delivered >= 1:
                self._store.record_notification_receipt(
                    "codex-hook",
                    receipt_hash,
                    delivered_ts=int(now),
                    delivered_count=delivered,
                )
                sent += 1
            elif not was_cancelled and item.attempts < 2:
                item.attempts += 1
                item.due_at = now + min(60, 5 * (2**item.attempts))
                self._pending[item.event_id] = item
        return sent
