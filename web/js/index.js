/* 总览看板 */

let period = 7;
let basis = 'all';
let indexChart = null;
let categoryChart = null;

const PERIOD_LABEL = { 1: '日', 7: '7 日', 30: '30 日' };
const BASIS_LABEL = { all: '全市场最低价', new: '全新最低价', used: '二手最低价' };

// 每个板块展示几款
const CARDS_PER_GROUP = 6;
// 每个板块取回多少候选（供组内按品类轮流挑，所以要留足余量）
const CANDIDATES_PER_GROUP = 40;
// 兜底分组：万一 /api/meta 没返回（或接口变动），也不至于整块空着
const FALLBACK_GROUPS = [
  { code: 'core', label: '核心配件', hint: '显卡 / CPU / 内存', categories: ['gpu', 'cpu', 'ram'] },
  { code: 'other', label: '其他硬件', hint: '主板 / 固态 / 电源 / 散热 / 机箱', categories: ['mb', 'ssd', 'psu', 'cooler', 'case'] },
];

async function loadAll() {
  document.getElementById('pageDesc').textContent = '正在加载市场数据…';
  try {
    // limit：首页每板块只渲染几款，没必要把 300+ 型号（100KB+）全拉回来
    const common = `days=180&period=${period}&basis=${basis}`;
    const [meta, overview, marketIndex, core, other, coverage] = await Promise.all([
      api('/api/meta'),
      api(`/api/overview?${common}`),
      api('/api/market-index?days=180'),
      api(`/api/products?${common}&group=core&limit=${CANDIDATES_PER_GROUP}`),
      api(`/api/products?${common}&group=other&limit=${CANDIDATES_PER_GROUP}`),
      // 覆盖面与涨跌周期无关，跟着一起拉；服务端按数据版本缓存，热态约 10ms
      api('/api/coverage'),
    ]);
    const groups = (meta && meta.groups && meta.groups.length) ? meta.groups : FALLBACK_GROUPS;
    const picks = { core: core.items, other: other.items };

    renderCoverage(coverage, { force: true });
    renderKpi(overview);
    const range = PERIOD_LABEL[period];
    renderRankList(overview.top_gainers, 'rankUp', `近 ${range}没有上涨的型号`);
    renderRankList(overview.top_losers, 'rankDown', `近 ${range}没有下跌的型号`);
    renderIndexChart(marketIndex);
    renderCategoryChart(overview, groups);
    renderCardSections(groups, picks);
    document.getElementById('pageDesc').textContent =
      `口径：${BASIS_LABEL[basis]} · 区间：近 ${PERIOD_LABEL[period]} · 数据截至 ${overview.newest_date || '—'}`;
  } catch (err) {
    document.getElementById('pageDesc').textContent = `加载失败：${err.message}`;
    toast(`加载失败：${err.message}`, true);
  }
}

/**
 * 「真实采集覆盖」板块。
 *
 * 口径要和后端 services/coverage.py 保持一致：**只算真实采集**（不含模拟数据），
 * 采集范围取自 DIYPRICE_FOCUS_CATEGORY（默认显卡 + CPU）。
 * 页面上必须把这个口径写出来 —— 否则"追踪 309 个但只完成 84 个"会被误读成漏采。
 *
 * 这个板块会**自动刷新**（见 startCoverageAutoRefresh），所以渲染要幂等：
 * 数据没变就一个字节都不动 DOM，否则每 60 秒闪烁一次，很烦。
 */
let lastCoverageSig = '';

/** 用数据内容生成指纹，用来判断"要不要重绘"。 */
function coverageSignature(cov) {
  const sc = cov.scope || {};
  const range = cov.date_range || {};
  return [
    sc.covered, sc.total, sc.completion,
    range.latest_at,
    ...(cov.platforms || []).map((p) => `${p.code}:${p.models}:${p.listings}:${p.last_at}`),
  ].join('|');
}

