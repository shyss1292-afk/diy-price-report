"""趋势指标计算。

所有指标都基于「跨平台口径」的日线序列：
  all_low  —— 全市场最低价（含二手），最贴近"最低多少钱能买到"
  new_low  —— 全新平台最低价
  used_low —— 二手平台最低价
  all_avg  —— 全市场均价
"""
from __future__ import annotations

import statistics
import threading
from datetime import date, timedelta
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Listing, Platform, PriceDaily, Product
from .aggregate import P25_MIN_SAMPLES

SERIES_KEYS = ("new_low", "used_low", "all_low", "new_avg", "all_avg", "all_high")

# 稳健序列（25 分位）—— **只供指标计算，不对外暴露**。
# 与 SERIES_KEYS 分开命名，是为了让"界面上显示的最低价"和"算涨跌幅用的底价"
# 一眼能区分开：前者是真实挂牌的最低价（可以真的买到），后者是去掉最便宜那
# 一小撮之后的稳健价（不会被一条引流价带跑）。
ROBUST_KEYS = ("new_low_robust", "used_low_robust", "all_low_robust")

_BASIS_ROBUST = {"new": "new_low_robust", "used": "used_low_robust", "all": "all_low_robust"}

# ---- 可比性闸门参数（**只有这一份定义**，见 _comparable）----
#
# MIN_SAMPLES 与聚合层的 `P25_MIN_SAMPLES` **必须是同一个数**，理由见那边注释：
# 样本不足 `P25_MIN_SAMPLES` 时稳健价退回 min，如果一个序列里既有"退回的 min"
# 又有"真 p25"，两者不在一个尺度上，相减会造出假涨跌。
# 直接从聚合层 import，避免任何一方被单独改动。
MIN_SAMPLES = P25_MIN_SAMPLES  # 两天各自的样本量下限
MAX_SAMPLE_RATIO = 4   # 两天样本量之比上限（17 条的最低价比 3 条的更容易低）
MIN_JACCARD = 0.5      # 两天平台集合的 Jaccard 重合度下限


class _Entry(NamedTuple):
    """某个 (型号, 平台, 交易日) 的一行聚合。

    用 NamedTuple 而不是裸元组：这里字段从 6 个涨到 7 个（加了 `p25`），
    裸元组靠下标取值时**插入一个字段就会静默错位**（`max` 变 `avg` 这种），
    且不会有任何报错。命名后按属性取，改字段顺序也不会错。
    """

    platform_id: int
    kind: str
    low: float
    p25: float
    high: float
    avg: float
    count: int


def _robust_low(vals: list[_Entry]) -> float | None:
    """样本量足够的平台里，各平台 p25 的最小值。

    **为什么必须卡样本量**：`aggregate._p25` 在样本 < `P25_MIN_SAMPLES` 时
    退回 min（3 条算分位没意义）。那些"退回值"不是稳健价，拿它们参与
    "跨平台取最小"就等于把**单条样本**请了回来 ——

      实测 RTX 5060 Ti 16G：京东当天只有 1 条 ¥3349（退回 min），
      闲鱼 19 条的真 p25 是 ¥4425。跨平台 min 被那条单样本拉到 3349，
      而前一天稳健值是 ¥4400 → 算出 **-23.9%**；
      可同一张卡片上"现价"显示的是真实最低价 ¥3300 vs ¥2959（**在涨**）
      —— 现价在涨、涨跌幅在跌，用户看到的就是"根本对不上"。

    所以：稳健价只在**样本够**的平台之间比较；一个都没有就如实返回 None
    （该天不参与涨跌计算）。
    """
    strong = [e for e in vals if e.count >= MIN_SAMPLES]
    return min(e.p25 for e in strong) if strong else None


