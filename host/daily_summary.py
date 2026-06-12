#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.27,<0.29"]
# ///
"""体检日报跟帖式 AI 总结(Phase 3):openclaw cron 每天 09:05 驱动。

取 sentinel 日报聚合数据 → 一次性 agent 会话生成 2-3 句中文总结 → 回写 sentinel 发卡。
失败退出非零交给 openclaw cron 失败通知;日报模板卡(09:00)与本脚本完全解耦。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx

# 复用 diag_orchestrator 的信封解析/env 加载,并导入 CLI 常量保持单点定义:
# T7 若改 SESSION_FLAG 只改 diag_orchestrator 一处,本脚本自动跟随
try:
    from host.diag_orchestrator import (
        AGENT_ID,
        OPENCLAW_BIN,
        SESSION_FLAG,
        _load_env,
        extract_text,
    )
except ImportError:  # 部署态:同目录直跑,无 host 包
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from diag_orchestrator import (
        AGENT_ID,
        OPENCLAW_BIN,
        SESSION_FLAG,
        _load_env,
        extract_text,
    )

_load_env(Path(__file__).resolve().parent / ".env")

SENTINEL_URL = os.environ.get("SENTINEL_URL", "http://127.0.0.1:8765")
TOKEN = os.environ.get("SENTINEL_DIAG_TOKEN", "")
AGENT_TIMEOUT_S = 120


def build_prompt(data: dict) -> str:
    return (
        "下面是 dev 服务器内部体检日报的聚合数据(JSON)。请用中文写 2-3 句运维视角总结:"
        "整体是否平稳、最值得注意的 1-2 个点(延迟相对基线、未决事件、错误趋势)。"
        "分析要点:多个服务的延迟/错误若同向变化(如 p95 普遍高于基线),"
        "优先怀疑宿主机层共享瓶颈(内存/swap/CPU/磁盘),并与未决资源类事件关联成同一根因,"
        "不要把各服务当独立问题孤立罗列;倍数虽高但绝对值很小(不构成用户可感知问题)时如实说明、不要夸大。"
        "直接输出总结文本,不要任何前后缀、不要列表、不要 json。\n\n"
        f"日期: {data['date']}\n数据: {json.dumps(data, ensure_ascii=False)}"
    )


def _run_agent_argv(argv: list[str]) -> str:
    """stdout 为空(openclaw 启动失败)时把退出码+stderr 打到本进程 stderr,跟着
    openclaw cron 失败通知带出去排障(同 diag_orchestrator 的教训);但绝不并入
    返回值——返回值会被当总结正文发公开飞书卡。"""
    out = subprocess.run(argv, capture_output=True, text=True, timeout=AGENT_TIMEOUT_S + 60)
    if not out.stdout.strip():
        print(
            f"agent produced no stdout: exit={out.returncode}\n{out.stderr[:2000]}",
            file=sys.stderr,
        )
    return out.stdout


def main() -> int:
    with httpx.Client() as client:
        r = client.get(f"{SENTINEL_URL}/report/daily-data", timeout=10)
        r.raise_for_status()
        data = r.json()
        argv = [
            OPENCLAW_BIN,
            "agent",
            "--agent",
            AGENT_ID,
            SESSION_FLAG,
            f"daily-summary-{data['date']}",
            "--timeout",
            str(AGENT_TIMEOUT_S),
            "--json",
            "-m",
            build_prompt(data),
        ]
        raw = _run_agent_argv(argv)
        extracted = extract_text(raw)
        text = extracted.strip()
        if not text:
            print("empty summary from agent", file=sys.stderr)
            return 1
        # --json 下合法输出必是可解析信封;extract_text 原样回退(stdout 是 CLI 报错
        # 文本,或信封文本落在不认识的键)说明拿到的不是总结,发出去就是垃圾公开卡
        # ——宁可响亮失败进 cron 通知(同 diag_orchestrator parse_diagnosis 的防线)
        if extracted == raw:
            print(f"unrecognized agent output, refusing to post: {raw[:2000]}", file=sys.stderr)
            return 1
        headers = {"X-Sentinel-Token": TOKEN} if TOKEN else {}
        resp = client.post(
            f"{SENTINEL_URL}/report/summary",
            json={"text": text[:1000]},
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
    print("summary sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
