# src/sentinel/registry.py
from __future__ import annotations

from collections.abc import Callable

from sentinel.adapters.base import StatusAdapter
from sentinel.adapters.google_cloud import GoogleCloudAdapter
from sentinel.adapters.statuspage import StatuspageAdapter

# 工厂表:provider 名 -> 构造一个适配器。v1 五家。
# 新增 Atlassian 同款 = 加一行 StatuspageAdapter(...)。
_FACTORIES: dict[str, Callable[[], StatusAdapter]] = {
    "anthropic": lambda: StatuspageAdapter(
        "anthropic", "Anthropic / Claude", "https://status.claude.com"
    ),
    "openai": lambda: StatuspageAdapter("openai", "OpenAI", "https://status.openai.com"),
    "github": lambda: StatuspageAdapter("github", "GitHub", "https://www.githubstatus.com"),
    "cloudflare": lambda: StatuspageAdapter(
        "cloudflare", "Cloudflare", "https://www.cloudflarestatus.com", track_components=False
    ),
    "google_cloud": lambda: GoogleCloudAdapter(),
}


def build_adapters(provider_names: list[str]) -> list[StatusAdapter]:
    """按给定顺序构造启用的适配器;未知名静默跳过。"""
    return [_FACTORIES[name]() for name in provider_names if name in _FACTORIES]
