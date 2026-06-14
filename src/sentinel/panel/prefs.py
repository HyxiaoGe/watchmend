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


def apply_pref_cookies(
    response: Response,
    *,
    lang: str | None = None,
    theme: str | None = None,
    window: int | None = None,
) -> None:
    """仅对显式传入（非 None）的偏好写 Set-Cookie。调用方：某偏好出现在 querystring 时才传。
    cookie 非 HttpOnly（前端无需读，但保留 JS 可读以便将来 PE 增强），SameSite=Lax、Path=/。"""
    if lang is not None:
        response.set_cookie("wm_lang", lang, max_age=_COOKIE_MAX_AGE, samesite="Lax", path="/")
    if theme is not None:
        response.set_cookie("wm_theme", theme, max_age=_COOKIE_MAX_AGE, samesite="Lax", path="/")
    if window is not None:
        response.set_cookie(
            "wm_win", str(window), max_age=_COOKIE_MAX_AGE, samesite="Lax", path="/"
        )
