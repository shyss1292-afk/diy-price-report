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
    const [status, jobs, logs] = await Promise.all([
      api('/api/admin/status'),
      api('/api/scheduler'),
      api('/api/admin/logs?limit=30'),
    ]);
    renderStatus(status);
    renderJobs(jobs.jobs || []);
    renderLogs(logs.items || []);
    schedulePoll(status.running);
  } catch (err) {
    document.getElementById('pageDesc').textContent = `加载失败：${err.message}`;
  }
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
