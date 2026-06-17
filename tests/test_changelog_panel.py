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


_NESTED_SAMPLE = """\
## [0.2.0] - 2026-06-14

### Added
- First public milestone, completing four capability areas on top of the
  0.1.x core:
  - **Environment auto-discovery**: zero-config detection of monitored
    containers via a read-only docker API.
  - **Multi-channel notifications**: Feishu + Telegram + ntfy.

[0.2.0]: https://github.com/HyxiaoGe/watchmend/compare/v0.1.0...v0.2.0
"""


def test_parse_nested_subbullets_become_separate_entries():
    # 真实 0.2.0 用缩进子弹列特性:不能压成一条 run-on,不能泄漏字面 "- **"
    added = parse_changelog(_NESTED_SAMPLE)[0].sections[0]
    assert added.category == "Added"
    assert len(added.entries) == 3  # 父句 + 两个子弹各成一条
    assert added.entries[0] == (
        "First public milestone, completing four capability areas on top of the 0.1.x core:"
    )
    assert added.entries[1].startswith("**Environment auto-discovery**:")
    assert added.entries[2].startswith("**Multi-channel notifications**:")
    # 子弹自身折行续行仍并入对应条目
    assert "read-only docker API." in added.entries[1]
    # 无字面子弹标记泄漏进任何条目
    for e in added.entries:
        assert "- **" not in e, f"字面子弹标记泄漏: {e!r}"


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
    assert "<script src" not in resp.text  # 无外部 JS(渐进增强:允许内联脚本)
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


async def test_version_chip_opens_modal_not_page(tmp_path, monkeypatch):
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    resp = await _get(app, "/")  # 任一含导航壳的页
    assert resp.status_code == 200
    # chip 是触发 :target 模态的锚,不再跳页(href 指向 #wm-changelog 而非 /changelog)
    assert 'class="ver mono' in resp.text
    assert 'href="#wm-changelog"' in resp.text
    store.close()


async def test_update_available_lives_in_chip_not_title(tmp_path, monkeypatch):
    # 更新提示移到版本 chip(updot + 模态横幅);tab 标题前缀让位给运行态(事故红灯)。
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    store.set_meta("latest_known_version", "v999.0.0")
    store.set_meta(
        "latest_release_url", "https://github.com/HyxiaoGe/watchmend/releases/tag/v999.0.0"
    )
    app = _build_app(store, settings)
    resp = await _get(app, "/")
    title = resp.text.split("<title>", 1)[1].split("</title>", 1)[0]
    assert "●" not in title  # 标题不再带更新标记
    assert "🔴" not in title  # 有新版 ≠ 有事故
    assert 'class="updot"' in resp.text  # chip 仍亮更新点
    store.close()


# ---- 版本 chip → 零-JS :target 居中模态(全量更新日志)+ changelog 面包屑 ----


async def test_version_chip_is_zero_js_target_modal(tmp_path, monkeypatch):
    # chip 锚触发 :target 模态;模态容器/遮罩/关闭锚就位;无 JS。
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    resp = await _get(app, "/")
    assert resp.status_code == 200
    assert 'href="#wm-changelog"' in resp.text  # chip 触发锚
    assert 'id="wm-changelog" class="vmodal"' in resp.text  # :target 模态容器
    assert 'class="vmodal-bg" href="#wm-close"' in resp.text  # 点外关闭(全屏遮罩 <a>)
    assert 'class="vmodal-x" href="#wm-close"' in resp.text  # × 关闭(与遮罩同锚)
    assert "vchev" in resp.text  # chip caret(暗示可展开)
    assert "<script src" not in resp.text  # 无外部 JS(渐进增强:允许内联脚本)
    store.close()


async def test_version_modal_embeds_full_changelog(tmp_path, monkeypatch):
    # 模态内联全量历史版本块(不再跳页);当前运行版本高亮。
    from sentinel import __version__
    from sentinel.panel.changelog import load_releases

    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))  # 无 latest_known_version → update_available False
    app = _build_app(store, settings)
    resp = await _get(app, "/?lang=zh")
    assert resp.status_code == 200
    assert "<title>● " not in resp.text  # 确无更新提示态
    versions = {r.version for r in load_releases("zh")}
    if len(versions) >= 2:
        # 模态嵌入了不止当前版本的历史块(全量 changelog 进了 / 页)
        present = sum(1 for v in versions if f"v{v}" in resp.text)
        assert present >= 2, f"模态只嵌了 {present} 个版本块,应为全量历史"
    if __version__ in versions:
        assert "当前版本" in resp.text  # changelog.current:当前运行版本高亮
    store.close()


async def test_version_modal_empty_changelog_degrades(tmp_path, monkeypatch):
    # changelog 定位失败(releases=[])→ 模态降级空态:不崩、200、显示 changelog.empty、仍零-JS。
    from sentinel.panel import changelog

    monkeypatch.setattr(changelog, "_resolve", lambda lang: None)
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    resp = await _get(app, "/?lang=zh")
    assert resp.status_code == 200
    assert 'id="wm-changelog" class="vmodal"' in resp.text  # 模态外壳仍在
    assert "暂无更新日志" in resp.text  # changelog.empty zh
    assert "<script src" not in resp.text  # 无外部 JS(渐进增强:允许内联脚本)
    store.close()


async def test_version_modal_has_standalone_page_link(tmp_path, monkeypatch):
    # 模态底部留「在独立页面打开 →」指向 /changelog(可分享/降级兜底,opt-in 非强制跳页)。
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    resp = await _get(app, "/?lang=zh")
    assert resp.status_code == 200
    assert 'class="vm-full" href="/changelog' in resp.text
    assert "在独立页面打开" in resp.text  # ver.fulllog
    store.close()


async def test_version_modal_update_banner_when_available(tmp_path, monkeypatch):
    # 有新版时模态顶部置更新横幅(版本 + 更新命令 + 发布说明链接)。
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    store.set_meta("latest_known_version", "v999.0.0")
    store.set_meta(
        "latest_release_url", "https://github.com/HyxiaoGe/watchmend/releases/tag/v999.0.0"
    )
    app = _build_app(store, settings)
    resp = await _get(app, "/?lang=zh")
    assert resp.status_code == 200
    assert 'class="vm-up"' in resp.text  # 更新横幅
    assert "999.0.0" in resp.text
    store.close()


async def test_changelog_page_has_breadcrumb(tmp_path, monkeypatch):
    # 整页不再是孤页:加面包屑回总览(与 event/service 详情页一致)。
    settings = _settings(monkeypatch)
    store = Store(str(tmp_path / "s.db"))
    app = _build_app(store, settings)
    resp = await _get(app, "/changelog?lang=zh")
    assert resp.status_code == 200
    assert 'class="crumb"' in resp.text
    assert 'href="/?' in resp.text or 'href="/"' in resp.text  # 面包屑回总览
    store.close()
