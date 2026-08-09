import re

from sentinel.config import Settings
from sentinel.panel import settings_view
from sentinel.panel.settings_view import _SECRETS

# 裸端点 URL:凭证在配对的 *_token 字段里,URL 本身过 redact 兜底,故有意不列为 secret
_URL_ALLOWLIST = {"sentinel_webhook_url", "sentinel_ntfy_url"}
# 凭证形态字段名启发式:命中即必须登记进 _SECRETS(裸 URL 例外见 _URL_ALLOWLIST)。
# 覆盖未来可能的命名(pat/auth/bearer/cookie/apikey/client_id/cred…),让遗漏登记
# 变成红测试而非静默泄露明文;'pat' 按 snake_case 词界锚定,避免误伤 db_path / *_pattern。
_CRED_RE = re.compile(
    r"(secret|token|key|apikey|password|passwd|pwd|sign|credential|cred|"
    r"webhook|auth|bearer|cookie|client[_-]?id|(?:^|_)pat(?:_|$))",
    re.I,
)


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
        SENTINEL_CODEX_INGEST_TOKEN="codexsecret-abc123def456",
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
    codex = rows["SENTINEL_CODEX_INGEST_TOKEN"]
    assert codex["secret"] is True and codex["configured"] is True
    assert "codexsecret" not in (codex["value"] or "")


def test_inventory_unset_secret_shows_not_configured(monkeypatch):
    s = _settings(monkeypatch)
    inv = settings_view.build_config_inventory(s, llm_config=None)
    rows = {r["env"]: r for g in inv["groups"] for r in g["rows"] if r.get("env")}
    assert rows["SENTINEL_WEBHOOK_TOKEN"]["configured"] is False


def test_inventory_redacts_embedded_secret_in_nonsecret_value(monkeypatch):
    # 非密钥字段意外内嵌 API-key 形态 → redact use_patterns 兜底脱敏。
    # 用 AWS 形态(AKIA…)而非 OpenAI 形态:redact 两者都能脱,但后者会触发发布期 leak_check 门禁。
    embedded = "AKIAIOSFODNN7EXAMPLE"
    s = _settings(monkeypatch, SENTINEL_PROMETHEUS_URL=f"https://x/?k={embedded}")
    inv = settings_view.build_config_inventory(s, llm_config=None)
    rows = {r["env"]: r for g in inv["groups"] for r in g["rows"] if r.get("env")}
    assert embedded not in rows["SENTINEL_PROMETHEUS_URL"]["value"]


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


def test_credential_name_heuristic_catches_future_shapes():
    """加固守卫:未来若新增这些命名的凭证字段,必须被识别为凭证形态——
    否则会绕过 _SECRETS 登记、静默泄露明文。同时普通字段不得被误伤。"""
    must_flag = [
        "github_pat",
        "gitlab_pat",
        "session_cookie",
        "basic_auth",
        "oauth_client_id",
        "service_apikey",
        "x_api_creds",
        "llm_bearer",
    ]
    for name in must_flag:
        assert _CRED_RE.search(name), f"凭证形态未被识别: {name}"
    must_not_flag = [
        "sentinel_db_path",
        "sentinel_telegram_chat_id",
        "sentinel_poll_interval",
        "sentinel_providers",
        "sentinel_panel_history_days",
        "sentinel_update_check_url",
    ]
    for name in must_not_flag:
        assert not _CRED_RE.search(name), f"普通字段被误判为凭证: {name}"


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


def test_field_meta_covers_every_grouped_field():
    """每个登记进 _GROUPS 的字段都必须有中/英友好名 + 说明——
    新增字段忘配元数据会变红,而不是在页面上裸露 env 名。"""
    from sentinel.panel.settings_view import _FIELD_META, _GROUPS

    grouped = {name for _, names in _GROUPS for name in names}
    missing = grouped - set(_FIELD_META)
    assert not missing, f"_FIELD_META 缺这些字段的友好名: {missing}"
    ghost = set(_FIELD_META) - {n for n in Settings.model_fields}
    assert not ghost, f"_FIELD_META 有幽灵字段(不在 Settings): {ghost}"
    for name, meta in _FIELD_META.items():
        for lang in ("zh", "en"):
            assert lang in meta, f"{name} 缺 {lang} 友好名"
            label, desc = meta[lang]
            assert label and desc, f"{name}.{lang} 友好名/说明不得为空"


