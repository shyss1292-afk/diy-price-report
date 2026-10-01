"""轮次告警：把「跑挂了」变成「你知道」。

为什么必须有
------------
2026-09-29 全天 15 轮**全部 0 条**、覆盖 0/130，跑了一整天**没有任何通知**。
项目当时只有 `collect.log` 和 `breaker.json` —— 日志是「去看才有」，
告警是「出事就告诉你」，两者不能互相替代。用户是碰巧让我分析外部仓库时
才发现的，隔了整整一天。

设计取舍
--------
1. **零配置可用**：默认走 macOS 系统通知（`osascript`），不需要任何申请、
   不需要填 webhook。先让它「能响」，再去谈「响得好看」。
   要更远的触达（手机）再配 webhook。
2. **必须带去重抑制**：一个持续故障（比如 PDD 熔断一整天）会每轮触发一次。
   没有抑制的话你会被淹没，最后把告警关掉 —— 那就等于没做。
   所以同一个 `key` 在 `cooldown_seconds` 内只发一次。
3. **告警失败绝不影响采集**：所有发送都包在 try 里，只记日志、不抛异常。
   告警是旁路，不能因为它把主流程带崩。
4. **状态落盘**：`data/alert_state.json` 记录每个 key 上次发送时间，
   进程重启后抑制仍然有效。

配置
----
`data/alert_config.json`（`data/` 被 .gitignore 整目录覆盖，不会进版本库）。
文件不存在时用内置默认值 —— **macOS 通知默认开，webhook 默认关**。

    {
      "enabled": true,
      "macos_notification": true,
      "webhook": {"enabled": false, "url": "", "type": "generic"},
      "cooldown_seconds": 3600,
      "rules": {
        "zero_yield_rounds": 1,
        "source_fail_rounds": 3,
        "model_stale_days": 7
      }
    }

`webhook.type` 支持：
  · `generic`  —— 直接 POST `{"title":..., "body":..., "level":...}`
  · `dingtalk` —— 钉钉机器人（`{"msgtype":"text","text":{"content":...}}`）
  · `wecom`    —— 企业微信机器人（同钉钉结构）
  · `bark`     —— Bark（URL 拼在路径上）

用法
----
    from app.services import alerting

    alerting.send("critical", "本轮 0 条", "三源全部失败", key="zero-yield")
    alerting.self_test()      # 发一条测试告警，验证通道通不通
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger("diyprice.alert")

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
CONFIG_FILE = DATA_DIR / "alert_config.json"
STATE_FILE = DATA_DIR / "alert_state.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": True,
    # 零配置通道：macOS 系统通知。不需要申请、不需要填 URL，装了就能响。
    "macos_notification": True,
    "webhook": {"enabled": False, "url": "", "type": "generic"},
    # 同一个 key 在这个窗口内只发一次。3600s = 1 小时。
    "cooldown_seconds": 3600,
    "rules": {
        # 连续 N 轮总明细为 0 → critical
        "zero_yield_rounds": 1,
        # 某个源连续 N 轮失败 → warn
        "source_fail_rounds": 3,
        # 某个型号连续 N 天没采到 → warn
        "model_stale_days": 7,
    },
}


# --------------------------------------------------------------------- 配置

def load_config() -> dict[str, Any]:
    """读配置。文件不存在/损坏时回退到默认值 —— 告警不能因为配置读不出来就哑掉。"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝
    if CONFIG_FILE.exists():
        try:
            user = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            logger.warning("告警配置读取失败，用默认值：%s", e)
    # 环境变量兜底（方便 plist / 命令行临时覆盖）
    if os.getenv("DIYPRICE_ALERT_DISABLE"):
        cfg["enabled"] = False
    return cfg


