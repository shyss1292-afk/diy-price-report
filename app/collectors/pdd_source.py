"""拼多多搜索适配器 —— 需要登录态。

与京东的差异（实测）：
  · 未登录访问搜索页会被**强制重定向**到 login.html（京东只是返回拦截页）
  · 页面是移动端 H5，需要移动 UA + 窄视口
  · 商品类名是纯哈希（`rjNMXsUm` / `_2OP4vH_6`），完全不可依赖
  · 好消息：价格是**文本**而非图片（`券后` / `¥` / `6893` 分行渲染）

因此解析策略用「文本锚点」而不是「CSS 选择器」：
  找到所有文本恰为 `¥` 的叶子节点 → 向上扩张容器，直到容器里出现第二个 `¥` 为止，
  此时容器正好是单个商品卡片；卡片首行是标题，`¥` 的下一行是价格。
这样即使拼多多整站重写类名，提取逻辑也不会失效。

关于限流：实测连续 6 次搜索未被拦，比京东宽松。但节奏规律化仍会积累风险，
所以同样接入拟人化随机间隔与游标轮转。
"""
from __future__ import annotations

import logging
import os
import urllib.parse
from datetime import date

from . import normalize
from . import policy
from .base import BaseCollector, Quote, page_dead, run_browser_batch
from .registry import register

logger = logging.getLogger("diyprice.collector.pdd")

# 首页。搜索前必须先落地这里 —— 直接深链搜索 URL 会触发安全验证，
# 成因与实测见 `_search()` 里的预热注释。
HOME_URL = "https://mobile.yangkeduo.com/"
SEARCH_URL = "https://mobile.yangkeduo.com/search_result.html?search_key={kw}"

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# ---- SSR 内嵌数据（`__NEXT_DATA__`）----
#
# 依据 `jynvch/pdd_spider/Workplace/pdd_不登录.py:100-109`：搜索页是 SSR 产物，
# `<script id="__NEXT_DATA__">` 里嵌了本次搜索的请求参数与数据；
# `Northxw/Pinduoduo/README.md:49` 也说 `listID`/`flip` 就在网页源码里。
#
# ⚠️⚠️ 这条通道**默认只观测、不产数据**，两个原因都很硬：
#
#   1. **字段路径未经实测**。2019 年的路径是 `props.pageProps.data.ssrListData.*`，
#      现在很可能已变。所以这里**不写死路径**，按"形状"找商品数组
#      （数组里 ≥N 个元素同时带 id 类键与价格类键），并把**命中的键路径**打日志
#      —— 下一次 PDD 真的跑起来，日志就会告诉我们真实结构，那时再决定启用。
#   2. **价格单位不确定**。拼多多的价格字段可能是**分**
#      （`OFZFZS/scrapy-pinduoduo`：`float(each['group']['price']) / 100`），
#      也可能是元。若猜错，6299 元会被写成 62.99 元 —— 与"万元价被记成 ¥2"
#      是同一类事故（口径错，且事后难分辨）。而便宜商品（9.9 元 = 990 分）
#      又会让"大于 100 就除 100"这种启发式判断哪个都不对。
#      所以**默认按元读**，要换算必须显式设 `DIYPRICE_PDD_CENTS=1`。
#
# 要启用产出：`DIYPRICE_PDD_NEXT_DATA=1`（建议先跑一轮看日志里的键路径与价格量级）。
_NEXT_DATA_JS = r"""() => {
    const el = document.getElementById('__NEXT_DATA__');
    return el ? (el.textContent || '') : '';
}"""

_ID_KEYS = ("goods_id", "goodsId", "goodsID", "goodsIDStr", "id")
_NAME_KEYS = ("goods_name", "goodsName", "goods_name_short", "title", "name")
_PRICE_KEYS = ("price", "priceInfo", "price_str", "min_group_price",
               "minGroupPrice", "group_price", "normal_price", "market_price")
_PDD_PRODUCT_MIN = 4


def _next_data_enabled() -> bool:
    """这条通道是否允许**产出数据**（默认否，见上面的说明）。"""
    return os.getenv("DIYPRICE_PDD_NEXT_DATA", "").strip().lower() in ("1", "true", "yes")


def _cents_enabled() -> bool:
    """价格是否按**分**读（默认否 = 按元读）。"""
    return os.getenv("DIYPRICE_PDD_CENTS", "").strip().lower() in ("1", "true", "yes")


