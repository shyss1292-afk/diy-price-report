"""采集适配器包。

每个数据源实现一个 BaseCollector 子类，通过 registry 注册。
接入真实平台时，只需新增适配器文件，无需改动流水线。
"""
from .base import BaseCollector, Quote
from .registry import available_codes, get_collectors, register

# 显式导入各适配器，触发 @register 注册。
# 新增真实平台适配器时，在这里补一行即可。
from . import jd_source  # noqa: F401  isort:skip
from . import mock_source  # noqa: F401  isort:skip
from . import pdd_source  # noqa: F401  isort:skip
from . import xianyu_source  # noqa: F401  isort:skip

__all__ = [
    "BaseCollector",
    "Quote",
    "get_collectors",
    "register",
    "available_codes",
]
