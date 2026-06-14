from sentinel.config import Settings


def test_panel_enabled_defaults_true():
    s = Settings(_env_file=None)
    assert s.sentinel_panel_enabled is True


def test_panel_can_be_disabled(monkeypatch):
    monkeypatch.setenv("SENTINEL_PANEL_ENABLED", "false")
    s = Settings(_env_file=None)
    assert s.sentinel_panel_enabled is False


def test_panel_redesign_field_defaults():
    s = Settings(_env_file=None)
    assert s.sentinel_panel_default_lang == ""
    assert s.sentinel_panel_default_theme == "system"
    assert s.sentinel_panel_history_days == 90
    assert s.sentinel_panel_page_size == 8
    assert s.sentinel_panel_services_cap == 20
    assert s.sentinel_panel_red_uptime_pct == 50.0
    assert s.sentinel_panel_partial_uptime_pct == 99.5
    assert s.sentinel_llm_lang == "zh"
    assert s.sentinel_event_feed_days == 30


def test_panel_redesign_fields_overridable(monkeypatch):
    monkeypatch.setenv("SENTINEL_PANEL_DEFAULT_THEME", "light")
    monkeypatch.setenv("SENTINEL_PANEL_HISTORY_DAYS", "30")
    monkeypatch.setenv("SENTINEL_LLM_LANG", "en")
    s = Settings(_env_file=None)
    assert s.sentinel_panel_default_theme == "light"
    assert s.sentinel_panel_history_days == 30
    assert s.sentinel_llm_lang == "en"
