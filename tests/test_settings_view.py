from sentinel.config import Settings
from sentinel.panel import settings_view


def _settings(monkeypatch, **env):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def test_inventory_covers_every_settings_field_exactly(monkeypatch):
    s = _settings(monkeypatch)
    inv = settings_view.build_config_inventory(s, llm_config=None)
    listed = {row["env"] for group in inv["groups"] for row in group["rows"] if row.get("env")}
    expected = {name.upper() for name in Settings.model_fields}
    assert listed == expected, f"漏掉: {expected - listed}  幽灵字段: {listed - expected}"


def test_inventory_secret_fields_show_status_not_value(monkeypatch):
    s = _settings(
        monkeypatch,
        SENTINEL_DIAG_TOKEN="supersecrettoken-abc123def456",
        SENTINEL_TELEGRAM_BOT_TOKEN="123456:AAbbCcDd-telegram-secret-xyz",
    )
    inv = settings_view.build_config_inventory(s, llm_config=None)
    rows = {r["env"]: r for g in inv["groups"] for r in g["rows"] if r.get("env")}
    tok = rows["SENTINEL_DIAG_TOKEN"]
    assert tok["secret"] is True
    assert tok["configured"] is True
    assert "supersecrettoken" not in (tok["value"] or "")
    tg = rows["SENTINEL_TELEGRAM_BOT_TOKEN"]
    assert tg["configured"] is True
    assert "telegram-secret" not in (tg["value"] or "")


def test_inventory_unset_secret_shows_not_configured(monkeypatch):
    s = _settings(monkeypatch)
    inv = settings_view.build_config_inventory(s, llm_config=None)
    rows = {r["env"]: r for g in inv["groups"] for r in g["rows"] if r.get("env")}
    assert rows["SENTINEL_WEBHOOK_TOKEN"]["configured"] is False


def test_inventory_redacts_embedded_secret_in_nonsecret_value(monkeypatch):
    s = _settings(monkeypatch, SENTINEL_PROMETHEUS_URL="https://x/?k=sk-ABCDEFGHIJKLMNOPQRSTUVWX")
    inv = settings_view.build_config_inventory(s, llm_config=None)
    rows = {r["env"]: r for g in inv["groups"] for r in g["rows"] if r.get("env")}
    assert "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in rows["SENTINEL_PROMETHEUS_URL"]["value"]


def test_inventory_llm_synthetic_rows_disabled_when_no_config(monkeypatch):
    s = _settings(monkeypatch)
    inv = settings_view.build_config_inventory(s, llm_config=None)
    llm_group = next(g for g in inv["groups"] if g["key"] == "llm")
    actives = [r for r in llm_group["rows"] if r.get("synthetic") == "active"]
    assert actives and actives[0]["value"] == "—"


class _FakeProfile:
    def __init__(self, name, model):
        self.name, self.model, self.base_url, self.api_key = name, model, "http://x", "SECRETKEY"


class _FakeLLM:
    def current(self):
        return _FakeProfile("deepseek", "deepseek-chat")

    def fallback(self):
        return _FakeProfile("kimi", "kimi-k2")


def test_inventory_llm_synthetic_rows_show_active_fallback(monkeypatch):
    s = _settings(monkeypatch)
    inv = settings_view.build_config_inventory(s, llm_config=_FakeLLM())
    llm_group = next(g for g in inv["groups"] if g["key"] == "llm")
    syn = {r["synthetic"]: r for r in llm_group["rows"] if r.get("synthetic")}
    assert "deepseek" in syn["active"]["value"] and "deepseek-chat" in syn["active"]["value"]
    assert "kimi" in syn["fallback"]["value"]
    assert "SECRETKEY" not in syn["active"]["value"]  # api_key never shown
