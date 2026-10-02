"""闲鱼（Goofish）搜索适配器 —— 需要登录态。

闲鱼是这几个电商里技术最特殊的：
  · 前后端完全分离，商品数据走 `h5api.m.goofish.com` 的 **mtop 接口**
  · 接口带 `sign=` 签名参数，逆向签名算法成本很高
  · 但签名是**浏览器端 mtop.js 自己算的** —— 所以这里不逆向签名，
    而是用真实浏览器加载页面，直接读它自己发出去、拿回来的数据

实测要点（2026-09）：
  · 未登录时搜索接口返回空（157B），必须登录。
    登录后新增的关键 Cookie：`unb`（淘宝用户 ID）、`tracknick`、`sgcookie`
  · PC 网页版 `www.goofish.com` 就够用，不需要移动 UA（与拼多多不同）
  · 价格是**文本**而非图片，但 `¥` 与数字之间**有换行**（`¥\\n3900`）——
    这是最初误判"拿不到数据"的原因，解析必须按行处理

解析用文本锚点（同拼多多）：找文本恰为 `¥` 的叶子节点，
向上扩张容器直到出现第二个 `¥` 为止，此时容器正好是单个商品卡片。

注意：站点 code 是 `goofish`（会话文件），平台 code 是 `xianyu`（platforms 表）。
"""
from __future__ import annotations

import logging
import os
import urllib.parse
from datetime import date

from . import normalize
from . import policy
from .policy import AuthExpiredError, RateLimitError
from .base import BaseCollector, Quote, note_stage, page_dead, run_browser_batch
from .registry import register

logger = logging.getLogger("diyprice.collector.xianyu")

SEARCH_URL = "https://www.goofish.com/search?q={kw}"

# 站点 code（用于读写登录会话）与平台 code（用于入库）不同名，这里显式声明
SITE_CODE = "goofish"
PLATFORM_CODE = "xianyu"

# 纯文本锚点提取，不依赖任何类名
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

SEARCH_API_MARK = "mtop.taobao.idlemtopsearch.pc.search"

# 「想要人数」的文案形态：`56人想要` / `1.2万人想要`
_WANT_RE = __import__("re").compile(r"(\d+(?:\.\d+)?(?:万)?)\s*人想要")

# 卡片标签里值得留的（其余是装饰）
_TAG_OF_INTEREST = ("验货宝", "包邮", "急售", "可小刀")


def _want_num(ex: dict, args: dict) -> int:
    """想要人数 —— 两条来源，**优先 fishTags 里的文案**。

    为什么优先文案而不是 `wantNum` 字段：文案才是页面上真正展示的那个数，
    而 `wantNum` 有时是 `'NaN'` 字符串、且实测与展示值可能不一致
    （`ai-goofish-monitor/src/parsers.py:38` 直接把它当默认值 `'NaN'` 用）。
    文案路径的依据是 `GuDong2003/utils/item_search.py:1015-1029` 的正则，
    并要处理「万」单位（`1.2万人想要` → 12000）。

    取不到返回 0（而不是 None）—— 它在库里的语义是"未知/为零"，两者都不该阻止入库。
    """
    for tag in ((ex.get("fishTags") or {}) or {}).values():
        for item in (tag or {}).get("tagList") or []:
            content = str(((item or {}).get("data") or {}).get("content") or "")
            m = _WANT_RE.search(content)
            if m:
                raw = m.group(1)
                try:
                    if raw.endswith("万"):
                        return int(float(raw[:-1]) * 10000)
                    return int(float(raw))
                except ValueError:
                    continue
    try:
        return int(float(args.get("wantNum")))
    except (TypeError, ValueError):
        return 0


def _fish_tags(ex: dict, args: dict) -> list[str]:
    """卡片标签（包邮 / 验货宝 / 急售…）。

    依据 `ai-goofish-monitor/src/parsers.py:42-48`：
      · `clickParam.args.tag == "freeship"` → 包邮（是**独立字段**，不在 fishTags 里）
      · `fishTags.<rN>.tagList[].data.content` 里出现「验货宝」等文案
    """
    tags: list[str] = []
    if str(args.get("tag") or "") == "freeship":
        tags.append("包邮")
    for tag in ((ex.get("fishTags") or {}) or {}).values():
        for item in (tag or {}).get("tagList") or []:
            content = str(((item or {}).get("data") or {}).get("content") or "").strip()
            if content and content in _TAG_OF_INTEREST and content not in tags:
                tags.append(content)
    return tags


