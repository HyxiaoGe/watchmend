import re

from sentinel.config import Settings
from sentinel.panel import settings_view
from sentinel.panel.settings_view import _SECRETS

# 裸端点 URL:凭证在配对的 *_token 字段里,URL 本身过 redact 兜底,故有意不列为 secret
_URL_ALLOWLIST = {"sentinel_webhook_url", "sentinel_ntfy_url"}
_CRED_RE = re.compile(r"(secret|token|key|password|passwd|pwd|sign|credential|webhook)", re.I)


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


def test_all_credential_shaped_fields_are_secret():
    """fail-safe:任何凭证形态字段名必须在 _SECRETS(除两个有意豁免的裸 URL)。
    新增密钥字段若忘记登记,这条测试会变红,而不是静默泄露明文。"""
    from sentinel.config import Settings

    suspects = {
        name
        for name in Settings.model_fields
        if _CRED_RE.search(name) and name not in _URL_ALLOWLIST
    }
    missing = suspects - set(_SECRETS)
    assert not missing, f"这些凭证形态字段未登记进 _SECRETS,会泄露明文: {missing}"


def test_bare_url_fields_redact_embedded_credentials(monkeypatch):
    s = _settings(
        monkeypatch,
        SENTINEL_WEBHOOK_URL="https://user:supersecretpw99@hook.example.com/path",
        SENTINEL_NTFY_URL="https://ntfy.example.com/topic?token=Zxy987Wvu654Tsr",
    )
    inv = settings_view.build_config_inventory(s, llm_config=None)
    rows = {r["env"]: r for g in inv["groups"] for r in g["rows"] if r.get("env")}
    assert "supersecretpw99" not in rows["SENTINEL_WEBHOOK_URL"]["value"]
    assert "Zxy987Wvu654Tsr" not in rows["SENTINEL_NTFY_URL"]["value"]


def test_secret_row_value_is_none_whether_configured_or_not(monkeypatch):
    # configured=True 和 configured=False 两种情况下,secret 行的 value 都必须是 None
    s = _settings(monkeypatch, SENTINEL_DIAG_TOKEN="sometoken-abc123def456")
    inv = settings_view.build_config_inventory(s, llm_config=None)
    rows = {r["env"]: r for g in inv["groups"] for r in g["rows"] if r.get("env")}
    assert rows["SENTINEL_DIAG_TOKEN"]["value"] is None  # 已配置
    assert rows["SENTINEL_DIAG_TOKEN"]["configured"] is True
    assert rows["SENTINEL_WEBHOOK_TOKEN"]["value"] is None  # 未配置
    assert rows["SENTINEL_WEBHOOK_TOKEN"]["configured"] is False
