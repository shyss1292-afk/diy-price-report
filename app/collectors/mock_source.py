"""Mock 数据源 —— 用于在接入真实平台前打通全链路。

价格模型（三层相乘，保证可复现）：
  最终价 = 基准价 × 平台系数 × 价格指数 × 商家噪声

其中价格指数 = 品类共同趋势 × 型号特异趋势，两者均为锚定固定日期、
带均值回归的随机游走，因此：
  · 同一次运行、同一天，结果完全一致（幂等，可反复回填）
  · 同品类型号走势相关（贴近真实行情）
  · 沿时间轴有连续涨跌，趋势图与指标才有意义
  · 价位不会长期漂移到不真实的极端值（均值回归）

「样本构成」按 型号 × 平台 固定（报价条数、二手成色组合），
只有价格本身随日期波动 —— 否则最低价会在不同成色档之间来回跳。
"""
from __future__ import annotations

import random
from datetime import date

from .base import BaseCollector, Quote
from .registry import register

# 随机游走锚点：所有序列都从这里开始，保证跨进程、跨天结果一致
_ANCHOR = date(2025, 1, 1)
_MAX_STEPS = 3000

# 已接入真实适配器（或已明确停用）的平台 —— Mock 不再模拟这些平台，避免互相覆盖。
# zol / pconline 已于 2026-09-17 停用（媒体参考报价，口径不同、缺参考价值），
# 这里仍然保留：万一将来有人把平台重新启用，也不会被 Mock 的假数据填满。
REAL_PLATFORMS: set[str] = {"zol", "pconline", "jd", "pdd", "xianyu"}

# 已停产 / 供给收缩的型号，长期呈上行趋势（更贴近现实）
_UPWARD_MODELS = {
    "RTX 4090 24G",
    "RX 7900 XTX 24G",
    "i5-12400F",
    "Ryzen 7 7800X3D",
    "DDR4 8GB 2666 单条",
    "DDR4 16GB(8G×2) 3200",
    "DDR4 32GB(16G×2) 3600",
}

_USED_CONDITIONS: list[tuple[str, float]] = [
    ("99新", 1.00),
    ("95新", 0.93),
    ("9成新", 0.86),
    ("8成新", 0.78),
]

_NEW_SELLER_SUFFIX = [
    "自营旗舰店",
    "官方旗舰店",
    "数码专营店",
    "电脑配件专营店",
    "授权专卖店",
]

_TITLE_PREFIX = ["【全新】", "全新正品 ", "行货 ", "", "【现货】"]
_TITLE_SUFFIX = [" 显卡", " 台式机配件", " 电脑硬件", " 盒装", ""]


class _Walk:
    """带均值回归的随机游走序列（惰性扩展）。

    theta 为回归强度：价格偏离 1.0 越远，回拉力越强，
    避免长期累积漂移到不真实的极端价位。
    """

    def __init__(
        self,
        seed: str,
        sigma: float,
        drift: float,
        theta: float = 0.012,
        lo: float = 0.6,
        hi: float = 1.7,
    ):
        self._rng = random.Random(seed)
        self._sigma = sigma
        self._drift = drift
        self._theta = theta
        self._lo = lo
        self._hi = hi
        self._values: list[float] = [1.0]

    def at(self, idx: int) -> float:
        if idx < 0:
            idx = 0
        while len(self._values) <= idx < _MAX_STEPS:
            cur = self._values[-1]
            step = self._drift + self._rng.gauss(0.0, self._sigma) + self._theta * (1.0 - cur)
            self._values.append(max(self._lo, min(self._hi, cur * (1.0 + step))))
        return self._values[min(idx, len(self._values) - 1)]


class MockMarket:
    """可复现的模拟行情。"""

    def __init__(self) -> None:
        self._cat_walk: dict[str, _Walk] = {}
        self._prod_walk: dict[int, _Walk] = {}

    # -- 品类共同趋势：波动小，代表整体供需
    def _category_index(self, category: str, idx: int) -> float:
        walk = self._cat_walk.get(category)
        if walk is None:
            rng = random.Random(f"cat-{category}")
            walk = _Walk(
                seed=f"cat-{category}",
                sigma=0.0030,
                drift=rng.uniform(-0.00030, 0.00020),
                theta=0.018,
                lo=0.78,
                hi=1.30,
            )
            self._cat_walk[category] = walk
        return walk.at(idx)

    # -- 型号特异趋势：波动稍大，代表个体供需与促销节奏
    def _product_index(self, product, idx: int) -> float:
        walk = self._prod_walk.get(product.id)
        if walk is None:
            upward = product.model in _UPWARD_MODELS
            rng = random.Random(f"drift-{product.model}")
            drift = rng.uniform(0.00012, 0.00042) if upward else rng.uniform(-0.00060, 0.00030)
            walk = _Walk(
                seed=f"prod-{product.id}-{product.model}",
                sigma=0.0065,
                drift=drift,
                theta=0.010,
                lo=0.55,
                hi=1.85,
            )
            self._prod_walk[product.id] = walk
        return walk.at(idx)

    def price_index(self, product, day: date) -> float:
        idx = (day - _ANCHOR).days
        return self._category_index(product.category, idx) * self._product_index(product, idx)


