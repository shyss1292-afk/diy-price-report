# DIY 配件价格追踪

显卡 / CPU 的多平台价格采集、清洗、日报与装机助手。

针对的是**反爬限流**场景：京东、拼多多、闲鱼三个源单轮都只能采少量型号，
靠**游标在多轮之间轮转**覆盖全库，而不是一次抓完。

---

## 它解决什么问题

自用装机选配件时，价格散在三个平台、同一个型号有一堆"看着像但不是在卖"的
挂单（换了卡顺手提一句、拆机件、工业卡、捆绑销售）。手工比价既慢又容易被
单条低价误导。这个项目把采集、清洗、比价、日报做成一条流水线。

---

## 架构

```
launchd (每小时 :30)
   └─ scripts/collect_scheduled.sh
        ├─ 单实例锁（含 PID + 绝对时间戳，可自愈）
        ├─ caffeinate 防休眠
        ├─ app.cli collect  ← 进程组隔离 + 看门狗
        └─ 汇总本轮结果 + 覆盖进度
             │
             ▼
        run_pipeline()
          ├─ 前置检查（系统代理 / 网络）      ← 不通就早退 + 告警
          ├─ 按源轮转采集（游标推进）
          │    ├─ jd_source    浏览器
          │    ├─ pdd_source   浏览器 + 登录态
          │    └─ xianyu_source 浏览器
          ├─ 型号匹配（1491 条规则）
          ├─ 质量闸门（四道）
          ├─ 写入 listings（幂等：按日期 × 平台 × 批次）
          └─ 聚合 → price_daily → 日报 / 装机助手
```

### 关键模块

| 模块 | 职责 |
|---|---|
| `app/services/pipeline.py` | 轮次编排 + 三道超时防线 |
| `app/services/browser_worker.py` | 短生命周期浏览器（按需拉起、用完彻底关闭） |
| `app/services/breaker.py` | 跨轮次熔断退避（硬/软风控独立阶梯） |
| `app/services/healthcheck.py` | 健康检查规则（0 条 / 单源连续失败 / 覆盖缺口） |
| `app/services/alerting.py` | 告警通道（macOS 通知 + webhook，带去重抑制） |
| `app/collectors/policy.py` | 平台节流策略（间隔、就绪判据、行为模拟、限流识别） |
| `app/collectors/quality.py` | 报价质量闸门 |
| `app/services/builds.py` | 装机助手取价（只读 `price_daily`） |

---

## 快速开始

```bash
# 1) 依赖
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pip install playwright && .venv/bin/playwright install chromium

# 2) 建库 + 写入平台/型号主数据
.venv/bin/python -m app.cli init

# 3) 登录（会打开浏览器让你扫码；登录态存 data/sessions/）
.venv/bin/python -m app.cli login --site jd
.venv/bin/python -m app.cli login --site pdd
.venv/bin/python -m app.cli login --site goofish

# 4) 采集一轮
.venv/bin/python -m app.cli collect --sources jd,pdd,xianyu

# 5) 起服务（看板 / 日报 / 装机助手）
.venv/bin/python -m app.cli serve --port 8848
```

> 脚本里的 Python 解释器由 `scripts/_python.sh` 统一解析：
> `$DIYPRICE_PYTHON` → 项目内 `.venv`/`venv` → 系统 `python3`。
> 不用改脚本就能换环境。

### 页面

| 路径 | 内容 |
|---|---|
| `/` | 总览看板 |
| `/report` | 价格日报（顶部有采集健康条，**只在有问题时出现**） |
| `/admin` | 采集管理（健康详情、日志、手动触发） |
| `/build` | 装机助手（手机优先，同 WiFi 可直连局域网 IP） |

---

## 命令行

```bash
python -m app.cli collect [--sources jd,pdd,xianyu]   # 采一轮
python -m app.cli serve   [--port 8848]               # 起服务
python -m app.cli login   --site pdd                  # 扫码登录
python -m app.cli sessions                            # 查看各站登录态
python -m app.cli breaker                             # 熔断状态 / 人工解除
python -m app.cli probe   [--source pdd]              # 退避期内探活
python -m app.cli alert   --test                      # 测告警通道
python -m app.cli health  [--check] [--reset]         # 健康 / 覆盖检查
```

---

## 设计要点

### 三道超时防线

采集走 CDP 驱动真实浏览器，卡死的形式不止一种，所以分三层：

1. **停滞看门狗**（进程内，240s）—— 单个型号超过 240 秒没有进展就判定卡死。
   针对的是：**断网时 `page.goto(timeout=40000)` 不返回**
   （Playwright 的 timeout 在网络栈卡住时不生效）。实测这一条把"卡住"的
   代价从 35 分钟压到 4 分钟。
