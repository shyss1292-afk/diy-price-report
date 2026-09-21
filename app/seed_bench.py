"""基准跑分参考数据 —— 供「日报」页计算性价比指数使用。

为什么单独维护
--------------
价格追踪本身只产生价格，不产生性能数据。而参考视频里的日报表有一列
「性价比」（跑分 / 现价），要做到同样的事就必须引入跑分。

口径
----
- GPU：3DMark Time Spy Extreme（Graphics Score，DX12 / 4K）
- CPU：Cinebench R23 多核

数据性质说明
------------
下面是**公开基准的参考量级**，用于同代产品横向比较是足够的，但不应
当作精确复现值 —— 驱动版本、平台配置、散热条件都会影响成绩。

需要更精确的数据时，直接改本文件即可：键必须与 products.model 完全一致。
缺少跑分的型号，「性价比」列会自动显示为 —，不会伪造数值。
"""
from __future__ import annotations

# ---------------------------------------------------------------- GPU（3DMark TSE）
GPU_BENCH: dict[str, int] = {
    # 50 系
    "RTX 5090 32G": 25485,
    "RTX 5090 D 32G": 25042,
    "RTX 5080 16G": 16066,
    "RTX 5080 D 16G": 15700,
    "RTX 5070 Ti 16G": 13485,
    "RTX 5070 12G": 10096,
    "RTX 5060 Ti 16G": 7136,
    "RTX 5060 Ti 8G": 7136,
    "RTX 5060 8G": 6529,
    "RTX 5050 8G": 4607,
    # 40 系
    "RTX 4090 24G": 19500,
    "RTX 4080 Super 16G": 14000,
    "RTX 4080 16G": 13900,
    "RTX 4070 Ti Super 16G": 11500,
    "RTX 4070 Ti 12G": 11300,
    "RTX 4070 Super 12G": 10500,
    "RTX 4070 12G": 8900,
    "RTX 4060 Ti 16G": 6700,
    "RTX 4060 Ti 8G": 6700,
    "RTX 4060 8G": 6300,
    # 30 系 / 20 系 / GTX
    "RTX 3090 24G": 10500,
    "RTX 3080 10G": 9000,
    "RTX 3070 8G": 7300,
    "RTX 3060 12G": 5100,
    "RTX 3050 8G": 3500,
    "RTX 2060 6G": 4200,
    "GTX 1660 Super 6G": 3200,
    # AMD RDNA4 / RDNA3
    "RX 9070 XT 16G": 14120,
    "RX 9070 16G": 12860,
    "RX 7900 XTX 24G": 13000,
    "RX 7900 XT 20G": 11500,
    "RX 7900 GRE 16G": 10000,
    "RX 9060 XT 16G": 7489,
    "RX 9060 XT 8G": 7234,
    "RX 7800 XT 16G": 8800,
    "RX 7700 XT 12G": 8500,
    "RX 6750 GRE 12G": 6000,
    # AMD 入门
    "RX 7600 8G": 5400,
    "RX 6650 XT 8G": 5000,
    "RX 6600 8G": 4300,
    # Intel Arc
    "Arc B580 12G": 7113,
    "Arc B570 10G": 6300,
    "Arc A750 8G": 6200,
    "Arc A380 6G": 2900,

    # ---------------------------------------------------------------- 10 系 → 50 系补全
    # 与上面的量级对齐（同为 3DMark TSE 显卡分参考值）。
    # 校验要点：同代内 型号越高分越高；跨代大致符合实际性能顺序
    # （例：1080 Ti 仍强于 2060，3060 Ti 明显高于 2060 Super）。
    #
    # ---- NVIDIA GTX 10 系（Pascal）----
    "GTX 1050 2G": 1300,
    "GTX 1050 Ti 4G": 1700,
    "GTX 1060 3G": 2300,
    "GTX 1060 6G": 2500,
    "GTX 1070 8G": 3400,
    "GTX 1070 Ti 8G": 3800,
    "GTX 1080 8G": 4300,
    "GTX 1080 Ti 11G": 5400,
    # ---- NVIDIA GTX 16 系 ----
    "GTX 1650 4G": 2000,
    "GTX 1650 Super 4G": 2600,
    "GTX 1660 6G": 2900,
    "GTX 1660 Ti 6G": 3400,
    # ---- NVIDIA RTX 20 系 ----
    "RTX 2060 Super 8G": 4900,
    "RTX 2070 8G": 5400,
    "RTX 2070 Super 8G": 6000,
    "RTX 2080 8G": 6700,
    "RTX 2080 Super 8G": 7200,
    "RTX 2080 Ti 11G": 8700,
    # ---- NVIDIA RTX 30 系 ----
    "RTX 3050 6G": 3300,
    # 3060 的 8G 版位宽从 192bit 砍到 128bit，实测略低于 12G 版
    "RTX 3060 8G": 4200,
    "RTX 3060 Ti 8G": 6600,
    "RTX 3070 Ti 8G": 8000,
    "RTX 3080 12G": 9800,
    "RTX 3080 Ti 12G": 10400,
    "RTX 3090 Ti 24G": 11300,
    # ---- NVIDIA RTX 40 系 ----
    "RTX 4090 D 24G": 18500,
    # ---- AMD RX 5000 系（RDNA1）----
    "RX 5500 XT 4G": 2200,
    "RX 5500 XT 8G": 2300,
    "RX 5600 XT 6G": 3400,
    "RX 5700 8G": 3900,
    "RX 5700 XT 8G": 4300,
    # ---- AMD RX 6000 系（RDNA2）----
    "RX 6400 4G": 1500,
    "RX 6500 XT 4G": 2000,
    "RX 6600 XT 8G": 4600,
    "RX 6700 10G": 5100,
    "RX 6700 XT 12G": 5700,
    "RX 6750 XT 12G": 6100,
    "RX 6800 16G": 7000,
    "RX 6800 XT 16G": 8300,
    "RX 6900 XT 16G": 9200,
    "RX 6950 XT 16G": 9700,
    # ---- AMD RX 7000 系 ----
    "RX 7600 XT 16G": 5800,
}

