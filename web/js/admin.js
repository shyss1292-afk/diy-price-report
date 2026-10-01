/* 采集管理 */

let pollTimer = null;

const TRIGGER_LABEL = { manual: '手动', schedule: '定时', backfill: '回填' };
const STATUS_LABEL = {
  running: '进行中',
  success: '成功',
  failed: '失败',
  partial: '部分失败',
  // 熔断退避期内被调度层 Fast-Fail 跳过：本轮没采，但不是失败
  skipped: '熔断跳过',
};

function statusPill(status) {
  const cls = status === 'success' ? 'down' : status === 'failed' ? 'up' : 'flat';
  return `<span class="pill ${cls}">${STATUS_LABEL[status] || status}</span>`;
}

async function loadStatus() {
  try {
    const [status, jobs, logs, health] = await Promise.all([
      api('/api/admin/status'),
      api('/api/scheduler'),
      api('/api/admin/logs?limit=30'),
      api('/api/admin/health').catch(() => null),   // 健康接口挂了不影响主面板
    ]);
    renderStatus(status);
    renderJobs(jobs.jobs || []);
    renderLogs(logs.items || []);
    if (health) renderHealth(health);
    schedulePoll(status.running);
  } catch (err) {
    document.getElementById('pageDesc').textContent = `加载失败：${err.message}`;
  }
}

/*
 * 采集健康面板。
 *
 * 报告页顶部那条是**概要**（只在有问题时出现）；这里是**详情**，
 * 无论好坏都显示 —— 排查时需要的正是"当时各项是什么状态"。
 * 2026-09-29 全天 0 条没人知道，事后复盘时连"当时告警开没开"都查不到。
 */
function renderHealth(h) {
  const el = document.getElementById('health');
  if (!el) return;

  const cov = h.coverage || {};
  const pct = Number(cov.pct || 0);
  const cfg = h.alerting || {};
  const st = h.health || {};
  const rules = cfg.rules || {};
  const fails = st.source_fails || {};
  const sup = cfg.suppressed || {};
  const br = h.breaker || {};

  // 用文件既有的样式约定（sep / muted / 内联），不引入新 class ——
  // 这个项目的 CSS 是手写的，凭空加 class 只会得到一个没样式的空壳。
  const line = (label, value) =>
    `<div style="display:flex;align-items:flex-start;justify-content:space-between;` +
    `gap:14px;padding:5px 0">` +
      `<span class="muted" style="flex:none;min-width:72px;font-size:12px">${label}</span>` +
      `<span style="flex:1;text-align:right;font-size:12px;word-break:break-all">${value}</span>` +
    `</div>`;

  const covCls = pct === 0 ? 'is-bad' : (pct < 50 ? 'is-warn' : 'is-ok');

  const chips = [];
  chips.push(`<span class="health-chip ${covCls}">今日覆盖 ${cov.covered ?? '—'}/${cov.total ?? '—'}（${pct}%）</span>`);

  Object.entries(br).forEach(([code, e]) => {
    const isLogin = /login|passport|登录/i.test(e.reason || '');
    chips.push(
      `<span class="health-chip ${isLogin ? 'is-bad' : 'is-warn'}">` +
      `${code} ${isLogin ? '登录失效' : '熔断'} · 剩 ${e.remaining_text || '?'}</span>`
    );
  });
  Object.entries(fails).filter(([c, n]) => n > 0 && c !== 'mock').forEach(([code, n]) => {
    chips.push(`<span class="health-chip is-bad">${code} 连续 ${n} 轮失败</span>`);
  });
  if ((st.zero_streak || 0) > 0) {
    chips.push(`<span class="health-chip is-bad">连续 ${st.zero_streak} 轮 0 条</span>`);
  }
  if (chips.length === 1 && covCls === 'is-ok') {
    chips.push('<span class="health-chip is-ok">一切正常</span>');
  }

  const chan = [];
  chan.push(cfg.enabled ? '总开关开' : '<b>总开关关</b>');
  chan.push(cfg.macos_notification ? 'macOS 通知开' : 'macOS 通知关');
  chan.push(cfg.webhook_enabled ? `webhook 开（${cfg.webhook_type}）` : 'webhook 关');

  const supList = Object.entries(sup);
  const last = st.last_round || {};

  el.innerHTML =
    `<div class="chip-row" style="gap:6px;margin-bottom:10px">${chips.join('')}</div>` +
    line('上一轮', last.at ? `${esc(last.at)} · ${int(last.listings)} 条` : '无记录') +
    line('告警通道', esc(chan.join(' · '))) +
    line('规则',
         `连续 ${rules.zero_yield_rounds} 轮 0 条 → critical<br>` +
         `单源连续 ${rules.source_fail_rounds} 轮失败 → warn<br>` +
         `型号 ${rules.model_stale_days} 天未覆盖 → warn`) +
    line('抑制中', supList.length
      ? esc(supList.map(([k, v]) => `${k}（${v}）`).join(' ｜ '))
      : '无') +
    line('配置文件', cfg.config_exists
      ? `<code style="font-size:11px">${esc(cfg.config_file)}</code>`
      : `<b>不存在，用默认值</b><br><code style="font-size:11px">${esc(cfg.config_file)}</code>`) +
    `<div class="sep" style="margin:10px 0"></div>` +
    `<div class="muted" style="font-size:11px">` +
      `自测通道：<code>python -m app.cli alert --test</code>　` +
      `覆盖检查：<code>python -m app.cli health --check</code>` +
    `</div>`;
}

