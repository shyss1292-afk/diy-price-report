# 已停用的采集器

## 为什么停用

ZOL（中关村在线）与太平洋电脑网是**媒体参考报价**，口径与电商实际售价不同
（系统性偏高），使用人数少、缺乏参考价值，于 2026-09-17 停用。

保留其余来源不变：jd（京东）/ pdd（拼多多）/ xianyu（闲鱼）。

## 停用动作清单

1. 采集适配器移出 `app/collectors/`（本目录）
2. `app/collectors/__init__.py` 移除注册导入
3. `app/seed_data.py` 的 `PLATFORMS` 移除两个平台定义
4. `app/services/report.py` 移除 `EXTRA_PLATFORMS` 与平台标签
5. 数据库中两个平台被置为 `is_active = 0`
6. `scripts/` 下的采集入口、站点探测、诊断脚本移除对应引用

## 如何恢复

把本目录的两个 `.py` 移回 `app/collectors/`，恢复 `__init__.py` 的导入与
`seed_data.py` 的平台定义，再把 `platforms.is_active` 置回 1。
数据库里的历史明细（listings / price_daily）**未被删除**，恢复后即可重新出现。