# ---------------------------------------------------------------- CPU（Cinebench R23 多核）
CPU_BENCH: dict[str, int] = {
    # AMD Zen5
    "Ryzen 9 9950X3D": 42377,
    "Ryzen 9 9950X": 42103,
    "R9 9900X3D": 34000,
    "Ryzen 9 9900X": 33500,
    "Ryzen 7 9800X3D": 23157,
    "Ryzen 7 9700X": 23900,
    "Ryzen 5 9600X": 16284,
    "Ryzen 5 9600": 17000,
    "Ryzen 5 9500F": 15884,
    # AMD Zen4 / Zen3
    "Ryzen 9 7950X": 38000,
    "Ryzen 9 7900X": 29000,
    "Ryzen 7 7800X3D": 18686,
    "Ryzen 7 7700": 20000,
    "Ryzen 7 8700F": 17000,
    "Ryzen 5 7600": 14500,
    "Ryzen 5 7500F": 13686,
    "Ryzen 5 8400F": 12500,
    "Ryzen 7 5800X3D": 14000,
    "Ryzen 7 5700X3D": 13500,
    "Ryzen 7 5700X": 14000,
    "Ryzen 5 5600": 11000,
    "Ryzen 5 5600G": 11000,
    "Ryzen 5 5500": 10000,
    # Intel Core Ultra（Arrow Lake）
    "Core Ultra 9 285K": 43000,
    "Core Ultra 7 270K Plus": 41558,
    "Core Ultra 7 265K": 36309,
    "Core Ultra 7 265KF": 36000,
    "Core Ultra 5 250K Plus": 32090,
    "Core Ultra 5 245K": 24935,
    "Core Ultra 5 245KF": 24800,
    # Intel 12~14 代
    "i9-14900K": 38497,
    "i9-14900KF": 38400,
    "i9-12900K": 27000,
    "i7-14700K": 34805,
    "i7-14700KF": 34800,
    "i7-12700KF": 23000,
    "i5-14600K": 23200,
    "i5-14600KF": 23000,
    "i5-14400F": 16500,
    "i5-13400F": 16500,
    "i5-12600KF": 17000,
    "i5-12490F": 12500,
    "i5-12400F": 12500,
    "i3-12100F": 8500,
}

# 各品类的跑分口径（用于表头与说明文案）
BENCH_META: dict[str, dict[str, str]] = {
    "gpu": {"field": "TSE跑分", "tooltip": "3DMark Time Spy Extreme 显卡分（参考值）"},
    "cpu": {"field": "R23跑分", "tooltip": "Cinebench R23 多核分（参考值）"},
}

_TABLES = {"gpu": GPU_BENCH, "cpu": CPU_BENCH}


def bench_score(model: str, category: str) -> int | None:
    """取型号的基准跑分；无数据返回 None（调用方应显示为 —，不要兜底成 0）。"""
    table = _TABLES.get(category)
    if not table:
        return None
    if model in table:
        return table[model]
    # 容忍别名差异：型号名里的大小写、空格、连字符不影响匹配
    key = model.replace(" ", "").replace("-", "").lower()
    for name, score in table.items():
        if name.replace(" ", "").replace("-", "").lower() == key:
            return score
    return None


def bench_label(category: str) -> str:
    return BENCH_META.get(category, {}).get("field", "跑分")


def bench_tooltip(category: str) -> str:
    return BENCH_META.get(category, {}).get("tooltip", "基准跑分")
