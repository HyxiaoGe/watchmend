# src/sentinel/llm_config.py
"""LLM provider 注册表:声明式 llm.yaml + 热加载 + CLI(init/switch/list)。

把"诊断用哪家 LLM"从 LLMDriver 剥出。LLMDriver 每次调用接收一个解析好的
LLMProfile;LLMConfig 负责从 llm.yaml(或回落老 LLM_* env)解析 active/fallback,
按文件 mtime 热加载,出错时保留上一份有效配置(fail-safe)。
"""

from __future__ import annotations

import logging
import os
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
