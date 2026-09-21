"""适配器注册表。

用法：
    @register
    class JdCollector(BaseCollector):
        code = "jd"
        ...
装饰器会自动实例化并登记，流水线拿到的是实例。
"""
from __future__ import annotations

from .base import BaseCollector

_REGISTRY: dict[str, BaseCollector] = {}


def register(cls: type[BaseCollector]) -> type[BaseCollector]:
    """类装饰器：实例化适配器并登记。"""
    instance = cls()
    _REGISTRY[cls.code] = instance
    return cls


def get_collectors(codes: list[str] | None = None) -> list[BaseCollector]:
    """按 code 取出适配器实例；codes 为 None 时返回全部。"""
    if codes is None:
        return list(_REGISTRY.values())
    return [_REGISTRY[c] for c in codes if c in _REGISTRY]


def available_codes() -> list[str]:
    return sorted(_REGISTRY)