def _pdd_price_of(item: dict) -> float | None:
    """从商品字典里取价格。

    ⚠️ **默认不做「分→元」换算**：那个启发式（`>100 就除以 100`）会把
    6299 元读成 62.99 元，而且对 9.9 元这种便宜货（=990 分）判断也是反的。
    单位错误属于**口径污染** —— 本项目被咬过多次（万元价被记成 ¥2、
    京东展示价 vs 到手价），所以宁可先按元读并显式记录，
    也不要"看起来更聪明"的猜测。
    """
    for key in _PRICE_KEYS:
        raw = item.get(key)
        if raw is None:
            continue
        if isinstance(raw, dict):
            raw = raw.get("price") or raw.get("value") or raw.get("text")
        try:
            value = float(str(raw).replace(",", "").replace("¥", "").strip())
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        if _cents_enabled():
            value = value / 100.0
        return round(value, 2)
    return None


def _find_goods_list(node, depth: int = 0, path: tuple = ()):
    """按**形状**找商品数组，返回 `(items, 命中的键路径)`。

    不写死字段路径的理由：路径会随发版变化，而"数组里 ≥N 个元素同时带
    id 类键与价格类键"这个形状稳定得多。顺带把命中的路径带出来，
    这样"下一次跑 PDD 时日志会告诉我们真实结构"这件事是自动发生的。
    """
    import json as _json

    if depth > 8:
        return None, ()
    if isinstance(node, str):
        stripped = node.strip()
        if stripped[:1] in "[{":
            try:
                return _find_goods_list(_json.loads(stripped), depth + 1, path + ("<json>",))
            except Exception:      # noqa: BLE001
                return None, ()
        return None, ()
    if isinstance(node, list):
        usable = sum(
            1 for x in node
            if isinstance(x, dict)
            and any(k in x for k in _ID_KEYS)
            and _pdd_price_of(x) is not None
        )
        if len(node) >= _PDD_PRODUCT_MIN and usable >= _PDD_PRODUCT_MIN:
            return node, path
        for idx, value in enumerate(node[:30]):
            found, sub = _find_goods_list(value, depth + 1, path + (f"[{idx}]",))
            if found:
                return found, sub
        return None, ()
    if isinstance(node, dict):
        for key, value in node.items():
            found, sub = _find_goods_list(value, depth + 1, path + (str(key),))
            if found:
                return found, sub
    return None, ()


def parse_next_data(text: str, diag: dict | None = None) -> list[dict]:
    """把 `__NEXT_DATA__` 的内嵌 JSON 解析成 rows（纯函数，可单测）。

    返回与 `_EXTRACT_JS` **同契约**的 rows（`title` / `price`）。
    `diag` 是可选出参：填入命中的键路径与商品条数，供日志定位真实结构。
    """
    import json as _json

    if not text or not text.strip():
        return []
    try:
        payload = _json.loads(text)
    except Exception:      # noqa: BLE001
        return []
    if diag is not None:
        diag["top_keys"] = list(payload)[:20] if isinstance(payload, dict) else []
    items, path = _find_goods_list(payload)
    if not items:
        return []
    if diag is not None:
        diag["path"] = ".".join(path)
        diag["items"] = len(items)
        diag["sample_keys"] = sorted(items[0])[:24] if isinstance(items[0], dict) else []

    rows: list[dict] = []
    for item in items:
        title = ""
        for key in _NAME_KEYS:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                title = value.strip()
                break
        price = _pdd_price_of(item)
        if not title or len(title) < 4 or price is None or price <= 0:
            continue
        rows.append({"title": title[:200], "price": price})
    return rows


_EXTRACT_JS = r"""() => {
    const out = [];
    document.querySelectorAll('*').forEach(el => {
        if (el.children.length !== 0) return;
        if ((el.textContent || '').trim() !== '¥') return;

        // 向上扩张，直到容器里出现第二个 ¥ —— 此时 card 正好是单个商品卡片
        let card = el;
        while (card.parentElement) {
            const parent = card.parentElement;
            const text = parent.innerText || '';
            if ((text.match(/¥/g) || []).length !== 1) break;
            card = parent;
        }

        const lines = (card.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
        const idx = lines.lastIndexOf('¥');
        if (idx < 0) return;

        // 价格可能被**拆成多行**渲染。2026-10-01 实测闲鱼 RTX 4090：
        //     ¥ ⏎ 2 ⏎ .30 ⏎ 万      （2.30万 = 23000）
        //     ¥ ⏎ 18 ⏎ .88           （18.88）
        //     ¥ ⏎ 4030               （4030）
        // 旧实现只取「¥ 的下一行」→ 把 2.30万 记成了 2，
        // 净效果是所有万元级二手报价被静默丢弃。
        // 这里只**原样取片段**，拼装与判读交给 Python（那边能写单测）。
        const seg = [];
        for (let k = idx + 1; k < lines.length && seg.length < 4; k++) {
            const t = lines[k];
            if (seg.length === 0) {
                // 首段：整数或小数（允许 .88 这种省略整数部分的写法）
                if (!/^(\d[\d,]*|\d+\.\d+|\.\d+)$/.test(t)) break;
                seg.push(t);
            } else if (/^\.\d+$/.test(t) || t === '万') {
                // 后续段：只接受小数部分或单位，遇到别的（如「56人想拼」）就停
                seg.push(t);
            } else {
                break;
            }
        }
        if (!seg.length) return;

        out.push({ title: lines[0] || '', priceSeg: seg });
    });
    return out;
}"""


