"""从商品原始标题里提取「品牌」与「商品型号」，并生成 CPU 短型号名。

为什么需要
----------
参考表格（显卡日报）有两列：
  「最低品牌」= 华硕 / 索泰 / 影驰 ……（**卖货的板卡品牌**）
  「型号」    = TUF Gaming RTX 5090 OC 32G ……（**具体商品**）

而本项目的 products.brand 存的是**芯片厂商**（NVIDIA / AMD / Intel），
两者不是一回事。所以要另外从 listings.title_raw 里识别板卡品牌。

CPU 短型号名是为了贴近参考表格的写法：
  Core Ultra 7 270K Plus → U7-270K P
  Ryzen 9 9950X          → R9-9950X
  i9-14900K              → i9-14900K（本来就是短名）
"""
from __future__ import annotations

import re

# (标准品牌名, 别名列表) —— 顺序即优先级，先匹配到的先用
GPU_BRANDS: list[tuple[str, tuple[str, ...]]] = [
    ("华硕", ("华硕", "ASUS", "TUF", "ROG", "ATS", "巨齿鲨", "猛禽")),
    ("微星", ("微星", "MSI", "万图师", "魔龙", "超龙", "幻影师")),
    ("技嘉", ("技嘉", "GIGABYTE", "AORUS", "魔鹰", "猎鹰", "风魔")),
    ("七彩虹", ("七彩虹", "Colorful", "iGame", "战斧", "镭风")),
    ("影驰", ("影驰", "GALAX", "金属大师", "星曜", "大将", "名人堂")),
    ("耕升", ("耕升", "Gainward", "追风", "踏雪", "星极")),
    ("索泰", ("索泰", "ZOTAC", "天启", "AMP", "霹雳版")),
    ("映众", ("映众", "Inno3D", "冰龙", "曜夜", "电竞叛客")),
    ("万丽", ("万丽", "Manli", "星云", "雪狐", "璎珞")),
    ("铭瑄", ("铭瑄", "MaxSun", "终结者", "瑄墨", "电竞之心")),
    ("盈通", ("盈通", "Yeston", "豪华版", "樱瞳", "萌宠")),
    ("翔升", ("翔升", "ASL", "曜影")),
    ("丽台", ("丽台", "Leadtek", "WinFast")),
    ("瀚铠", ("瀚铠", "HanKai")),
    ("蓝宝石", ("蓝宝石", "Sapphire", "超白金", "极光", "白金版")),
    ("讯景", ("讯景", "XFX", "雪狼", "黑狼")),
    ("撼讯", ("撼讯", "PowerColor", "红魔", "暗黑犬")),
    ("华擎", ("华擎", "ASRock", "钢铁传奇", "幻影电竞")),
    ("迪兰", ("迪兰", "Dataland", "战将")),
    ("旌宇", ("旌宇", "Sparkle")),
    ("PNY", ("PNY", "必恩威")),
    ("蓝戟", ("蓝戟", "GUNNIR")),
    ("锐炫", ("锐炫", "Arc")),
]

CPU_BRANDS: list[tuple[str, tuple[str, ...]]] = [
    ("Intel", ("Intel", "英特尔", "酷睿", "core", "i3-", "i5-", "i7-", "i9-")),
    ("AMD", ("AMD", "锐龙", "ryzen", "R3-", "R5-", "R7-", "R9-")),
]

# 商品标题里常见的、不属于型号的噪声词（比价时用来裁剪显示）
_NOISE = ("包邮", "现货", "全新", "正品", "顺丰", "国行", "拆机", "二手", "二手价",
          "分期", "免息", "官方", "旗舰店", "自营", "当天发货", "支持", "显卡")


def guess_brand(title: str, category: str) -> str:
    """从标题猜测板卡品牌；识别不出返回空串（不猜、不编）。

    取**出现位置最靠前**的品牌，而不是品牌表里排最前的那个 ——
    标题通常以品牌开头（"万丽RTX5090D 32G 显卡 影驰同款"），
    按表序匹配会把"影驰"这种出现在修饰语里的词误判成主品牌。
    """
    table = CPU_BRANDS if category == "cpu" else GPU_BRANDS
    text = (title or "").strip()
    upper = text.upper()

    best: tuple[int, str] | None = None
    for name, aliases in table:
        pos = None
        for alias in aliases:
            idx = upper.find(alias.upper()) if alias.isascii() else text.find(alias)
            if idx >= 0 and (pos is None or idx < pos):
                pos = idx
        if pos is not None and (best is None or pos < best[0]):
            best = (pos, name)
    return best[1] if best else ""


def clean_title(title: str, limit: int = 40) -> str:
    """裁出标题里最像「商品型号」的一段，用于表格显示。"""
    text = re.sub(r"\s+", " ", (title or "").strip())
    # 去掉促销噪声后缀（从噪声词开始截断）
    for word in _NOISE:
        pos = text.find(word)
        if pos > 8:
            text = text[:pos]
            break
    text = text.strip(" -|,，、/")
    return text[:limit] if len(text) > limit else text


# ------------------------------------------------------------------ CPU 短型号

_ULTRA_RE = re.compile(r"Core\s+Ultra\s+([3579])\s+(\d+K(?:F|S)?)(?:\s*(Plus|P))?", re.I)
_RYZEN_RE = re.compile(r"Ryzen\s+([3579])\s+(\d+\w*?)(?:X3D|X|F|G|GE)?\b", re.I)
_INTEL_RE = re.compile(r"\b(i[3579])-(\d{4,5}\w*)\b", re.I)


def short_model(model: str, category: str) -> str:
    """生成贴近参考表格写法的短型号名；不匹配时原样返回。"""
    name = (model or "").strip()

    if category == "cpu":
        m = _ULTRA_RE.search(name)
        if m:
            tier, num, plus = m.group(1), m.group(2), m.group(3)
            suffix = " P" if plus and plus.lower() in ("plus", "p") else ""
            return f"U{tier}-{num}{suffix}"

        m = _RYZEN_RE.search(name)
        if m:
            tier, num = m.group(1), m.group(2)
            tail = name[m.end(2):].strip()
            tail = re.sub(r"^(X3D|XT|X|F|G|GE|X3D)", lambda x: x.group(1), tail)
            return f"R{tier}-{num}{tail[:4]}"

        m = _INTEL_RE.search(name)
        if m:
            return f"{m.group(1).lower()}-{m.group(2)}"

    return name