function renderCoverage(cov, opts) {
  const force = !!(opts && opts.force);
  const sig = coverageSignature(cov);
  const sc = cov.scope || {};
  const lb = cov.library || {};
  const plats = cov.platforms || [];

  // 这个时间是**页面刷新时间**，用来证明板块活着（和下方的"最近入库"不同）。
  // 即使数据没变也要更新它 —— 否则数字一直不动，用户没法判断是不是卡死了。
  const hintEl = document.getElementById('covHint');
  const hint = `${int(sc.covered)}/${int(sc.total)} 个型号已采到真实价格 · 刷新于 ${clock()}`;

  if (!force && sig === lastCoverageSig) {
    hintEl.textContent = hint;   // 数据没变 → 只动这一行，不重绘表格（避免闪烁）
    return;
  }
  lastCoverageSig = sig;

  const host = document.getElementById('covBody');
  const pct100 = Math.max(0, Math.min(100, sc.completion || 0));

  const metrics = `
    <div class="cov-metrics">
      <div>
        <div class="cov-metric-label">采集范围型号</div>
        <div class="cov-metric-value">${int(sc.total)}</div>
        <div class="cov-metric-foot">${esc((sc.labels || []).join(' + '))}</div>
      </div>
      <div>
        <div class="cov-metric-label">已采到价格</div>
        <!-- 不要用 .up（红色）—— 本项目的红色约定是"价格上涨"，用在这里会被误读 -->
        <div class="cov-metric-value">${int(sc.covered)}</div>
        <div class="cov-metric-foot">${sc.missing > 0 ? `还有 ${int(sc.missing)} 个未轮到` : '已全部覆盖'}</div>
      </div>
      <div>
        <div class="cov-metric-label">完成率</div>
        <div class="cov-metric-value">${sc.completion}%</div>
        <div class="cov-metric-foot">${int(sc.covered)} / ${int(sc.total)}</div>
      </div>
    </div>
    <div class="cov-bar"><i style="width:${pct100}%"></i></div>`;

  const rows = plats.length
    ? plats.map((p) => {
        const c = Math.max(0, Math.min(100, p.completion || 0));
        return `
          <tr>
            <td>
              <span class="cov-plat">
                <i class="cov-dot" style="background:${p.kind === 'used' ? COLORS.down : COLORS.accent}"></i>
                ${esc(p.name)}
                <span class="cov-kind">${p.kind === 'used' ? '二手' : '全新'}</span>
              </span>
            </td>
            <td class="num">${int(p.models)}</td>
            <td class="num">
              <span class="cov-rate">
                <span class="cov-rate-bar"><i style="width:${c}%"></i></span>
                <span class="cov-rate-num">${p.completion}%</span>
              </span>
            </td>
            <td class="num">${int(p.listings)}</td>
            <td class="num">${int(p.days)}</td>
            <td class="muted">${esc(p.last_at || '—')}</td>
          </tr>`;
      }).join('')
    : '<tr><td colspan="6" class="muted">暂无真实采集数据</td></tr>';

  const table = `
    <table class="data">
      <thead>
        <tr>
          <th>平台</th>
          <th class="num">覆盖型号</th>
          <th class="num">覆盖率</th>
          <th class="num">报价条数</th>
          <th class="num">数据天数</th>
          <th>最近入库</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>`;

  const range = cov.date_range || {};
  const note = `
    <div class="cov-note">
      统计口径：<b>只计真实采集</b>（不含模拟数据），采集范围由
      <b>DIYPRICE_FOCUS_CATEGORY</b> 决定，当前为
      <b>${esc((sc.labels || []).join(' + '))}</b>。<br>
      网站共追踪 <b>${int(lb.total)}</b> 个型号，其中 <b>${int(lb.covered)}</b> 个有真实价格
      （${lb.completion}%）—— 其余品类尚未纳入采集范围，页面上的历史曲线来自模拟数据。<br>
      数据区间 <b>${esc(range.from || '—')} ~ ${esc(range.to || '—')}</b>，
      共 <b>${plats.length ? Math.max(...plats.map((p) => p.days)) : 0}</b> 天；
      最近一次入库 <b>${esc(range.latest_at || '—')}</b>。
      ${cov.single_platform_models ? `有 <b>${int(cov.single_platform_models)}</b> 个型号只在一个平台有价，暂时无法横向比价。` : ''}
    </div>`;

  host.innerHTML = metrics + table + note;
  hintEl.textContent = hint;
}

/** HH:MM:SS —— 标在板块标题旁，让人一眼看出数据是不是新的。 */
function clock() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// 覆盖数据每轮采集后才变（约每小时），60 秒轮询足够"实时"，
// 服务端按数据版本缓存，没变时返回成本约 10ms。
const COVERAGE_REFRESH_MS = 60000;
let coverageTimer = null;

function startCoverageAutoRefresh() {
  if (coverageTimer) clearInterval(coverageTimer);
  coverageTimer = setInterval(async () => {
    if (document.hidden) return;   // 后台标签页不轮询，省资源
    try {
      renderCoverage(await api('/api/coverage'));
    } catch (_) {
      // 静默失败：下一轮会自动重试，不弹 toast 打扰
    }
  }, COVERAGE_REFRESH_MS);
}

