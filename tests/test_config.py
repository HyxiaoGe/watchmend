# tests/test_config.py
import pytest

from sentinel.config import Settings


def test_defaults(monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    s = Settings(_env_file=None)
    assert s.providers_list == ["anthropic", "openai", "github", "cloudflare", "google_cloud"]
    assert s.sentinel_poll_interval == 60
    assert s.sentinel_incident_verbosity == "phase"
    assert s.sentinel_fail_threshold == 3
    assert s.sentinel_editor_mode == "off"
    assert s.editor_enabled is False
    assert s.sentinel_error_alert_enabled is False
    assert s.sentinel_restic_backup_max_age_hours == 0
    assert s.sentinel_notification_mode == "live"
    assert s.sentinel_codex_ingest_token == ""
    assert s.sentinel_codex_receipt_retention_days == 30
    assert s.sentinel_codex_hook_grace_seconds == 300
    assert s.sentinel_codex_long_turn_seconds == 180
    assert s.sentinel_codex_hook_poll_seconds == 1


def test_status_editor_enabled_when_mode_and_endpoint_are_complete(monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://x")
    monkeypatch.setenv("SENTINEL_EDITOR_MODE", "gate")
    monkeypatch.setenv("SENTINEL_EDITOR_BASE_URL", "http://litellm:4000")
    monkeypatch.setenv("SENTINEL_EDITOR_MODEL", "gemini/gemini-2.5-flash")
    assert Settings(_env_file=None).editor_enabled is True


def test_providers_list_parses_csv(monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://x")
    monkeypatch.setenv("SENTINEL_PROVIDERS", "anthropic, github")
    s = Settings(_env_file=None)
    assert s.providers_list == ["anthropic", "github"]


def test_phase1_defaults(monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://x")
    s = Settings(_env_file=None)
    assert s.sentinel_probe_interval == 300
    assert s.sentinel_services_file == "services.yaml"
    assert s.sentinel_report_hour == 9
    assert s.sentinel_probe_retention_days == 30


def test_patrol_webhook_falls_back_to_vendor(monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://vendor")
    monkeypatch.setenv("FEISHU_VENDOR_SIGN_SECRET", "vsec")
    s = Settings(_env_file=None)
    assert s.patrol_webhook == "https://vendor"
    assert s.patrol_sign_secret == "vsec"


def test_patrol_webhook_uses_own_value_when_set(monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://vendor")
    monkeypatch.setenv("FEISHU_VENDOR_SIGN_SECRET", "vsec")
    monkeypatch.setenv("FEISHU_PATROL_WEBHOOK", "https://patrol")
    s = Settings(_env_file=None)
    assert s.patrol_webhook == "https://patrol"
    # 独立 webhook 未配 secret → None,绝不能误用 vendor 的 secret
    assert s.patrol_sign_secret is None
    monkeypatch.setenv("FEISHU_PATROL_SIGN_SECRET", "psec")
    assert Settings(_env_file=None).patrol_sign_secret == "psec"


def test_vendor_webhook_falls_back_to_patrol(monkeypatch):
    # 对称回退:只配 patrol(vendor 留空)时,vendor 流也必须有渠道,否则状态页事件静默不发
    monkeypatch.delenv("FEISHU_VENDOR_WEBHOOK", raising=False)
    monkeypatch.setenv("FEISHU_PATROL_WEBHOOK", "https://patrol")
    monkeypatch.setenv("FEISHU_PATROL_SIGN_SECRET", "psec")
    s = Settings(_env_file=None)
    assert s.vendor_webhook == "https://patrol"
    assert s.vendor_sign_secret == "psec"


def test_vendor_webhook_uses_own_value_when_set(monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://vendor")
    monkeypatch.setenv("FEISHU_VENDOR_SIGN_SECRET", "vsec")
    monkeypatch.setenv("FEISHU_PATROL_WEBHOOK", "https://patrol")
    monkeypatch.setenv("FEISHU_PATROL_SIGN_SECRET", "psec")
    s = Settings(_env_file=None)
    assert s.vendor_webhook == "https://vendor"
    assert s.vendor_sign_secret == "vsec"  # 各自独立,不串用


def test_phase2_defaults(monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    from sentinel.config import Settings

    s = Settings(_env_file=None)
    assert s.sentinel_prometheus_url == ""  # 默认空=不接 prometheus(opt-in 数据源)
    assert s.sentinel_loki_url == ""  # 默认空=不接 loki(opt-in 数据源)
    assert s.sentinel_scan_interval == 900
    assert s.sentinel_cooldown_hours == 6
    assert s.sentinel_scan_fail_threshold == 3
    assert s.sentinel_probe_fail_streak == 3
    assert s.sentinel_latency_ratio == 2.0
    assert s.sentinel_latency_margin_ms == 500.0
    assert s.sentinel_latency_min_samples == 6
    assert s.sentinel_log_spike_ratio == 3.0
    assert s.sentinel_log_spike_min == 10
    assert s.sentinel_disk_usage_pct == 85.0
    assert s.sentinel_disk_forecast_days == 14
    assert s.sentinel_container_mem_pct == 90.0
    assert s.sentinel_swap_pct == 80.0
    assert s.sentinel_backup_dir == "/backups/postgresql"
    assert s.sentinel_backup_max_age_hours == 28
    assert s.sentinel_cert_min_days == 14
    assert s.sentinel_cert_domains == ""  # 默认空=跳过证书检查,部署者自填
    assert s.sentinel_middleware_metrics == ""  # 默认空=跳过中间件检查,部署者自填


def test_cert_domains_list_parses_csv(monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_CERT_DOMAINS", " a.example.org, ,b.example.org ")
    from sentinel.config import Settings

    s = Settings(_env_file=None)
    assert s.cert_domains_list == ["a.example.org", "b.example.org"]


def test_docker_endpoint_host_takes_precedence(monkeypatch):
    # 同时设 host + socket(弃用别名)时,host 优先,socket 被忽略
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DOCKER_HOST", "tcp://docker-proxy:2375")
    monkeypatch.setenv("SENTINEL_DOCKER_SOCKET", "/var/run/docker.sock")
    s = Settings(_env_file=None)
    assert s.docker_endpoint == "tcp://docker-proxy:2375"


def test_docker_endpoint_socket_only_gets_triple_slash(monkeypatch):
    # 只配弃用的 socket 路径:绝对路径 /var/run/docker.sock → unix:///var/run/docker.sock(三斜杠)
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DOCKER_SOCKET", "/var/run/docker.sock")
    s = Settings(_env_file=None)
    assert s.docker_endpoint == "unix:///var/run/docker.sock"


def test_docker_endpoint_neither_is_empty(monkeypatch):
    # host 和 socket 都不配:docker 诊断/巡检整体关闭,endpoint 为空串
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    s = Settings(_env_file=None)
    assert s.docker_endpoint == ""


def test_docker_exclude_list_parses_csv(monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DOCKER_EXCLUDE", " a-svc, ,b-svc ")
    s = Settings(_env_file=None)
    assert s.docker_exclude_list == ["a-svc", "b-svc"]


def test_feishu_vendor_webhook_now_optional(monkeypatch):
    # 不再必填:不设 FEISHU_VENDOR_WEBHOOK 也能构造 Settings(由 build_jobs 校验至少一渠道)
    monkeypatch.delenv("FEISHU_VENDOR_WEBHOOK", raising=False)
    s = Settings(_env_file=None)
    assert s.feishu_vendor_webhook == ""
    assert s.feishu_enabled is False


def test_channel_enabled_flags(monkeypatch):
    monkeypatch.delenv("FEISHU_VENDOR_WEBHOOK", raising=False)
    monkeypatch.setenv("SENTINEL_TELEGRAM_BOT_TOKEN", "TESTtok")
    monkeypatch.setenv("SENTINEL_TELEGRAM_CHAT_ID", "-100200300")
    monkeypatch.setenv("SENTINEL_NTFY_URL", "https://ntfy.sh/t")
    monkeypatch.setenv("SENTINEL_WEBHOOK_URL", "https://hook/x")
    s = Settings(_env_file=None)
    assert s.telegram_enabled is True
    assert s.ntfy_enabled is True
    assert s.webhook_enabled is True
    assert s.feishu_enabled is False


def test_telegram_needs_both_token_and_chat(monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://x")
    monkeypatch.setenv("SENTINEL_TELEGRAM_BOT_TOKEN", "TESTtok")
    monkeypatch.delenv("SENTINEL_TELEGRAM_CHAT_ID", raising=False)
    s = Settings(_env_file=None)
    assert s.telegram_enabled is False  # 缺 chat_id → 不启用


def test_feishu_enabled_via_patrol_only(monkeypatch):
    monkeypatch.delenv("FEISHU_VENDOR_WEBHOOK", raising=False)
    monkeypatch.setenv("FEISHU_PATROL_WEBHOOK", "https://patrol")
    s = Settings(_env_file=None)
    assert s.feishu_enabled is True  # 任一飞书 webhook 即视为启用


def test_llm_config_file_default(monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    from sentinel.config import Settings

    assert Settings(_env_file=None).sentinel_llm_config_file == "llm.yaml"


def test_crashloop_defaults(monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    from sentinel.config import Settings

    s = Settings(_env_file=None)
    assert s.sentinel_docker_crashloop_window == 600
    assert s.sentinel_docker_crashloop_threshold == 3


def test_crashloop_overridable(monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("SENTINEL_DOCKER_CRASHLOOP_WINDOW", "1200")
    monkeypatch.setenv("SENTINEL_DOCKER_CRASHLOOP_THRESHOLD", "5")
    from sentinel.config import Settings

    s = Settings(_env_file=None)
    assert s.sentinel_docker_crashloop_window == 1200
    assert s.sentinel_docker_crashloop_threshold == 5


@pytest.mark.parametrize(
    "var",
    ["SENTINEL_DOCKER_CRASHLOOP_THRESHOLD", "SENTINEL_DOCKER_CRASHLOOP_WINDOW"],
)
@pytest.mark.parametrize("bad", ["0", "-1"])
def test_crashloop_knobs_reject_nonpositive(monkeypatch, var, bad):
    # 误配 <=0 会让 delta>=N 恒真 / 发"0 分钟"的噪声卡 → 启动即拒绝(Field ge=1),逼操作员改对
    from pydantic import ValidationError

    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv(var, bad)
    from sentinel.config import Settings

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