def parse_search_payload(payload) -> list[dict]:
    """把闲鱼搜索接口的响应解析成行。

    结构（实测 2026-10-02，`resultList` 30 条）：

        data.resultList[i].data.item.main
            ├─ exContent.title / .area / .itemId / .picUrl
            ├─ clickParam.args.price / .displayPrice / .publishTime / .id
            └─ targetUrl   （fleimarket://item?id=...）

    为什么值钱：这是**平台的对外契约**，不是渲染结果 —— 类名改版不影响它，
    而且能拿到 DOM 上看不到的字段（发布时间 / 地区 / 商品 id）。

    ⚠️ 这里的 price 是接口给的定价，是**完整数值** —— 不需要
       `parse_split_price` 那套拆行拼装（那是 DOM 兜底路径才需要的）。
    """
    out: list[dict] = []
    if not isinstance(payload, dict):
        return out

    # ① 先看 ret 字段 —— 闲鱼 mtop 响应的「外层状态」（2026-10-02 新增）。
    #    登录失效以 ret: ["FAIL_SYS_SESSION_EXPIRED::令牌过期"] 形式返回，
    #    此前只看 resultList，登录失效时静默返回空 → 被上层当成"限流"
    #    推进退避阶梯。依据 fancyboi999/goofish-cli core/mtop.py:141-177：
    #    TOKEN_EXOIRED/TOKEN_EMPTY/SESSION_EXPIRED 是「刷 cookie 可救」；
    #    FAIL_SYS_ILLEGAL_ACCESS 是风控层，刷 cookie 救不了 → RateLimitError。
    _ret = payload.get("ret") or []
    if isinstance(_ret, list) and _ret:
        from .policy import AUTH_EXPIRED_CODES, RISK_CODES  # 局部导入：避免模块级循环
        _code = str(_ret[0]).split("::", 1)[0].strip()
        if _code in AUTH_EXPIRED_CODES:
            raise AuthExpiredError("xianyu", _code, "接口 ret 字段返回登录失效码")
        if _code in RISK_CODES:
            raise RateLimitError("xianyu", _code, "接口 ret 字段返回风控码")

    data = payload.get("data")
    if not isinstance(data, dict):
        return out

    for node in data.get("resultList") or []:
        try:
            main = (((node or {}).get("data") or {}).get("item") or {}).get("main") or {}
            ex = main.get("exContent") or {}
            args = (main.get("clickParam") or {}).get("args") or {}

            title = str(ex.get("title") or "").strip()
            if not title:
                continue

            price = normalize.parse_price(args.get("price") or args.get("displayPrice"))
            if price is None:
                continue

            item_id = str(args.get("id") or ex.get("itemId") or "")

            # 商品链接优先用 targetUrl，但它**不总是可信** —— 实测同一份响应里
            # 既有 `fleamarket://item?id=...` 也有加密的不透明串。
            # 所以拿不到合法 URL 时，用 item_id **自己拼** —— 这是确定性的，
            # 不依赖任何可能被替换的字段。
            item_url = normalize.normalize_url(main.get("targetUrl") or "")
            if not item_url and item_id:
                item_url = f"https://www.goofish.com/item?id={item_id}"

            out.append(
                {
                    "title": title,
                    "price": price,
                    "item_url": item_url,
                    # publishTime 是**毫秒时间戳**（`ai-goofish-monitor/src/parsers.py:59`），
                    # 归一成 ISO 日期后入库，免得下游各自猜单位
                    "publish_time": normalize.parse_publish_time(
                        args.get("publishTime")
                    ),
                    "area": str(ex.get("area") or ""),
                    "item_id": item_id,
                    # ---- 以下三个字段此前完全没取（2026-10-02 补）----
                    # 同一份 resultList 里现成就有，DOM 上却看不到：
                    #   seller  卖家昵称 —— 也是"这台机器是不是同一个卖家反复挂"的线索
                    #   ori_price 原价（划掉价）—— 有它才算得出"降了多少"
                    #   want_num / tags —— 热度与保障信息
                    "seller": str(
                        main.get("userNickName") or ex.get("userNickName") or ""
                    ).strip(),
                    "ori_price": normalize.parse_price(ex.get("oriPrice")),
                    "want_num": _want_num(ex, args),
                    "tags": _fish_tags(ex, args),
                }
            )
        except Exception:  # noqa: BLE001 —— 单条脏数据不该毁掉整页
            continue
    return out


_NEW_KEYWORDS = ("全新", "未拆封", "未拆", "仅拆封", "全新未使用")


