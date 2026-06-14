# tests/test_llm_config_cli.py
YAML_WITH_COMMENT = """\
# 我的 LLM 注册表(顶部注释)
active: deepseek   # 当前诊断器
fallback: kimi
providers:
  # deepseek 段注释
  deepseek:
    base_url: https://api.deepseek.com/v1
    model: deepseek-chat
    api_key_env: LLM_KEY_DEEPSEEK
  kimi:
    base_url: https://api.moonshot.cn/v1
    model: kimi-k2
    api_key_env: LLM_KEY_KIMI
"""


def test_switch_rewrites_active_and_preserves_comments(tmp_path):
    from sentinel.llm_config import cmd_switch

    path = tmp_path / "llm.yaml"
    path.write_text(YAML_WITH_COMMENT, encoding="utf-8")
    rc = cmd_switch(str(path), "kimi")
    assert rc == 0
    text = path.read_text(encoding="utf-8")
    assert "active: kimi" in text
    assert "active: deepseek" not in text
    assert "# 我的 LLM 注册表(顶部注释)" in text  # 其它注释保留
    assert "# deepseek 段注释" in text


def test_switch_unknown_provider_fails_no_write(tmp_path, capsys):
    from sentinel.llm_config import cmd_switch

    path = tmp_path / "llm.yaml"
    path.write_text(YAML_WITH_COMMENT, encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    rc = cmd_switch(str(path), "ghost")
    assert rc == 1
    assert path.read_text(encoding="utf-8") == before  # 未改文件
    err = capsys.readouterr().err
    assert "ghost" in err and "deepseek" in err  # 列出可用名


def test_list_shows_active_and_key_status(tmp_path, monkeypatch, capsys):
    from sentinel.config import Settings
    from sentinel.llm_config import cmd_list

    monkeypatch.setenv("FEISHU_VENDOR_WEBHOOK", "https://open.feishu.cn/hook/T")
    monkeypatch.setenv("LLM_KEY_DEEPSEEK", "sk-fake")
    monkeypatch.delenv("LLM_KEY_KIMI", raising=False)
    path = tmp_path / "llm.yaml"
    path.write_text(YAML_WITH_COMMENT, encoding="utf-8")
    monkeypatch.setenv("SENTINEL_LLM_CONFIG_FILE", str(path))
    rc = cmd_list(str(path), Settings(_env_file=None))
    out = capsys.readouterr().out
    assert rc == 0
    assert "deepseek" in out and "active" in out
    assert "✓" in out and "✗" in out  # deepseek key 在、kimi key 缺


def test_build_provider_entry_env_mode_has_no_key():
    from sentinel.llm_config import build_provider_entry

    entry = build_provider_entry(
        {"base_url": "http://x/v1", "model": "m", "key_mode": "env", "api_key_env": "LLM_KEY_X"}
    )
    assert entry == {"base_url": "http://x/v1", "model": "m", "api_key_env": "LLM_KEY_X"}
    assert "api_key" not in entry  # env 模式绝不落 key


def test_merge_provider_env_mode_never_writes_secret(tmp_path, monkeypatch):
    from sentinel.llm_config import _merge_provider, build_provider_entry

    # 真把一个假 key 设进环境:env 模式只引用变量名,这个值必须永不落盘。
    monkeypatch.setenv("LLM_KEY_X", "sk-realsecret-should-never-hit-disk")
    path = tmp_path / "llm.yaml"
    entry = build_provider_entry(
        {"base_url": "http://x/v1", "model": "m", "key_mode": "env", "api_key_env": "LLM_KEY_X"}
    )
    _merge_provider(str(path), "x", entry, set_active=True)
    text = path.read_text(encoding="utf-8")
    assert "LLM_KEY_X" in text  # 写的是变量名
    assert "active: x" in text
    assert "sk-realsecret-should-never-hit-disk" not in text  # 环境里的真 key 绝不落盘
    assert "sk-" not in text