function renderKpi(ov) {
  document.getElementById('kpiTracked').textContent = int(ov.tracked);
  document.getElementById('kpiTrackedFoot').textContent = `${ov.categories.length} 个品类 · ${BASIS_LABEL[ov.basis]}`;
  document.getElementById('kpiUp').textContent = int(ov.up_count);
  document.getElementById('kpiUpFoot').textContent = `较 ${PERIOD_LABEL[ov.period]} 前上涨`;
  document.getElementById('kpiDown').textContent = int(ov.down_count);
  document.getElementById('kpiDownFoot').textContent = `较 ${PERIOD_LABEL[ov.period]} 前下跌`;
  document.getElementById('kpiDate').textContent = ov.newest_date || '—';

  // 把"可比数"标出来：真实数据才积累了两天，多数型号还没有 N 天前的基准价，
  // 不写清楚会让"追踪 309 但只有 45 个上涨"看起来像漏了数据。
  const comparable = (ov.up_count || 0) + (ov.down_count || 0) + (ov.flat_count || 0);
  const hint =
    `较 ${PERIOD_LABEL[ov.period]} 前 · 可比 ${comparable}/${ov.tracked}` +
    (comparable < ov.tracked ? `（另 ${ov.tracked - comparable} 个历史不足）` : '');
  document.getElementById('upHint').textContent = hint;
  document.getElementById('downHint').textContent = hint;
}

function renderRankList(items, hostId, emptyText) {
  const host = document.getElementById(hostId);
  if (!items || !items.length) {
    // 说清"是没有，还是没数据"——只写"暂无数据"会让人以为页面坏了
    host.innerHTML = `<div class="loading">${esc(emptyText || '暂无数据')}</div>`;
    return;
  }
  host.innerHTML = items.map((it, i) => `
    <div class="rank-row" onclick="location.href='/product?id=${it.product_id}'">
      <div class="rank-idx">${i + 1}</div>
      <div class="rank-name">
        <b>${esc(it.model)}</b>
        <span>${esc(it.brand)} · ${esc(it.category_label)}</span>
      </div>
      <div class="rank-price">
        <b>${money(it.latest)}</b>
        <span class="${trendCls(it.change_pct)}">${pct(it.change_pct)}</span>
      </div>
    </div>`).join('');
}

/**
 * 组内按品类轮流取。
 *
 * 直接按价格截前 N 个会有个问题：显卡和主板价格最高，会把整块占满 ——
 * "其他硬件"那块变成清一色主板，看不出这是"其他硬件"的概览。
 * 轮流取保证每个品类都有代表，多出来的名额再按价格补。
 * 入参已按价格降序，因此每轮取到的都是该品类里最贵的那款。
 */
function diversify(items, max) {
  const buckets = new Map();
  for (const it of items) {
    if (!buckets.has(it.category)) buckets.set(it.category, []);
    buckets.get(it.category).push(it);
  }
  const out = [];
  for (let round = 0; out.length < max; round += 1) {
    let added = false;
    for (const list of buckets.values()) {
      if (round < list.length) {
        out.push(list[round]);
        added = true;
        if (out.length >= max) break;
      }
    }
    if (!added) break;   // 所有品类都取空了
  }
  return out;
}

/**
 * 按板块渲染卡片 —— 「核心配件」与「其他硬件」各自成块，不混排。
 * picks 形如 { core: [...], other: [...] }
 */
function renderCardSections(groups, picks) {
  const host = document.getElementById('cardSections');
  const blocks = groups.map((g) => {
    const items = diversify(picks[g.code] || [], CARDS_PER_GROUP);
    if (!items.length) return '';
    const kinds = new Set(items.map((it) => it.category_label)).size;
    return `
      <div class="card-section">
        <div class="card-section-head">
          <span class="card-section-title">${esc(g.label)}</span>
          <span class="card-section-hint">${esc(g.hint || '')}</span>
          <span class="muted">共 ${kinds} 个品类 · ${items.length} 款</span>
        </div>
        <div class="grid grid-cards">${items.map(cardHtml).join('')}</div>
      </div>`;
  }).filter(Boolean).join('');

  host.innerHTML = blocks || '<div class="loading">暂无数据</div>';
}

function cardHtml(it) {
  return `
    <a class="pcard" href="/product?id=${it.product_id}">
      <div class="pcard-top">
        <div>
          <div class="pcard-model">${esc(it.model)}${freshBadge(it)}</div>
          <div class="pcard-spec">${esc(it.brand)} · ${esc(it.category_label)}</div>
        </div>
        ${trendPill(it.change_pct)}
      </div>
      <div class="pcard-row">
        <div>
          <div class="pcard-price">${money(it.latest)}</div>
          <div class="pcard-sub">全新 ${money(it.latest_new_low)} · 二手 ${money(it.latest_used_low)}</div>
        </div>
        ${sparkline(it.sparkline, { width: 92, height: 30 })}
      </div>
    </a>`;
}

