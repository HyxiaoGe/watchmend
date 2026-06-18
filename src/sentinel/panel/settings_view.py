"""设置页 view-model（纯函数，无 Request 依赖，便于单测）。
build_config_inventory：按组列出 Settings 全字段（env 变量名为项标识），
9 个密钥字段只产出"已配置/未配置"状态、绝不出值；非密钥值过 redact_text 兜底。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sentinel.config import Settings
from sentinel.redact import redact_text

if TYPE_CHECKING:
    from sentinel.llm_config import LLMConfig, LLMProfile

# 密钥字段:只显状态,永不出值。(必须覆盖全部敏感字段)
# 注:feishu_*_webhook 把签名 token 嵌在 URL path 里,故计入密钥;
# sentinel_webhook_url / sentinel_ntfy_url 是裸端点,其 Bearer 另存于
# *_token 字段(已计入),故 URL 本身非密钥(仍过 redact 兜底)。
_SECRETS: frozenset[str] = frozenset(
    {
        "feishu_vendor_webhook",
        "feishu_vendor_sign_secret",
        "feishu_patrol_webhook",
        "feishu_patrol_sign_secret",
        "llm_api_key",
        "sentinel_telegram_bot_token",
        "sentinel_ntfy_token",
        "sentinel_webhook_token",
        "sentinel_diag_token",
    }
)

# 分组（顺序即展示顺序）。覆盖 Settings 全部字段——test 守护集合相等。
_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "probe",
        (
            "sentinel_providers",
            "sentinel_poll_interval",
            "sentinel_incident_verbosity",
            "sentinel_fail_threshold",
            "sentinel_probe_interval",
            "sentinel_services_file",
            "sentinel_probe_retention_days",
            "sentinel_cooldown_hours",
            "sentinel_probe_fail_streak",
            "sentinel_latency_ratio",
            "sentinel_latency_margin_ms",
            "sentinel_latency_min_samples",
            "sentinel_log_spike_ratio",
            "sentinel_log_spike_min",
            "sentinel_disk_usage_pct",
            "sentinel_disk_forecast_days",
            "sentinel_container_mem_pct",
            "sentinel_swap_pct",
            "sentinel_middleware_metrics",
        ),
    ),
    (
        "datasource",
        (
            "sentinel_prometheus_url",
            "sentinel_loki_url",
            "sentinel_scan_interval",
            "sentinel_scan_fail_threshold",
        ),
    ),
    (
        "channels",
        (
            "feishu_vendor_webhook",
            "feishu_vendor_sign_secret",
            "feishu_patrol_webhook",
            "feishu_patrol_sign_secret",
            "sentinel_telegram_bot_token",
            "sentinel_telegram_chat_id",
            "sentinel_ntfy_url",
            "sentinel_ntfy_token",
            "sentinel_webhook_url",
            "sentinel_webhook_token",
            "sentinel_heartbeat_enabled",
            "sentinel_heartbeat_hour",
            "sentinel_heartbeat_utc_offset",
            "sentinel_report_hour",
        ),
    ),
    (
        "llm",
        (
            "llm_base_url",
            "llm_api_key",
            "llm_model",
            "llm_timeout_seconds",
            "llm_max_tool_rounds",
            "sentinel_diag_interval",
            "sentinel_llm_config_file",
            "sentinel_llm_lang",
        ),
    ),
    (
        "docker",
        (
            "sentinel_docker_socket",
            "sentinel_docker_host",
            "sentinel_docker_scan_interval",
            "sentinel_docker_crashloop_window",
            "sentinel_docker_crashloop_threshold",
            "sentinel_docker_exclude",
        ),
    ),
    (
        "panel",
        (
            "sentinel_panel_enabled",
            "sentinel_panel_default_lang",
            "sentinel_panel_default_theme",
            "sentinel_panel_history_days",
            "sentinel_panel_default_window",
            "sentinel_panel_refresh_seconds",
            "sentinel_panel_page_size",
            "sentinel_panel_red_uptime_pct",
            "sentinel_panel_partial_uptime_pct",
            "sentinel_panel_green_uptime_pct",
            "sentinel_panel_overview_roster_cap",
            "sentinel_event_feed_days",
            "sentinel_update_check_enabled",
            "sentinel_update_check_url",
            "sentinel_update_check_interval",
        ),
    ),
    (
        "backup_cert",
        (
            "sentinel_backup_dir",
            "sentinel_backup_max_age_hours",
            "sentinel_cert_domains",
            "sentinel_cert_min_days",
        ),
    ),
    ("security", ("sentinel_db_path", "sentinel_diag_token")),
)


def _secret_values(settings: Settings) -> list[str]:
    """已配置的密钥真值,喂给 redact_text 做兜底子串脱敏。"""
    out: list[str] = []
    for name in _SECRETS:
        v = getattr(settings, name, None)
        if v:
            out.append(str(v))
    return out


def _field_row(settings: Settings, name: str, secret_vals: list[str]) -> dict:
    raw = getattr(settings, name, None)
    is_secret = name in _SECRETS
    configured = bool(raw)
    if is_secret:
        value = None  # 永不出值
    else:
        text = "" if raw is None else str(raw)
        value = redact_text(text, secrets=secret_vals, use_patterns=True)[0] if text else ""
    return {
        "env": name.upper(),
        "value": value,
        "secret": is_secret,
        "configured": configured,
        "change": "env",
    }


def _llm_synthetic_rows(llm_config: LLMConfig | None) -> list[dict]:
    """LLM 组顶部 active/fallback 合成行(取自注册表,非 Settings 字段)。
    只显 provider+model,绝不显 api_key/base_url;未启用显占位 '—'。"""

    def _fmt(profile: LLMProfile | None) -> str:
        if profile is None:
            return "—"
        return f"{profile.name} · {profile.model}"

    active = llm_config.current() if llm_config is not None else None
    fallback = llm_config.fallback() if llm_config is not None else None
    return [
        {
            "synthetic": "active",
            "env": None,
            "value": _fmt(active),
            "secret": False,
            "configured": active is not None,
            "change": "llm",
        },
        {
            "synthetic": "fallback",
            "env": None,
            "value": _fmt(fallback),
            "secret": False,
            "configured": fallback is not None,
            "change": "llm",
        },
    ]


def build_config_inventory(settings: Settings, *, llm_config: LLMConfig | None = None) -> dict:
    """按组只读一览。返回 {"groups": [{"key", "rows": [...]}, ...]}。"""
    secret_vals = _secret_values(settings)
    groups: list[dict] = []
    for key, names in _GROUPS:
        rows: list[dict] = []
        if key == "llm":
            rows.extend(_llm_synthetic_rows(llm_config))
        rows.extend(_field_row(settings, n, secret_vals) for n in names)
        groups.append({"key": key, "rows": rows})
    return {"groups": groups}


def build_display_prefs(
    *,
    lang_eff: str,
    lang_cookie: str | None,
    theme_eff: str,
    window_eff: int,
    history_days: int,
    refresh_eff: int,
    refresh_cookie: str | None,
    server_refresh: int,
) -> dict:
    """显示偏好表单的当前选中态。lang/refresh 的"中性选项"(自动/默认)在无显式 cookie 时选中。"""
    lang_sel = (
        lang_cookie.strip().lower()
        if (lang_cookie or "").strip().lower() in ("zh", "en")
        else "auto"
    )
    refresh_sel = (
        refresh_cookie.strip()
        if (refresh_cookie or "").strip() in ("0", "15", "30", "60")
        else "default"
    )
    return {
        "lang": {"selected": lang_sel, "options": ["auto", "zh", "en"]},
        "theme": {"selected": theme_eff, "options": ["system", "dark", "light"]},
        "window": {"selected": window_eff, "options": [30, history_days]},
        "refresh": {
            "selected": refresh_sel,
            "options": ["default", "0", "15", "30", "60"],
            "server": server_refresh,
        },
    }
