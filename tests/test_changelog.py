"""Guard tests for the bilingual changelog + release docs.

Pure-filesystem checks (no app, no network, no fixtures): the two changelog
files must stay structurally mirrored, and the latest *released* version block
must match the version declared in pyproject.toml.
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHANGELOG_EN = ROOT / "CHANGELOG.md"
CHANGELOG_ZH = ROOT / "CHANGELOG.zh-CN.md"
RELEASING = ROOT / "RELEASING.md"
PYPROJECT = ROOT / "pyproject.toml"

_VERSION_HEADER = re.compile(r"^## \[(\d+\.\d+\.\d+)\]", re.MULTILINE)


def _versions(path: Path) -> list[str]:
    """Released version strings in document order (skips [Unreleased])."""
    return _VERSION_HEADER.findall(path.read_text(encoding="utf-8"))


def _pyproject_version() -> str:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]


def test_changelog_files_exist():
    for p in (CHANGELOG_EN, CHANGELOG_ZH, RELEASING):
        assert p.is_file(), f"missing {p.name}"


def test_changelog_versions_parity():
    # 两份变更日志的已发布版本集合必须完全一致(防补一边漏一边)
    en, zh = set(_versions(CHANGELOG_EN)), set(_versions(CHANGELOG_ZH))
    assert en == zh, f"changelog drift: en-only={en - zh}, zh-only={zh - en}"


def test_latest_changelog_matches_pyproject():
    # 最新已发布块必须等于 pyproject 版本(防 bump 版本却忘剪 CHANGELOG)
    pv = _pyproject_version()
    for path in (CHANGELOG_EN, CHANGELOG_ZH):
        versions = _versions(path)
        assert versions, f"{path.name} has no released version block"
        assert versions[0] == pv, f"{path.name} latest={versions[0]} != pyproject={pv}"


def test_changelog_entries_descending():
    # 版本块按语义版本降序(最新在上)
    for path in (CHANGELOG_EN, CHANGELOG_ZH):
        versions = _versions(path)
        keys = [tuple(int(p) for p in v.split(".")) for v in versions]
        assert keys == sorted(keys, reverse=True), f"{path.name} not descending: {versions}"
