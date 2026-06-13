# tests/test_findings.py
from sentinel.findings import (
    DOCKER_OPEN_RULES,
    DOCKER_RULES,
    RULE_NAMES,
)


def test_docker_rules_have_display_names():
    # every docker rule + the scan-failure sentinel must render in cards/daily report
    for name in DOCKER_RULES:
        assert name in RULE_NAMES, f"{name} missing from RULE_NAMES"
    assert "scan_failed_docker" in RULE_NAMES


def test_docker_rules_membership():
    assert DOCKER_RULES == frozenset({"container_down", "container_unhealthy", "container_oom"})


def test_docker_open_rules_subset_excludes_oom():
    # open rules drive recovery; oom is a point event so it must NOT be here
    assert DOCKER_OPEN_RULES <= DOCKER_RULES
    assert "container_oom" not in DOCKER_OPEN_RULES
    assert DOCKER_OPEN_RULES == frozenset({"container_down", "container_unhealthy"})
