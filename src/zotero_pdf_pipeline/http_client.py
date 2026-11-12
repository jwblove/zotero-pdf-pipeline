"""HTTPX 客户端工厂：兼容 NO_PROXY 中的 IPv6 回环，不丢弃代理规则。"""
from __future__ import annotations

import os
from threading import RLock

import httpx

_ENV_LOCK = RLock()


def normalize_no_proxy(value: str) -> str:
    """把 IPv6 回环写成 HTTPX 可识别的显式 URL pattern；其他规则原样保留。"""
    return ",".join(
        "all://[::1]" if entry.strip() in {"::1", "[::1]"} else entry
        for entry in value.split(",")
    )


def make_client(**kwargs) -> httpx.Client:
    """仅构造客户端时规范化进程环境，随后原样恢复；不打印任何代理配置。"""
    with _ENV_LOCK:
        saved = {key: os.environ.get(key) for key in ("NO_PROXY", "no_proxy")}
        try:
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = normalize_no_proxy(value)
            return httpx.Client(**kwargs)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
