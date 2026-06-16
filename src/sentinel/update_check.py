# src/sentinel/update_check.py
"""版本更新检查:后台 job 周期拉 GitHub releases/latest,比对运行版本,写 meta(零新表)。
render 路径绝不外呼,只读 meta。隐私:仅公开 GET,无身份/遥测;默认开但显式可关。"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("sentinel")


def _parse(v: str) -> tuple[int, ...] | None:
    """剥 v/V 前缀,按 . 切 int 元组;任一段非数字(pre-release/脏串)→ None。"""
    parts = v.strip().lstrip("vV").split(".")
    out: list[int] = []
    for p in parts:
        if not p.isdigit():
            return None
        out.append(int(p))
    return tuple(out)


def is_newer(latest: str | None, current: str) -> bool:
    """latest 是否严格新于 current。解析失败 → False(绝不误报有更新)。"""
    if not latest:
        return False
    a, b = _parse(latest), _parse(current)
    if a is None or b is None:
        return False
    return a > b


async def fetch_latest(
    client: httpx.AsyncClient, url: str, *, user_agent: str
) -> tuple[str, str] | None:
    """拉最新 release。成功 → (tag_name, html_url);任何网络/HTTP/解析异常 → None(fail-safe)。"""
    try:
        resp = await client.get(
            url,
            timeout=10.0,
            headers={"Accept": "application/vnd.github+json", "User-Agent": user_agent},
        )
        resp.raise_for_status()
        data = resp.json()
        tag = data.get("tag_name")
        if not isinstance(tag, str) or not tag:
            return None
        html_url = data.get("html_url")
        return tag, html_url if isinstance(html_url, str) else ""
    except Exception:
        logger.warning("update check failed for %s", url)
        return None
