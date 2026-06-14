# tests/test_deploy_wiring.py
"""守护 P1 部署接线:llm.yaml 必须挂进容器,否则 host 的 `make llm-*` 改动不生效。

Codex P1 回归防护:host CLI 写 ./llm.yaml,容器经 bind-mount 读同一文件;
make up/demo 先脚手架出占位文件(否则 docker 对缺失 bind 源会误建空目录)。
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
LLM_MOUNT = "./llm.yaml:/app/llm.yaml:ro"


def _sentinel_volumes(compose_file: str) -> list[str]:
    spec = yaml.safe_load((ROOT / compose_file).read_text(encoding="utf-8"))
    return spec["services"]["sentinel"]["volumes"]


def test_compose_mounts_llm_yaml():
    assert LLM_MOUNT in _sentinel_volumes("docker-compose.yml")


def test_demo_compose_mounts_llm_yaml():
    assert LLM_MOUNT in _sentinel_volumes("docker-compose.demo.yml")


def test_makefile_scaffolds_llm_yaml_before_up_and_demo():
    mk = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "llm-yaml-ready:" in mk  # 有脚手架目标
    up_line = next(ln for ln in mk.splitlines() if ln.startswith("up:"))
    demo_line = next(ln for ln in mk.splitlines() if ln.startswith("demo:"))
    assert "llm-yaml-ready" in up_line  # up 依赖它,否则容器看不到 host 改动
    assert "llm-yaml-ready" in demo_line