2. **墙钟兜底**（进程内，35 分钟）—— 用 `time.time()`（含休眠）+ 5 秒轮询，
   即使主线程卡在不可中断的系统调用里也照样终止。
3. **shell 看门狗**（30 分钟）—— 按**进程组**广播信号，覆盖
   Python + Chrome + Playwright 的 node 驱动全树。

> 停滞看门狗用 `os._exit()` 而不是 SIGALRM 中断 Playwright ——
> 实测 SIGALRM 确实能精准中断（`wait_for_timeout(60s)` 在 3.0s 被打断），
> **但中断后浏览器实例进入不一致状态**，紧接着的 `goto` 挂了 2 分钟没返回。
> 与其修一个坏实例，不如结束本轮让下一轮重建（约 3 秒）。

### 归因必须准确

同一个"没采到数据"的表象，背后可能是完全不同的原因，处置方式也完全不同：

| 现象 | 真实原因 | 处置 |
|---|---|---|
| 连续 3 个型号 0 条 | 网络断了 | 重试；**不是**平台限流 |
| 跳转到 `login.html` | 登录态失效 | **重新扫码**；等退避没用 |
| 页面 `has been closed` | 浏览器实例挂了 | 重建实例；**不是**限流 |
| 命中限流特征 | 平台在拦你 | 退避 |

这些都踩过坑：把网络故障当成限流，就会去查平台风控、调退避阶梯 ——
**方向指反，越查越远**。所以每类异常都有独立的判定函数和独立计数。

### 熔断退避分硬/软两档

- **硬风控**（明确的限流特征）：30min → 2h → 6h → 1天 封顶
- **软风控**（系统繁忙 / 429 / 40001）：15min → 2h → 4h 封顶

分开是因为两者恢复速度差一个数量级，用同一套阶梯要么太激进要么太保守。

### 报价质量闸门

原始报价要过四道闸门才进 `listings`：

1. **型号匹配**（1491 条规则，含别名与负向词）
2. **品相识别**（全新 / 二手，两者本来就不能比价）
3. **离群剔除**（相对型号基准价的偏离度）
4. **质量闸门**（坏卡、捆绑、工业卡、错配）

⚠️ 已知局限：**"提到但不是在卖"的错配，词表抓不到**。
实测一条标题「技嘉 RX6800 超级雕…**换了 5060Ti 故出**」，
卖的是 RX 6800，只因句子里提到"5060Ti"就被归到该型号名下，
把当天最低价从 ¥4400 拉到 ¥2300。
所以装机助手算总价用的是 **`price_daily.p25_price`（稳健价）**，
真实最低挂牌只作为提示透出、不参与总价。

### 健康检查与告警

- **规则**：连续 N 轮 0 条 → critical；单源连续 N 轮失败 → warn；
  全源不可用 → critical；型号 N 天未覆盖 → warn
- **熔断跳过不算失败** —— 它是设计行为，把它当故障报警会让人对告警脱敏
- **去重抑制**：同一 key 在冷却窗口内只发一次。持续故障每轮一条的话，
  人最后会把告警关掉，那比不报更糟
- **默认走 macOS 系统通知**（零配置）；要手机也能收到就配 webhook

```bash
python -m app.cli alert --set-webhook "<钉钉/企微/Bark URL>" --type dingtalk
```

---

## 测试

```bash
.venv/bin/python -m scripts.selftest        # 纯逻辑自检（745 条，不依赖浏览器与网络）
.venv/bin/python -m scripts.contract_report # 分板块契约测试
.venv/bin/python -m scripts.reverse_check   # 反向验证：证明断言真的能拦住回归
```

自检覆盖的重点不是"函数返回值对不对"，而是**那些踩过坑的判据**：
阈值之间的相对关系、归因函数的边界、配置的完整性。

---

## 目录

```
app/
  collectors/   各平台采集器 + 节流策略 + 质量闸门
  services/     流水线 / 熔断 / 健康检查 / 告警 / 聚合
  api/          FastAPI 路由
  cli.py        命令行入口
web/            看板 / 日报 / 管理页 / 装机助手
scripts/        定时脚本、自检、诊断工具
reports/        调研与说明文档
data/           运行数据（不进版本库）
```

---

## 合规说明

仅供个人学习与自用比价。请遵守各平台用户协议与 `robots.txt`，
控制请求频率。**不要**把它用于商业采集或对平台造成压力。

本项目不含任何账号凭据 —— 登录态保存在 `data/sessions/`（已被 `.gitignore` 排除）。
