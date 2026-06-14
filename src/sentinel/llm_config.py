# src/sentinel/llm_config.py
"""LLM provider 注册表:声明式 llm.yaml + 热加载 + CLI(init/switch/list)。

把"诊断用哪家 LLM"从 LLMDriver 剥出。LLMDriver 每次调用接收一个解析好的
LLMProfile;LLMConfig 负责从 llm.yaml(或回落老 LLM_* env)解析 active/fallback,
按文件 mtime 热加载,出错时保留上一份有效配置(fail-safe)。
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from sentinel.config import Settings

logger = logging.getLogger("sentinel")


@dataclass(frozen=True)
class LLMProfile:
    """driver 直接消费的一份已解析配置。api_key 可为 ""(本地无鉴权,合法)。"""

    name: str
    base_url: str
    api_key: str
    model: str


@dataclass(frozen=True)
class _Registry:
    active: LLMProfile
    fallback: LLMProfile | None


class LLMConfigError(ValueError):
    """active 配置无效:语法/缺字段/active 指向缺失 provider/api_key_env 未设/协议不支持。"""


def _resolve_provider(name: str, spec: object) -> LLMProfile:
    """把一个 provider 条目解析成 LLMProfile;任一硬性问题抛 LLMConfigError。"""
    if not isinstance(spec, dict):
        raise LLMConfigError(f"provider {name} 不是映射")
    protocol = spec.get("protocol", "openai")
    if protocol != "openai":
        raise LLMConfigError(f"provider {name} 的 protocol={protocol} 暂不支持(仅 openai)")
    base_url = spec.get("base_url")
    model = spec.get("model")
    if not base_url or not model:
        raise LLMConfigError(f"provider {name} 缺 base_url 或 model")
    if "api_key_env" in spec:
        if "api_key" in spec:
            logger.info(
                "provider %s 同时给了 api_key_env 与 api_key,采用 api_key_env(更安全)", name
            )
        env_name = spec["api_key_env"]
        api_key = os.environ.get(env_name)
        if api_key is None:
            raise LLMConfigError(f"provider {name} 的 api_key_env={env_name} 引用的环境变量未设")
    else:
        api_key = spec.get("api_key", "")  # 内联;缺省 "" = 无鉴权
    return LLMProfile(name=name, base_url=str(base_url), api_key=str(api_key), model=str(model))


def _parse_and_resolve(path: str) -> _Registry:
    """读 llm.yaml → 解析 active(严判,抛)+ fallback(宽判,坏则忽略)。"""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise LLMConfigError("llm.yaml 顶层不是映射")
    providers = raw.get("providers")
    if not isinstance(providers, dict) or not providers:
        raise LLMConfigError("llm.yaml 缺 providers 映射")
    active_name = raw.get("active")
    if not isinstance(active_name, str) or active_name not in providers:
        raise LLMConfigError(f"active={active_name!r} 未在 providers 中定义")
    active = _resolve_provider(active_name, providers[active_name])
    fallback: LLMProfile | None = None
    fb_name = raw.get("fallback")
    if isinstance(fb_name, str) and fb_name:
        if fb_name not in providers:
            logger.warning("fallback=%s 未在 providers 中定义,忽略", fb_name)
        else:
            try:
                fallback = _resolve_provider(fb_name, providers[fb_name])
            except LLMConfigError as e:
                logger.warning("fallback=%s 无效,忽略: %s", fb_name, e)
    return _Registry(active=active, fallback=fallback)


class LLMConfig:
    """持有 llm.yaml/env 来源,按 mtime 热加载,出错保留 last-good。

    current()/fallback() 每次调用先看文件 mtime:变了才重解析,坏了保留上一份有效配置。
    yaml 缺席时回落老 LLM_* env(零 breaking);env 也空则整层关闭(current()=None)。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._path = settings.sentinel_llm_config_file
        self._last_mtime: float | None = None
        self._last_good: _Registry | None = None

    def _env_registry(self) -> _Registry | None:
        s = self._settings
        if not (s.llm_base_url and s.llm_model):
            return None
        profile = LLMProfile(
            name="env", base_url=s.llm_base_url, api_key=s.llm_api_key, model=s.llm_model
        )
        return _Registry(active=profile, fallback=None)

    def _reload_if_changed(self) -> None:
        try:
            mtime = os.stat(self._path).st_mtime
        except FileNotFoundError:
            self._last_good = self._env_registry()  # yaml 缺席 → env 回落
            self._last_mtime = None
            return
        if mtime == self._last_mtime:
            return
        self._last_mtime = mtime
        try:
            self._last_good = _parse_and_resolve(self._path)
            logger.info("llm.yaml 已加载,active=%s", self._last_good.active.name)
        except Exception as e:
            logger.warning("llm.yaml 无效,保留上一份有效配置: %s", e)

    def current(self) -> LLMProfile | None:
        self._reload_if_changed()
        return self._last_good.active if self._last_good else None

    def fallback(self) -> LLMProfile | None:
        self._reload_if_changed()
        return self._last_good.fallback if self._last_good else None

    @property
    def enabled(self) -> bool:
        return self.current() is not None


# ---- CLI(python -m sentinel.llm_config) ----

