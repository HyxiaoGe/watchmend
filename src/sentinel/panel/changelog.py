# src/sentinel/panel/changelog.py
"""面板更新日志内容源:解析 Keep-a-Changelog 文本 + 定位打包/源码两态的 changelog。
纯只读静态文件,绝不外呼;条目以纯文本渲染(autoescape),不解释 Markdown 内联。
版本 chip 点击弹出的 :target 模态与 /changelog 整页都消费 load_releases() 的全量版本块。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_VERSION_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\](?:\s*-\s*(\S+))?\s*$")
_SECTION_RE = re.compile(r"^### (.+?)\s*$")
# 顶层与缩进子弹都算独立条目(子弹的 `- ` 前可有缩进);否则缩进子弹会被当作续行
# 并入上一条,压成 run-on 且泄漏字面 "- "。子弹自身的折行续行(缩进、无 `- `)仍由
# 下方续行分支并入对应条目。
_ENTRY_RE = re.compile(r"^\s*- (.+?)\s*$")


@dataclass(frozen=True)
class Section:
    category: str  # "Added"/"Fixed"/... 或 "新增"/"修复"/...(取自文件,不翻译)
    entries: list[str]


@dataclass(frozen=True)
class Release:
    version: str  # "0.10.1"
    date: str  # "2026-06-16";缺失为 ""
    sections: list[Section]


def parse_changelog(text: str) -> list[Release]:
    """解析成有序版本块。跳过 [Unreleased] 与底部 compare-link;折行续行合并折叠空白。"""
    releases: list[Release] = []
    cur_rel: dict | None = None
    cur_sec: dict | None = None

    def flush_sec() -> None:
        nonlocal cur_sec
        if cur_rel is not None and cur_sec is not None:
            cur_rel["sections"].append(Section(cur_sec["category"], cur_sec["entries"]))
        cur_sec = None

    def flush_rel() -> None:
        nonlocal cur_rel
        flush_sec()
        if cur_rel is not None:
            releases.append(Release(cur_rel["version"], cur_rel["date"], cur_rel["sections"]))
        cur_rel = None

    for raw in text.splitlines():
        m = _VERSION_RE.match(raw)
        if m:
            flush_rel()
            cur_rel = {"version": m.group(1), "date": m.group(2) or "", "sections": []}
            continue
        if raw.startswith("## "):  # 非版本 ## 头(如 [Unreleased])→ 退出 release 区
            flush_rel()
            continue
        if cur_rel is None:
            continue
        ms = _SECTION_RE.match(raw)
        if ms:
            flush_sec()
            cur_sec = {"category": ms.group(1), "entries": []}
            continue
        me = _ENTRY_RE.match(raw)
        if me:
            if cur_sec is not None:
                cur_sec["entries"].append(me.group(1).strip())
            continue
        # 缩进续行(行首空白、非空)并入上一条
        if cur_sec is not None and cur_sec["entries"] and raw.strip() and raw[:1].isspace():
            merged = cur_sec["entries"][-1] + " " + raw.strip()
            cur_sec["entries"][-1] = re.sub(r"\s+", " ", merged).strip()

    flush_rel()
    return releases


_FILES = {"zh": "CHANGELOG.zh-CN.md", "en": "CHANGELOG.md"}


def _resolve(lang: str) -> Path | None:
    """包内 force-include 副本优先(装 wheel),回落仓库根(源码/测试)。"""
    name = _FILES.get(lang, _FILES["en"])
    packaged = Path(__file__).resolve().parent.parent / "_changelog" / name
    if packaged.is_file():
        return packaged
    root = Path(__file__).resolve().parents[3] / name  # src/sentinel/panel/ → 仓库根
    if root.is_file():
        return root
    return None


def load_releases(lang: str) -> list[Release]:
    """定位 → 读 → 解析;定位失败返回 [](降级空态)。"""
    path = _resolve(lang)
    if path is None:
        return []
    return parse_changelog(path.read_text(encoding="utf-8"))
