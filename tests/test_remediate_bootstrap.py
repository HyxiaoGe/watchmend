from pathlib import Path

BOOTSTRAP = Path(__file__).resolve().parent.parent / "host" / "recovery" / "remediate-bootstrap.md"


def test_bootstrap_exists_and_locks_down():
    assert BOOTSTRAP.exists(), "缺少 remediate-bootstrap.md"
    text = BOOTSTRAP.read_text(encoding="utf-8")
    # 必须点明唯一职责与脚本目录(占位形式,部署时整体替换为实际绝对路径)
    assert "<RECOVERY_DIR>" in text
    assert "sentinel-restart.sh" in text
    # 必须带部署替换说明,否则占位符会原样喂给 agent
    assert "替换" in text
    # 必须明令禁止主要逃逸/注入手段
    for forbidden in ("bash -c", "sh -c", "eval", "$()", "&&", "||", "管道", "分号"):
        assert forbidden in text, f"bootstrap 未声明禁止: {forbidden}"
    # 必须出现完整的唯一合法脚本路径(防子串分散误过)
    assert "<RECOVERY_DIR>/sentinel-restart.sh" in text
    # 必须有"越界一律拒绝"的强约束动作词
    assert "拒绝" in text
    # 不能残留默认 hello-world 向导口吻(会让 agent 去"认识自己")
    assert "Hello, World" not in text
    assert "figure out who you are" not in text
