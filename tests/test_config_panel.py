from sentinel.config import Settings


def test_panel_enabled_defaults_true():
    s = Settings(_env_file=None)
    assert s.sentinel_panel_enabled is True


def test_panel_can_be_disabled(monkeypatch):
    monkeypatch.setenv("SENTINEL_PANEL_ENABLED", "false")
    s = Settings(_env_file=None)
    assert s.sentinel_panel_enabled is False
