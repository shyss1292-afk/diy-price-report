"""京东商品搜索适配器 —— 需要登录态。

与 ZOL / 太平洋这类静态报价站完全不同，京东是前后端分离 + 强反爬：
  · 未登录直接请求只返回 2.7KB 拦截页（实测）
  · 渲染出来的类名带构建哈希（如 `_price_9y3st_31`），会随京东发版变化

⚠️ 2026-10-02 实测**纠正了一个存在很久的错误结论**。旧的 MEMORY 与调研报告都写着
   「京东桌面搜索页是服务端渲染 HTML，**没有 JSON 接口可截获**」，并据此把京东
   排除在「响应截获」之外。实测结果恰恰相反：

     · 服务端 HTML 只有 47,294 字符，`data-sku` 出现 **0** 次、商品卡片 **0** 个
       → **不是 SSR**，商品根本不在首屏 HTML 里
     · 商品数据在 `api.m.jd.com/api?appid=search-pc-java` 的**明文 JSON** 里：
       商品数组约 242KB，含 `wareId`（30 个，正好等于页面上 30 张卡片）、
       `wareName`、`jdPrice`、`shopName`、`stock`、`finalPrice`（到手价）、`oriPrice`
     · `jdPrice` 与 DOM 卡片展示价**逐条吻合**（7299 ↔ ¥7299、7399 ↔ ¥7399）
       ⇒ 口径一致，可以放心替换
     · 同一接口还会被调用若干次（AB 配置，约 7KB）—— 按体积取最大的那个即可区分

  所以京东和闲鱼是**同一套模式**：接住页面自己发的响应。收益是对改版免疫
  （不再依赖卡片选择器）、字段更全，且首次拿到**商品级身份**
  （`wareId` → `https://item.jd.com/{wareId}.html` → `normalize.link_key` 可用）。

取数路线（按优先级）：
  1. 响应截获 `search-pc-java` 的 JSON（`page.on("response")`，用完即摘）
  2. DOM 解析兜底 —— 接口失效时不至于整源归零，见 `_quotes_from_dom`
  3. 登录态由 `python -m app.cli login --site jd` 扫码一次，持久化在独立 profile

关于限流（实测数据）：
  · 触发条件不是"总次数"，而是**频率与节奏规律** —— 按固定 3 秒连搜 7~8 次必中
  · 被拦后并非全天不可用，**冷却约 20~25 分钟**即可恢复
  · 所以单轮采少量、隔一段时间再来一轮，一天足以覆盖整个型号库
  应对手段见 `collectors/policy.py`（平台差异化节奏）+ `services/breaker.py`（退避）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.parse
from datetime import date

from . import normalize
from . import policy
from .base import BaseCollector, Quote, note_stage, page_dead, run_browser_batch
from .registry import register

logger = logging.getLogger("diyprice.collector.jd")

SEARCH_URL = "https://search.jd.com/Search?keyword={kw}&enc=utf-8"

# 唯一依赖的类名：京东给插件化卡片保留的可读类名，不带构建哈希。
# 响应截获生效时用不到它；接口失效回落 DOM 时才走到（见 _quotes_from_dom）。
CARD_SELECTOR = '[class*="goodsCardWrapper"]'
PRICE_SELECTOR = '[class*="_price_"]'

# 商品数据的接口标识。实测同一 appid 会被调用多次，只有一个是商品列表：
#   · 含商品数组那个 ≈ 240~330 KB
#   · AB 配置 / 开关 那几个 ≈ 7 KB
# 用体积下限就能干净区分（见 _MIN_PAYLOAD_BYTES）。
SEARCH_API_MARK = "search-pc-java"
_MIN_PAYLOAD_BYTES = 20_000

# 商品页链接**自己拼** —— 接口里没有可直接用的商品 URL
# （productUrl / labelUrl 实测均为 null），而 wareId 是确定性的。
# 这也让京东第一次有了**商品级去重键**：此前 Quote.url 是搜索页 URL，
# `normalize.link_key()` 对它返回空串，去重只能回落到 (平台,标题,价格)。
ITEM_URL = "https://item.jd.com/{ware_id}.html"

# 「商品数组」的判据：一个数组里至少这么多个元素同时有 wareId 与可用价格。
_PRODUCT_MIN = 8


def _strip_tags(text: str) -> str:
    """去掉接口文本里内嵌的 HTML。

    京东会把命中的关键词包成 `<font class="skcolor_ljg">爆款</font>`，
    不剥掉会带着标签进库，并污染标题匹配与去重（实测样本里出现过）。
    """
    return re.sub(r"<[^>]+>", "", text or "")


def _price_of(item: dict) -> float | None:
    """取**页面展示价**。

    实测（2026-10-02，与 DOM 卡片逐条对照）：`jdPrice` 就是搜索页卡片上的价格。
      技嘉 5070 卡片「¥ 7299 包邮」↔ `jdPrice: 7299.00`
      七彩虹「¥ 7399」        ↔ `jdPrice: 7399.00`

    ⚠️ 其余价格字段**语义不同，不能混用**（混了就是又一次口径污染）：
       `finalPrice.estimatedPrice` 是**到手价**（叠加补贴后，可能低于展示价）
       `oriPrice` 是**原价**（划掉的那个）
       `wredisPrice` / `hprice` / `qyPrice` 等实测多为空串
    所以只认 jdPrice / realPrice 两个同义字段；取不到就**丢掉该条**，
    而不是退而求其次拿别的价格顶替。
    """
    for key in ("jdPrice", "realPrice"):
        value = normalize.parse_price(item.get(key))
        if value:
            return value
    return None


def _find_product_list(node, depth: int = 0):
    """在接口返回里找出「商品数组」。

    **不写死字段路径** —— 外层键会变（实测到过
    `['abBuriedTagMap','code','data','msg']`），但「一个数组里 ≥N 个元素同时带
    wareId 和 jdPrice」这个**形状**很稳定。字符串一律尝试当 JSON 再往下找，
    因为京东会把 `data` 序列化成字符串。
    """
    if depth > 6:
        return None
    if isinstance(node, str):
        stripped = node.strip()
        if stripped[:1] in "[{":
            try:
                return _find_product_list(json.loads(stripped), depth + 1)
            except Exception:      # noqa: BLE001 —— 不是 JSON 就当没找到
                return None
        return None
    if isinstance(node, list):
        usable = sum(
            1 for x in node
            if isinstance(x, dict) and x.get("wareId") and _price_of(x) is not None
        )
        if len(node) >= _PRODUCT_MIN and usable >= _PRODUCT_MIN:
            return node
        for value in node[:20]:
            found = _find_product_list(value, depth + 1)
            if found:
                return found
        return None
    if isinstance(node, dict):
        for value in node.values():
            found = _find_product_list(value, depth + 1)
            if found:
                return found
    return None


def parse_search_payload(payload: object) -> list[dict]:
    """把京东搜索接口的载荷翻译成统一 rows（纯函数，可用合成样本单测）。

    与闲鱼 `parse_search_payload` **同契约**（同样的键），
    这样上层 `_quotes_from_rows` 两层能共用一套字段口径。
    """
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except Exception:          # noqa: BLE001
            return []
    items = _find_product_list(payload)
    if not items:
        return []

    rows: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = _strip_tags(str(item.get("wareName") or "")).strip()
        price = _price_of(item)
        if not title or len(title) < 4 or price is None or price <= 0:
            continue
        ware_id = str(item.get("wareId") or item.get("skuId") or "").strip()
        final = item.get("finalPrice")
        final_price = ""
        if isinstance(final, dict):
            final_price = str(final.get("estimatedPrice") or "")
        rows.append({
            "title": title[:200],
            "price": price,
            "item_url": ITEM_URL.format(ware_id=ware_id) if ware_id else "",
            "ware_id": ware_id,
            "shop": str(item.get("shopName") or ""),
            "final_price": final_price,
            "ori_price": str(item.get("oriPrice") or ""),
            "stock": str(item.get("stock") or ""),
        })
    return rows



def _to_price(text: str) -> float | None:
    """京东的价格在一个节点里（如 "4599.00"），直接交给统一解析器。

    统一到 `normalize.parse_price` 是为了让三个平台的**价格语义只有一处定义**
    —— 顺带获得「万」单位支持（京东不出现，但没必要为此留一条特殊路径）。
    """
    return normalize.parse_price(text)


@register
class JdCollector(BaseCollector):
    """京东在售价格采集器（依赖登录态）。"""

    code = "jd"
    name = "京东"

    # 京东限流比预想更严：实测「恢复」后也只能再搜几次就再次被拦
    # （17:40 探测正常 → 17:43 采集又中）。所以单轮量压到 2 个、间隔拉到 20~45 秒。
    #
    # 为什么是 2 而不是 4：实测每轮通常只有**第 1 个**型号能采到，之后连续空结果
    # 触发退避。留 4 个名额只是白等 2 分半钟（3 次空结果 × 各自的重试间隔），
    # 不会多拿到数据，还给账号添了一道访问记录。
    default_limit = 2        # 单轮最多采几个型号（环境变量 DIYPRICE_JD_LIMIT 可覆盖）
    # ⚠️ 请求间隔与页面等待**不在这里配** —— 统一由
    # `collectors/policy.PLATFORM_THROTTLE_CONFIG["jd"]` 提供
    # （高斯扰动 4~7s + 上下限夹取、就绪判据、行为模拟）。
    # 原来这里另有一组 interval_min/max，和策略各写一套迟早互相矛盾。
    max_empty_streak = 3     # 连续这么多次空结果就判定限流并提前结束，不去撞墙
    page_timeout = 30000

    @property
    def supported_platforms(self) -> list[str]:
        return ["jd"]

    def collect(self, platform, products: list, day: date) -> list[Quote]:
        if platform.code != "jd":
            return []
        if day != date.today():  # 搜索页只反映当前在售价，没有历史
            return []

        from ..services.session import load_session

        if load_session("jd") is None:
            logger.warning("京东未登录，跳过。先执行：python -m app.cli login --site jd")
            return []

        # 浏览器交给 browser_worker 管（短生命周期 + 按阈值回收），
        # 队列、增量落盘、断点续爬都在 base.run_browser_batch 里。
        #
        # 相比改造前删掉了两处：
        #   · 自己 launch_browser + connect_over_cdp + new_page
        #     → worker.page() 统一负责，并在借用前决定是否回收重启
        #   · 「本轮 0 条就 save_offset 回退游标」
        #     → 那个逻辑会把游标钉死在原地（同批型号→0条→回退→同批型号…），
        #       实测让京东空转了 3 个多小时。现在任务留在队列里下轮优先重做，
        #       游标继续前进。
        return run_browser_batch(
            self,
            products,
            day,
            site="jd",
            limit=int(os.getenv("DIYPRICE_JD_LIMIT", str(self.default_limit))),
            search_fn=self._search,
            empty_streak_limit=self.max_empty_streak,
        )

    # ------------------------------------------------------------------ 内部

    def _search(self, page, product) -> list[Quote]:
        url = SEARCH_URL.format(kw=urllib.parse.quote(product.model))

        # ---- 响应截获 ----
        # 京东的商品数据来自 `api.m.jd.com/api?appid=search-pc-java` 的**明文 JSON**
        # （实测 2026-10-02：商品数组约 242KB、30 个 wareId = 页面上 30 张卡片）。
        # 监听必须在 navigate **之前**挂上，否则接不到这次请求。
        #
        # ⚠️ 用完必须摘掉：同一个 page 会连搜多个型号，不摘就会挂 N 个监听器，
        #    每个都把 300KB 响应体重复读一遍（闲鱼侧踩过同样的坑）。
        captured: dict = {}

        def _on_response(resp) -> None:
            try:
                if SEARCH_API_MARK not in resp.url:
                    return
                body = resp.text()
                if len(body) < _MIN_PAYLOAD_BYTES:
                    return
                # 同一 appid 会被调用多次（AB 配置等），只留最大的那个 = 商品列表
                if len(body) > len(captured.get("body", "")):
                    captured["body"] = body
            except Exception:      # noqa: BLE001 —— 监听器绝不能把采集搞崩
                pass

        page.on("response", _on_response)
        try:
            try:
                # navigate 按平台策略等到"可以取数"，并**先查一次限流**。
                # 命中会抛 RateLimitError —— 那是熔断信号，必须原样上抛给
                # run_browser_batch，绝不能被下面的 except 吞成"搜索失败"：
                # 吞掉它就会变成"记一笔失败、继续拿下一个型号去撞"。
                policy.navigate(page, url, self.code, timeout=self.page_timeout)
            except policy.RateLimitError:
                raise
            except Exception as exc:
                # ⚠️ 「页面/浏览器已关闭」**不能**被吞成"搜索失败"。
                # 吞掉后 run_browser_batch 会当成"搜索无结果"累加 empty_streak，
                # 连续 3 次就**误判为被限流**并提前结束本轮 —— 实测 2026-10-01 20:30
                # 闲鱼一轮 15 个型号只采到 1 个就"判定限流"退出，真凶是浏览器实例挂了。
                if page_dead(exc):
                    raise
                logger.warning("京东搜索失败 %s：%s", product.model, exc)
                return []

            # 提取前行为：平滑向下滚动 300~600px → 悬停 1~2s，然后静默一段
            # 等 whwswswws 等埋点参数上报完再取数。
            policy.behave(page, self.code)
            policy.settle(page, self.code)
            # settle 期间页面也可能被替换成限流页，取数前再确认一次
            policy.assert_not_rate_limited(page, self.code)
        finally:
            try:
                page.remove_listener("response", _on_response)
            except Exception:      # noqa: BLE001
                pass

        # ---- 优先用接口数据；失败则回落 DOM ----
        rows: list[dict] = []
        if captured.get("body"):
            try:
                rows = parse_search_payload(json.loads(captured["body"]))
            except Exception as exc:      # noqa: BLE001
                logger.warning("京东接口解析失败，回落 DOM %s：%s", product.model, exc)
                rows = []
        if rows:
            note_stage("接口命中")
            logger.info("京东 %s 命中搜索接口：%d 条（含店铺/到手价/wareId）",
                        product.model, len(rows))
            return self._quotes_from_rows(rows, product, url)

        note_stage("回落DOM")
        return self._quotes_from_dom(page, product, url)

    def _quotes_from_rows(self, rows: list[dict], product, fallback_url: str) -> list[Quote]:
        """接口 rows → Quote（与闲鱼 `_quotes_from_rows` 同一套字段口径）。"""
        quotes: list[Quote] = []
        for row in rows:
            title = (row.get("title") or "").strip()
            price = row.get("price")
            if not title or len(title) < 4 or not price or price <= 0:
                continue
            quotes.append(
                Quote(
                    platform_code="jd",
                    title_raw=title[:200],
                    price=price,
                    condition="全新",
                    # 商品页链接（item.jd.com/{wareId}.html）—— 让京东第一次有
                    # **商品级去重键**；此前存的是搜索页 URL，link_key 返回空串。
                    url=(row.get("item_url") or fallback_url),
                    seller="京东",
                    extra={
                        "category": product.category,
                        "keyword": product.model,
                        "ware_id": row.get("ware_id") or "",
                        "shop": row.get("shop") or "",
                        "final_price": row.get("final_price") or "",
                        "ori_price": row.get("ori_price") or "",
                        "stock": row.get("stock") or "",
                    },
                )
            )
        return quotes

    def _quotes_from_dom(self, page, product, url: str) -> list[Quote]:
        """DOM 兜底：接口失效时仍能取到数（只依赖一个稳定容器类名）。"""
        try:
            cards = page.locator(CARD_SELECTOR)
            count = cards.count()
        except Exception as exc:
            # ⚠️ 和 navigate 同理：页面/浏览器已关闭不能被吞成"卡片定位失败"。
            # 吞掉会累加 empty_streak，连续 3 次就误判为限流 —— 方向指反。
            if page_dead(exc):
                raise
            logger.warning("京东卡片定位失败 %s：%s", product.model, exc)
            return []

        quotes: list[Quote] = []
        for i in range(count):
            card = cards.nth(i)
            try:
                price = _to_price(card.locator(PRICE_SELECTOR).first.inner_text(timeout=1200))
            except Exception:
                continue
            if price is None:
                continue
            title = self._title_of(card)
            if not title:
                continue
            quotes.append(
                Quote(
                    platform_code="jd",
                    title_raw=title,
                    price=price,
                    condition="全新",
                    url=url,
                    seller="京东",
                    extra={"category": product.category, "keyword": product.model},
                )
            )
        return quotes

    @staticmethod
    def _title_of(card) -> str:
        """标题取卡片文本的第一行有效内容（京东把标题渲染在卡片顶部）。"""
        try:
            text = card.inner_text(timeout=1200)
        except Exception:
            return ""
        for line in text.split("\n"):
            line = line.strip()
            if len(line) > 4 and line not in {"广告", "到手价", "自营"}:
                return line[:200]
        return ""