@register
class PddCollector(BaseCollector):
    """拼多多在售价格采集器（依赖登录态）。"""

    code = "pdd"
    name = "拼多多"

    default_limit = 6        # 单轮最多采几个型号（DIYPRICE_PDD_LIMIT 可覆盖）
    # ⚠️ 请求间隔与页面等待**不在这里配** —— 统一由
    # `collectors/policy.PLATFORM_THROTTLE_CONFIG["pdd"]` 提供
    # （等待 networkidle + 700ms 签名窗口、行为模拟、单会话任务上限）。
    # 原来这里另有一组 interval_min/max，两套配置迟早互相矛盾。
    scroll_pixels = 1200     # 功能性滚动：触发懒加载（与拟人化滚动是两回事）
    max_empty_streak = 3     # 连续这么多次空结果就判定被风控
    page_timeout = 35000

    @property
    def supported_platforms(self) -> list[str]:
        return ["pdd"]

    def collect(self, platform, products: list, day: date) -> list[Quote]:
        if platform.code != "pdd":
            return []
        if day != date.today():  # 搜索页只反映当前在售价，没有历史
            return []

        from ..services.session import load_session

        if load_session("pdd") is None:
            logger.warning("拼多多未登录，跳过。先执行：python -m app.cli login --site pdd")
            return []

        # 移动端 H5 的两件必需品：窄视口 + 移动 UA，交给 run_browser_batch 统一设置。
        #
        # 视口仍走 page.set_viewport_size：worker 已经把窗口放在屏幕外，
        # 缩小窗口尺寸影响不到用户；而改用 setDeviceMetricsOverride 会让
        # window.innerWidth 与真实窗口尺寸脱钩，反而多一层可识别的矛盾。
        return run_browser_batch(
            self,
            products,
            day,
            site="pdd",
            limit=int(os.getenv("DIYPRICE_PDD_LIMIT", str(self.default_limit))),
            search_fn=self._search,
            viewport={"width": 430, "height": 900},
            user_agent=MOBILE_UA,
            empty_streak_limit=self.max_empty_streak,
        )

    # ------------------------------------------------------------------ 内部

    def _rows_from_anchors(self, page, product) -> list[dict]:
        """主通道：文本锚点（`¥` 的下一行），不依赖任何类名。

        ⚠️ 页面/浏览器已关闭**不能**被吞成"解析失败" —— 吞掉会累加
        empty_streak，连续 3 次就误判为限流（方向指反）。
        """
        try:
            rows = page.evaluate(_EXTRACT_JS)
        except Exception as exc:      # noqa: BLE001
            if page_dead(exc):
                raise
            logger.warning("拼多多解析失败 %s：%s", product.model, exc)
            return []
        out: list[dict] = []
        for row in rows or []:
            price = normalize.parse_split_price(row.get("priceSeg"))
            title = (row.get("title") or "").strip()
            if price is None or price <= 0 or not title:
                continue
            out.append({"title": title, "price": price})
        return out

    def _rows_from_next_data(self, page) -> list[dict]:
        """备用通道：SSR 内嵌的 `__NEXT_DATA__`（详见文件头说明）。

        ⚠️ 这条通道**默认只观测、不产数据**（`DIYPRICE_PDD_NEXT_DATA=1` 才启用）。
        原因：字段路径来自 2019 年实现、价格单位（元/分）也不确定 ——
        在实测确认之前让它产数，风险是"把 6299 元写成 62.99 元"，
        而这正是本项目反复踩过的口径污染。

        但**观测是照做的**：每次都把长度、顶层键、命中的键路径、样本字段名、
        价格量级打进日志。下一次 PDD 真跑起来，这些日志就直接给出了
        "要不要启用、要不要按分读"的答案 —— 不需要再猜。
        """
        try:
            text = page.evaluate(_NEXT_DATA_JS)
        except Exception as exc:      # noqa: BLE001
            if page_dead(exc):
                raise
            return []
        if not text:
            return []
        diag: dict = {}
        rows = parse_next_data(text, diag)
        prices = sorted(r["price"] for r in rows)[:3]
        logger.info(
            "拼多多 __NEXT_DATA__ 长度 %d｜顶层键 %s｜命中路径 %s｜商品 %s｜"
            "样本字段 %s｜价格量级 %s｜产出 %s",
            len(text), diag.get("top_keys"), diag.get("path") or "（未找到商品数组）",
            diag.get("items", 0), diag.get("sample_keys"), prices or "-",
            f"{len(rows)} 条" if _next_data_enabled() else "关闭（只观测）",
        )
        if not _next_data_enabled():
            return []
        return rows

    def _search(self, page, product) -> list[Quote]:
        url = SEARCH_URL.format(kw=urllib.parse.quote(product.model))

        # 预热：先落到首页，再进搜索页。
        #
        # 为什么需要（2026-09-24 实测）
        # ----------------------------
        # **直接深链到搜索 URL 会被重定向到 `psnl_verification.html`（安全验证）**，
        # 采集器把它判成限流 → 整轮 0 条 → 连续几次就吃满退避阶梯。
        # 今早 08:01 那次「命中 login.html → 24 小时退避」有一部分就是这个成因。
        #
        # 先访问一次首页拿到会话上下文再搜索就正常 —— 实测连抓 3 个型号
        # **3/3 成功、每个 20 条**（RTX 5090 ¥49830 / RTX 5080 ¥18300 /
        # RTX 5070 Ti ¥9389），耗时 ~8.7s/型号。
        #
        # 判据用「当前 URL 不含 search_result」而不是「只做一次」：
        # 这样被验证页劫持之后（URL 变成 psnl_verification.html）下一轮
        # 会自动重新预热，**能自愈**，不用重启 Worker。
        try:
            if "search_result" not in (page.url or ""):
                page.goto(HOME_URL, wait_until="domcontentloaded", timeout=self.page_timeout)
                page.wait_for_timeout(1200)
        except Exception:  # noqa: BLE001 —— 预热失败不致命，继续走正常流程
            pass

        try:
            # navigate 内按策略严格等到 networkidle（或核心商品节点出现），
            # 再额外留 700ms 给前端 JS 完成 anti-content 签名运算 ——
            # 早于它取数只能拿到未签名的空壳 DOM。
            # 被重定向到验证页 / error_code=40001 会抛 RateLimitError（熔断信号）。
            policy.navigate(page, url, self.code, timeout=self.page_timeout)
        except (policy.RateLimitError, policy.AuthExpiredError):
            # ⚠️ `AuthExpiredError` 必须一起上抛：PDD 的 40001 语义就是
            #    「登录已过期，请重新登录」（前端错误码表实测），
            #    被吞掉会退化成"再试下一个型号"，最后被 empty_streak
            #    推断成"被限流"—— 归因指反，还要白等一个冷却期。
            raise
        except Exception as exc:
            # ⚠️ 「页面/浏览器已关闭」**不能**被吞成"搜索失败"。
            # 吞掉之后 run_browser_batch 会把它当成"搜索无结果"累加 empty_streak，
            # 连续 3 次就**误判为被限流**并提前结束本轮 —— 实测 2026-10-01 20:30
            # 闲鱼一轮 15 个型号只采到 1 个就"判定限流"退出，真凶其实是浏览器实例挂了。
            # 原样上抛，让上层走 mark_dirty + 重建实例（重建只要约 3 秒）。
            if page_dead(exc):
                raise
            logger.warning("%s搜索失败 %s：%s", "拼多多", product.model, exc)
            return []

        try:  # 功能性滚动一屏，触发懒加载
            page.evaluate(f"() => window.scrollTo(0, {self.scroll_pixels})")
            page.wait_for_timeout(1800)
        except Exception:
            pass

        policy.behave(page, self.code)
        policy.settle(page, self.code)
        policy.assert_not_rate_limited(page, self.code)

        rows = self._rows_from_anchors(page, product)
        if not rows:
            # 文本锚点没拿到 → 试 SSR 内嵌数据（比 DOM 更早可用、字段可能更全）
            rows = self._rows_from_next_data(page)
            if rows:
                note_stage("NEXT_DATA命中")
                logger.info(
                    "拼多多 %s 文本锚点未命中，改用 __NEXT_DATA__：%d 条",
                    product.model, len(rows),
                )
        if not rows:
            # 两条路都空 —— 问平台"是不是真的没有"（41001/41002/41003 是真无货）
            policy.raise_if_empty(page, self.code)

        quotes: list[Quote] = []
        for row in rows:
            # 两个通道都已把价格解析成数值（文本锚点走 parse_split_price，
            # SSR 通道走 _pdd_price_of），这里只做统一的合法性过滤。
            try:
                price = float(row.get("price"))
            except (TypeError, ValueError):
                continue
            title = (row.get("title") or "").strip()
            if price <= 0 or not title:
                continue
            quotes.append(
                Quote(
                    platform_code="pdd",
                    title_raw=title[:200],
                    price=price,
                    condition="全新",
                    url=url,
                    seller="拼多多",
                    extra={"category": product.category, "keyword": product.model},
                )
            )
        return quotes