@register
class MockCollector(BaseCollector):
    """模拟采集器：为每个（型号 × 平台 × 日期）生成若干条报价。"""

    code = "mock"
    name = "模拟数据源"

    def __init__(self) -> None:
        self.market = MockMarket()

    def collect(self, platform, products: list, day: date) -> list[Quote]:
        # 已被真实适配器接管的平台不再模拟 —— 否则两个源会互相覆盖同一平台的数据
        if platform.code in REAL_PLATFORMS:
            return []
        quotes: list[Quote] = []
        for product in products:
            quotes.extend(self._quote_for(product, platform, day))
        return quotes

    def _quote_for(self, product, platform, day: date) -> list[Quote]:
        idx = self.market.price_index(product, day)
        anchor_price = float(product.base_price) * float(platform.factor) * idx

        # 日变种子：决定当天的价格噪声
        rng = random.Random(f"{platform.code}-{product.id}-{day.isoformat()}")
        # 稳定种子：决定该型号在该平台的样本构成（条数 / 成色），不随日期变化
        struct_rng = random.Random(f"struct-{platform.code}-{product.id}")

        if platform.kind == "used":
            return self._used_quotes(product, platform, anchor_price, day, rng, struct_rng)
        return self._new_quotes(product, platform, anchor_price, day, rng, struct_rng)

    # ---------------------------------------------------------- 全新
    def _new_quotes(self, product, platform, anchor_price, day, rng, struct_rng) -> list[Quote]:
        count = struct_rng.choice([2, 3, 3, 3, 4])
        sellers = rng.sample(_NEW_SELLER_SUFFIX, k=min(count, len(_NEW_SELLER_SUFFIX)))
        quotes: list[Quote] = []
        for i in range(count):
            spread = rng.uniform(-0.022, 0.016)  # 同平台商家价差
            price = anchor_price * (1.0 + spread)
            if rng.random() < 0.08:  # 少量促销价
                price *= rng.uniform(0.960, 0.985)
            title = (
                rng.choice(_TITLE_PREFIX)
                + self._display_name(product, rng)
                + rng.choice(_TITLE_SUFFIX)
            )
            quotes.append(
                Quote(
                    platform_code=platform.code,
                    title_raw=title,
                    price=max(1.0, price),
                    condition="全新",
                    url=f"https://example.com/{platform.code}/{product.id}/{day.isoformat()}",
                    seller=sellers[i % len(sellers)],
                )
            )
        return quotes

    # ---------------------------------------------------------- 二手
    def _used_quotes(self, product, platform, anchor_price, day, rng, struct_rng) -> list[Quote]:
        count = struct_rng.choice([1, 2, 2, 3])
        chosen = struct_rng.sample(_USED_CONDITIONS, k=min(count, len(_USED_CONDITIONS)))
        quotes: list[Quote] = []
        for cond_name, cond_factor in chosen:
            # 二手溢价/折价随行情浮动，但幅度小于全新
            noise = rng.uniform(0.965, 1.055)
            price = anchor_price * cond_factor * noise
            title = f"二手 {self._display_name(product, rng)} {cond_name} 个人闲置"
            quotes.append(
                Quote(
                    platform_code=platform.code,
                    title_raw=title,
                    price=max(1.0, price),
                    condition=cond_name,
                    url=f"https://example.com/{platform.code}/i/{product.id}{day.strftime('%m%d')}",
                    seller=f"个人卖家{rng.randint(1000, 9999)}",
                )
            )
        return quotes

    @staticmethod
    def _display_name(product, rng) -> str:
        """90% 用标准型号名，10% 用别名写法，用于验证归一化模块。"""
        if rng.random() < 0.10 and product.aliases:
            options = [a.strip() for a in product.aliases.split(",") if a.strip()]
            if options:
                return rng.choice(options)
        return product.model
