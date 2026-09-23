"""平台字典与全品类型号主数据。

base_price 为该型号的参考基准价（元，以京东自营全新价为锚），
Mock 采集器以此为起点做波动模拟；接入真实源后，此字段亦可用于异常价校验。
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------- 品类
CATEGORIES: dict[str, str] = {
    "gpu": "显卡",
    "cpu": "处理器",
    "ram": "内存",
    "mb": "主板",
    "ssd": "固态硬盘",
    "psu": "电源",
    "cooler": "散热器",
    "case": "机箱",
}

# 监控范围：**只保留三大件**（2026-09-23 决策）
#
# 为什么砍：长尾品类（主板/固态/电源/散热/机箱）的样本极稀疏 ——
# 单个型号一天常常只有 1~3 条报价，算出来的"最低价"和"涨跌幅"没有统计意义
# （实测 990 EVO 1TB：1 条 ¥350 vs 2 条 ¥835 = +139% 的假暴涨）。
# 与其展示一堆不可信的数字，不如把采集配额集中到三大件上，
# 让每个型号每天都能攒够样本。
#
# ⚠️ CATEGORIES 标签表**不要删** —— 历史数据（listings / price_daily）
#    里还有这些品类的记录，标签查不到会退化成裸 code 显示。
CATEGORY_ORDER = ["gpu", "cpu", "ram"]

# 页面板块分组：把「核心三大件」和其他硬件分开排，避免混在一起。
# 这是**展示层**的分组 —— 采集、统计、存储一律仍按 CATEGORY_ORDER 处理。
CATEGORY_GROUPS: list[dict] = [
    {
        "code": "core",
        "label": "核心配件",
        "hint": "显卡 / CPU / 内存",
        "categories": ["gpu", "cpu", "ram"],
    },
]

# code -> group code，便于前端/接口直接查
CATEGORY_GROUP_OF: dict[str, str] = {
    cat: g["code"] for g in CATEGORY_GROUPS for cat in g["categories"]
}


def category_group(code: str) -> str:
    """品类所属板块 code；未登记的品类归入 other。"""
    return CATEGORY_GROUP_OF.get(code, "other")


# ---------------------------------------------------------------- 平台
# factor：相对基准价的价格系数。全新平台围绕 1.0 浮动，二手平台显著折价。
PLATFORMS: list[dict] = [
    {"code": "jd", "name": "京东", "kind": "new", "factor": 1.000, "color": "#E1251B", "sort_order": 10},
    {"code": "tmall", "name": "天猫", "kind": "new", "factor": 1.025, "color": "#FF6A00", "sort_order": 20},
    {"code": "pdd", "name": "拼多多", "kind": "new", "factor": 0.938, "color": "#C62F2F", "sort_order": 30},
    {"code": "mmb", "name": "慢慢买", "kind": "new", "factor": 1.012, "color": "#1565C0", "sort_order": 50},
    {"code": "xianyu", "name": "闲鱼", "kind": "used", "factor": 0.760, "color": "#E8A800", "sort_order": 60},
    {"code": "zhuanzhuan", "name": "转转", "kind": "used", "factor": 0.800, "color": "#00A870", "sort_order": 70},
]

# ---------------------------------------------------------------- 型号
# (category, brand, model, spec, base_price)
_PRODUCTS_RAW: list[tuple[str, str, str, str, float]] = [
    # ---- 显卡 ----
    ("gpu", "NVIDIA", "RTX 5090 32G", "Blackwell · 32GB GDDR7", 18999),
    ("gpu", "NVIDIA", "RTX 5080 16G", "Blackwell · 16GB GDDR7", 9999),
    ("gpu", "NVIDIA", "RTX 5070 Ti 16G", "Blackwell · 16GB GDDR7", 7299),
    ("gpu", "NVIDIA", "RTX 5070 12G", "Blackwell · 12GB GDDR7", 4899),
    ("gpu", "NVIDIA", "RTX 5060 Ti 16G", "Blackwell · 16GB GDDR7", 3499),
    ("gpu", "NVIDIA", "RTX 4090 24G", "Ada · 24GB GDDR6X", 15999),
    ("gpu", "NVIDIA", "RTX 4080 Super 16G", "Ada · 16GB GDDR6X", 8499),
    ("gpu", "NVIDIA", "RTX 4070 Ti Super 16G", "Ada · 16GB GDDR6X", 6499),
    ("gpu", "NVIDIA", "RTX 4070 Super 12G", "Ada · 12GB GDDR6X", 4999),
    ("gpu", "AMD", "RX 9070 XT 16G", "RDNA4 · 16GB GDDR6", 4999),
    ("gpu", "AMD", "RX 7900 XTX 24G", "RDNA3 · 24GB GDDR6", 6999),
    ("gpu", "Intel", "Arc B580 12G", "Battlemage · 12GB GDDR6", 1799),
    # ---- 处理器 ----
    ("cpu", "AMD", "Ryzen 7 9800X3D", "Zen5 · 8核16线程 · 96MB 缓存", 3399),
    ("cpu", "AMD", "Ryzen 9 9950X", "Zen5 · 16核32线程", 4299),
    ("cpu", "AMD", "Ryzen 7 9700X", "Zen5 · 8核16线程", 1999),
    ("cpu", "AMD", "Ryzen 5 9600X", "Zen5 · 6核12线程", 1299),
    ("cpu", "AMD", "Ryzen 7 7800X3D", "Zen4 · 8核16线程 · 96MB 缓存", 2599),
    ("cpu", "AMD", "Ryzen 5 7500F", "Zen4 · 6核12线程", 899),
    ("cpu", "Intel", "Core Ultra 9 285K", "Arrow Lake · 24核", 4299),
    ("cpu", "Intel", "Core Ultra 7 265K", "Arrow Lake · 20核", 2599),
    ("cpu", "Intel", "Core Ultra 5 245K", "Arrow Lake · 14核", 1799),
    ("cpu", "Intel", "i5-14600KF", "Raptor Lake · 14核20线程", 1499),
    ("cpu", "Intel", "i7-14700K", "Raptor Lake · 20核28线程", 2499),
    ("cpu", "Intel", "i5-12400F", "Alder Lake · 6核12线程", 799),
    # ---- 内存 ----
    ("ram", "光威", "DDR5 32GB(16G×2) 6000 C30", "海力士 A-die · 套装", 599),
    ("ram", "金百达", "DDR5 16GB(8G×2) 6000", "6000MHz · 套装", 329),
    ("ram", "宏想", "DDR5 64GB(32G×2) 6000", "大容量 · 套装", 1199),
    ("ram", "芝奇", "DDR5 32GB(16G×2) 6400 C32", "幻锋戟 · RGB", 699),
    ("ram", "金士顿", "DDR4 32GB(16G×2) 3600", "骇客神条 · 套装", 399),
    ("ram", "威刚", "DDR4 16GB(8G×2) 3200", "XPG · 套装", 219),
    ("ram", "英睿达", "DDR5 24GB(12G×2) 6000", "Pro 系列 · 套装", 469),
    ("ram", "三星", "DDR4 8GB 2666 单条", "原厂颗粒", 99),
    ("ram", "美商海盗船", "DDR5 32GB 5600 单条", "复仇者 · 单条", 529),
    ("ram", "宏想", "DDR5 48GB(24G×2) 7000", "高频 · 套装", 1099),
    # ---- 主板 ----
    ("mb", "微星", "MAG B850M MORTAR WIFI", "B850 · M-ATX", 1099),
    ("mb", "微星", "MAG B650M MORTAR WIFI", "B650 · M-ATX", 899),
    ("mb", "华硕", "ROG X870E HERO", "X870E · ATX", 2999),
    ("mb", "华擎", "Z890 Taichi", "Z890 · ATX", 2499),
    ("mb", "华硕", "TUF B760M-PLUS", "B760 · M-ATX", 749),
    ("mb", "华硕", "ROG Z790-A 吹雪", "Z790 · ATX", 2599),
    ("mb", "技嘉", "B650 AORUS ELITE", "B650 · ATX", 1299),
    ("mb", "华硕", "ROG X670E-E", "X670E · ATX", 2799),
    # ---- 固态硬盘 ----
    ("ssd", "三星", "990 PRO 2TB", "PCIe 4.0 · 7450MB/s", 1099),
    ("ssd", "致态", "TiPlus7100 1TB", "PCIe 4.0 · 长江存储", 399),
    ("ssd", "西数", "WD_BLACK SN850X 1TB", "PCIe 4.0 · 7300MB/s", 699),
    ("ssd", "铠侠", "EXCERIA PRO 1TB", "PCIe 4.0", 599),
    ("ssd", "长江存储", "PC411 512GB", "PCIe 4.0 · 原厂颗粒", 259),
    ("ssd", "三星", "990 EVO 1TB", "PCIe 4.0 / 5.0 双模", 549),
    ("ssd", "致态", "TiPro9000 2TB", "PCIe 5.0 · 14000MB/s", 1199),
    ("ssd", "金士顿", "NV3 1TB", "PCIe 4.0", 399),
    # ---- 电源 ----
    ("psu", "海韵", "Focus GX-750", "750W · 金牌全模", 699),
    ("psu", "振华", "冰山金蝶 750W", "750W · 金牌全模", 599),
    ("psu", "长城", "猎金部落 850W", "850W · 金牌全模", 499),
    ("psu", "航嘉", "WD650K", "650W · 铜牌", 329),
    ("psu", "美商海盗船", "RM850e", "850W · 金牌全模", 899),
    ("psu", "鑫谷", "GM850 全模", "850W · 金牌全模", 449),
    # ---- 散热器 ----
    ("cooler", "利民", "Peerless Assassin 120 SE", "双塔风冷 · 6 热管", 149),
    ("cooler", "九州风神", "大霜塔 V5", "双塔风冷", 229),
    ("cooler", "猫头鹰", "NH-D15", "双塔风冷 · 旗舰", 899),
    ("cooler", "利民", "AXP90-X47", "下压式 · ITX", 139),
    ("cooler", "恩杰", "Kraken 360", "360 一体水冷 · LCD", 1399),
    ("cooler", "瓦尔基里", "E360", "360 一体水冷", 899),
    # ---- 机箱 ----
    ("case", "追风者", "P400A", "中塔 · 网孔前面板", 399),
    ("case", "联力", "O11 Dynamic", "海景房 · 中塔", 899),
    ("case", "先马", "趣造", "M-ATX · 侧透", 299),
    ("case", "爱国者", "星璨岚", "中塔 · ARGB", 359),
    ("case", "迎广", "A1 Prime", "ITX · 铝箱", 799),
    ("case", "乔思伯", "D31", "M-ATX · 侧透", 429),
]

# 手工补充的别名：真实采集时平台标题写法差异很大，这里先给出常见变体
_EXTRA_ALIASES: dict[str, list[str]] = {
    "RTX 5090 32G": ["5090", "GeForce RTX 5090", "RTX5090"],
    "RTX 5080 16G": ["5080", "GeForce RTX 5080", "RTX5080"],
    "RTX 5070 Ti 16G": ["5070Ti", "5070 TI", "RTX5070Ti"],
    "RTX 5070 12G": ["5070", "RTX5070"],
    "RTX 5060 Ti 16G": ["5060Ti", "5060 TI", "RTX5060Ti"],
    "RTX 4090 24G": ["4090", "RTX4090"],
    "RTX 4080 Super 16G": ["4080S", "4080 Super", "RTX4080S"],
    "RTX 4070 Ti Super 16G": ["4070TiS", "4070 Ti S", "RTX4070TiSuper"],
    "RTX 4070 Super 12G": ["4070S", "4070 Super", "RTX4070S"],
    "RX 9070 XT 16G": ["9070XT", "RX9070XT"],
    "RX 7900 XTX 24G": ["7900XTX", "RX7900XTX"],
    "Arc B580 12G": ["B580", "Arc B580"],
    "Ryzen 7 9800X3D": ["9800X3D", "R7 9800X3D"],
    "Ryzen 7 7800X3D": ["7800X3D", "R7 7800X3D"],
    "Ryzen 5 7500F": ["7500F", "R5 7500F"],
    "Core Ultra 9 285K": ["285K", "Ultra 9 285K"],
    "Core Ultra 7 265K": ["265K", "Ultra 7 265K"],
    "Core Ultra 5 245K": ["245K", "Ultra 5 245K"],
    "i5-14600KF": ["14600KF", "i5 14600KF"],
    "i7-14700K": ["14700K", "i7 14700K"],
    "i5-12400F": ["12400F", "i5 12400F"],
    "MAG B850M MORTAR WIFI": ["B850M 迫击炮", "B850M MORTAR"],
    "MAG B650M MORTAR WIFI": ["B650M 迫击炮", "B650M MORTAR"],
    "ROG Z790-A 吹雪": ["Z790-A", "Z790A 吹雪"],
    "990 PRO 2TB": ["990PRO 2T", "990 Pro 2T"],
    "WD_BLACK SN850X 1TB": ["SN850X 1T", "SN850X"],
    "TiPlus7100 1TB": ["TiPlus7100 1T"],
    "TiPro9000 2TB": ["TiPro9000 2T"],
    "Peerless Assassin 120 SE": ["PA120 SE", "PA120SE"],
    "O11 Dynamic": ["O11D", "包豪斯 O11D"],

    # --- 以下来自真实站点未匹配清单（2026-09 汇总）---
    # 中文标题的习惯写法：Core 写成"酷睿"、WD 写成"西部数据"，
    # 品牌前缀被替换成中文后英文型号就匹配不上了，必须补短名。
    "Core Ultra 7 270K Plus": ["Ultra 7 270K Plus", "270K Plus", "270K"],
    "Core Ultra 5 250K Plus": ["Ultra 5 250K Plus", "250K Plus", "250K"],
    "WD SN3000 1TB": ["SN3000", "WD SN3000"],
    "WD SN3000 500GB": ["SN3000 500GB", "WD SN3000"],
    "星璨岚": ["星璨 岚"],
    "星璨 岚 Pro": ["星璨岚 Pro"],
    "CF500 镭风 1TB": ["CF500", "镭风 CF500"],
    "990 EVO 1TB": ["990 EVO"],
    "990 EVO Plus 1TB": ["990 EVO PLUS"],
    "WD_BLACK SN850X 1TB": ["SN850X 1T", "SN850X"],
    "WD_BLACK SN770 1TB": ["SN770"],
    "WD_BLACK SN580 1TB": ["SN580"],
}


# "RTX 5060 Ti 16G" 去掉容量后缀得到 "RTX 5060 Ti"。
# 平台标题常写成 "O16G" / "16GB" / 干脆不写容量，短名能显著提高命中率。
_CAPACITY_SUFFIX = re.compile(r"\s+\d+\s*[GT]B?$", re.IGNORECASE)

# 品牌前缀：真实标题常**不写**品牌，只写 "3080 12G"、"5700 8G"、"2080 8G"。
# 长前缀必须排在前面，否则 "GEFORCE RTX 5090" 只会被 "RTX " 截一刀。
_BRAND_PREFIXES: tuple[str, ...] = (
    "GEFORCE RTX ", "GEFORCE GTX ", "RTX ", "RX ", "GTX ", "ARC ",
)

# "数字 + 空格 + 短字母后缀" → 允许粘连（"5500 XT" → "5500XT"、"5070 Ti" → "5070Ti"）。
#
# ⚠️ 这不是锦上添花，而是**必须**：`normalize_text` 会把分隔符折叠成空格，
# 却不会把粘连的字母拆开 —— 所以 "5500XT" 与 "5500 XT" 在匹配器眼里是
# **两个完全不同的 token**。只给带空格的形式，写成 "5500XT 4G" 的标题
# 会整类漏匹配（实测闲鱼未匹配样本里这一类占大头）。
_GLUE_SUFFIX = re.compile(r"(\d)\s+([A-Za-z]{1,4})(?=\s|$)")

# 容量写法：8G ↔ 8GB。闲鱼标题两种都常见，而边界校验会让 "8G" 匹配不上 "8GB"。
_TO_GB = re.compile(r"(\d)\s*G(?![A-Za-z])")
_TO_G = re.compile(r"(\d)\s*GB(?![A-Za-z])")


# 品牌与型号数字粘连："RTX 4060 Ti" → "RTX4060 Ti"。
#
# ⚠️ 这条是修一个**真 bug**，不是锦上添花：
#    标题常写成 "微星RTX4060 Ti魔龙"（品牌与数字粘、后缀不粘），
#    而 normalize_text 不会把粘连的拆开 —— 于是所有以数字开头的别名
#    （"4060 Ti"）都会**左边界校验失败**（前面是 "X"，属于 [0-9A-Z]），
#    只剩过宽的 "RTX4060" 命中，结果 4060 Ti 被判成 4060 8G。
#    实测："技嘉RTX5070 Ti 魔鹰 16G" 被判成 RTX 5070 12G。
#
#    只粘**品牌那一段**（不粘后缀、不做全组合），避免别名组合爆炸。
_GLUE_BRAND = re.compile(r"^(GEFORCE RTX|GEFORCE GTX|RTX|RX|GTX|ARC)\s+", re.IGNORECASE)


def _alias_variants(model: str) -> set[str]:
    """机械派生的别名变体（不含 `_EXTRA_ALIASES` 里的人工别名）。

    这里刻意做成**通用规则**而不是逐型号手写：型号库会持续扩充
    （2026-09 一次加了 42 个显卡），手写别名必然跟不上，而
    "新加型号忘了写别名" 的表现是"页面搜不到"，很难归因。

    ⚠️ **"去品牌"与"去容量"绝对不能叠加。**
    两者相乘会产出**裸数字别名**（`RTX 3060 12G` → `3060`），而裸数字
    无法区分容量版本 —— 实测它会把
      · "华硕猛禽3080 vga联名 **12G**显卡" 抢到 **RTX 3080 10G**
      · "梅捷焱龙5500xt显卡…**8G**显存" 抢到 **RX 5500 XT 4G**
    即用户明确禁止的"把 3070 误匹配进 3070 Ti"那一类。
    代价是"型号与容量在标题里分离"的样本匹配不上 —— 那是**诚实的漏匹配**，
    比往库里写错容量好得多。
    """
    forms: set[str] = {model}

    # ① 去品牌前缀（**容量必须保留**）：真实标题常只写 "3080 12G"、"5700 8G"
    for prefix in _BRAND_PREFIXES:
        if model.upper().startswith(prefix):
            forms.add(model[len(prefix):])

    # ② 去容量后缀（**品牌必须保留**）："RTX 5060 Ti 16G" → "RTX 5060 Ti"。
    #    这是原有行为，用于标题干脆不写容量的场景；保留品牌前缀才能让
    #    "更具体的长别名优先"这条规则继续起作用。
    stripped = _CAPACITY_SUFFIX.sub("", model)
    if stripped != model:
        forms.add(stripped)

    # ③ 粘连（见 _GLUE_SUFFIX 注释：两种写法必须都有）
    forms |= {_GLUE_SUFFIX.sub(r"\1\2", f) for f in list(forms)}

    # ③b 品牌与数字粘连（见 _GLUE_BRAND 注释）
    forms |= {_GLUE_BRAND.sub(lambda m: m.group(1), f) for f in list(forms)}

    # ④ 容量写法 G ↔ GB
    forms |= {_TO_GB.sub(r"\1GB", f) for f in list(forms)}
    forms |= {_TO_G.sub(r"\1G", f) for f in list(forms)}

    # ⑤ 去空格（"RTX2080" / "RTX308012G" 这类）
    forms |= {f.replace(" ", "") for f in list(forms)}

    return forms


def build_aliases(model: str) -> str:
    """为型号生成别名串（逗号分隔），供跨平台标题匹配使用。

    变体越多命中率越高，但**每一条都仍受字母数字边界校验约束**
    （见 `services/normalize.ModelMatcher`）—— 所以 "3070" 不会命中
    "3070Ti"，"2080" 不会命中 "2080Super"。放宽的是"同一型号的写法差异"，
    不是"相邻型号的区分"。
    """
    base = _alias_variants(model)
    for alias in _EXTRA_ALIASES.get(model, []):
        base.add(alias)
    return ",".join(sorted(a for a in base if a))


def product_rows() -> list[dict]:
    """返回可直接写入 products 表的字典列表（含扩充包，按 model 去重）。"""
    from .seed_extra import EXTRA_PRODUCTS

    rows: list[dict] = []
    seen: set[str] = set()
    for category, brand, model, spec, base_price in list(_PRODUCTS_RAW) + list(EXTRA_PRODUCTS):
        if model in seen:
            continue
        seen.add(model)
        rows.append(
            {
                "category": category,
                "brand": brand,
                "model": model,
                "spec": spec,
                "base_price": float(base_price),
                "aliases": build_aliases(model),
                "is_active": True,
            }
        )
    return rows


def platform_rows() -> list[dict]:
    return [dict(p, is_active=True) for p in PLATFORMS]


def category_label(code: str) -> str:
    return CATEGORIES.get(code, code)
