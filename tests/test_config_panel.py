import pytest
from pydantic import ValidationError

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
    assert s.sentinel_panel_default_window == 30  # 首屏默认窗口降噪为 30（issue #11）
    assert s.sentinel_panel_page_size == 8
    assert s.sentinel_panel_red_uptime_pct == 50.0
    assert s.sentinel_panel_partial_uptime_pct == 99.5
    assert s.sentinel_llm_lang == "zh"
    assert s.sentinel_event_feed_days == 30
    assert s.sentinel_panel_overview_roster_cap == 6  # 总览服务表截断行数
    assert s.sentinel_panel_green_uptime_pct == 99.9  # ≥此 → 今日可用率绿(阈值色绿界)


def test_panel_redesign_fields_overridable(monkeypatch):
    monkeypatch.setenv("SENTINEL_PANEL_DEFAULT_THEME", "light")
    monkeypatch.setenv("SENTINEL_PANEL_HISTORY_DAYS", "30")
    monkeypatch.setenv("SENTINEL_LLM_LANG", "en")
    s = Settings(_env_file=None)
    assert s.sentinel_panel_default_theme == "light"
    assert s.sentinel_panel_history_days == 30
    assert s.sentinel_llm_lang == "en"


def test_overview_knobs_overridable(monkeypatch):
    monkeypatch.setenv("SENTINEL_PANEL_OVERVIEW_ROSTER_CAP", "8")
    monkeypatch.setenv("SENTINEL_PANEL_GREEN_UPTIME_PCT", "99.95")
    s = Settings(_env_file=None)
    assert s.sentinel_panel_overview_roster_cap == 8
    assert s.sentinel_panel_green_uptime_pct == 99.95


def test_panel_refresh_seconds_default():
    s = Settings(_env_file=None)
    assert s.sentinel_panel_refresh_seconds == 30  # 默认 30,行为不变


def test_panel_refresh_seconds_overridable(monkeypatch):
    monkeypatch.setenv("SENTINEL_PANEL_REFRESH_SECONDS", "45")
    s = Settings(_env_file=None)
    assert s.sentinel_panel_refresh_seconds == 45


def test_panel_refresh_seconds_floor(monkeypatch):
    # 下限 5s:防过频整页渲染打服务端;<5 启动即拒绝(逼操作员改对)
    monkeypatch.setenv("SENTINEL_PANEL_REFRESH_SECONDS", "3")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
