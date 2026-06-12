# tests/test_config.py
from sentinel.config import Settings


def test_defaults(monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    s = Settings(_env_file=None)
    assert s.providers_list == ["anthropic", "openai", "github", "cloudflare", "google_cloud"]
    assert s.sentinel_poll_interval == 60
    assert s.sentinel_incident_verbosity == "phase"
    assert s.sentinel_fail_threshold == 3


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


def test_phase2_defaults(monkeypatch):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    from sentinel.config import Settings

    s = Settings(_env_file=None)
    assert s.sentinel_prometheus_url == "http://prometheus:9090"
    assert s.sentinel_loki_url == "http://loki:3100"
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
