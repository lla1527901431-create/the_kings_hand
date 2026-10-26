"""LLM 客户端。

按请求构造，不做全局单例 —— 单例会让所有请求共用同一个客户端，
而且并发时配置（温度、超时）会互相污染。

key 的来源只有一个：**管理员在「访问管理」里配的那把全局 Key**
（加密存在 app_settings 表里，由调用方解密后传进来）。
普通成员没有、也不需要自己的 Key。
"""

from __future__ import annotations

import config
from langchain_openai import ChatOpenAI


class CredentialsMissing(RuntimeError):
    """全站还没有可用的 DeepSeek Key。"""


def _clean_key(key: str | None) -> str:
    text = (key or "").strip()
    if not text:
        return ""
    if not text.isascii():
        raise CredentialsMissing("API Key 含有非 ASCII 字符（是不是粘贴时带进了中文？）")
    return text


def build_llm(deepseek_key: str | None = None) -> ChatOpenAI:
    """对话用的 LLM。temperature=0 让输出稳定。"""
    key = _clean_key(deepseek_key)
    if not key:
        raise CredentialsMissing("还没有配置 DeepSeek API Key，请联系管理员在「访问管理」里配置。")
    return ChatOpenAI(
        model=config.DEEPSEEK_MODEL,
        api_key=key,
        base_url=config.DEEPSEEK_BASE_URL,
        temperature=0,
        timeout=60,
        max_retries=2,
    )


def build_memory_llm(deepseek_key: str | None = None) -> ChatOpenAI:
    """记忆蒸馏用的 LLM（可以用更便宜的模型）。"""
    key = _clean_key(deepseek_key)
    if not key:
        raise CredentialsMissing("还没有配置 DeepSeek API Key。")
    return ChatOpenAI(
        model=config.MEMORY_MODEL,
        api_key=key,
        base_url=config.DEEPSEEK_BASE_URL,
        temperature=0,
        timeout=60,
        max_retries=2,
    )
