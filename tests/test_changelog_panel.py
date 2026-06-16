# tests/test_changelog_panel.py
import tomllib
from pathlib import Path

from sentinel.panel.changelog import Release, Section, parse_changelog

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
