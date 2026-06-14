from starlette.responses import Response

from sentinel.panel import prefs


def _set_cookies(resp: Response) -> list[str]:
    return [v.decode() for k, v in resp.raw_headers if k == b"set-cookie"]


def test_resolve_lang_delegates_to_i18n():
    assert prefs.resolve_lang("en", None, "zh-CN") == "en"
    assert prefs.resolve_lang(None, None, None) == "zh"


def test_resolve_theme():
    assert prefs.resolve_theme("light", "dark", "system") == "light"
    assert prefs.resolve_theme(None, "dark", "system") == "dark"
    assert prefs.resolve_theme(None, None, "system") == "system"
    assert prefs.resolve_theme("BOGUS", None, "system") == "system"  # 非法 → 默认
    assert prefs.resolve_theme("DARK", None, "system") == "dark"  # 大小写不敏感


def test_resolve_window():
    assert prefs.resolve_window("30", None, history_days=90) == 30
    assert prefs.resolve_window("90", None, history_days=90) == 90
    assert prefs.resolve_window(None, "30", history_days=90) == 30
    assert prefs.resolve_window(None, None, history_days=90) == 90  # 默认 = 上限
    assert prefs.resolve_window("999", None, history_days=90) == 90  # 非法 → 默认
    # history_days=30 时，30 即唯一上限
    assert prefs.resolve_window("90", None, history_days=30) == 30


def test_resolve_page():
    assert prefs.resolve_page("3") == 3
    assert prefs.resolve_page(None) == 1
    assert prefs.resolve_page("0") == 1  # 钳到 ≥1
    assert prefs.resolve_page("-5") == 1
    assert prefs.resolve_page("abc") == 1  # 非整 → 1


def test_apply_pref_cookies_sets_only_provided():
    resp = Response()
    prefs.apply_pref_cookies(resp, lang="en")
    cookies = _set_cookies(resp)
    assert any(c.startswith("wm_lang=en") for c in cookies)
    assert all("wm_theme" not in c for c in cookies)
    assert all("wm_win" not in c for c in cookies)
    assert any("SameSite=Lax" in c for c in cookies)
    assert any("Max-Age=" in c for c in cookies)
    assert any("Path=/" in c for c in cookies)


def test_apply_pref_cookies_multiple():
    resp = Response()
    prefs.apply_pref_cookies(resp, theme="light", window=30)
    cookies = _set_cookies(resp)
    assert any(c.startswith("wm_theme=light") for c in cookies)
    assert any(c.startswith("wm_win=30") for c in cookies)
    assert all("wm_lang" not in c for c in cookies)


def test_apply_pref_cookies_noop_when_all_none():
    resp = Response()
    prefs.apply_pref_cookies(resp)
    assert _set_cookies(resp) == []
