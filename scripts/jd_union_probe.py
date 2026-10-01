"""京东联盟 API 探针：用 1 个关键词验证**覆盖率**与数据可用性。

为什么先写探针、而不是直接接采集器
--------------------------------
官方联盟接口只覆盖**参加联盟推广的商品**，未参加的商品查不到。
覆盖率没确认之前就改造采集器，等于赌一把；而且本项目 MEMORY 里把
「写了但没接线」列为明确要避免的模式（服务在跑、日志干净、实际没调用）。

所以先用最小代价拿一次真实返回：
  · 返回了几条？价格合不合理？
  · 想要的型号（如 RTX 5070 12G）在不在里面？

覆盖够 → 再接 `app/collectors/`；不够 → 这条路直接放弃，不浪费改造成本。

签名算法：两种都试
------------------
京东网关的签名在不同文档里有**两种说法**，而且都流传很广：
  A) `md5(secret + "".join(sorted(k+v)))`  → 大写
  B) `hmac_sha256(secret, "".join(sorted(k+v)))` → 大写
我没法在不登录控制台的情况下确认哪个是当前口径，所以**两个都试**，
把网关的真实反馈打出来 —— 一次调用就能定论，比查资料可靠。

用法
----
凭据两种给法（**都绝不进版本库**）：

    # 方式一：环境变量
    export JD_UNION_APP_KEY=你的AppKey
    export JD_UNION_APP_SECRET=你的AppSecret

    # 方式二：写进 data/jd_union.json（data/ 已被 .gitignore 整目录覆盖）
    {"app_key": "...", "secret_key": "..."}

    python -m scripts.jd_union_probe                          # 默认探 RTX 5070 12G
    python -m scripts.jd_union_probe --keyword "RTX 5090 32G"
    python -m scripts.jd_union_probe --method promotiongoodsinfo --sku 100012345678
    python -m scripts.jd_union_probe --params '{"keyword":"4060","pageIndex":1,"pageSize":30}'

⚠️ 联盟后台的原话：「请妥善保管您的 appkey 和 secretkey，禁止保存在任何版本库托管服务
   （如 GitHub）或以其他途径公开，否则可能被禁用。」—— 所以这里只从环境变量或
   **gitignore 覆盖的** `data/` 目录读取，**任何情况下都不写进源码**。
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# 凭据文件（data/ 被 .gitignore 整目录覆盖，不会进版本库）
CRED_FILE = Path(__file__).resolve().parent.parent / "data" / "jd_union.json"

# 网关。
#
# ⚠️ 联盟开放平台首页有一条重要通知（2026-10-01 截图确认）：
#     「联盟开放平台系统已升级到新版服务。旧版服务（域名：**router.jd.com** 及
#       sdk：jd-cps-client-x.x.jar）**已停止维护**，请仍在使用旧版服务的用户尽早迁移。」
#     所以备用网关 router.jd.com 已经**不能再依赖**了 —— 它当时还能转发，
#     但官方已宣布停维护，随时可能失效。
GATEWAY = "https://api.jd.com/routerjson"

# 官方 jdunion 页面列出的两个商品类接口
METHODS = {
    "goods": "jd.union.open.goods.query",
    "promotiongoodsinfo": "jd.union.open.goods.promotiongoodsinfo.query",
}

# 业务参数的键名。⚠️ 这是**最可能出错的地方** ——
# `goods_req` 的确切结构来自控制台的 API 文档页（登录后才渲染，我抓不到）。
# 如果这里报「参数错误」，去控制台把该接口的「请求参数」原样抄过来。
BUSINESS_KEY = {
    "goods": "goods_req",
    "promotiongoodsinfo": "goods_req",
}


def sign_md5(secret: str, params: dict) -> str:
    """A) md5(secret + sorted(key+value...)) 大写 —— JOS 老网关的经典口径。"""
    raw = secret + "".join(f"{k}{v}" for k, v in sorted(params.items()))
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def sign_hmac(secret: str, params: dict) -> str:
    """B) HMAC-SHA256(secret, sorted(key+value...)) 大写 —— 部分文档的说法。"""
    raw = "".join(f"{k}{v}" for k, v in sorted(params.items()))
    return hmac.new(secret.encode("utf-8"), raw.encode("utf-8"), hashlib.sha256).hexdigest().upper()


def build_common(app_key: str, method: str) -> dict:
    """网关公共参数。实测缺 app_key 会返回 10001「入参异常(appkey为空…)」。"""
    return {
        "app_key": app_key,
        "method": method,
        "v": "2.0",
        "format": "json",
        # ⚠️ 毫秒时间戳，官方要求 10 分钟内有效
        "timestamp": str(int(time.time() * 1000)),
    }


def call(
    app_key: str,
    secret: str,
    method: str,
    business_key: str,
    business: dict,
    signer,
    gateway: str = GATEWAY,
    timeout: float = 20.0,
):
    """POST 到网关。用标准库 urllib —— 本项目没装任何 HTTP 客户端库
    （采集全靠 Playwright），探针脚本不该为它引入新依赖。"""
    params = build_common(app_key, method)
    params[business_key] = json.dumps(business, separators=(",", ":"), ensure_ascii=False)
    params["sign"] = signer(secret, params)
    body = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(
        gateway,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def explain(text: str) -> str:
    """把网关错误码翻成人话 + 下一步该干什么。"""
    try:
        body = json.loads(text)
    except Exception:
        return ""
    err = body.get("error_response") or {}
    code = str(err.get("code", ""))
    zh = err.get("zh_desc") or err.get("en_desc") or ""
    hints = {
        "10001": "app_key 没传对 / 请求体过大。检查环境变量是否生效。",
        "15": "**方法名不存在 = 该 appkey 没有这个接口的权限**。\n"
              "     官方错误码表原话：「没有调用该接口权限，请到\"控制中心\"->\"应用管理\""
              "->\"接口管理\"进行申请」。\n"
              "     实测：**连故意不存在的方法也返回同一个 15** —— 说明网关是拿"
              "「该 appkey 的授权方法清单」匹配的，不在清单里一律报 15。",
        "11": "签名错误。换另一种签名算法再试（本脚本会自动试两种）。",
        "10003": "签名校验失败，同上。",
        "10002": "app_key 无效或未审核通过。",
    }
    out = []
    if code:
        out.append(f"网关错误码 {code}：{zh}")
    if code in hints:
        out.append(f"  → {hints[code]}")
    return "\n".join(out)


def load_credentials(cred_file: Path | None = None) -> tuple[str, str, str]:
    """按 环境变量 → 凭据文件 的顺序取凭据。

    ⚠️ 只从这两处读，**绝不硬编码**。联盟后台明说密钥公开会导致应用被禁用，
       而这个项目是 git 仓库 —— 写进源码就等于写进 git 历史。
    """
    app_key = os.getenv("JD_UNION_APP_KEY", "").strip()
    secret = os.getenv("JD_UNION_APP_SECRET", "").strip()
    path = cred_file or CRED_FILE
    note = "环境变量"
    if not (app_key and secret) and path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            app_key = (data.get("app_key") or "").strip()
            # 联盟后台叫 secretkey，这里两种拼写都认
            secret = (data.get("secret_key") or data.get("secretkey") or "").strip()
            note = str(path)
        except Exception as e:
            print(f"!! 读取 {path} 失败：{e}")
    return app_key, secret, note


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="京东联盟 API 探针（验证覆盖率，不写入任何数据）")
    ap.add_argument("--keyword", default="RTX 5070 12G", help="搜索关键词")
    ap.add_argument("--method", default="goods", choices=sorted(METHODS), help="用哪个接口")
    ap.add_argument("--sku", default=None, help="promotiongoodsinfo 用：逗号分隔的 skuId")
    ap.add_argument("--params", default=None, help="直接给业务参数 JSON（覆盖默认构造）")
    ap.add_argument("--page-size", type=int, default=30)
    ap.add_argument("--raw", action="store_true", help="只打印原始响应")
    ap.add_argument("--cred", default=None,
                    help="换一份凭据文件（用于 A/B 对比不同媒体类型的 appkey，如 data/jd_union_app.json）")
    args = ap.parse_args(argv)

    app_key, secret, src = load_credentials(Path(args.cred) if args.cred else None)
    if not app_key or not secret:
        print("!! 缺凭据。两种给法（都不会进版本库）：")
        print("     方式一：export JD_UNION_APP_KEY=...  JD_UNION_APP_SECRET=...")
        print(f"     方式二：写进 {CRED_FILE}")
        print('             {"app_key": "...", "secret_key": "..."}')
        print("\n（AppKey / secretKey 在 京东联盟 → 推广管理 → 导购媒体管理 → 查看 → 弹窗里）")
        return 2
    # 只打印前后各 4 位，避免完整密钥进日志
    print(f"凭据来源：{src}   appkey={app_key[:4]}…{app_key[-4:]}")

    method = METHODS[args.method]
    if args.params:
        business = json.loads(args.params)
    elif args.method == "promotiongoodsinfo":
        if not args.sku:
            print("!! promotiongoodsinfo 需要 --sku")
            return 2
        business = {"skuIds": [s.strip() for s in args.sku.split(",") if s.strip()]}
    else:
        business = {"keyword": args.keyword, "pageIndex": 1, "pageSize": args.page_size}

    print("=" * 72)
    print(f"接口    : {method}")
    print(f"网关    : {GATEWAY}")
    print(f"业务参数: {json.dumps(business, ensure_ascii=False)}")
    print("=" * 72)

    last = None
    bkey = BUSINESS_KEY[args.method]
    for name, signer in (("hmac-sha256", sign_hmac), ("md5", sign_md5)):
        print(f"\n---- 尝试签名算法：{name} ----")
        try:
            status, text = call(app_key, secret, method, bkey, business, signer)
        except Exception as e:  # 网络层
            print(f"  请求异常：{e}")
            continue
        print(f"  HTTP {status}  返回 {len(text)} 字节")
        if args.raw:
            print(text)
        if "error_response" in text:
            print(explain(text))
            print(f"  原始：{text[:400]}")
            last = text
            # 签名错就换下一个算法；权限错就没必要再试
            if '"code":"15"' in text or '"code":15' in text:
                break
            continue
        # 成功
        print("  ✅ 调用成功")
        try:
            data = json.loads(text)
            key = next((k for k in data if "response" in k), None)
            payload = data.get(key, {}) if key else {}
            print(f"\n  响应键：{key}")
            for k, v in list(payload.items())[:6]:
                if isinstance(v, list):
                    print(f"    {k}: {len(v)} 条")
                    for item in v[:3]:
                        if isinstance(item, dict):
                            print(f"       · {json.dumps(item, ensure_ascii=False)[:260]}")
                else:
                    print(f"    {k}: {v}")
        except Exception as e:
            print(f"  解析失败：{e}\n  原始：{text[:600]}")
        print("\n把上面的原始返回贴给我，我来判断覆盖率够不够、要不要接采集器。")
        return 0

    print("\n" + "=" * 72)
    # ⚠️ 别把「权限没下来」说成「签名没调通」—— 两者该修的地方完全不同。
    if last and ('"code":"15"' in last or '"code":15' in last):
        print("结论：**不是签名问题，是接口权限没下来。**")
        print("  网关明确回了「不存在的方法名」= 这个 app_key 没有该接口的权限。")
        print("  下一步：控制台「应用管理 → 接口权限」申请这两个接口；")
        print("         若控制台申请不到，按官方文档发邮件到 cps@jd.com 开通。")
    else:
        print("两种签名算法都没调通。把上面的原始响应贴给我，我按实际错误码定位。")
        if last:
            print(f"最后一次响应：{last[:500]}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
