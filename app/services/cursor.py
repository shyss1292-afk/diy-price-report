"""采集游标：记录每个数据源下一轮从哪里继续。

京东 / 拼多多这类有反爬限流的源，单轮只能采少量型号。
靠游标轮转，多轮之后就能覆盖整个型号库，而不是每次都重复采同一批。

游标存在 `data/collect_cursor.json`：
    {"jd": {"offset": 18, "updated_at": "2026-09-16T17:40:00"}}

偏移量对「按品类轮转展开后的序列长度」取模，所以型号库增删也不会越界。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..config import DATA_DIR

CURSOR_FILE = DATA_DIR / "collect_cursor.json"


def _load_all() -> dict:
    if not CURSOR_FILE.exists():
        return {}
    try:
        data = json.loads(CURSOR_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def load_offset(source: str) -> int:
    """读取某数据源的当前游标（轮次进度）。"""
    entry = _load_all().get(source) or {}
    try:
        return max(0, int(entry.get("offset", 0)))
    except (TypeError, ValueError):
        return 0


def save_offset(source: str, offset: int) -> None:
    data = _load_all()
    data[source] = {
        "offset": int(offset),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    CURSOR_FILE.parent.mkdir(parents=True, exist_ok=True)
    CURSOR_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def peek(source: str) -> dict:
    return _load_all().get(source, {})


def reset(source: str | None = None) -> None:
    """重置游标（不带参数则清空全部）。"""
    if source is None:
        CURSOR_FILE.unlink(missing_ok=True)
        return
    data = _load_all()
    data.pop(source, None)
    CURSOR_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
