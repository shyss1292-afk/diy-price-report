"""京东联盟 API 探针：验证接口能否调通 + **覆盖率**（不写入任何数据）。

为什么先写探针、而不是直接接采集器
--------------------------------
官方联盟接口只覆盖**参加联盟推广的商品**，未参加的商品查不到。
覆盖率没确认之前就改造采集器，等于赌一把；而且本项目 MEMORY 里把
「写了但没接线」列为明确要避免的模式（服务在跑、日志干净、实际没调用）。

所以先用最小代价拿一次真实返回：
  · 返回了几条？价格合不合理？
  · 想要的型号（如 RTX 5070 12G）在不在里面？

覆盖够 → 再接 `app/collectors/`；不够 → 这条路直接放弃，不浪费改造成本。

⚠️ 参数规范全部来自官方文档《（新版）联盟API接口文档》
   `https://union.jd.com/searchResultDetail?articleId=108188`
   （2026-10-01 登录后逐字核对）

踩过的坑（这些都是错的，别再犯）
--------------------------------
1. **`v` 必须是 `1.0`，不是 `2.0`**。
   传 `2.0` 会返回 `code 15 不存在的方法名` —— 网关按 method+version 查表，
   版本不对就当成"方法不存在"。**这个错误害我误判了两天**：
   我一度以为"连伪造方法也返回 15 ⇒ 是无权限"，其实两边都是版本写错。
2. **`timestamp` 是 `yyyy-MM-dd HH:mm:ss`（GMT+8），不是毫秒时间戳**。
   传毫秒会返回 `code 8 时间戳参数不正确`。
3. **业务参数键名是 `360buy_param_json`**，且要包一层 DTO：
   `{"goodsReqDTO": {"keyword": "鞋", "pageIndex": "1"}}`
4. **签名是 `md5(secret + 排序拼接 + secret)`，secret 夹两端**。
   不是 `md5(secret + 拼接)`，也不是 hmac-sha256
   （官方原话：「签名的摘要算法，暂时只支持 md5」）。
5. `sign_method=md5` 是**必传**的系统参数。

调用地址（官方 1.3 节确认）：`https://api.jd.com/routerjson`
⚠️ 旧版域名 `router.jd.com` 官方已宣布**停止维护**，别再用。

错误码速查
----------
· 外层 `code=0` → **网关调用成功**，真正的结果在 `queryResult` 里
· 外层 `code=15` → 方法名+版本对不上（**先检查 `v` 是不是 1.0**）
· 外层 `code=8`  → 时间戳格式错（要 `yyyy-MM-dd HH:mm:ss`）
· 内层 `code=403 无访问权限` → 参数都对了，**差接口权限**，去申请：
  `https://union.jd.com/openplatform/groupApply` 选推广模式，审批通过自动开通

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
    python -m scripts.jd_union_probe --page-size 100
    python -m scripts.jd_union_probe --raw                    # 打印完整原始响应

⚠️ 联盟后台的原话：「请妥善保管您的 appkey 和 secretkey，禁止保存在任何版本库托管服务
   （如 GitHub）或以其他途径公开，否则可能被禁用。」—— 所以这里只从环境变量或
   **gitignore 覆盖的** `data/` 目录读取，**任何情况下都不写进源码**。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 凭据文件（data/ 被 .gitignore 整目录覆盖，不会进版本库）
CRED_FILE = Path(__file__).resolve().parent.parent / "data" / "jd_union.json"

# 调用地址。官方文档 1.3 节：正式环境 = https://api.jd.com/routerjson
# ⚠️ 旧版域名 router.jd.com 官方已宣布停止维护（联盟开放平台首页通知），不要再用。
GATEWAY = "https://api.jd.com/routerjson"

# 业务参数最外层参数名。官方文档 1.4 节。
BUSINESS_PARAM_KEY = "360buy_param_json"

# API 协议版本。官方文档 1.4 节示例值是 1.0。
# ⚠️ 传 2.0 会得到 code 15「不存在的方法名」—— 见模块头「踩过的坑」第 1 条。
API_VERSION = "1.0"

# 权限申请入口（官方文档「三、权限申请」）
PERMISSION_URL = "https://union.jd.com/openplatform/groupApply"


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


def jd_timestamp() -> str:
    """官方要求的时间戳格式：`yyyy-MM-dd HH:mm:ss`，时区 GMT+8，误差 ≤10 分钟。

    ⚠️ 不是毫秒时间戳 —— 传毫秒会得到 `code 8 时间戳参数不正确`。
    """
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def generate_sign(secret: str, params: dict) -> str:
    """官方签名算法（文档 1.5 节）：

      1. 参数名按字母序排列
      2. 参数名与参数值依次拼接
      3. **appSecret 夹在字符串两端**
      4. MD5 加密后转大写

    ⚠️ secret 是**两端**，不是只加前缀。官方原话：「把 appSecret 夹在字符串（上一步
       拼接串）的两端」。也**不是** hmac-sha256（官方：「暂时只支持 md5」）。
    """
    raw = "".join(f"{k}{v}" for k, v in sorted(params.items()))
    return hashlib.md5((secret + raw + secret).encode("utf-8")).hexdigest().upper()


def build_request(app_key: str, method: str, business: dict) -> dict:
    """组装完整请求参数（系统参数 + 业务参数）。"""
    return {
        "method": method,
        "app_key": app_key,
        "timestamp": jd_timestamp(),
        "format": "json",
        "v": API_VERSION,
        "sign_method": "md5",   # 官方 1.4 节：必传
        BUSINESS_PARAM_KEY: json.dumps(business, separators=(",", ":"), ensure_ascii=False),
    }


def call(app_key: str, secret: str, method: str, business: dict,
         gateway: str = GATEWAY, timeout: float = 25.0):
    """POST 到网关。用标准库 urllib —— 本项目没装任何 HTTP 客户端库
    （采集全靠 Playwright），探针脚本不该为它引入新依赖。"""
    params = build_request(app_key, method, business)
    params["sign"] = generate_sign(secret, params)
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


# 网关层错误码 → 人话 + 下一步
GATEWAY_HINTS = {
    "15": "方法名 + 版本对不上。**先检查 `v` 是不是 1.0** —— 传 2.0 就会报这个。",
    "8": "时间戳格式错。官方要 `yyyy-MM-dd HH:mm:ss`（GMT+8），不是毫秒。",
    "11": "签名错误。检查：secret 是否夹在两端、参数是否按字母序、sign_method 是否传了。",
    "10001": "app_key 没传对 / 请求体过大。检查凭据是否生效。",
    "10002": "app_key 无效或未审核通过。",
    "21": "appkey 信息无效（网关不认得这个 key）。",
}


def explain(text: str) -> str:
    """把响应翻成人话 + 下一步该干什么。区分**网关层**与**业务层**两个 code。"""
    try:
        body = json.loads(text)
    except Exception:
        return ""

    out: list[str] = []

    err = body.get("error_response")
    if err:
        code = str(err.get("code", ""))
        zh = err.get("zh_desc") or err.get("en_desc") or ""
        out.append(f"❌ 网关层错误 code={code}：{zh}")
        if code in GATEWAY_HINTS:
            out.append(f"   → {GATEWAY_HINTS[code]}")
        return "\n".join(out)

    # 业务层有两层 code，别搞混：
    #   外层 `code=0`  = 网关调用成功（签名/参数都过了）
    #   内层 `queryResult.code` = **真正的业务结果**（403 之类在这里）
    # ⚠️ 只看外层会把「403 无访问权限」误报成成功 —— 踩过一次。
    key = next((k for k in body if k.endswith("_responce")), None)
    if not key:
        return ""
    payload = body[key]
    outer_code = str(payload.get("code", ""))
    inner: dict = {}
    qr = payload.get("queryResult")
    if isinstance(qr, str):
        try:
            inner = json.loads(qr)
        except Exception:
            inner = {}

    inner_code = str(inner.get("code", "")) if inner else ""

    if outer_code != "0":
        out.append(f"⚠️ 网关层 code={outer_code}（外层非 0）")
        if outer_code in GATEWAY_HINTS:
            out.append(f"   → {GATEWAY_HINTS[outer_code]}")
        return "\n".join(out)

    if inner_code and inner_code != "0":
        msg = inner.get("message") or inner.get("msg") or ""
        out.append(f"⚠️ 网关通过（外层 code=0），但**业务层失败 code={inner_code}**：{msg}")
        if inner_code == "403" or "无访问权限" in text:
            out.append("   → 参数全对了，**差的是接口权限**。去申请：")
            out.append(f"     {PERMISSION_URL} （选推广模式，审批通过自动开通）")
        elif inner_code in GATEWAY_HINTS:
            out.append(f"   → {GATEWAY_HINTS[inner_code]}")
        return "\n".join(out)

    out.append("✅ 网关与业务层都通过（外层 code=0，内层 code=0/空）")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="京东联盟 API 探针（验证权限与覆盖率，不写入任何数据）")
    ap.add_argument("--keyword", default="RTX 5070 12G", help="搜索关键词")
    ap.add_argument("--page-size", type=int, default=30, help="每页条数")
    ap.add_argument("--page-index", type=int, default=1)
    ap.add_argument("--raw", action="store_true", help="打印完整原始响应")
    ap.add_argument("--cred", default=None, help="换一份凭据文件（A/B 对比不同媒体类型）")
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

    method = "jd.union.open.goods.query"
    # 官方 1.6 节示例：业务参数包一层 goodsReqDTO
    business = {
        "goodsReqDTO": {
            "keyword": args.keyword,
            "pageIndex": str(args.page_index),
            "pageSize": str(args.page_size),
        }
    }

    print("=" * 72)
    print(f"接口    : {method}")
    print(f"网关    : {GATEWAY}")
    print(f"版本    : v={API_VERSION}  sign_method=md5")
    print(f"业务参数: {json.dumps(business, ensure_ascii=False)}")
    print("=" * 72)

    try:
        status, text = call(app_key, secret, method, business)
    except Exception as e:
        print(f"请求异常：{e}")
        return 1

    print(f"\nHTTP {status}  返回 {len(text)} 字节")
    hint = explain(text)
    if hint:
        print(hint)

    if args.raw:
        print("\n--- 原始响应 ---")
        print(text)

    # 尝试解析出商品列表
    try:
        body = json.loads(text)
        key = next((k for k in body if k.endswith("_responce")), None)
        payload = body.get(key, {}) if key else {}
        qr = payload.get("queryResult")
        if isinstance(qr, str):
            q = json.loads(qr)
            data = q.get("data") or {}
            lst = data.get("goodsList") or data.get("goods_list") or []
            print(f"\n总条数 total={data.get('totalCount') or data.get('total')}  本页 {len(lst)} 条")
            for g in lst[:5]:
                print(f"   · {json.dumps(g, ensure_ascii=False)[:300]}")
            if lst:
                print("\n✅ 拿到数据了 —— 把上面几条贴给我，我判断覆盖率与字段可用性。")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
