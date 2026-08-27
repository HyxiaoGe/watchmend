from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from sentinel.codex_reset.models import FetchedSource, ResetEvidence, ResetStage


class ReferenceProbeError(RuntimeError):
    """本机 Codex 参考账号只读探针失败；异常消息不得包含子进程输出。"""


class ReferenceRateLimitSource:
    name = "reference_account"
    family = "local_reference"

    def __init__(
        self,
        *,
        cli_path: str,
        codex_home: str,
        runtime_home: str,
        timeout_seconds: int,
        min_window_minutes: int,
        max_reset_age_seconds: int,
        clock=time.time,
    ) -> None:
        self._cli_path = cli_path
        self._codex_home = Path(codex_home)
        self._runtime_home = Path(runtime_home)
        self._timeout_seconds = timeout_seconds
        self._min_window_minutes = min_window_minutes
        self._max_reset_age_seconds = max_reset_age_seconds
        self._clock = clock
        self._last_candidate: tuple[str, int] | None = None

    async def fetch(self, fetcher) -> FetchedSource:  # noqa: ARG002
        self._validate_home()
        result = await asyncio.wait_for(self._read_rate_limits(), self._timeout_seconds)
        now_ts = int(self._clock())
        evidence = evidence_from_rate_limits(
            result,
            now_ts=now_ts,
            min_window_minutes=self._min_window_minutes,
            max_reset_age_seconds=self._max_reset_age_seconds,
        )
        current = (evidence.source_item_id, evidence.observed_at) if evidence is not None else None
        stable = evidence if current is not None and current == self._last_candidate else None
        self._last_candidate = current
        if stable is not None:
            stable = replace(
                stable,
                summary="本机参考账号连续两次只读观察到同一共享 Codex 七日额度窗口起点。",
            )
        return FetchedSource(
            name=self.name,
            family=self.family,
            content_ts=now_ts,
            evidence=[stable] if stable is not None else [],
        )

    def _validate_home(self) -> None:
        if not (self._codex_home / "auth.json").is_file():
            raise ReferenceProbeError("reference auth file unavailable")
        self._runtime_home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._runtime_home.chmod(0o700)

    async def _read_rate_limits(self) -> dict[str, Any]:
        process = await asyncio.create_subprocess_exec(
            self._cli_path,
            "app-server",
            "--listen",
            "stdio://",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=self._subprocess_env(),
            limit=1024 * 1024,
        )
        try:
            await self._send(
                process,
                {
                    "method": "initialize",
                    "id": 1,
                    "params": {
                        "clientInfo": {
                            "name": "watchmend_reference_probe",
                            "title": "WatchMend reference probe",
                            "version": "1",
                        }
                    },
                },
            )
            await self._response(process, 1)
            await self._send(process, {"method": "initialized", "params": {}})
            await self._send(process, {"method": "account/rateLimits/read", "id": 2})
            response = await self._response(process, 2)
            result = response.get("result")
            if not isinstance(result, dict):
                raise ReferenceProbeError("rate-limit response missing result")
            return result
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except TimeoutError:
                    process.kill()
                    await process.wait()

    @staticmethod
    async def _send(process: asyncio.subprocess.Process, payload: dict[str, Any]) -> None:
        if process.stdin is None:
            raise ReferenceProbeError("app-server stdin unavailable")
        process.stdin.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
        await process.stdin.drain()

    @staticmethod
    async def _response(process: asyncio.subprocess.Process, response_id: int) -> dict[str, Any]:
        if process.stdout is None:
            raise ReferenceProbeError("app-server stdout unavailable")
        while True:
            line = await process.stdout.readline()
            if not line:
                raise ReferenceProbeError("app-server exited before response")
            try:
                payload = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or payload.get("id") != response_id:
                continue
            if payload.get("error") is not None:
                raise ReferenceProbeError("app-server returned protocol error")
            return payload

    def _subprocess_env(self) -> dict[str, str]:
        env = {
            "CODEX_HOME": str(self._codex_home),
            "HOME": str(self._runtime_home),
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "RUST_LOG": "error",
        }
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "no_proxy",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
        ):
            if value := os.environ.get(name):
                env[name] = value
        return env


def evidence_from_rate_limits(
    result: dict[str, Any],
    *,
    now_ts: int,
    min_window_minutes: int,
    max_reset_age_seconds: int,
) -> ResetEvidence | None:
    """把官方 app-server 的额度窗口转成不含额度百分比的本机确认事实。"""
    candidates: list[tuple[int, str]] = []
    by_limit_id = result.get("rateLimitsByLimitId")
    groups = by_limit_id.items() if isinstance(by_limit_id, dict) else ()
    for limit_id, limits in groups:
        # 模型专属空窗口可能每次返回 now + 7d，不能证明共享额度重置。
        if limit_id == "codex" and isinstance(limits, dict):
            _collect_windows(candidates, str(limit_id), limits, min_window_minutes)

    if not candidates:
        limits = result.get("rateLimits")
        if isinstance(limits, dict) and limits.get("limitId", "codex") == "codex":
            _collect_windows(candidates, "codex", limits, min_window_minutes)
    if not candidates:
        return None

    reset_start_ts, window_name = max(candidates)
    age = now_ts - reset_start_ts
    if age < 0 or age > max_reset_age_seconds:
        return None
    return ResetEvidence(
        source_name="reference_account",
        source_family="local_reference",
        source_item_id=f"codex:{window_name}:{reset_start_ts}",
        canonical_hint=f"local-reference:{reset_start_ts}",
        signal_stage=ResetStage.CONFIRMED,
        title="Codex reset 本机参考确认",
        summary="本机参考账号只读观察到共享 Codex 七日额度窗口起点。",
        url="",
        observed_at=reset_start_ts,
        explicit_completed=True,
        local_reference=True,
    )


def _collect_windows(
    candidates: list[tuple[int, str]],
    limit_id: str,
    limits: dict[str, Any],
    min_window_minutes: int,
) -> None:
    for window_name in ("primary", "secondary"):
        window = limits.get(window_name)
        if not isinstance(window, dict):
            continue
        duration = window.get("windowDurationMins")
        resets_at = window.get("resetsAt")
        if not isinstance(duration, int | float) or not isinstance(resets_at, int | float):
            continue
        duration_minutes = int(duration)
        if duration_minutes < min_window_minutes:
            continue
        reset_start_ts = int(resets_at) - duration_minutes * 60
        candidates.append((reset_start_ts, f"{limit_id}:{window_name}"))
