"""全局配置。"""
from __future__ import annotations

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

HOST = "127.0.0.1"
PORT = 8848

# 历史回填天数（Mock 源用）
BACKFILL_DAYS = 180
# 趋势指标计算窗口（天）
TREND_WINDOWS = (1, 7, 30)
# 历史分位参考窗口（天）
PERCENTILE_WINDOW = 90
