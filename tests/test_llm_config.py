# tests/test_llm_config.py
import pytest


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return str(path)


VALID = """\
active: deepseek
fallback: kimi
providers:
  deepseek:
    base_url: https://api.deepseek.com/v1
    model: deepseek-chat
    api_key_env: LLM_KEY_DEEPSEEK
  kimi:
    base_url: https://api.moonshot.cn/v1
    model: kimi-k2
    api_key_env: LLM_KEY_KIMI
"""


def test_parse_valid_resolves_active_and_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_KEY_DEEPSEEK", "sk-fake-ds")
    monkeypatch.setenv("LLM_KEY_KIMI", "sk-fake-kimi")
    from sentinel.llm_config import _parse_and_resolve

    reg = _parse_and_resolve(_write(tmp_path / "llm.yaml", VALID))
    assert reg.active.name == "deepseek"
    assert reg.active.base_url == "https://api.deepseek.com/v1"
    assert reg.active.model == "deepseek-chat"
    assert reg.active.api_key == "sk-fake-ds"
    assert reg.fallback.name == "kimi"
    assert reg.fallback.api_key == "sk-fake-kimi"


def test_api_key_env_unset_is_invalid(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_KEY_DEEPSEEK", raising=False)
    monkeypatch.setenv("LLM_KEY_KIMI", "sk-fake-kimi")
    from sentinel.llm_config import LLMConfigError, _parse_and_resolve

    with pytest.raises(LLMConfigError, match="api_key_env"):
        _parse_and_resolve(_write(tmp_path / "llm.yaml", VALID))


def test_inline_empty_api_key_is_valid(tmp_path):
    from sentinel.llm_config import _parse_and_resolve

    text = (
        "active: ollama\n"
        "providers:\n"
        "  ollama:\n"
        "    base_url: http://localhost:11434/v1\n"
        "    model: qwen3\n"
        '    api_key: ""\n'
    )
    reg = _parse_and_resolve(_write(tmp_path / "llm.yaml", text))
    assert reg.active.api_key == ""
    assert reg.fallback is None


def test_active_missing_provider_is_invalid(tmp_path):
    from sentinel.llm_config import LLMConfigError, _parse_and_resolve

    text = (
        "active: ghost\nproviders:\n  real:\n    base_url: http://x/v1\n"
        "    model: m\n    api_key: ''\n"
    )
    with pytest.raises(LLMConfigError, match="active"):
        _parse_and_resolve(_write(tmp_path / "llm.yaml", text))


def test_provider_missing_base_url_or_model_is_invalid(tmp_path):
    from sentinel.llm_config import LLMConfigError, _parse_and_resolve

    text = "active: a\nproviders:\n  a:\n    model: m\n    api_key: ''\n"
    with pytest.raises(LLMConfigError, match="base_url"):
        _parse_and_resolve(_write(tmp_path / "llm.yaml", text))


def test_unsupported_protocol_is_invalid(tmp_path):
    from sentinel.llm_config import LLMConfigError, _parse_and_resolve

    text = (
        "active: a\nproviders:\n  a:\n    base_url: http://x/v1\n"
        "    model: m\n    protocol: anthropic\n    api_key: ''\n"
    )
    with pytest.raises(LLMConfigError, match="protocol"):
        _parse_and_resolve(_write(tmp_path / "llm.yaml", text))


def test_broken_fallback_kept_lenient(tmp_path, monkeypatch):
    # fallback 指向不存在的 provider → 忽略,active 仍工作(不抛)
    monkeypatch.setenv("LLM_KEY_DEEPSEEK", "sk-fake-ds")
    from sentinel.llm_config import _parse_and_resolve

    text = (
        "active: deepseek\nfallback: ghost\nproviders:\n"
        "  deepseek:\n    base_url: https://api.deepseek.com/v1\n"
        "    model: deepseek-chat\n    api_key_env: LLM_KEY_DEEPSEEK\n"
    )
    reg = _parse_and_resolve(_write(tmp_path / "llm.yaml", text))
    assert reg.active.name == "deepseek"
    assert reg.fallback is None


def test_fallback_resolve_error_kept_lenient(tmp_path, monkeypatch):
    # fallback 在 providers 里但本身无效(协议不支持)→ 忽略,active 仍工作(不抛)
    monkeypatch.setenv("LLM_KEY_DEEPSEEK", "sk-fake-ds")
    from sentinel.llm_config import _parse_and_resolve

    text = (
        "active: deepseek\nfallback: bad\nproviders:\n"
        "  deepseek:\n    base_url: https://api.deepseek.com/v1\n"
        "    model: deepseek-chat\n    api_key_env: LLM_KEY_DEEPSEEK\n"
        "  bad:\n    base_url: http://x/v1\n    model: m\n"
        "    protocol: anthropic\n    api_key: ''\n"
    )
    reg = _parse_and_resolve(_write(tmp_path / "llm.yaml", text))
    assert reg.active.name == "deepseek"
    assert reg.fallback is None


def test_both_keys_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_KEY_DEEPSEEK", "sk-from-env")
    from sentinel.llm_config import _parse_and_resolve

    text = (
        "active: deepseek\nproviders:\n  deepseek:\n"
        "    base_url: https://api.deepseek.com/v1\n    model: deepseek-chat\n"
        "    api_key_env: LLM_KEY_DEEPSEEK\n    api_key: sk-inline-should-be-ignored\n"
    )
    reg = _parse_and_resolve(_write(tmp_path / "llm.yaml", text))
    assert reg.active.api_key == "sk-from-env"


def test_yaml_syntax_error_raises(tmp_path):
    import yaml

    from sentinel.llm_config import _parse_and_resolve

    with pytest.raises(yaml.YAMLError):  # 未闭合 flow 序列 → ScannerError(YAMLError 子类)
        _parse_and_resolve(_write(tmp_path / "llm.yaml", "active: [unclosed\n"))
