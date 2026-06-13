# src/sentinel/discover.py
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from sentinel.config import Settings

if TYPE_CHECKING:
    from sentinel.docker_client import DockerClient

# (Image 正则, 展示名, 对应 Settings 字段, 默认端口)。命中且字段为空 → 出一条启用建议。
FINGERPRINTS = [
    (re.compile(r"prom/prometheus"), "Prometheus", "sentinel_prometheus_url", 9090),
    (re.compile(r"grafana/loki"), "Loki", "sentinel_loki_url", 3100),
]


async def probe(docker: DockerClient | None, settings: Settings) -> list[str]:
    """扫描运行中的容器,发现已部署但未接入的观测栈 → 返回启用建议(MVP 仅日志)。

    docker 未配置 → []。ps 失败不上抛(自动发现是锦上添花,绝不影响启动)。
    """
    if docker is None:
        return []
    try:
        rows = await docker.ps(all=True)
    except Exception:
        return []
    suggestions: list[str] = []
    for row in rows:
        names = row.get("Names") or []
        if not names:
            continue  # 无名容器跳过:否则建议会渲染成 http://:port 坏 URL(与 scan_docker 一致)
        image = row.get("Image") or ""
        name = names[0].lstrip("/")
        for pattern, display, field, port in FINGERPRINTS:
            if pattern.search(image) and not getattr(settings, field, ""):
                env_name = field.upper()
                suggestions.append(
                    f"💡 发现 {display}(容器 {name},:{port}),"
                    f"启用:.env 设 {env_name}=http://{name}:{port} 后重启"
                )
    return suggestions
