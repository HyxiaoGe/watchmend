# tests/test_changelog_panel.py
from sentinel.panel.changelog import Release, Section, parse_changelog

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