function schedulePoll(running) {
  clearTimeout(pollTimer);
  if (running) {
    pollTimer = setTimeout(loadStatus, 2000);
    setButtons(true);
  } else {
    setButtons(false);
  }
}

function setButtons(disabled) {
  document.getElementById('btnCollect').disabled = disabled;
  document.getElementById('btnBackfill').disabled = disabled;
}

function renderStatus(s) {
  const stateText = s.running ? '采集中…' : s.last_error ? '上次失败' : '空闲';
  document.getElementById('kpiState').textContent = stateText;
  document.getElementById('kpiState').className =
    'kpi-value ' + (s.running ? '' : s.last_error ? 'up' : 'down');
  document.getElementById('kpiStateFoot').textContent = s.started_at
    ? `开始于 ${s.started_at}${s.finished_at ? ' · 结束于 ' + s.finished_at : ''}`
    : '暂无执行记录';

  document.getElementById('kpiListings').textContent = int(s.counts.listings);
  document.getElementById('kpiDaily').textContent = int(s.counts.price_daily);
  document.getElementById('kpiRange').textContent =
    s.data_range.from ? `${s.data_range.from} ~ ${s.data_range.to}` : '暂无数据';
  document.getElementById('kpiRangeFoot').textContent =
    `${s.counts.products} 个型号 · ${s.counts.platforms} 个平台 · ${int(s.counts.crawl_log)} 条日志`;

  document.getElementById('lastRunAt').textContent = s.finished_at || '—';
  const host = document.getElementById('lastResult');
  if (s.last_error) {
    host.innerHTML = `<div class="up">${esc(s.last_error)}</div>`;
  } else if (s.last_result) {
    const r = s.last_result;
    host.innerHTML = `
      <div style="margin-bottom:8px">
        <span class="pill tag">${TRIGGER_LABEL[r.trigger] || r.trigger}</span>
        <span class="muted" style="margin-left:8px">${r.day_count} 天 · 耗时 ${r.elapsed_sec}s</span>
      </div>
      <table class="data">
        <thead><tr><th>数据源</th><th>状态</th><th class="num">写入</th><th class="num">未匹配</th></tr></thead>
        <tbody>${r.sources.map((src) => `
          <tr>
            <td>${esc(src.name || src.source)}</td>
            <td>${statusPill(src.status)}</td>
            <td class="num">${int(src.items)}</td>
            <td class="num muted">${int(src.unmatched)}</td>
          </tr>`).join('')}
        </tbody>
      </table>
      <div class="muted" style="margin-top:8px;font-size:11px">
        明细合计 ${int(r.listings)} 条 · 聚合 ${int(r.aggregated)} 行
      </div>`;
  } else {
    host.innerHTML = '<div class="muted">尚无记录</div>';
  }
}

function renderJobs(jobs) {
  const host = document.getElementById('jobs');
  if (!jobs.length) {
    host.innerHTML = '<div class="muted">调度器未启动（以 --no-scheduler 方式运行时不会注册定时任务）</div>';
    return;
  }
  host.innerHTML = jobs.map((j) => `
    <div style="display:flex;align-items:center;justify-content:space-between;padding:6px 0">
      <div>
        <b style="font-weight:500">${esc(j.id)}</b>
        <div class="muted" style="font-size:11px">每 ${j.id.includes('collect') ? '日 02:00' : '日 03:30'}</div>
      </div>
      <div class="muted" style="font-size:12px">下次执行 ${esc(j.next_run || '—')}</div>
    </div>`).join('')
    + '<div class="sep" style="margin:10px 0"></div>'
    + '<div class="muted" style="font-size:11px">伴随 launchd 后台常驻，Mac 睡眠唤醒后错过的任务会自动补跑。</div>';
}

function renderLogs(items) {
  const tbody = document.getElementById('logTbody');
  if (!items.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="loading">暂无日志</td></tr>';
    return;
  }
  tbody.innerHTML = items.map((r) => `
    <tr>
      <td class="muted">${esc(r.started_at)}</td>
      <td><b style="font-weight:500">${esc(r.source)}</b></td>
      <td><span class="pill tag">${TRIGGER_LABEL[r.trigger] || r.trigger}</span></td>
      <td>${statusPill(r.status)}</td>
      <td class="num">${int(r.items)}</td>
      <td class="num muted">${r.duration_sec === null ? '—' : r.duration_sec + 's'}</td>
      <td class="muted" style="max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
          title="${esc(r.message)}">${esc(r.message)}</td>
    </tr>`).join('');
}

async function trigger(url, body, label) {
  try {
    await api(url, { method: 'POST', body: body || {} });
    toast(`${label}已启动`);
    setButtons(true);
    setTimeout(loadStatus, 800);
  } catch (err) {
    toast(`${label}失败：${err.message}`, true);
    setButtons(false);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('btnCollect').addEventListener('click', () => {
    trigger('/api/admin/collect', {}, '采集任务');
  });
  document.getElementById('btnBackfill').addEventListener('click', () => {
    const days = Number(document.getElementById('backfillDays').value);
    trigger('/api/admin/backfill', { days }, `回填 ${days} 天`);
  });
  document.getElementById('btnRefresh').addEventListener('click', loadStatus);
  loadStatus();
});