_PRESETS = {
    "openai": ("https://api.openai.com/v1", "gpt-5.5"),
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "kimi": ("https://api.moonshot.cn/v1", "kimi-k2-turbo-preview"),
    "glm": ("https://open.bigmodel.cn/api/paas/v4", "glm-4"),
    "ollama": ("http://localhost:11434/v1", "qwen3"),
    "vllm": ("http://localhost:8000/v1", "your-served-model"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-2.5-flash"),
    "claude": ("https://api.anthropic.com/v1/", "claude-opus-4-8"),
    "litellm": ("http://localhost:4000", "your-alias"),
}


def build_provider_entry(answers: dict) -> dict:
    """把向导收集的答案构造成一个 provider 映射(纯函数,可单测)。
    env 模式只落 api_key_env(变量名),绝不把真 key 写进返回值。"""
    entry: dict = {"base_url": answers["base_url"], "model": answers["model"]}
    if answers.get("key_mode") == "env":
        entry["api_key_env"] = answers["api_key_env"]
    else:
        entry["api_key"] = answers.get("api_key", "")
    return entry


def _merge_provider(path: str, name: str, entry: dict, set_active: bool) -> None:
    """把 provider 合并进 llm.yaml(无则建)。注:全量 round-trip,会丢已有注释——
    向导生成/追加用足够;想保留手写注释请直接编辑文件(switch 不丢注释)。"""
    p = Path(path)
    raw = {}
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    raw.setdefault("providers", {})
    raw["providers"][name] = entry
    if set_active or "active" not in raw:
        raw["active"] = name
    p.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def _key_status(spec: object) -> str:
    if not isinstance(spec, dict):
        return "✗"
    if "api_key_env" in spec:
        return "✓" if os.environ.get(spec["api_key_env"]) is not None else "✗"
    return "✓"  # 内联(含空=无鉴权)


def cmd_switch(path: str, name: str) -> int:
    """改 active:校验 name 存在后定点重写 ^active 行,保留其余(含注释)。"""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"✗ {path} 不存在,先跑 make llm-init 生成", file=sys.stderr)
        return 1
    try:
        providers = (yaml.safe_load(text) or {}).get("providers") or {}
    except yaml.YAMLError as e:
        print(f"✗ {path} 解析失败: {e}", file=sys.stderr)
        return 1
    if name not in providers:
        avail = ", ".join(providers) or "(无)"
        print(f"✗ provider '{name}' 不存在;可用: {avail}", file=sys.stderr)
        return 1
    out, replaced = [], False
    for line in text.splitlines():
        if not replaced and re.match(r"^active\s*:", line):
            out.append(f"active: {name}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        print(f"✗ {path} 没有顶层 active: 行,请手动编辑", file=sys.stderr)
        return 1
    Path(path).write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"✓ active → {name}(下一轮诊断生效,无需重启)")
    return 0


def cmd_list(path: str, settings: Settings) -> int:
    """印各 provider + active/fallback 标记 + key 是否解析得到。"""
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        if settings.llm_base_url and settings.llm_model:
            print(f"(无 llm.yaml,回落环境变量)  env  {settings.llm_base_url}  {settings.llm_model}")
        else:
            print("(无 llm.yaml,且 LLM_* 环境变量未配置 → LLM 层关闭)")
        return 0
    providers = raw.get("providers") or {}
    if not providers:
        print("(llm.yaml 无 providers)")
        return 0
    active, fb = raw.get("active"), raw.get("fallback")
    for name, spec in providers.items():
        tags = [t for t, on in (("active", name == active), ("fallback", name == fb)) if on]
        model = spec.get("model", "?") if isinstance(spec, dict) else "?"
        marker = f"[{','.join(tags)}]" if tags else ""
        print(f"{name:14} {_key_status(spec)}  {model:24} {marker}")
    return 0


def cmd_init(path: str) -> int:
    """交互向导:加一个 provider 并(可选)设为 active。"""
    print("WatchMend LLM 配置向导。预设: " + ", ".join(_PRESETS) + ", custom")
    choice = input("选平台 [deepseek]: ").strip() or "deepseek"
    if choice in _PRESETS:
        default_url, default_model = _PRESETS[choice]
    else:
        choice = input("provider 名: ").strip()
        default_url, default_model = "", ""
    base_url = input(f"base_url [{default_url}]: ").strip() or default_url
    model = input(f"model [{default_model}]: ").strip() or default_model
    mode = input("key 方式 env(引用环境变量)/inline(内联) [env]: ").strip() or "env"
    answers = {"base_url": base_url, "model": model, "key_mode": mode}
    if mode == "env":
        env_name = input("环境变量名 [LLM_API_KEY]: ").strip() or "LLM_API_KEY"
        answers["api_key_env"] = env_name
        print(f"  → 记得 export {env_name}=... 或写进 .env(真 key 不会写进 llm.yaml)")
    else:
        answers["api_key"] = input("api_key(留空=无鉴权,会落盘): ").strip()
        print("  ⚠ 内联 key 会明文写入 llm.yaml")
    set_active = (input("设为 active? [Y/n]: ").strip().lower() or "y") == "y"
    _merge_provider(path, choice, build_provider_entry(answers), set_active)
    print(f"✓ 已写入 {path} 的 provider '{choice}'" + ("(active)" if set_active else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = Settings()
    parser = argparse.ArgumentParser(prog="python -m sentinel.llm_config")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sw = sub.add_parser("switch")
    sw.add_argument("name")
    sub.add_parser("list")
    args = parser.parse_args(argv)
    path = settings.sentinel_llm_config_file
    if args.cmd == "init":
        return cmd_init(path)
    if args.cmd == "switch":
        return cmd_switch(path, args.name)
    if args.cmd == "list":
        return cmd_list(path, settings)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