function renderIndexChart(payload) {
  const dom = document.getElementById('chartIndex');
  if (indexChart) disposeChart(indexChart);
  if (!payload.series || !payload.series.length) {
    dom.innerHTML = '<div class="empty-chart">暂无数据</div>';
    return;
  }
  dom.innerHTML = '';
  indexChart = initChart(dom);

  const option = baseChartOption();
  option.grid = { left: 4, right: 12, top: 30, bottom: 4, containLabel: true };
  option.tooltip.valueFormatter = (v) => (v === null ? '—' : Number(v).toFixed(2));
  option.xAxis = {
    type: 'time',
    axisLine: { lineStyle: { color: COLORS.border } },
    axisTick: { show: false },
    axisLabel: { color: COLORS.text3, fontSize: 11, hideOverlap: true },
    splitLine: { show: false },
  };
  option.yAxis = {
    type: 'value',
    scale: true,
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: COLORS.text3, fontSize: 11, formatter: (v) => Number(v).toFixed(0) },
    splitLine: { lineStyle: { color: COLORS.grid } },
  };
  option.series = payload.series.map((s, i) => ({
    name: s.label,
    type: 'line',
    showSymbol: false,
    smooth: true,
    data: s.data,
    // 一屏十几条线时 1.8px 会糊成一片，1.5px + 轻微透明更透气；
    // 鼠标悬停时 ECharts 会把其余序列压暗（emphasis.focus），所以细一点也不影响读数
    lineStyle: { width: 1.5, opacity: .92 },
    itemStyle: { color: SERIES_COLORS[i % SERIES_COLORS.length] },
    emphasis: { focus: 'series' },
  }));
  indexChart.setOption(option);
}

function renderCategoryChart(ov, groups) {
  const dom = document.getElementById('chartCategory');
  if (categoryChart) disposeChart(categoryChart);
  if (!ov.categories || !ov.categories.length) {
    dom.innerHTML = '<div class="empty-chart">暂无数据</div>';
    return;
  }
  dom.innerHTML = '';
  categoryChart = initChart(dom);

  // 后端已按 CATEGORY_ORDER（核心配件在前）返回，reverse 后核心三项落在图表顶部，
  // 天然与「其他硬件」分开；再用 markArea 给核心板块铺一层淡底，视觉上明确成组。
  const cats = [...ov.categories].reverse();
  const labels = cats.map((c) => c.label);
  const coreCode = (groups && groups[0] && groups[0].code) || 'core';
  const coreCats = cats.filter((c) => c.group === coreCode);
  const coreLabels = coreCats.map((c) => c.label);

  const option = baseChartOption();
  option.grid = { left: 4, right: 20, top: 30, bottom: 4, containLabel: true };
  option.xAxis = {
    type: 'value',
    minInterval: 1,
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { color: COLORS.text3, fontSize: 11 },
    splitLine: { lineStyle: { color: COLORS.grid } },
  };
  option.yAxis = {
    type: 'category',
    data: labels,
    axisLine: { lineStyle: { color: COLORS.border } },
    axisTick: { show: false },
    axisLabel: { color: COLORS.text2, fontSize: 12 },
  };
  option.series = [
    {
      name: '上涨', type: 'bar', stack: 'total', barWidth: 13,
      data: cats.map((c) => c.up), itemStyle: { color: COLORS.up, borderRadius: [3, 0, 0, 3] },
      // 给「核心配件」那几条铺一层淡底，让分组一眼可见
      markArea: coreLabels.length
        ? {
            silent: true,
            itemStyle: { color: 'rgba(47,111,237,.055)' },
            data: [[{ yAxis: coreLabels[0], x: '0%' }, { yAxis: coreLabels[coreLabels.length - 1], x: '100%' }]],
          }
        : undefined,
    },
    {
      name: '下跌', type: 'bar', stack: 'total', barWidth: 13,
      data: cats.map((c) => c.down), itemStyle: { color: COLORS.down, borderRadius: [0, 3, 3, 0] },
    },
  ];
  categoryChart.setOption(option);
}

document.addEventListener('DOMContentLoaded', () => {
  const sel = document.getElementById('periodSelect');
  sel.value = String(period);
  sel.addEventListener('change', () => {
    period = Number(sel.value);
    loadAll();
  });

  // 切回前台时立刻补一次（后台期间没轮询），不用等下一个 60 秒
  document.addEventListener('visibilitychange', async () => {
    if (document.hidden) return;
    try {
      renderCoverage(await api('/api/coverage'));
    } catch (_) { /* 忽略 */ }
  });

  loadAll();
  startCoverageAutoRefresh();
});