def _comparable(series: dict, idx: int | None, prev_idx: int | None) -> bool:
    """两个下标对应的交易日是否**口径可比**。

    判据用**平台集合的 Jaccard 重合度**，不是"平台数量"：
      · 两天都只有闲鱼 → 重合度 1.0 → **可比**（同为二手口径）
        （早先试过"平台数必须 ≥2"，结果 123 个型号只剩 2 个有涨跌幅 ——
          真实数据里大部分型号每天就只有闲鱼一个平台）
      · 昨天 {jd,pdd,xianyu}、今天 {pdd} → 重合度 0.33 → 不可比
    再加**样本量**两道：各 ≥3 条（单条样本的"最低价"就是那条本身，
    与几十条里的最低价不可比）；且量级相当（≤4 倍，样本越多越容易捞到极端低价）。

    这套判据原先只长在 `build_snapshot`（首页涨跌榜）里，而**详情页的
    d1/d7/d30 完全没有闸门** —— 所以同一个型号在首页显示"—"、点进详情页
    却显示"一天涨 44%"，用户看到的就是后者。抽成函数后两处共用。
    """
    if idx is None or prev_idx is None:
        return False
    # ⚠️ 比的是**有效平台集合**（样本量够算 p25 的那些），不是"所有平台"。
    #
    # 踩过的坑（Core Ultra 7 265K，2026-10-03）：
    #   10-02 只有闲鱼（18 条，p25 ¥1435）→ 10-03 变成京东 5 条 + 闲鱼 3 条，
    #   闲鱼那 3 条不够算 p25，于是稳健值只剩京东的 ¥1899（全新）。
    #   若按"所有平台"算，10-03 {jd,xianyu} vs 10-02 {xianyu} 的 Jaccard 恰好
    #   是 0.5（放行了），可**两边真正参与比较的平台根本没重合** ——
    #   本质是"拿京东全新价 vs 闲鱼二手价"，算出假 +32.33%。
    #   按有效平台算：{jd} vs {xianyu} 交集为空 → 不可比 → 显示 —。
    sets = series.get("robust_platform_ids") or series.get("platform_ids") or []
    counts = series.get("sample_counts") or []
    if idx >= len(sets) or prev_idx >= len(sets):
        return False
    a, b = sets[idx], sets[prev_idx]
    union = a | b
    if not union or len(a & b) / len(union) < MIN_JACCARD:
        return False
    if idx >= len(counts) or prev_idx >= len(counts):
        return False
    a_n, b_n = counts[idx], counts[prev_idx]
    if a_n < MIN_SAMPLES or b_n < MIN_SAMPLES:
        return False
    return max(a_n, b_n) <= min(a_n, b_n) * MAX_SAMPLE_RATIO


# ---------------------------------------------------------------- 市场序列缓存

# 为什么需要缓存
# ---------------
# `load_market_series()` 要把 300+ 型号 × 180 天的聚合行全部读出来再在 Python 里
# 拼成序列，实测约 110ms（冷启动首次要读 7 万行，接近 800ms）。而打开一次首页会
# 触发 **5 次**：
#     /api/overview  内部 3 次（自身快照 + 涨跌榜上下各一次）
#     /api/market-index  1 次
#     /api/products      1 次（按板块两次）
# 它们在同一个服务里并发执行，于是首屏要等好几秒。
#
# 序列只依赖「天数 + 品类」和库里的数据，而数据只在采集时变化（每小时一轮），
# 所以按数据版本号做进程内缓存即可 —— 版本没变就直接复用。
#
# 返回值视为**只读**：调用方不得修改 frame 或其内部的列表，否则会污染缓存。

_FRAME_CACHE: dict[tuple, tuple[str, dict]] = {}
_FRAME_CACHE_MAX = 16

# 单飞锁：首屏那 5 个请求会同时到达、同时发现缓存是空的。
# 没有这把锁，每个请求都会把整份序列算一遍（实测首屏 4 秒）；
# 有了它，只有第一个真正去算，其余等结果复用。
_FRAME_LOCKS: dict[tuple, threading.Lock] = {}
_FRAME_LOCKS_GUARD = threading.Lock()


def _frame_lock(key: tuple) -> threading.Lock:
    with _FRAME_LOCKS_GUARD:
        lock = _FRAME_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _FRAME_LOCKS[key] = lock
        return lock


def data_version(session: Session) -> str:
    """数据版本号 —— 库里数据变了它才变，用于让缓存失效。

    取多个维度是因为不同变更方式留下的痕迹不同：
      · PriceDaily 的 MAX(id) 能捕获"删除后重新插入"（采集流水线的固定行为）
      · MAX(trade_date) 能捕获跨日
      · COUNT(*) 能捕获纯粹的删除
      · 型号数 / 启用平台数变化会改变口径，也必须算进去

    ⚠️ **必须同时盯 `listings`**（踩过的坑）
    --------------------------------------------------
    最早只盯 `price_daily`（聚合表），因为趋势缓存读的就是它。但覆盖统计
    （`services/coverage.py`）读的是 **`listings` 明细表** —— 于是采集刚写完明细、
    聚合还没跑完的那段时间里，覆盖数会停在旧值：界面上"已采到 84 个"明明该变成
    85，刷新却纹丝不动。实测就是这么发现的（在库副本上写一条真实报价，覆盖数不变）。

    这里只用 `MAX(listing.id)` 而**不**加 `COUNT(*)`：主键 MAX 走索引，实测 **0.1ms**；
    而 COUNT(*) 要 8.6ms —— 这个函数每个请求都要调，不能为它多花 8ms。
    明细的删除只发生在流水线里、且总是伴随重新插入（纯删除会被跳过），
    所以 MAX(id) 足够捕获变更。
    """
    row = session.execute(
        select(
            func.max(PriceDaily.id),
            func.max(PriceDaily.trade_date),
            func.count(PriceDaily.id),
        )
    ).one()
    listing_max = session.execute(select(func.max(Listing.id))).scalar()
    product_count = session.execute(select(func.count(Product.id))).scalar() or 0
    # 平台启停会改变口径（只统计启用平台），也必须纳入版本号
    platform_count = (
        session.execute(
            select(func.count()).select_from(Platform).where(Platform.is_active.is_(True))
        ).scalar()
        or 0
    )
    return f"{row[0]}|{row[1]}|{row[2]}|{listing_max}|{product_count}|{platform_count}"