def _condition_of(title: str) -> str:
    """闲鱼以二手为主，但不少标着"全新未拆封"，按标题粗略区分。"""
    return "全新" if any(k in title for k in _NEW_KEYWORDS) else "二手"


@register
class XianyuCollector(BaseCollector):
    """闲鱼在售价格采集器（依赖登录态）。"""

    code = PLATFORM_CODE
    name = "闲鱼"

    @property
    def browser_site(self) -> str:
        """闲鱼是唯一 code 与浏览器站点不同名的源（会话文件叫 goofish）。"""
        return SITE_CODE

    default_limit = 6        # 单轮最多采几个型号（DIYPRICE_XIANYU_LIMIT 可覆盖）
    page_wait_ms = 8000      # 闲鱼首屏渲染偏慢
    scroll_pixels = 1200     # 滚动触发懒加载
    # ⚠️ 请求间隔**不在这里配** —— 见 collectors/policy.PLATFORM_THROTTLE_CONFIG["xianyu"]
    max_empty_streak = 3
    page_timeout = 40000

    @property
    def supported_platforms(self) -> list[str]:
        return [PLATFORM_CODE]

    def collect(self, platform, products: list, day: date) -> list[Quote]:
        if platform.code != PLATFORM_CODE:
            return []
        if day != date.today():  # 搜索页只反映当前在售价，没有历史
            return []

        from ..services.session import load_session

        if load_session(SITE_CODE) is None:
            logger.warning(
                "闲鱼未登录，跳过。先执行：python -m app.cli login --site goofish"
            )
            return []

        # 注意 site 与 code 不同名：site=goofish 用于读登录会话，
        # collector.code=xianyu 用于队列与入库（见文件头说明）。
        # PC 网页版就够用，不需要移动 UA（与拼多多不同）。
        return run_browser_batch(
            self,
            products,
            day,
            site=SITE_CODE,
            limit=int(os.getenv("DIYPRICE_XIANYU_LIMIT", str(self.default_limit))),
            search_fn=self._search,
            viewport={"width": 1440, "height": 900},
            empty_streak_limit=self.max_empty_streak,
        )

    # ------------------------------------------------------------------ 内部

    def _search(self, page, product) -> list[Quote]:
        url = SEARCH_URL.format(kw=urllib.parse.quote(product.model))

        # ---- 响应截获：页面自己会调 mtop 搜索接口，我们直接接住它的 JSON ----
        #
        # 为什么不解析 DOM：DOM 是**渲染结果**，类名与层级每次发版都可能变；
        # 而接口是平台的**对外契约**，字段还更全（发布时间 / 地区 / 商品 id）。
        # 实测该接口返回约 300 KB 明文 JSON、resultList 30 条。
        #
        # ⚠️ 监听器必须**用完即摘**：run_browser_batch 在同一个 page 上连续搜
        #    多个型号，不摘除的话 15 个型号就挂 15 个监听器，每次响应都要
        #    重复解析 300 KB。
        captured: dict = {}

        def _on_response(resp) -> None:
            try:
                if SEARCH_API_MARK not in resp.url:
                    return
                if SEARCH_API_MARK + ".shade" in resp.url:
                    return          # 这是"搜索页装饰"接口，不是结果列表
                captured["data"] = resp.json()
            except Exception:  # noqa: BLE001 —— 监听失败绝不能影响采集
                pass

        page.on("response", _on_response)
        try:
            try:
                # navigate 按平台策略导航并先查一次限流（闲鱼惩罚页是 `punish` 页）。
                # RateLimitError 是熔断信号，原样上抛，不在这里吞掉。
                policy.navigate(page, url, self.code, timeout=self.page_timeout)
            except (policy.RateLimitError, policy.AuthExpiredError):
                # ⚠️ `AuthExpiredError` 必须一起上抛：它是"去重新登录"的信号，
                #    被下面的 `except Exception` 吞掉就会退化成"记一笔失败、
                #    继续下一个型号"，最后被 empty_streak 推断成"被限流"—— 归因指反。
                raise
            except Exception as exc:
                # ⚠️ 「页面/浏览器已关闭」**不能**被吞成"搜索失败"。
                # 吞掉之后 run_browser_batch 会把它当成"搜索无结果"累加
                # empty_streak，连续 3 次就**误判为被限流**并提前结束本轮 ——
                # 实测 2026-10-01 20:30 闲鱼一轮 15 个型号只采到 1 个就"判定限流"
                # 退出，真凶其实是浏览器实例挂了。原样上抛，让上层走 mark_dirty
                # + 重建实例（重建只要约 3 秒）。
                if page_dead(exc):
                    raise
                logger.warning("%s搜索失败 %s：%s", "闲鱼", product.model, exc)
                return []

            # 闲鱼是单页应用，没有可靠的服务端渲染信号，保留一个显式首屏等待
            page.wait_for_timeout(self.page_wait_ms)
            try:  # 滚动触发懒加载
                page.evaluate(f"() => window.scrollTo(0, {self.scroll_pixels})")
                page.wait_for_timeout(2500)
            except Exception:
                pass

            # 行为模拟：闲鱼对"鼠标是否有真实位移"较敏感，
            # 光标从头到尾停在 (0,0) 本身就是脚本特征
            policy.behave(page, self.code)
            policy.settle(page, self.code)
            policy.assert_not_rate_limited(page, self.code)

            rows = parse_search_payload(captured.get("data"))
        finally:
            try:
                page.remove_listener("response", _on_response)
            except Exception:  # noqa: BLE001
                pass

        if rows:
            note_stage("接口命中")
            logger.info(
                "闲鱼 %s 命中搜索接口：%d 条（含发布时间/地区/商品链接）",
                product.model, len(rows),
            )
        else:
            # 接口没拿到（改版/被降级/超时）→ 回落 DOM。
            # **两条路都留着**，任何一条断了都不至于整源归零。
            note_stage("回落DOM")
            rows = self._rows_from_dom(page, product)
            if rows:
                logger.info(
                    "闲鱼 %s 接口未命中，回落 DOM 解析：%d 条", product.model, len(rows),
                )

        if not rows:
            # 两条路都是 0 条 —— 先问平台自己"是不是真的没有"，再决定要不要
            # 让它计入"可疑"的证据链。见 `policy.raise_if_empty` 的说明。
            policy.raise_if_empty(page, self.code)

        quotes: list[Quote] = []
        for row in rows:
            title = (row.get("title") or "").strip()
            price = row.get("price")
            if not title or len(title) < 4 or not price or price <= 0:
                continue
            quotes.append(
                Quote(
                    platform_code=PLATFORM_CODE,
                    title_raw=title[:200],
                    price=price,
                    condition=_condition_of(title),
                    # 接口给的是**商品页**链接（fleamarket:// 已归一为 https），
                    # 比"搜索页 URL"有用得多 —— 也是商品级去重键的基础
                    url=(row.get("item_url") or url),
                    # ⚠️ 这里长期有个实证缺陷：`parse_search_payload` **从不设置**
                    #    `seller` 键，于是 `row.get("seller")` 恒为 None，
                    #    落库的 `Quote.seller` **恒为字面量 "闲鱼卖家"** ——
                    #    `userNickName` 从未被读过。现在真的取到了。
                    seller=(row.get("seller") or "闲鱼卖家"),
                    extra={
                        "category": product.category,
                        "keyword": product.model,
                        "publish_time": row.get("publish_time") or "",
                        "area": row.get("area") or "",
                        # 实测 2026-10-02：`parse_search_payload` 解析出了 item_id，
                        # 但构造 Quote 时被丢掉了（只用来拼 URL）。它是去重键与
                        # 排障的**原始身份** —— 没有它，"同一商品为什么算了两次"
                        # 就无从对照。
                        "item_id": row.get("item_id") or "",
                        # 原价 / 想要人数 / 标签：DOM 上看不到，接口里现成有。
                        # 原价是"这条挂牌降了多少"的分母，而降价是二手捡漏的核心信号。
                        "ori_price": row.get("ori_price"),
                        "want_num": row.get("want_num") or 0,
                        "tags": row.get("tags") or [],
                    },
                )
            )
        return quotes

    def _rows_from_dom(self, page, product) -> list[dict]:
        """DOM 兜底：接口不可用时仍能取到数（类名无关的纯文本锚点）。"""
        try:
            dom = page.evaluate(_EXTRACT_JS)
        except Exception as exc:
            # ⚠️ 与 _search 同理：页面已关闭不能被吞成"解析失败"，
            # 吞掉会累加 empty_streak，连续 3 次就误判为限流 —— 方向指反。
            if page_dead(exc):
                raise
            logger.warning("闲鱼解析失败 %s：%s", product.model, exc)
            return []
        rows: list[dict] = []
        for r in dom or []:
            # 价格在页面上可能被拆成多行（¥ / 2 / .30 / 万），
            # 拼装逻辑在 collectors/normalize.py（可单测）
            price = normalize.parse_split_price(r.get("priceSeg"))
            if price is None:
                continue
            rows.append({"title": (r.get("title") or "").strip(), "price": price})
        return rows