def ensure_config_file() -> Path:
    """首次运行时落一份带注释的默认配置，方便用户改。"""
    if not CONFIG_FILE.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(
            json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return CONFIG_FILE


# --------------------------------------------------------------------- 抑制

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning("告警状态写入失败：%s", e)


def _suppressed(key: str, cooldown: float) -> bool:
    """这个 key 是否还在抑制窗口内。"""
    if not key or cooldown <= 0:
        return False
    last = _load_state().get(key, {}).get("at", 0)
    return (time.time() - last) < cooldown


def _mark_sent(key: str) -> None:
    if not key:
        return
    state = _load_state()
    state[key] = {"at": time.time(), "at_text": time.strftime("%Y-%m-%d %H:%M:%S")}
    _save_state(state)


# --------------------------------------------------------------------- 通道

def _send_macos(title: str, body: str, level: str) -> bool:
    """macOS 系统通知。用 osascript，零依赖。

    ⚠️ 字符串要转义：标题/正文里出现双引号会把 AppleScript 语法打断，
       通知直接不发（而且不报错），排查起来很费劲。
    """
    if not shutil.which("osascript"):
        return False

    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace('"', '\\"')

    sound = 'sound name "Basso"' if level == "critical" else ""
    script = (
        f'display notification "{esc(body[:400])}" '
        f'with title "{esc(title[:120])}" {sound}'
    )
    try:
        subprocess.run(["osascript", "-e", script], timeout=10, capture_output=True)
        return True
    except Exception as e:
        logger.warning("macOS 通知发送失败：%s", e)
        return False


def _send_webhook(cfg: dict, title: str, body: str, level: str) -> bool:
    hook = cfg.get("webhook") or {}
    url = (hook.get("url") or "").strip()
    if not url:
        return False
    kind = (hook.get("type") or "generic").lower()
    text = f"[{level.upper()}] {title}\n{body}"

    if kind in ("dingtalk", "wecom"):
        payload = {"msgtype": "text", "text": {"content": text}}
        target = url
    elif kind == "bark":
        # Bark 把内容拼在路径上，且要 urlencode
        from urllib.parse import quote
        target = f"{url.rstrip('/')}/{quote(title)}/{quote(body[:300])}"
        payload = None
    else:
        payload = {"title": title, "body": body, "level": level}
        target = url

    try:
        if payload is None:
            req = urllib.request.Request(target, method="GET")
        else:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                target, data=data,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
        with urllib.request.urlopen(req, timeout=8) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        logger.warning("webhook 发送失败（%s）：%s", kind, e)
        return False


# --------------------------------------------------------------------- 对外

def send(level: str, title: str, body: str = "", key: str | None = None) -> dict:
    """发一条告警。level: info | warn | critical。

    `key` 是抑制键：同一个 key 在 cooldown 内只发一次。不传则不抑制。

    ⚠️ 绝不抛异常 —— 告警是旁路，不能因为它把采集带崩。
    """
    cfg = load_config()
    if not cfg.get("enabled", True):
        return {"sent": False, "reason": "disabled"}

    cooldown = float(cfg.get("cooldown_seconds") or 0)
    if _suppressed(key or "", cooldown):
        logger.info("告警被抑制（%s 在 %.0fs 冷却期内）：%s", key, cooldown, title)
        return {"sent": False, "reason": "suppressed"}

    channels: list[str] = []
    if cfg.get("macos_notification", True) and _send_macos(title, body, level):
        channels.append("macos")
    if (cfg.get("webhook") or {}).get("enabled") and _send_webhook(cfg, title, body, level):
        channels.append("webhook")

    if channels:
        _mark_sent(key or "")
    # 通道全挂时也写日志，至少 collect.log 里有痕迹
    logger.warning("【告警·%s】%s | %s（通道：%s）", level.upper(), title, body, channels or "无")
    return {"sent": bool(channels), "channels": channels}


def self_test() -> dict:
    """发一条测试告警，验证通道是否通。用户第一次配完可以跑这个。"""
    return send(
        "info",
        "DIY价格汇报 · 告警测试",
        f"如果你看到这条，说明告警通道是通的。{time.strftime('%H:%M:%S')}",
        key=None,   # 测试不抑制
    )


def status() -> dict:
    """当前配置与抑制状态，供 CLI/网页展示。"""
    cfg = load_config()
    state = _load_state()
    return {
        "config_file": str(CONFIG_FILE),
        "config_exists": CONFIG_FILE.exists(),
        "enabled": cfg.get("enabled", True),
        "macos_notification": cfg.get("macos_notification", True),
        "webhook_enabled": bool((cfg.get("webhook") or {}).get("enabled")),
        "webhook_type": (cfg.get("webhook") or {}).get("type", ""),
        "cooldown_seconds": cfg.get("cooldown_seconds"),
        "rules": cfg.get("rules", {}),
        "suppressed": {
            k: v.get("at_text") for k, v in sorted(state.items())
        },
    }


def clear_suppression() -> int:
    """清掉抑制状态 —— 调完配置想立刻验证时用。"""
    n = len(_load_state())
    _save_state({})
    return n


def update_config(**changes) -> dict:
    """局部更新配置文件并返回新配置。

    供 CLI 用（`alert --set-webhook` / `--off` / `--set-cooldown`）。
    写文件而不是让用户手改 JSON —— 手改容易把 JSON 写坏，
    而配置坏了告警会**静默失效**（load_config 会回退默认值，用户不会知道）。
    """
    cfg = load_config()
    for k, v in changes.items():
        if v is None:
            continue
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return cfg
