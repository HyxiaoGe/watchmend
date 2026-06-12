#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27,<0.29"]
# ///
"""sentinel 诊断编排(Phase 3):openclaw cron --command 每 5 分钟驱动,无模型零成本空轮询。

流程:flock 防重叠 → GET /events/pending → 逐事件(≤3)起隔离 openclaw agent 会话(只读
诊断,300s 超时,失败重试 1 次)→ 解析结构化 JSON → POST 回写;sentinel 端负责发诊断卡。
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import httpx


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


_load_env(Path(__file__).resolve().parent / ".env")

SENTINEL_URL = os.environ.get("SENTINEL_URL", "http://127.0.0.1:8765")
TOKEN = os.environ.get("SENTINEL_DIAG_TOKEN", "")
OPENCLAW_BIN = os.environ.get("OPENCLAW_BIN", "openclaw")
SESSION_FLAG = "--session-key"  # 评审已实测核验存在于 2026.6.5;daily_summary 从本模块导入,单点定义
AGENT_ID = "sentinel-diag"
AGENT_TIMEOUT_S = 300
MAX_EVENTS = 3
LOCK_FILE = "/tmp/sentinel-diag.lock"

PROMPT_TEMPLATE = """你是 dev 服务器的只读运维诊断助手。一个确定性巡检规则刚命中了异常事件,\
请你用可用工具自主调查并给出根因诊断。

## 事件
- 规则: {rule}({rule_zh_hint})  主体: {subject}  严重度: {severity}
- 触发详情: {detail}
- 原始证据 payload: {payload}
- 事件时间戳: {ts}

## 调查纪律(必须遵守)
- 诊断只用只读工具(loki 日志查询、ops 系列系统/容器/指标查询);即使工具列表里有 exec
  也严禁使用——执行类操作需要人工飞书审批,不属于诊断流程
- 结论必须基于工具查到的证据,查不到就如实说置信度低,严禁编造
- 建议命令只是给人看的,你自己不要尝试执行任何变更

## 输出格式(最后一条消息必须且只能是一个 json 代码块)
```json
{{"summary": "一句话现象", "root_cause": "推测根因", "evidence": ["证据1", "证据2"],
  "suggested_commands": ["人工执行的建议命令"], "confidence": "high|medium|low"}}
```"""


def build_prompt(event: dict) -> str:
    return PROMPT_TEMPLATE.format(
        rule=event["rule"],
        rule_zh_hint="见 detail",
        subject=event["subject"],
        severity=event["severity"],
        detail=event["detail"],
        payload=event["payload_json"],
        ts=event["ts"],
    )


def _run_agent_argv(argv: list[str]) -> str:
    """独立小函数方便测试替换;超时给 agent timeout 之上再留 60s 余量。
    stdout 为空时把退出码+stderr 并入返回值:openclaw 启动失败通常报错在 stderr,
    不带上的话最终回写的 diagnosis.raw 是空串,没法排障。"""
    out = subprocess.run(argv, capture_output=True, text=True, timeout=AGENT_TIMEOUT_S + 60)
    if not out.stdout.strip():
        return f"exit={out.returncode}\n{out.stderr[:2000]}"
    return out.stdout


def run_agent(prompt: str, session: str) -> str:
    argv = [
        OPENCLAW_BIN,
        "agent",
        "--agent",
        AGENT_ID,
        SESSION_FLAG,
        session,
        "--timeout",
        str(AGENT_TIMEOUT_S),
        "--json",
        "-m",
        prompt,
    ]
    return _run_agent_argv(argv)


def _join_payload_texts(payloads: object) -> str | None:
    if not isinstance(payloads, list):
        return None
    texts = [p.get("text") for p in payloads if isinstance(p, dict)]
    joined = "\n".join(t for t in texts if isinstance(t, str))
    return joined if joined.strip() else None


def extract_text(stdout: str) -> str:
    """openclaw --json 信封防御解析:result.payloads[].text → payloads[].text →
    result/text 字符串 → 原文。T7 冒烟实测(2026.6.5):顶层形状是
    {result: {payloads: [{text, mediaUrl}], meta}, runId, status, summary},
    正文在 result.payloads[].text;顶层 summary 是 run 摘要不是正文,绝不能取。"""
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return stdout
    if isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, dict):
            joined = _join_payload_texts(result.get("payloads"))
            if joined is not None:
                return joined
        joined = _join_payload_texts(payload.get("payloads"))
        if joined is not None:
            return joined
        for key in ("result", "text"):
            v = payload.get(key)
            if isinstance(v, str):
                return v
    return stdout


# 诊断 JSON 至少要有其中之一,否则视为解析失败(防止把 --json 信封本身或其他
# 杂散 JSON 当成诊断回写 done——评审实测过这个误判路径,比解析失败更糟)
_REQUIRED_ANY = ("summary", "root_cause")
# 信封标志键:T7 冒烟实测信封顶层恰好也有 summary 键,若 extract_text 因形状
# 变化原样回退,花括号兜底会把整个信封当诊断——含这些键的 dict 一律拒收
_ENVELOPE_MARKERS = ("runId", "payloads")


def parse_diagnosis(text: str) -> dict | None:
    """提取 ```json 围栏;退化为首个能 json.loads 的 {...} 片段;校验最低形状。"""
    fence = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = [fence.group(1)] if fence else []
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))
    for c in candidates:
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(obj, dict)
            and any(k in obj for k in _REQUIRED_ANY)
            and not any(k in obj for k in _ENVELOPE_MARKERS)
        ):
            return obj
    return None


def post_back(client: httpx.Client, event_id: int, status: str, diagnosis: dict) -> None:
    headers = {"X-Sentinel-Token": TOKEN} if TOKEN else {}
    r = client.post(
        f"{SENTINEL_URL}/events/{event_id}/diagnosis",
        json={"status": status, "diagnosis": diagnosis},
        headers=headers,
        timeout=10,
    )
    r.raise_for_status()


def diagnose(client: httpx.Client, event: dict) -> None:
    eid = event["id"]
    prompt = build_prompt(event)
    raw = ""
    for attempt in (1, 2):  # 共 2 次尝试 = 失败重试 1 次
        try:
            raw = run_agent(prompt, f"diag-evt-{eid}")
        except Exception as exc:  # 子进程超时/启动失败也算一次尝试
            raw = f"agent invocation error: {exc}"
            continue
        diagnosis = parse_diagnosis(extract_text(raw))
        if diagnosis:
            post_back(client, eid, "done", diagnosis)
            print(f"event {eid}: diagnosis done (attempt {attempt})")
            return
    post_back(client, eid, "failed", {"raw": extract_text(raw)[:2000]})
    print(f"event {eid}: diagnosis failed after retry")


def main() -> int:
    lock = open(LOCK_FILE, "w")  # noqa: SIM115 锁文件生命周期=进程
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another run in progress, skip")
        return 0
    with httpx.Client() as client:
        r = client.get(f"{SENTINEL_URL}/events/pending", timeout=10)
        r.raise_for_status()
        events = r.json()["events"]
        if not events:
            return 0
        print(f"{len(events)} pending event(s)")
        for event in events[:MAX_EVENTS]:
            try:
                diagnose(client, event)
            except Exception as exc:  # 单事件失败不挡后续
                print(f"event {event['id']}: orchestration error: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
