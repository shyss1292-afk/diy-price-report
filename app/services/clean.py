"""报价清洗：剔除整机、合并商品与价格离群值。

真实电商搜索结果里混杂着大量噪声 —— 搜"RTX 5090"会返回：
  · 含该显卡的**整机**（实测出现 ¥817700 的报价，直接把价格区间撑爆）
  · **多型号合并**商品（"RTX5060/5060Ti/5070/5070Ti…"），价格无参考意义
  · 配件、转接线、水冷头等蹭关键词的商品

不做清洗的话，一个型号的"最高价"会被这些噪声主导，趋势图彻底失真。
"""
from __future__ import annotations

import re

# 命中即判定为整机 / 套装，不是单个配件
BUNDLE_KEYWORDS: tuple[str, ...] = (
    "整机", "主机", "组装机", "组装电脑", "全套", "台式机电脑",
    "电脑整机", "游戏台式", "工作站", "服务器整机", "准系统",
)

# 笔记本 —— 必须和桌面配件分开。
# 实测：拼多多搜 "RTX 5090 32G"，返回的整机里有 ROG 枪神9 这类游戏本，
# 价格 ¥27994 看着像显卡，其实是整台笔记本（移动版 GPU）。
LAPTOP_KEYWORDS: tuple[str, ...] = (
    "笔记本", "游戏本", "轻薄本", "超级本", "商务本", "办公本", "上网本",
    "枪神", "天选", "魔霸", "冰刃", "幻13", "幻14", "幻15", "幻16",
    "拯救者", "暗影精灵", "光影精灵", "游匣", "灵越", "外星人", "Alienware",
    "ThinkPad", "ThinkBook", "小新", "机械革命", "蛟龙", "耀世",
    "MagicBook", "MateBook", "RedmiBook", "MacBook", "平板电脑",
)

# 命中即判定为「非电脑配件」。
# 拼多多搜 "P400A"（追风者机箱）会返回「P400A 重型手动拔销器」这种工业工具 ——
# 型号是纯代号、又没有品牌词时，跨行业同名商品就会混进来。
OFF_TOPIC_KEYWORDS: tuple[str, ...] = (
    "拔销", "扳手", "螺丝刀", "五金", "工具", "量具", "电钻", "切割",
    "焊接", "水暖", "建材", "家具", "文具", "玩具", "汽车配件",
    "摩托车", "自行车", "服装", "运动鞋", "食品", "化妆品", "母婴",
    "宠物", "图书", "健身", "医疗器械", "农机", "渔具", "雨具",
    "轴承", "液压", "气动", "密封件", "紧固件",
)

# 全新配件的合理价格区间（相对 base_price）。放宽到 0.4~2.6 倍，
# 既能挡住整机与离谱值，又不会误杀停产涨价（如 4090）或渠道低价。
MIN_RATIO = 0.40
MAX_RATIO = 2.60

# 求购 / 收购帖 —— 闲鱼上大量"1500 收一张 3070"这类帖子，价格是**求购价**
# 不是成交价，混进价格区间会把趋势带偏。
#
# ⚠️ 这几条模式是**逐个在 10069 条存量真实明细上量过误杀率**之后才留下的，
#    不是拍脑袋写的。被否掉的两条（都实测误杀严重，绝不能加）：
#
#      · "收购" —— 命中 5 条，**全部**是卖家声明「绝不收购被封机码硬件」，
#        是**否定语境**，加进去等于误杀正常在售的卡。
#      · 编号式罗列 `1. 2. 3.` —— 命中 395 条（3.9%），全是把规格当编号
#        （"PCIe 5.0" / "蓝牙5.4" / "2.5K MiniLED"），误杀率远超收益。
#
# 所以判据只用**明确的求购语义**，并显式排除已知的反例：
#   · `(?<!回)` 排除"个人一手**回**收一张"（卖家在售，不是求购）
#   · `(?!货|款|入|藏…)` 排除"编号3088**收货**请拍开箱视频"
_WANTED_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"求购"),
    re.compile(r"(?<!回)收一[张块个台]"),                 # "自用收一张" / "收一个"
    re.compile(r"\d{2,}\s*[元块]?\s*收(?![货款入藏购件到益费])"),   # "1500收" / "620 收"
)

# 多件打包 —— 价格是**整包价**或单件批发价，不是一张卡的市场价。
# 实测 19 条存量命中，均为"一起24个打包出售"/"每片899打包出，一共有4片"这类。
_LOT_KEYWORDS: tuple[str, ...] = ("打包出", "打包出售", "打包卖")

_SEPARATORS = re.compile(r"[/／|｜、]")
_MULTI_MODEL_THRESHOLD = 3


def _looks_like_multi_model(title: str) -> bool:
    """标题里用 / 分隔出 3 个以上片段，多半是合并商品。"""
    return len(_SEPARATORS.findall(title)) >= _MULTI_MODEL_THRESHOLD


def check(price: float, base_price: float | None, title: str = "") -> tuple[bool, str]:
    """判断一条报价是否可用。

    Returns:
        (是否保留, 剔除原因)
    """
    text = title or ""

    for keyword in BUNDLE_KEYWORDS:
        if keyword in text:
            return False, f"整机/套装（命中「{keyword}」）"

    for keyword in LAPTOP_KEYWORDS:
        if keyword in text:
            return False, f"笔记本（命中「{keyword}」）"

    for keyword in OFF_TOPIC_KEYWORDS:
        if keyword in text:
            return False, f"非电脑配件（命中「{keyword}」）"

    for pattern in _WANTED_PATTERNS:
        if pattern.search(text):
            return False, f"求购帖（命中「{pattern.pattern}」）"

    for keyword in _LOT_KEYWORDS:
        if keyword in text:
            return False, f"多件打包（命中「{keyword}」）"

    if _looks_like_multi_model(text):
        return False, "多型号合并商品"

    if base_price and base_price > 0:
        ratio = price / base_price
        if ratio < MIN_RATIO:
            return False, f"价格异常偏低（基准价的 {ratio:.0%}）"
        if ratio > MAX_RATIO:
            return False, f"价格异常偏高（基准价的 {ratio:.0%}）"

    return True, ""
