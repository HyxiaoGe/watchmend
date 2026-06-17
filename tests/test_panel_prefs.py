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
    assert prefs.resolve_window(None, None, history_days=90) == 90  # 不传 default → 上限(兼容)
    assert prefs.resolve_window("999", None, history_days=90) == 90  # 非法 → 回退
    # history_days=30 时，30 即唯一上限
    assert prefs.resolve_window("90", None, history_days=30) == 30
    # 配置默认(在允许集内)：无 query/cookie 时采用
    assert prefs.resolve_window(None, None, history_days=90, default=30) == 30
    assert prefs.resolve_window("90", None, history_days=90, default=30) == 90  # query 仍优先
    assert prefs.resolve_window(None, "30", history_days=90, default=90) == 30  # cookie 优先于默认
    assert prefs.resolve_window(None, None, history_days=90, default=999) == 90  # 默认越界→回退上限


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


def test_resolve_refresh_query_over_cookie_over_default():
    assert prefs.resolve_refresh("15", "60", 30) == 15  # query 优先
    assert prefs.resolve_refresh(None, "60", 30) == 60  # 退 cookie
    assert prefs.resolve_refresh(None, None, 30) == 30  # 退服务端默认
    assert prefs.resolve_refresh("0", None, 30) == 0  # 0 = 关闭,合法


def test_resolve_refresh_rejects_out_of_allowlist():
    assert prefs.resolve_refresh("999", None, 30) == 30
    assert prefs.resolve_refresh("default", None, 30) == 30
    assert prefs.resolve_refresh("abc", "777", 45) == 45  # 都非法 → 服务端默认(原样)
    assert prefs.resolve_refresh(None, None, 45) == 45  # 服务端默认可为允许集外任意值


def _set_cookie_headers(resp: Response) -> str:
    # Starlette Response 的 Headers 用 getlist(httpx 才是 get_list);多条 Set-Cookie 各成一行
    return "\n".join(resp.headers.getlist("set-cookie"))


def test_apply_pref_cookies_sets_refresh():
    resp = Response()
    prefs.apply_pref_cookies(resp, refresh=15)
    assert "wm_refresh=15" in _set_cookie_headers(resp)


def test_apply_pref_cookies_clear_deletes_named_cookies():
    resp = Response()
    # clear 优先于 set:同一偏好既在 clear 又给值时,删除(delete_cookie → Max-Age=0)
    prefs.apply_pref_cookies(resp, lang="zh", refresh=30, clear=("wm_lang", "wm_refresh"))
    joined = _set_cookie_headers(resp)
    assert "wm_lang=" in joined
    # 删除语义:wm_lang / wm_refresh 都带 Max-Age=0
    assert joined.count("Max-Age=0") == 2


def test_apply_pref_cookies_backward_compatible():
    # 老调用(仅 lang/theme/window)行为不变:只 SET 显式非 None 项
    resp = Response()
    prefs.apply_pref_cookies(resp, lang="en", theme=None, window=30)
    joined = _set_cookie_headers(resp)
    assert "wm_lang=en" in joined
    assert "wm_win=30" in joined
    assert "wm_theme" not in joined
