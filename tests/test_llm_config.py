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


def _settings(monkeypatch, **env):
    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from sentinel.config import Settings

    return Settings(_env_file=None)


def test_env_fallback_when_yaml_absent(tmp_path, monkeypatch):
    from sentinel.llm_config import LLMConfig

    s = _settings(
        monkeypatch,
        SENTINEL_LLM_CONFIG_FILE=str(tmp_path / "absent.yaml"),
        LLM_BASE_URL="http://llm.test/v1",
        LLM_MODEL="m",
        LLM_API_KEY="sk-env",
    )
    cfg = LLMConfig(s)
    prof = cfg.current()
    assert cfg.enabled is True
    assert prof.name == "env"
    assert prof.base_url == "http://llm.test/v1"
    assert prof.model == "m"
    assert prof.api_key == "sk-env"
    assert cfg.fallback() is None


def test_disabled_when_yaml_absent_and_env_empty(tmp_path, monkeypatch):
    from sentinel.llm_config import LLMConfig

    s = _settings(monkeypatch, SENTINEL_LLM_CONFIG_FILE=str(tmp_path / "absent.yaml"))
    cfg = LLMConfig(s)
    assert cfg.current() is None
    assert cfg.enabled is False


def test_hot_reload_on_mtime_change(tmp_path, monkeypatch):
    import os

    from sentinel.llm_config import LLMConfig

    path = tmp_path / "llm.yaml"
    monkeypatch.setenv("LLM_KEY_A", "ka")
    monkeypatch.setenv("LLM_KEY_B", "kb")

    def write(active):
        path.write_text(
            f"active: {active}\nproviders:\n"
            "  a:\n    base_url: http://a/v1\n    model: ma\n    api_key_env: LLM_KEY_A\n"
            "  b:\n    base_url: http://b/v1\n    model: mb\n    api_key_env: LLM_KEY_B\n",
            encoding="utf-8",
        )

    s = _settings(monkeypatch, SENTINEL_LLM_CONFIG_FILE=str(path))
    cfg = LLMConfig(s)
    write("a")
    os.utime(path, (1000, 1000))
    assert cfg.current().name == "a"
    write("b")
    os.utime(path, (2000, 2000))
    assert cfg.current().name == "b"


def test_hot_reload_broken_keeps_last_good(tmp_path, monkeypatch):
    import os

    from sentinel.llm_config import LLMConfig

    path = tmp_path / "llm.yaml"
    monkeypatch.setenv("LLM_KEY_A", "ka")
    good = (
        "active: a\nproviders:\n  a:\n    base_url: http://a/v1\n"
        "    model: ma\n    api_key_env: LLM_KEY_A\n"
    )
    path.write_text(good, encoding="utf-8")
    os.utime(path, (1000, 1000))
    s = _settings(monkeypatch, SENTINEL_LLM_CONFIG_FILE=str(path))
    cfg = LLMConfig(s)
    assert cfg.current().name == "a"
    path.write_text("active: [broken\n", encoding="utf-8")  # 半截/坏 yaml
    os.utime(path, (2000, 2000))
    assert cfg.current().name == "a"  # 保留 last-good,不应用坏配置


def test_broken_at_startup_disables(tmp_path, monkeypatch):
    from sentinel.llm_config import LLMConfig

    path = tmp_path / "llm.yaml"
    path.write_text("active: a\nproviders: {}\n", encoding="utf-8")  # active 指向空 providers
    s = _settings(monkeypatch, SENTINEL_LLM_CONFIG_FILE=str(path))
    cfg = LLMConfig(s)
    assert cfg.current() is None  # 启动即坏 → 关层(fail-safe)


def test_example_yaml_parses(monkeypatch):
    from pathlib import Path

    from sentinel.llm_config import _parse_and_resolve

    monkeypatch.setenv("LLM_API_KEY_DEEPSEEK", "sk-fake-ds")
    monkeypatch.setenv("LLM_API_KEY_KIMI", "sk-fake-kimi")
    example = Path(__file__).resolve().parent.parent / "llm.example.yaml"
    reg = _parse_and_resolve(str(example))
    assert reg.active.name == "deepseek"
    assert reg.fallback.name == "kimi"
