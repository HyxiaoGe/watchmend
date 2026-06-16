# tests/test_changelog_panel.py
import tomllib
from pathlib import Path

import httpx
from fastapi import FastAPI

from sentinel.config import Settings
from sentinel.panel.changelog import Release, Section, parse_changelog
from sentinel.panel.routes import register_panel_routes
from sentinel.store import Store

_ROOT = Path(__file__).resolve().parents[1]
_CHANGELOGS = ("CHANGELOG.md", "CHANGELOG.zh-CN.md")

_SAMPLE = """\
# Changelog

## [Unreleased]

## [0.2.0] - 2026-06-14

### Added
- First public milestone, completing four capability areas on top of the
  0.1.x detection core.
- Multi-channel notifications.

### Changed
- Internal package name retained.

## [0.1.0] - 2026-06-12

### Security
- Secret-leak redaction.

[Unreleased]: https://github.com/HyxiaoGe/watchmend/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/HyxiaoGe/watchmend/compare/v0.1.0...v0.2.0
"""


def test_parse_skips_unreleased_and_footer():
    rels = parse_changelog(_SAMPLE)
    assert [r.version for r in rels] == ["0.2.0", "0.1.0"]  # 跳过 [Unreleased],footer 不入


def test_parse_extracts_date_and_sections():
    rels = parse_changelog(_SAMPLE)
    r0 = rels[0]
    assert r0.version == "0.2.0"
    assert r0.date == "2026-06-14"
    assert [s.category for s in r0.sections] == ["Added", "Changed"]


def test_parse_merges_continuation_lines():
    rels = parse_changelog(_SAMPLE)
    added = rels[0].sections[0]
    assert added.category == "Added"
    assert added.entries[0] == (
        "First public milestone, completing four capability areas on top of "
        "the 0.1.x detection core."
    )
    assert added.entries[1] == "Multi-channel notifications."


def test_parse_returns_dataclasses():
    rels = parse_changelog(_SAMPLE)
    assert isinstance(rels[0], Release)
    assert isinstance(rels[0].sections[0], Section)


def test_parse_empty_text():
    assert parse_changelog("") == []


def test_resolve_finds_source_tree_changelog():
    # 源码树(无 wheel):回落仓库根的真实 CHANGELOG
    from sentinel.panel.changelog import _resolve

    assert _resolve("en") is not None and _resolve("en").name == "CHANGELOG.md"
    assert _resolve("zh") is not None and _resolve("zh").name == "CHANGELOG.zh-CN.md"
    # 未知 lang 回落 en
    assert _resolve("de").name == "CHANGELOG.md"


def test_load_releases_includes_pyproject_version():
    # 运行版本必须能解析出对应版本块(防 bump 后渲染不出当前块)
    import tomllib
    from pathlib import Path

    from sentinel.panel.changelog import load_releases

    pv = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    for lang in ("en", "zh"):
        versions = {r.version for r in load_releases(lang)}
        assert pv in versions, f"{lang}: changelog 缺当前版本 {pv}"


def test_load_releases_missing_returns_empty(monkeypatch):
    # 文件定位失败 → [](页面降级空态,不抛错)
    from sentinel.panel import changelog

    monkeypatch.setattr(changelog, "_resolve", lambda lang: None)
    assert changelog.load_releases("en") == []


def test_pyproject_force_includes_changelogs():
    cfg = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    fi = cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    for name in _CHANGELOGS:
        assert fi.get(name) == f"sentinel/_changelog/{name}", f"force-include 缺 {name}"


def test_dockerfile_copies_changelogs_before_install():
    lines = (_ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
    copy_idx = next(
        (
            i
            for i, ln in enumerate(lines)
            if ln.startswith("COPY") and all(n in ln for n in _CHANGELOGS)
        ),
        None,
    )
    install_idx = next(
        (i for i, ln in enumerate(lines) if "uv pip install" in ln and ln.rstrip().endswith(".")),
        None,
    )
    assert copy_idx is not None, "Dockerfile 未 COPY 两个 changelog"
    assert install_idx is not None, "未找到 wheel 安装行"
    assert copy_idx < install_idx, "CHANGELOG COPY 必须在 wheel 安装之前"


def _settings(monkeypatch, **env):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def _build_app(store, settings):
    app = FastAPI()
    app.state.store = store
    app.state.settings = settings
    app.state.docker = None
    register_panel_routes(app)
    return app


async def _get(app, path):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get(path)


async def test_changelog_page_renders_zero_js(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    resp = await _get(app, "/changelog")
    assert resp.status_code == 200
    assert "<script" not in resp.text  # 零-JS 铁律
    assert "v0.1.0" in resp.text  # 含历史版本块
    assert "</html>" in resp.text  # 渲染了 _base 外壳
    store.close()


async def test_changelog_marks_current_version(tmp_path, monkeypatch):
    from sentinel import __version__
    from sentinel.panel.changelog import load_releases

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    resp = await _get(app, "/changelog?lang=zh")
    assert resp.status_code == 200
    if __version__ in {r.version for r in load_releases("zh")}:
        assert "当前版本" in resp.text  # changelog.current 标记(运行版本在 changelog 中)
    store.close()


async def test_changelog_respects_lang(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    resp = await _get(app, "/changelog?lang=en")
    assert resp.status_code == 200
    assert "Changelog" in resp.text  # changelog.title(en)
    store.close()


async def test_version_pill_links_to_changelog(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    resp = await _get(app, "/")  # 任一含导航壳的页
    assert resp.status_code == 200
    assert 'href="/changelog' in resp.text  # 版本胶囊入口(带 querystring)
    store.close()


async def test_title_marker_when_update_available(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    store.set_meta("latest_known_version", "v999.0.0")
    store.set_meta(
        "latest_release_url", "https://github.com/HyxiaoGe/watchmend/releases/tag/v999.0.0"
    )
    app = _build_app(store, settings)
    resp = await _get(app, "/")
    assert "<title>● " in resp.text  # 后台 tab 标签标记
    store.close()


async def test_no_title_marker_when_current(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))  # 无 latest_known_version → update_available False
    app = _build_app(store, settings)
    resp = await _get(app, "/")
    assert "<title>● " not in resp.text
    store.close()
