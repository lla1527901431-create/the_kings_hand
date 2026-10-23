"""LLM 客户端。

按请求构造（每个用户用自己的 DeepSeek Key），不做全局单例 ——
单例会让所有用户共用同一个 key，而且并发时会互相污染。

key 的来源只有一个：用户在页面上填的那个（存 SQLite）。
不再有"服务器兜底 key"，避免任何人白嫖服务器额度。
"""

from __future__ import annotations

import config
from langchain_openai import ChatOpenAI


class CredentialsMissing(RuntimeError):
    """用户还没填自己的 API Key。"""


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
        raise CredentialsMissing("还没有配置 DeepSeek API Key。请点右上角「API Key 设置」填写。")
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
