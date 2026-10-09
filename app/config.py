"""全局配置。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
WEB_DIR = BASE_DIR / "web"
SCRIPTS_DIR = BASE_DIR / "scripts"

DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "diyprice.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"

APP_NAME = "DIY 配件价格追踪系统"
APP_VERSION = "0.1.0"

# 监听地址。默认只绑回环（**不要把默认值改成 0.0.0.0**）——
# 这个服务没有任何鉴权，绑到 0.0.0.0 等于把整库行情和配置单暴露给同网段所有人。
# 需要手机访问时**显式**开：`DIYPRICE_HOST=0.0.0.0`（本项目已由 launchd plist 设置）。
HOST = os.environ.get("DIYPRICE_HOST", "127.0.0.1")
PORT = int(os.environ.get("DIYPRICE_PORT", "8848"))

# 历史回填天数（Mock 源用）
BACKFILL_DAYS = 180
# 趋势指标计算窗口（天）
TREND_WINDOWS = (1, 7, 30)
# 历史分位参考窗口（天）
PERCENTILE_WINDOW = 90
