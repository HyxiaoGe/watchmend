# tests/test_registry.py
from sentinel.registry import build_adapters


def test_builds_known_providers_in_order():
    adapters = build_adapters(["anthropic", "google_cloud"])
    assert [a.provider for a in adapters] == ["anthropic", "google_cloud"]


def test_cloudflare_disables_component_tracking():
    (cf,) = build_adapters(["cloudflare"])
    assert cf.track_components is False


def test_unknown_provider_is_skipped():
    adapters = build_adapters(["anthropic", "bogus"])
    assert [a.provider for a in adapters] == ["anthropic"]
