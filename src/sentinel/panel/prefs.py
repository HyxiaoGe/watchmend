# src/sentinel/panel/prefs.py
"""证据台偏好解析（lang/theme/window/page）+ Set-Cookie 写回。
来源优先级 querystring > cookie > (lang 额外回退 Accept-Language) > 内置默认（见 spec §3）。
偏好必须跨 30s meta 重载存活，故写 cookie；分页是瞬时态只走 querystring，不落 cookie。
纯函数 + Starlette Response，无 FastAPI Request 依赖，便于单测。"""

from __future__ import annotations

from starlette.responses import Response

from sentinel.panel.i18n import resolve_lang as _i18n_resolve_lang

_THEMES = ("dark", "light", "system")
_COOKIE_MAX_AGE = 365 * 24 * 3600  # 1 年
_REFRESH_ALLOWED = {0, 15, 30, 60}  # 浏览器可选的自动刷新值;0=关闭


def resolve_lang(
    query: str | None,
    cookie: str | None,
    accept_language: str | None,
    *,
    default: str | None = None,
) -> str:
    return _i18n_resolve_lang(query, cookie, accept_language, default=default)


def resolve_theme(query: str | None, cookie: str | None, default: str) -> str:
    for cand in (query, cookie):
        if cand and cand.strip().lower() in _THEMES:
            return cand.strip().lower()
    return default


def resolve_window(
    query: str | None, cookie: str | None, *, history_days: int, default: int | None = None
) -> int:
    """允许集 = {30, history_days}；非法 query/cookie 落到配置默认 default（仍须在允许集内，
    否则回退 history_days）。不传 default 时回退 history_days（向后兼容老调用/单测）。"""
    allowed = {"30", str(history_days)}
    for cand in (query, cookie):
        if cand and cand.strip() in allowed:
            return int(cand.strip())
    if default is not None and str(default) in allowed:
        return default
    return history_days


def resolve_page(query: str | None) -> int:
    """1-based 页码；非整/越下界钳到 1（越上界由 view 据总页数钳定）。"""
    try:
        return max(1, int(query))
    except (TypeError, ValueError):
        return 1


def resolve_refresh(query: str | None, cookie: str | None, server_default: int) -> int:
    """自动刷新间隔(秒):query > cookie > server_default。
    query/cookie 仅当为允许集 {0,15,30,60} 之一才采用('default' 哨兵/越界一律忽略);
    server_default 原样返回(操作员配置 ge>=5 的任意值,不受允许集约束)。"""
    for cand in (query, cookie):
        if cand is None:
            continue
        s = cand.strip()
        if s.isdigit() and int(s) in _REFRESH_ALLOWED:
            return int(s)
    return server_default


def apply_pref_cookies(
    response: Response,
    *,
    lang: str | None = None,
    theme: str | None = None,
    window: int | None = None,
    refresh: int | None = None,
    clear: tuple[str, ...] = (),
) -> None:
    """显式非 None 的偏好 → SET（1 年期，SameSite=Lax，Path=/）；
    出现在 clear 里的 cookie 名 → DELETE（Max-Age=0），且 clear 优先于同名 SET。
    调用方：普通页面某偏好出现在 querystring 时才传该项；/settings 表单按用户选择决定 SET/CLEAR。
    cookie 非 HttpOnly（前端无需读，但保留 JS 可读以便将来 PE 增强）。"""
    clear_set = set(clear)

    def _put(name: str, value: str | None) -> None:
        if name in clear_set:
            response.delete_cookie(name, path="/")
        elif value is not None:
            response.set_cookie(name, value, max_age=_COOKIE_MAX_AGE, samesite="Lax", path="/")

    _put("wm_lang", lang)
    _put("wm_theme", theme)
    _put("wm_win", None if window is None else str(window))
    _put("wm_refresh", None if refresh is None else str(refresh))
