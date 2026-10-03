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
    """按 code 取出适配器实例。

    `codes is None`（即默认轮次）时只返回 **`is_default_source` 为真**的适配器
    —— 把 Mock 这类开发工具挡在生产轮次之外。
    ⚠️ 2026-10-03：此前返回全部，导致 Mock 源每轮都给「没有真实采集器却
       `is_active=1`」的平台灌 1000+ 条模拟数据。
    需要 Mock（演示 / 打通链路）时显式传 ``codes=["mock"]``。
    """
    if codes is None:
        return [c for c in _REGISTRY.values() if getattr(c, "is_default_source", True)]
    return [_REGISTRY[c] for c in codes if c in _REGISTRY]


def available_codes() -> list[str]:
    return sorted(_REGISTRY)
