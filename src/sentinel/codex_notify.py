"""Codex ``notify`` 分发器：保留既有回调，并把主回合完成事件送入 WatchMend。

该进程位于 Codex 回合收尾路径，所有失败都必须 fail-open：既不改变 Codex 结果，也不让
WatchMend 网络故障阻断现有 Computer Use ``turn-ended`` 回调。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from sentinel.redact import redact_text

logger = logging.getLogger("watchmend.codex_notify")

_TASK_MAX = 800
_RESULT_MAX = 2000
_ID_MAX = 128
_CWD_MAX = 1024
_PROJECT_MAX = 160
_RETRYABLE_HTTP = {408, 425, 429}
_HOOK_EVENTS = {
    "UserPromptSubmit",
    "PermissionRequest",
    "PostToolUse",
    "Stop",
    "SessionEnd",
}
_HOOK_STDIN_MAX = 262_144


class ConfigError(ValueError):
    """本机私有通知配置无效。异常文本不得包含 token。"""


@dataclass(frozen=True)
class ClientConfig:
    url: str
    token: str
    timeout_seconds: float = 4.0
    retries: int = 1


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.replace("\x00", "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _safe_text(value: object, limit: int) -> str:
    text = value if isinstance(value, str) else str(value or "")
    redacted, _ = redact_text(text, use_patterns=True)
    return _clip(redacted, limit) or "（无摘要）"


def _last_input_message(value: object) -> str:
    if isinstance(value, list):
        for item in reversed(value):
            if isinstance(item, str) and item.strip():
                return item
            if isinstance(item, dict):
                content = item.get("content") or item.get("text")
                if isinstance(content, str) and content.strip():
                    return content
    return value if isinstance(value, str) else ""


def normalize_codex_event(event: dict) -> dict | None:
    """把官方 agent-turn-complete 载荷收窄成 WatchMend 白名单字段。"""
    if event.get("type") != "agent-turn-complete":
        return None
    thread_id = _clip(str(event.get("thread-id") or ""), _ID_MAX)
    turn_id = _clip(str(event.get("turn-id") or ""), _ID_MAX)
    cwd = _clip(str(event.get("cwd") or ""), _CWD_MAX)
    if not thread_id or not turn_id or not cwd:
        return None
    project = _clip(Path(cwd).name or cwd, _PROJECT_MAX)
    task = _safe_text(_last_input_message(event.get("input-messages")), _TASK_MAX)
    result = _safe_text(event.get("last-assistant-message"), _RESULT_MAX)
    return {
        "event_id": f"{thread_id}:{turn_id}",
        "thread_id": thread_id,
        "turn_id": turn_id,
        "project": project,
        "cwd": cwd,
        "task_summary": task,
        "result_summary": result,
    }


def _optional_safe_text(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return _safe_text(value, limit)


def normalize_codex_hook_event(event: dict) -> dict | None:
    """把 Codex Hook stdin 收窄为服务端状态机协议，不发送工具原始输入或输出。"""
    name = str(event.get("hook_event_name") or "")
    if name not in _HOOK_EVENTS:
        return None
    session_id = _clip(str(event.get("session_id") or ""), _ID_MAX)
    turn_id = _clip(str(event.get("turn_id") or ""), _ID_MAX) or None
    cwd = _clip(str(event.get("cwd") or ""), _CWD_MAX)
    if not session_id or not cwd or (name != "SessionEnd" and not turn_id):
        return None
    project = _clip(Path(cwd).name or cwd, _PROJECT_MAX)
    payload: dict[str, object] = {
        "event_name": name,
        "session_id": session_id,
        "turn_id": turn_id,
        "project": project,
        "cwd": cwd,
    }
    tool_input = event.get("tool_input")
    tool_name = _clip(str(event.get("tool_name") or ""), 256) or None
    tool_fingerprint = None
    if tool_input is not None:
        canonical = json.dumps(
            tool_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        tool_fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    suffix = name
    if name == "UserPromptSubmit":
        payload["task_summary"] = _safe_text(event.get("prompt"), _TASK_MAX)
    elif name == "PermissionRequest":
        description = tool_input.get("description") if isinstance(tool_input, dict) else None
        summary = _optional_safe_text(description, _RESULT_MAX)
        payload["result_summary"] = (
            f"等待审批：{summary}" if summary else f"等待审批：{tool_name or '工具调用'}"
        )
        payload["tool_name"] = tool_name
        payload["tool_fingerprint"] = tool_fingerprint
        suffix = f"{name}:{tool_fingerprint or tool_name or 'tool'}"
    elif name == "PostToolUse":
        tool_use_id = _clip(str(event.get("tool_use_id") or ""), 256) or None
        payload["tool_name"] = tool_name
        payload["tool_use_id"] = tool_use_id
        payload["tool_fingerprint"] = tool_fingerprint
        discriminator = str(tool_use_id or tool_fingerprint or tool_name or "tool")
        suffix = f"{name}:{hashlib.sha256(discriminator.encode('utf-8')).hexdigest()}"
    elif name == "Stop":
        payload["result_summary"] = _safe_text(event.get("last_assistant_message"), _RESULT_MAX)
        payload["stop_hook_active"] = bool(event.get("stop_hook_active"))
    elif name == "SessionEnd":
        suffix = name
    payload["event_id"] = f"{session_id}:{turn_id or 'session'}:{suffix}"
    return payload


def load_config(path: str | Path) -> ClientConfig:
    config_path = Path(path).expanduser()
    try:
        mode = stat.S_IMODE(config_path.stat().st_mode)
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise ConfigError("无法读取 Codex 通知私有配置") from exc
    if not isinstance(raw, dict):
        raise ConfigError("Codex 通知私有配置必须是 JSON 对象")
    url = str(raw.get("url") or "").strip()
    token = str(raw.get("token") or "")
    if not url or urlparse(url).scheme not in {"http", "https"}:
        raise ConfigError("Codex 通知 URL 必须使用 http 或 https")
    if not token:
        raise ConfigError("Codex 通知 token 不能为空")
    if os.name == "posix" and mode & 0o077:
        raise ConfigError("Codex 通知私有配置权限必须为 0600")
    try:
        timeout = float(raw.get("timeout_seconds", 4.0))
        retries = int(raw.get("retries", 1))
    except (TypeError, ValueError) as exc:
        raise ConfigError("Codex 通知超时或重试配置无效") from exc
    if not 0.1 <= timeout <= 15:
        raise ConfigError("Codex 通知 timeout_seconds 必须在 0.1 到 15 之间")
    if not 0 <= retries <= 3:
        raise ConfigError("Codex 通知 retries 必须在 0 到 3 之间")
    return ClientConfig(url=url, token=token, timeout_seconds=timeout, retries=retries)


def run_upstream(
    command: list[str],
    raw_event: str,
    *,
    runner: Callable = subprocess.Popen,
) -> bool:
    """原样异步转发 Codex 事件；不用 shell，也不截断既有回调的执行时间。"""
    if not command:
        return True
    try:
        runner(
            [*command, raw_event],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        logger.warning("Codex 既有 notify 回调执行失败")
        return False
    return True


def _is_retryable_http(code: int) -> bool:
    return code in _RETRYABLE_HTTP or code >= 500


def send_to_watchmend(
    payload: dict,
    config: ClientConfig,
    *,
    opener: Callable = urlopen,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    for attempt in range(config.retries + 1):
        request = Request(
            config.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-WatchMend-Token": config.token,
            },
            method="POST",
        )
        try:
            with opener(request, timeout=config.timeout_seconds) as response:
                if response.status >= 300:
                    raise HTTPError(config.url, response.status, "WatchMend rejected", {}, None)
                result = json.loads(response.read().decode("utf-8"))
                return isinstance(result, dict) and result.get("ok") is True
        except HTTPError as exc:
            retryable = _is_retryable_http(exc.code)
        except (URLError, TimeoutError, OSError, ValueError):
            retryable = True
        if not retryable or attempt >= config.retries:
            return False
        sleeper(0.25 * (2**attempt))
    return False


def send_hook_to_watchmend(
    payload: dict,
    config: ClientConfig,
    *,
    opener: Callable = urlopen,
) -> bool:
    """Hook 处于 Codex 生命周期热路径：固定短超时、零重试，快速 fail-open。"""
    base = config.url.rstrip("/")
    if base.endswith("/notifications/codex"):
        url = f"{base}/hooks"
    else:
        url = f"{base}/notifications/codex/hooks"
    hook_config = replace(
        config, url=url, timeout_seconds=min(config.timeout_seconds, 1.5), retries=0
    )
    return send_to_watchmend(payload, hook_config, opener=opener)


def _parse_args(argv: list[str]) -> tuple[Path | None, list[str], str | None]:
    if not argv:
        return None, [], None
    raw_event = argv[-1]
    args = argv[:-1]
    config_path: Path | None = None
    upstream: list[str] = []
    if "--upstream" in args:
        index = args.index("--upstream")
        upstream = args[index + 1 :]
        args = args[:index]
    if "--config" in args:
        index = args.index("--config")
        if index + 1 < len(args):
            config_path = Path(args[index + 1])
    return config_path, upstream, raw_event


def main(argv: list[str] | None = None) -> int:
    config_path, upstream, raw_event = _parse_args(list(sys.argv[1:] if argv is None else argv))
    if raw_event is None:
        return 0

    # 既有回调先独立启动，随后发送网络请求；任一支失败都不影响另一支和 Codex 结果。
    if not run_upstream(upstream, raw_event):
        logger.warning("Codex 既有 notify 回调启动失败")
    try:
        event = json.loads(raw_event)
        payload = normalize_codex_event(event) if isinstance(event, dict) else None
        if config_path is not None and payload is not None:
            config = load_config(config_path)
            if not send_to_watchmend(payload, config):
                logger.warning("WatchMend Codex 通知投递失败")
    except (ConfigError, ValueError, TypeError):
        logger.warning("WatchMend Codex 通知已跳过：事件或私有配置无效")
    return 0


def hook_main(argv: list[str] | None = None) -> int:
    """Codex command Hook 入口；任何错误都返回中性 JSON，不干预代理循环。"""
    args = list(sys.argv[1:] if argv is None else argv)
    config_path: Path | None = None
    if "--config" in args:
        index = args.index("--config")
        if index + 1 < len(args):
            config_path = Path(args[index + 1])
    try:
        raw = sys.stdin.read(_HOOK_STDIN_MAX + 1)
        if len(raw) > _HOOK_STDIN_MAX:
            raise ValueError("Hook 事件过大")
        event = json.loads(raw)
        payload = normalize_codex_hook_event(event) if isinstance(event, dict) else None
        if config_path is not None and payload is not None:
            config = load_config(config_path)
            if not send_hook_to_watchmend(payload, config):
                logger.warning("WatchMend Codex Hook 投递失败")
    except (ConfigError, ValueError, TypeError, OSError):
        logger.warning("WatchMend Codex Hook 已跳过：事件或私有配置无效")
    # Stop Hook 的 stdout 必须是 JSON；空对象不改变任何 Codex 决策。
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