def test_inventory_rows_carry_friendly_label_distinct_from_env(monkeypatch):
    # 每个真实字段行都带小写 field + 友好名 label + 说明;label 不是裸 env 名。
    s = _settings(monkeypatch)
    inv = settings_view.build_config_inventory(s, llm_config=None, lang="zh")
    rows = [r for g in inv["groups"] for r in g["rows"] if r.get("env")]
    assert rows
    for r in rows:
        assert r["field"] and r["field"] == r["env"].lower()
        assert r["label"] and r["label"] != r["env"]
        assert "desc" in r


def test_inventory_label_follows_lang(monkeypatch):
    s = _settings(monkeypatch)
    zh = settings_view.build_config_inventory(s, llm_config=None, lang="zh")
    en = settings_view.build_config_inventory(s, llm_config=None, lang="en")
    zh_rows = {r["field"]: r for g in zh["groups"] for r in g["rows"] if r.get("field")}
    en_rows = {r["field"]: r for g in en["groups"] for r in g["rows"] if r.get("field")}
    assert zh_rows["sentinel_disk_usage_pct"]["label"] == "磁盘水位阈值"
    assert en_rows["sentinel_disk_usage_pct"]["label"] == "Disk usage threshold"


def test_display_prefs_lang_auto_when_no_cookie():
    d = settings_view.build_display_prefs(
        lang_eff="zh",
        lang_cookie=None,
        theme_eff="dark",
        window_eff=30,
        history_days=90,
        refresh_eff=30,
        refresh_cookie=None,
        server_refresh=30,
    )
    assert d["lang"]["selected"] == "auto"  # 无 cookie → 自动
    assert d["theme"]["selected"] == "dark"
    assert d["window"]["selected"] == 30
    assert d["window"]["options"] == [30, 90]
    assert d["refresh"]["selected"] == "default"  # 无 cookie → 默认
    assert d["refresh"]["server"] == 30


def test_display_prefs_explicit_cookie_selected():
    d = settings_view.build_display_prefs(
        lang_eff="en",
        lang_cookie="en",
        theme_eff="light",
        window_eff=90,
        history_days=90,
        refresh_eff=15,
        refresh_cookie="15",
        server_refresh=30,
    )
    assert d["lang"]["selected"] == "en"
    assert d["refresh"]["selected"] == "15"
    assert d["window"]["selected"] == 90


def test_display_prefs_invalid_cookie_falls_to_neutral():
    # 非法 cookie 值(不在受支持集合)回落到中性选项
    d = settings_view.build_display_prefs(
        lang_eff="zh",
        lang_cookie="fr",
        theme_eff="system",
        window_eff=30,
        history_days=90,
        refresh_eff=30,
        refresh_cookie="999",
        server_refresh=30,
    )
    assert d["lang"]["selected"] == "auto"
    assert d["refresh"]["selected"] == "default"


def test_settings_i18n_keys_present_in_both_langs():
    from sentinel.panel import i18n

    keys = [
        # KEEP THIS LIST IN SYNC WITH THE KEYS settings.html ACTUALLY USES
        "nav.settings",
        "set.title",
        "set.prefs",
        "set.prefs_hint",
        "set.inventory",
        "set.inventory_hint",
        "set.configured",
        "set.not_configured",
        "set.change_llm",
        "set.save",
        "set.f_lang",
        "set.f_theme",
        "set.f_window",
        "set.f_refresh",
        "set.lang_auto",
        "set.refresh_default",
        "set.refresh_off",
        "set.llm_active",
        "set.llm_fallback",
        "set.llm_active_hint",
        "set.llm_fallback_hint",
        "set.g_probe",
        "set.g_datasource",
        "set.g_channels",
        "set.g_llm",
        "set.g_docker",
        "set.g_panel",
        "set.g_backup_cert",
        "set.g_security",
    ]
    for k in keys:
        assert k in i18n.MESSAGES["zh"], f"zh 缺 {k}"
        assert k in i18n.MESSAGES["en"], f"en 缺 {k}"