def warm_market_cache(session: Session, days: int = 180) -> None:
    """预计算默认窗口的市场序列，供服务启动时预热调用。

    目的：让**第一个**用户请求也走缓存。否则服务重启后的首次访问要等整份序列
    算完（实测约 800ms），用户会感觉"打开网站要等一下"。
    """
    load_market_series(session, days=days)


# ---------------------------------------------------------------- 工具

def _pct(cur: float | None, prev: float | None) -> float | None:
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / prev * 100.0


def _lookup(dates: list[date], values: list[float | None], target: date) -> float | None:
    """取 target 当日或之前最近的可用值。"""
    for i in range(len(dates) - 1, -1, -1):
        if dates[i] <= target:
            return values[i]
    return None


def _lookup_idx(dates: list[date], target: date) -> int | None:
    """取 target 当日或之前最近可用值的**下标**。

    与 `_lookup` 配对使用：`_lookup` 取值，这个取下标 ——
    需要读**同期的其它字段**（如当天的平台覆盖数）时用它对齐。
    """
    for i in range(len(dates) - 1, -1, -1):
        if dates[i] <= target:
            return i
    return None


def _clean(values: list[float | None]) -> list[float]:
    return [v for v in values if v is not None]


def _ma(values: list[float | None], window: int) -> float | None:
    seq = _clean(values[-window:])
    if len(seq) < max(2, window // 3):
        return None
    return sum(seq) / len(seq)


def _volatility(values: list[float | None], window: int = 30) -> float | None:
    seq = _clean(values[-(window + 1) :])
    if len(seq) < 5:
        return None
    rets = [
        (seq[i] - seq[i - 1]) / seq[i - 1]
        for i in range(1, len(seq))
        if seq[i - 1]
    ]
    if len(rets) < 4:
        return None
    return statistics.pstdev(rets) * 100.0


def _percentile(values: list[float | None], window: int = 90) -> float | None:
    seq = _clean(values[-window:])
    if len(seq) < 5:
        return None
    last = seq[-1]
    below = sum(1 for v in seq if v <= last)
    return below / len(seq) * 100.0


# ---------------------------------------------------------------- 市场序列

def load_market_series(
    session: Session, days: int = 180, category: str | None = None
) -> dict[int, dict]:
    """加载每个型号的跨平台日线序列（带进程内缓存，返回值视为只读）。"""
    key = (days, category)
    version = data_version(session)

    hit = _FRAME_CACHE.get(key)
    if hit is not None and hit[0] == version:
        return hit[1]

    with _frame_lock(key):
        # 双重检查：等在锁上的这段时间里，可能已经有别的线程算好了
        hit = _FRAME_CACHE.get(key)
        if hit is not None and hit[0] == version:
            return hit[1]

        frame = _compute_market_series(session, days=days, category=category)

        # 简单 LRU：满了就丢掉最早插入的一项（组合数很少，不会真成为瓶颈）
        if len(_FRAME_CACHE) >= _FRAME_CACHE_MAX and key not in _FRAME_CACHE:
            _FRAME_CACHE.pop(next(iter(_FRAME_CACHE)))
        _FRAME_CACHE[key] = (version, frame)
        return frame


def _compute_market_series(
    session: Session, days: int = 180, category: str | None = None
) -> dict[int, dict]:
    """真正干活的实现 —— 只允许 `load_market_series()` 调用。"""
    since = date.today() - timedelta(days=days - 1)

    stmt = (
        select(
            PriceDaily.product_id,
            PriceDaily.trade_date,
            PriceDaily.platform_id,
            Platform.kind,
            PriceDaily.min_price,
            PriceDaily.p25_price,
            PriceDaily.max_price,
            PriceDaily.avg_price,
            PriceDaily.sample_count,
        )
        .join(Platform, Platform.id == PriceDaily.platform_id)
        .where(
            PriceDaily.trade_date >= since,
            # 停用平台不参与市场均线 —— 它们的报价口径不同（媒体参考价系统性偏高），
            # 混进来会把"市场行情线"整体抬高。
            Platform.is_active.is_(True),
        )
        # 按 platform_id 逐行取（不再按 kind 预聚合）—— 需要知道每天
        # **具体是哪几个平台**在报价，这是判断跨日"可不可比"的唯一依据。
        # 实测 RTX 5090 在 9/17 有 3 个平台（最低 ¥7898，闲鱼二手），
        # 9/22 只剩 1 个（¥47777，拼多多全新）—— 直接取差值算出 +505%，
        # 那是**口径换了**，不是涨价。
    )
    if category:
        stmt = stmt.join(Product, Product.id == PriceDaily.product_id).where(
            Product.category == category
        )

    raw: dict[int, dict[date, list[_Entry]]] = {}
    for pid, d, plid, kind, lo, p25, hi, avg, cnt in session.execute(stmt).all():
        low = float(lo)
        # `p25_price` 是后加的列：迁移刚补上、聚合还没重算时它是 0。
        # 0 会让"稳健底价"变成 ¥0（比坏数据更糟），所以**只认正数**，
        # 否则如实退回 min —— 老数据最多是"没有变稳"，不会凭空多出 0 元报价。
        robust = float(p25) if p25 and float(p25) > 0 else low
        raw.setdefault(pid, {}).setdefault(d, []).append(
            _Entry(int(plid), kind, low, robust, float(hi), float(avg), int(cnt))
        )

    frame: dict[int, dict] = {}
    for pid, by_date in raw.items():
        dates = sorted(by_date)
        series: dict = {"dates": dates}
        for key in SERIES_KEYS + ROBUST_KEYS:
            series[key] = []
        # 每天**有哪些平台**在报价（与 SERIES_KEYS 对齐的独立字段，不是价格序列）。
        # 存集合而不是数量 —— 判"可不可比"要看**集合是否重合**：
        # 「两天都只有闲鱼」可比（同为二手口径），
        # 「昨天三平台、今天只有拼多多」不可比（口径整个换了）。
        series["platform_ids"] = []
        # **有效平台** = 当天样本量够算 p25 的平台。闸门比的是这个，不是"所有平台"。
        # 理由见 `_comparable`。
        series["robust_platform_ids"] = []
        series["sample_counts"] = []

        for d in dates:
            entries = by_date[d]
            new_vals = [e for e in entries if e.kind == "new"]
            used_vals = [e for e in entries if e.kind == "used"]

            series["new_low"].append(min(e.low for e in new_vals) if new_vals else None)
            series["used_low"].append(min(e.low for e in used_vals) if used_vals else None)
            series["all_low"].append(min(e.low for e in entries) if entries else None)
            series["all_high"].append(max(e.high for e in entries) if entries else None)
            series["new_avg"].append(
                sum(e.avg for e in new_vals) / len(new_vals) if new_vals else None
            )
            series["all_avg"].append(
                sum(e.avg for e in entries) / len(entries) if entries else None
            )
            # ---- 稳健底价（25 分位）----
            # 语义："在**每个平台内部**先排除最便宜的那一小撮，再跨平台比最低"。
            # 直接对全体报价取分位是错的 —— 那会把"闲鱼二手"和"京东全新"
            # 混成一个样本池，分位点落在两个价带之间，两头都不代表。
            # 样本量的门槛见 `_robust_low`。
            series["new_low_robust"].append(_robust_low(new_vals))
            series["used_low_robust"].append(_robust_low(used_vals))
            series["all_low_robust"].append(_robust_low(entries))
            series["platform_ids"].append(frozenset(e.platform_id for e in entries))
            series["robust_platform_ids"].append(
                frozenset(e.platform_id for e in entries if e.count >= MIN_SAMPLES)
            )
            # 当天该型号的**总样本条数** —— 最低价的稳定性取决于它。
            # 单条样本的"最低价"就是那条本身，跟多天几十条里的最低价
            # 不是一回事（实测 990 EVO 1TB：1 条 ¥350 vs 2 条 ¥835，算出 +139%）。
            series["sample_counts"].append(sum(e.count for e in entries))

        # 只回填**前导** None（首次有数据之前的那几个），让图表左端连续。
        #
        # ⚠️ 绝不能回填中间缺口和末尾 —— 那是**错数据**：
        #    某型号按 basis=new 切时，末尾几天可能只有二手没有全新，
        #    那些位置是 None。回填成"第一个有效值"会让界面显示一个
        #    很久以前的全新价，而用户以为那是最新的。
        #    （原实现用 `if v is None` 无差别回填，正是这个毛病。）
        #    中间缺口保持 None，图表自己会断开 —— 断开的线是诚实的，
        #    连上去的假线不是。
        for key in SERIES_KEYS + ROBUST_KEYS:
            seq = series[key]
            first = next((v for v in seq if v is not None), None)
            if first is None:
                continue
            for i, v in enumerate(seq):
                if v is not None:
                    break
                seq[i] = first

        frame[pid] = series
    return frame


# ---------------------------------------------------------------- 指标

def compute_metrics(series: dict) -> dict:
    """基于日线序列计算趋势指标。

    ⚠️ **涨跌幅 / 分位 / 均线一律用稳健序列（25 分位），不用 min**
    ---------------------------------------------------------
    最低价是**单点**：当天最便宜的那一条说了算，于是"最便宜的那条换人了"
    就被读成"涨价了"。实测两例（用户 2026-10-03 直接质疑的两个数）：
      · RTX 4080 Super 16G：09-30 京东零售卡 ¥7039 → 10-02 京东 3 条
        全是涡轮/工包卡（最低 ¥9800）→ d1 = **+39.2%**、d7 = **+44.1%**
      · RX 7600 8G：闲鱼一条 ¥1050 的引流价 → 次日 ¥1420 → **+35.2%**
    两者都是"最低价换人"，不是市场涨价。稳健序列 + `_comparable` 闸门后，
    这两条都会落回真实波动（±2%）或直接置 None（显示 —）。

    `history.min`（历史最低价）**仍用真实 min** —— 那是真实存在的挂牌价，
    用户要看"最低多少钱买到过"；而且要看的正是"是不是真出现过这个价"。
    """
    dates: list[date] = series["dates"]
    low: list[float | None] = series["all_low"]
    if not dates:
        return {}

    # 稳健序列。老缓存 / 构造出来的 series 可能没有这个键，退回 min 序列。
    robust: list[float | None] = series.get("all_low_robust") or low

    today = dates[-1]
    last = low[-1]
    last_robust = robust[-1]
    last_idx = len(dates) - 1

    changes: dict[str, dict | None] = {}
    for label, back in (("d1", 1), ("d7", 7), ("d30", 30)):
        target = today - timedelta(days=back)
        prev_idx = _lookup_idx(dates, target)
        prev = _lookup(dates, robust, target)
        pct = _pct(last_robust, prev)
        # ---- 可比性闸门（原先只有首页涨跌榜有，详情页缺）----
        if not _comparable(series, last_idx, prev_idx):
            pct = None
        changes[label] = (
            None
            if pct is None
            else {"abs": round((last_robust or 0) - (prev or 0), 2), "pct": round(pct, 2)}
        )

    clean_low = _clean(low)
    hist_min = min(clean_low) if clean_low else None
    hist_max = max(clean_low) if clean_low else None
    min_date = dates[low.index(hist_min)] if hist_min is not None else None
    max_date = dates[low.index(hist_max)] if hist_max is not None else None

    pct90 = _percentile(robust, 90)
    chg30 = changes["d30"]["pct"] if changes["d30"] else None
    chg7 = changes["d7"]["pct"] if changes["d7"] else None

    return {
        "last_date": today.isoformat(),
        "last_low": round(last, 2) if last is not None else None,
        "last_new_low": _r(series["new_low"][-1]),
        "last_used_low": _r(series["used_low"][-1]),
        "last_avg": _r(series["all_avg"][-1]),
        "changes": changes,
        # 均线 / 波动率同用稳健序列 —— 它们在界面上是"判断价位"的依据，
        # 被一条引流价拉低会给出"已到低位、可入手"的错误信号。
        "ma7": _r(_ma(robust, 7)),
        "ma30": _r(_ma(robust, 30)),
        "volatility_30d": _r(_volatility(robust, 30), 3),
        "percentile_90d": _r(pct90, 1),
        "history": {
            "min": _r(hist_min),
            "max": _r(hist_max),
            "min_date": min_date.isoformat() if min_date else None,
            "max_date": max_date.isoformat() if max_date else None,
            "span_pct": _r(_pct(hist_max, hist_min), 1),
        },
        "signal": _signal(pct90, chg30, chg7),
    }


def _r(v, nd: int = 2):
    return None if v is None else round(float(v), nd)


def _signal(pct90: float | None, chg30: float | None, chg7: float | None) -> dict:
    if pct90 is None:
        return {"level": "neutral", "text": "数据积累中"}
    if pct90 <= 20:
        return {"level": "good", "text": "处于近 90 天低位，可考虑入手"}
    if pct90 >= 80:
        return {"level": "warn", "text": "处于近 90 天高位，建议观望"}
    if chg30 is not None and chg30 >= 8:
        return {"level": "warn", "text": "近 30 天涨幅明显，留意追高"}
    if chg30 is not None and chg30 <= -8:
        return {"level": "good", "text": "近 30 天明显回落"}
    if chg7 is not None and abs(chg7) < 0.5:
        return {"level": "neutral", "text": "价格平稳，波动很小"}
    return {"level": "neutral", "text": "价格区间内正常波动"}


# ---------------------------------------------------------------- 平台维度

def load_platform_series(session: Session, product_id: int, days: int = 180) -> list[dict]:
    """加载某型号在各平台的日线序列。"""
    since = date.today() - timedelta(days=days - 1)
    stmt = (
        select(
            Platform.id,
            Platform.code,
            Platform.name,
            Platform.kind,
            Platform.color,
            PriceDaily.trade_date,
            PriceDaily.min_price,
            PriceDaily.max_price,
            PriceDaily.avg_price,
            PriceDaily.sample_count,
        )
        .join(Platform, Platform.id == PriceDaily.platform_id)
        .where(
            PriceDaily.product_id == product_id,
            PriceDaily.trade_date >= since,
            # 只看仍在用的平台 —— 停用平台的聚合行会残留在表里（数据未删，
            # 便于回滚），不过滤就会在型号详情/对比页冒出来。
            Platform.is_active.is_(True),
        )
        .order_by(Platform.sort_order, PriceDaily.trade_date)
    )

    grouped: dict[str, dict] = {}
    for pid, code, name, kind, color, d, lo, hi, avg, cnt in session.execute(stmt).all():
        item = grouped.setdefault(
            code,
            {
                "platform_id": pid,
                "code": code,
                "name": name,
                "kind": kind,
                "color": color,
                "dates": [],
                "min": [],
                "max": [],
                "avg": [],
                "last_count": 0,
            },
        )
        item["dates"].append(d)
        item["min"].append(round(float(lo), 2))
        item["max"].append(round(float(hi), 2))
        item["avg"].append(round(float(avg), 2))
        item["last_count"] = int(cnt)

    return list(grouped.values())


def _spread_of(rows: list[dict]) -> dict | None:
    """同口径平台间价差（少于 2 个平台无意义）。"""
    if len(rows) < 2:
        return None
    lo, hi = rows[0], rows[-1]
    return {
        "cheapest": {"code": lo["code"], "name": lo["name"], "price": lo["min"]},
        "priciest": {"code": hi["code"], "name": hi["name"], "price": hi["min"]},
        "abs": _r(hi["min"] - lo["min"]),
        "pct": _r(_pct(hi["min"], lo["min"]), 2),
    }


def build_latest_platform_table(platform_series: list[dict]) -> tuple[list[dict], dict]:
    """取每个平台最后一天的价格，用于横向比价。

    价差按口径分开计算：全新平台之间比、二手平台之间比，
    避免把二手价和全新价混在一起得出无意义的 90% 价差。
    """
    table: list[dict] = []
    for item in platform_series:
        if not item["dates"]:
            continue
        table.append(
            {
                "code": item["code"],
                "name": item["name"],
                "kind": item["kind"],
                "color": item["color"],
                "date": item["dates"][-1].isoformat(),
                "min": item["min"][-1],
                "avg": item["avg"][-1],
                "max": item["max"][-1],
                "sample_count": item["last_count"],
            }
        )
    table.sort(key=lambda r: r["min"])

    if table:
        base = table[0]["min"]
        for row in table:
            row["vs_cheapest_pct"] = _r(_pct(row["min"], base), 2)
            row["is_cheapest"] = row["min"] == base

    spread = {
        "new": _spread_of([r for r in table if r["kind"] == "new"]),
        "used": _spread_of([r for r in table if r["kind"] == "used"]),
        "overall": _spread_of(table),
    }
    return table, spread


# ---------------------------------------------------------------- 对外入口

def build_product_trend(session: Session, product_id: int, days: int = 180) -> dict | None:
    product = session.get(Product, product_id)
    if product is None:
        return None

    frame = load_market_series(session, days=days)
    series = frame.get(product_id)
    if not series or not series["dates"]:
        return {
            "product": _product_brief(product),
            "metrics": {},
            "series": {"dates": [], **{k: [] for k in SERIES_KEYS}},
            "platforms": [],
            "latest_platforms": [],
            "spread": None,
        }

    metrics = compute_metrics(series)
    platform_series = load_platform_series(session, product_id, days=days)
    latest_table, spread = build_latest_platform_table(platform_series)
    if spread:
        metrics["platform_spread"] = spread

    return {
        "product": _product_brief(product),
        "metrics": metrics,
        "series": {
            "dates": [d.isoformat() for d in series["dates"]],
            **{k: series[k] for k in SERIES_KEYS},
        },
        "platforms": [
            {
                **{k: v for k, v in item.items() if k not in ("dates", "min", "max", "avg")},
                "dates": [d.isoformat() for d in item["dates"]],
                "min": item["min"],
                "max": item["max"],
                "avg": item["avg"],
            }
            for item in platform_series
        ],
        "latest_platforms": latest_table,
        "spread": spread,
    }


def _product_brief(product: Product) -> dict:
    from ..seed_data import category_label

    return {
        "id": product.id,
        "category": product.category,
        "category_label": category_label(product.category),
        "brand": product.brand,
        "model": product.model,
        "spec": product.spec,
        "base_price": product.base_price,
    }


BASIS_KEYS = {"all": "all_low", "new": "new_low", "used": "used_low"}


def build_snapshot(
    session: Session,
    period: int = 1,
    category: str | None = None,
    days: int = 180,
    basis: str = "all",
) -> list[dict]:
    """全型号最新价格快照（含区间涨跌与迷你走势）。

    Args:
        basis: 主口径 —— all 全市场最低价 / new 全新最低价 / used 二手最低价
    """
    from ..seed_data import category_label

    key = BASIS_KEYS.get(basis, "all_low")
    robust_key = _BASIS_ROBUST.get(basis, "all_low_robust")
    frame = load_market_series(session, days=days, category=category)
    if not frame:
        return []

    products = {
        p.id: p
        for p in session.execute(
            select(Product).where(Product.id.in_(list(frame.keys())))
        ).scalars()
    }

    rows: list[dict] = []
    today = date.today()
    for pid, series in frame.items():
        product = products.get(pid)
        if product is None:
            continue
        dates, low = series["dates"], series[key]
        if not dates:
            continue

        # 「最新有效报价兜底」：从末尾往前找**第一个有值**的日期。
        #
        # 为什么不能直接取 `low[-1]`：`dates` 是这个型号**自己有数据**的日期，
        # 但按 basis=new / used 口径切时，某天可能只有二手、没有全新 ——
        # 那个位置就是 None，直接取末位会把"有数据的型号"误判成空白。
        #
        # 另外整点分批轮巡下，型号今天可能还没轮到，末位日期就是昨天/前天。
        # 这不是要隐藏的缺陷，而是**要如实标出来**的信息（见 captured_date /
        # is_today / stale_days 三个字段）—— 用户有权知道这个价是哪天的。
        idx = None
        for i in range(len(low) - 1, -1, -1):
            if low[i] is not None:
                idx = i
                break
        if idx is None:
            continue

        last_date = dates[idx]
        last = low[idx]
        robust = series.get(robust_key) or low
        # 涨跌基准跟着**数据日期**走，不是跟着"今天"走 ——
        # 否则拿昨天的价跟"今天减 7 天"比，区间口径就错了。
        target = last_date - timedelta(days=period)
        prev_idx = _lookup_idx(dates, target)
        # 显示用的"上一价"仍是**真实最低价**（和 latest 同一把尺子）；
        # 涨跌幅改用稳健序列 —— 否则会出现"现价 ¥1420、上一价 ¥1050、
        # 涨幅只有 1%"这种自相矛盾的展示（见 compute_metrics 的说明）。
        prev = low[prev_idx] if prev_idx is not None else None
        pct = _pct(
            robust[idx], robust[prev_idx] if prev_idx is not None else None
        )

        # ---- 可比性闸门（判据与常量见模块顶部的 `_comparable`）----
        # 「全市场最低价」是跨平台取 min，**样本一换结论就变**。实测 RTX 5090：
        #   9/17 有 3 个平台（最低 ¥7898，闲鱼二手）→ 9/22 只剩 1 个
        #   （¥47777，拼多多全新）→ 直接取差值 = **+505%**，
        #   而真实市场并没有涨 5 倍 —— 那是**口径换了**，不是涨价。
        # 不够就置空（界面显示 —）。**宁可缺，不可错**：
        # 一个 +505% 的假信号比一个空格有害得多。
        prev_idx = _lookup_idx(dates, last_date - timedelta(days=period))
        comparable = _comparable(series, idx, prev_idx)
        if not comparable:
            pct = None

        spark = [v for v in low[: idx + 1][-30:] if v is not None]
        per90 = _percentile(low, 90)
        rows.append(
            {
                "product_id": pid,
                "model": product.model,
                "brand": product.brand,
                "spec": product.spec,
                "category": product.category,
                "category_label": category_label(product.category),
                "basis": basis,
                "latest": _r(last),
                "latest_new_low": _r(series["new_low"][-1]),
                "latest_used_low": _r(series["used_low"][-1]),
                "prev": _r(prev),
                # abs 与 change_pct 同生共死 —— 涨跌幅不可比时，绝对差值同样不可比，
                # 不能只藏一个留一个（否则界面上会出现"有价差但没百分比"的怪状态）
                # abs 必须与 change_pct 同源（都用稳健序列），否则会出现
                # "涨跌额 ¥370 但百分比 1%" 这种凑不出来的组合。
                "abs": _r(
                    (robust[idx] - robust[prev_idx])
                    if (
                        comparable
                        and prev_idx is not None
                        and robust[idx] is not None
                        and robust[prev_idx] is not None
                    )
                    else None
                ),
                "change_pct": None if pct is None else _r(pct, 2),
                "percentile_90d": _r(per90, 1),
                "sparkline": [round(v, 2) for v in spark],
                # ---- 数据新鲜度：前端据此标「今日」/「昨日」/「N 天前」----
                "captured_date": last_date.isoformat(),
                "is_today": last_date == today,
                "stale_days": (today - last_date).days,
            }
        )
    return rows


def build_ranking(
    session: Session,
    period: int = 1,
    category: str | None = None,
    direction: str = "up",
    limit: int = 20,
    days: int = 180,
    basis: str = "all",
    rows: list[dict] | None = None,
) -> list[dict]:
    """涨跌排行榜。

    约定：
      · `up`   **只收录上涨的型号**（change_pct > 0），从高到低
      · `down` **只收录下跌的型号**（change_pct < 0），从低到高
      · 没有可比基准（change_pct 为 None）的一律排除 —— 它们既不是涨也不是跌

    为什么必须先过滤再排序（踩过的坑）：
    早先是 `sorted(rows, key=(change_pct is None, value), reverse=direction=="up")`，
    想用元组第一项把"无数据"压到最后。但 `reverse=True` 会把**整个 key 一起反转** ——
    `True > False`，于是涨榜里 None 行反而排到了最前面，一屏 8 条全是"—"，
    而跌榜因为 `reverse=False` 恰好正确，所以这个 bug 只在涨榜露出。
    正确做法是先把 None 滤掉，排序键里就不再需要那个守卫。

    rows 可传入已算好的快照（build_overview 同时要涨榜和跌榜，
    复用同一份快照即可，不必把 300+ 型号的指标算两遍）。
    """
    if rows is None:
        rows = build_snapshot(session, period=period, category=category, days=days, basis=basis)

    want_up = direction == "up"
    picked = [
        r
        for r in rows
        if r["change_pct"] is not None and (r["change_pct"] > 0 if want_up else r["change_pct"] < 0)
    ]
    # 这里是新列表，可以原地排序（rows 本身可能是调用方还要用的共享快照）
    picked.sort(key=lambda r: r["change_pct"], reverse=want_up)
    return picked[:limit]


def build_market_index(session: Session, days: int = 180) -> dict:
    """品类价格指数：每个型号以首日价为 100 归一化，再按品类取平均。

    用于观察"整个品类是涨是跌"，不受个别型号绝对价格量级影响。
    """
    from ..seed_data import CATEGORY_ORDER, category_label

    frame = load_market_series(session, days=days)
    products = {p.id: p for p in session.execute(select(Product)).scalars()}

    buckets: dict[str, dict[date, list[float]]] = {}
    for pid, series in frame.items():
        product = products.get(pid)
        if product is None:
            continue
        base = next((v for v in series["new_low"] if v), None)
        if not base:
            continue
        for d, value in zip(series["dates"], series["new_low"]):
            if value is None:
                continue
            buckets.setdefault(product.category, {}).setdefault(d, []).append(value / base * 100.0)

    series_out = []
    for code in CATEGORY_ORDER:
        by_date = buckets.get(code)
        if not by_date:
            continue
        dates = sorted(by_date)
        series_out.append(
            {
                "category": code,
                "label": category_label(code),
                "data": [
                    [d.isoformat(), round(sum(by_date[d]) / len(by_date[d]), 2)] for d in dates
                ],
            }
        )
    return {"days": days, "series": series_out}


def build_overview(session: Session, days: int = 180, period: int = 1, basis: str = "all") -> dict:
    """总览统计：品类分布 + 整体涨跌计数。"""
    from ..seed_data import CATEGORY_ORDER

    snapshot = build_snapshot(session, period=period, days=days, basis=basis)
    by_category: dict[str, dict] = {}
    for row in snapshot:
        item = by_category.setdefault(
            row["category"],
            {
                "code": row["category"],
                "label": row["category_label"],
                "count": 0,
                "up": 0,
                "down": 0,
                "flat": 0,
            },
        )
        item["count"] += 1
        pct = row["change_pct"]
        if pct is None:
            item["flat"] += 1
        elif pct > 0:
            item["up"] += 1
        elif pct < 0:
            item["down"] += 1
        else:
            item["flat"] += 1

    ordered = [by_category[c] for c in CATEGORY_ORDER if c in by_category]
    newest = session.execute(select(func.max(PriceDaily.trade_date))).scalar()

    return {
        "tracked": len(snapshot),
        "period": period,
        "basis": basis,
        "newest_date": newest.isoformat() if newest else None,
        "up_count": sum(1 for r in snapshot if (r["change_pct"] or 0) > 0),
        "down_count": sum(1 for r in snapshot if (r["change_pct"] or 0) < 0),
        "flat_count": sum(1 for r in snapshot if r["change_pct"] == 0),
        "categories": ordered,
        "top_gainers": build_ranking(
            session, period=period, direction="up", limit=8, days=days, basis=basis, rows=snapshot
        ),
        "top_losers": build_ranking(
            session, period=period, direction="down", limit=8, days=days, basis=basis, rows=snapshot
        ),
    }
