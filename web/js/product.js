/* 型号详情 */

let trendChart = null;
let platformChart = null;
let historyChart = null;
let payload = null;
let days = 90;

const PLATFORM_COLORS = {
  all_low: '#2f6fed',
  new_low: '#d93025',
  used_low: '#0f9d58',
  ma7: '#a4700d',
  ma30: '#8a9099',
};

function movingAverage(values, window) {
  const out = [];
  for (let i = 0; i < values.length; i += 1) {
    if (i < window - 1) { out.push(null); continue; }
    const slice = values.slice(i - window + 1, i + 1).filter((v) => v !== null);
    out.push(slice.length ? Number((slice.reduce((a, b) => a + b, 0) / slice.length).toFixed(2)) : null);
  }
  return out;
}

async function load() {
  const id = query('id');
  if (!id) {
    document.getElementById('title').textContent = '缺少型号 ID';
    return;
  }
  try {
    payload = await api(`/api/products/${id}/trend?days=${days}`);
    renderHead();
    renderMetrics();
    renderTrendChart();
    renderPlatformChart();
    renderPlatformTable();
    renderHistoryChart();
  } catch (err) {
    document.getElementById('title').textContent = `加载失败：${err.message}`;
    toast(`加载失败：${err.message}`, true);
  }
}

function renderHead() {
  const p = payload.product;
  const m = payload.metrics;
  document.getElementById('title').textContent = p.model;
  document.getElementById('subtitle').textContent =
    `${p.brand} · ${p.category_label} · ${p.spec} · 基准参考价 ${money(p.base_price)}`;
  document.title = `${p.model} · DIY 配件价格追踪`;

  const c1 = m.changes?.d1;
  document.getElementById('priceMain').innerHTML =
    `${money(m.last_low)} <span class="pill ${trendCls(c1 ? c1.pct : null)}" style="font-size:13px;vertical-align:3px">${c1 ? pct(c1.pct) : '—'}</span>`;
  document.getElementById('priceMeta').textContent =
    `全市场最低价 · ${m.last_date || '—'} · 较前一日 ${c1 ? money(c1.abs, 2) : '—'}`;
  document.getElementById('priceNew').textContent = money(m.last_new_low);
}

function metricCell(label, value, note, cls = '') {
  return `<div class="metric">
    <div class="metric-label">${esc(label)}</div>
    <div class="metric-value ${cls}">${value}</div>
    <div class="metric-note">${note}</div>
  </div>`;
}

function renderMetrics() {
  const m = payload.metrics;
  const c7 = m.changes?.d7;
  const c30 = m.changes?.d30;
  const pc = m.percentile_90d;
  const pcCls = pc === null || pc === undefined ? '' : pc <= 30 ? 'down' : pc >= 70 ? 'up' : '';
  const newSpread = payload.spread?.new;

  document.getElementById('signalText').textContent = m.signal ? m.signal.text : '—';

  document.getElementById('metrics').innerHTML = [
    metricCell('全新平台最低价', money(m.last_new_low), `上一日 ${money(m.last_avg)} 均价`),
    metricCell('二手平台最低价', money(m.last_used_low), '含 99新 ~ 8成新'),
    metricCell('7 日涨跌', c7 ? pct(c7.pct) : '—', c7 ? money(c7.abs, 2) : '数据不足', trendCls(c7 ? c7.pct : null)),
    metricCell('30 日涨跌', c30 ? pct(c30.pct) : '—', c30 ? money(c30.abs, 2) : '数据不足', trendCls(c30 ? c30.pct : null)),
    metricCell('MA7', money(m.ma7), '7 日均线'),
    metricCell('MA30', money(m.ma30), '30 日均线'),
    metricCell('30 日波动率', m.volatility_30d === null ? '—' : `${num(m.volatility_30d)}%`, '日收益率标准差'),
    metricCell('近 90 天分位', pc === null ? '—' : `${num(pc, 0)}%`, '越高越贵', pcCls),
    metricCell('历史最低', money(m.history.min), m.history.min_date || '—', 'down'),
    metricCell('历史最高', money(m.history.max), m.history.max_date || '—', 'up'),
    metricCell('区间振幅', m.history.span_pct === null ? '—' : `${num(m.history.span_pct, 1)}%`, '最高 / 最低'),
    metricCell('全新平台价差', newSpread ? `${num(newSpread.pct, 2)}%` : '—',
      newSpread ? `${newSpread.cheapest.name} ↔ ${newSpread.priciest.name}` : '平台不足'),
  ].join('');
}

function renderTrendChart() {
  const dom = document.getElementById('chartTrend');
  if (trendChart) disposeChart(trendChart);
  const s = payload.series;
  if (!s.dates.length) {
    dom.innerHTML = '<div class="empty-chart">暂无数据</div>';
    return;
  }
  dom.innerHTML = '';
  trendChart = initChart(dom);

  const seriesData = s.dates.map((d, i) => ({
    date: d,
    all: s.all_low[i],
    newLow: s.new_low[i],
    usedLow: s.used_low[i],
  }));
  const values = (key) => seriesData.map((r) => (r[key] === null ? null : [r.date, r[key]]));

  const option = baseChartOption();
  option.grid = { left: 4, right: 16, top: 34, bottom: 4, containLabel: true };
  option.tooltip.valueFormatter = (v) => (v === null ? '—' : money(v, 2));
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
    axisLabel: { color: COLORS.text3, fontSize: 11, formatter: (v) => `¥${Math.round(v)}` },
    splitLine: { lineStyle: { color: COLORS.grid } },
  };

  const allLow = seriesData.map((r) => r.all);
  const ma7 = movingAverage(allLow, 7);
  const ma30 = movingAverage(allLow, 30);

  option.series = [
    {
      name: '全市场最低价', type: 'line', showSymbol: false, smooth: true,
      data: values('all'), lineStyle: { width: 2, color: PLATFORM_COLORS.all_low },
      itemStyle: { color: PLATFORM_COLORS.all_low }, z: 5,
    },
    {
      name: '全新最低价', type: 'line', showSymbol: false, smooth: true,
      data: values('newLow'), lineStyle: { width: 1.4, color: PLATFORM_COLORS.new_low },
      itemStyle: { color: PLATFORM_COLORS.new_low },
    },
    {
      name: '二手最低价', type: 'line', showSymbol: false, smooth: true,
      data: values('usedLow'), lineStyle: { width: 1.4, color: PLATFORM_COLORS.used_low },
      itemStyle: { color: PLATFORM_COLORS.used_low },
    },
    {
      name: 'MA7', type: 'line', showSymbol: false, smooth: true,
      data: s.dates.map((d, i) => (ma7[i] === null ? null : [d, ma7[i]])),
      lineStyle: { width: 1, type: 'dashed', color: PLATFORM_COLORS.ma7 },
      itemStyle: { color: PLATFORM_COLORS.ma7 },
    },
    {
      name: 'MA30', type: 'line', showSymbol: false, smooth: true,
      data: s.dates.map((d, i) => (ma30[i] === null ? null : [d, ma30[i]])),
      lineStyle: { width: 1, type: 'dashed', color: PLATFORM_COLORS.ma30 },
      itemStyle: { color: PLATFORM_COLORS.ma30 },
    },
  ];
  trendChart.setOption(option);
}

function renderPlatformChart() {
  const dom = document.getElementById('chartPlatform');
  if (platformChart) disposeChart(platformChart);
  const rows = payload.latest_platforms || [];
  if (!rows.length) {
    dom.innerHTML = '<div class="empty-chart">暂无数据</div>';
    return;
  }
  dom.innerHTML = '';
  platformChart = initChart(dom);

  const sorted = [...rows].sort((a, b) => a.min - b.min);
  const labels = sorted.map((r) => `${r.name}${r.kind === 'used' ? ' (二手)' : ''}`);
  const mins = sorted.map((r) => r.min);
  const spans = sorted.map((r) => Math.max(r.max - r.min, 0));
  const floor = Math.max(0, Math.min(...mins) * 0.9);

  const option = baseChartOption();
  option.grid = { left: 4, right: 60, top: 12, bottom: 4, containLabel: true };
  option.legend.show = false;
  option.tooltip.trigger = 'axis';
  option.tooltip.axisPointer = { type: 'shadow' };
  option.xAxis = {
    type: 'value', min: floor,
    axisLine: { show: false }, axisTick: { show: false },
    axisLabel: { color: COLORS.text3, fontSize: 11, formatter: (v) => `¥${Math.round(v)}` },
    splitLine: { lineStyle: { color: COLORS.grid } },
  };
  option.yAxis = {
    type: 'category', data: labels, inverse: true,
    axisLine: { lineStyle: { color: COLORS.border } }, axisTick: { show: false },
    axisLabel: { color: COLORS.text2, fontSize: 12 },
  };
  option.series = [
    {
      name: '低价基准', type: 'bar', stack: 'range', barWidth: 15,
      data: mins.map((v) => v - floor), itemStyle: { color: 'transparent' }, silent: true,
    },
    {
      name: '价格区间', type: 'bar', stack: 'range', barWidth: 15,
      data: spans.map((v) => Math.max(v, 0)),
      itemStyle: { color: '#9dc0f5', borderRadius: [0, 3, 3, 0] },
    },
    {
      name: '当日最低价', type: 'bar', stack: 'range2', barWidth: 15,
      data: mins.map((v) => v - floor),
      itemStyle: {
        color: (params) => sorted[params.dataIndex].color,
        borderRadius: [3, 0, 0, 3],
      },
      label: {
        show: true, position: 'right', color: COLORS.text2, fontSize: 11,
        formatter: (params) => money(sorted[params.dataIndex].min),
      },
    },
  ];
  platformChart.setOption(option);
}

function renderPlatformTable() {
  const rows = payload.latest_platforms || [];
  const tbody = document.getElementById('platformTbody');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="6" class="loading">暂无数据</td></tr>';
    return;
  }
  tbody.innerHTML = rows.map((r) => `
    <tr>
      <td><b style="font-weight:500">${esc(r.name)}</b></td>
      <td><span class="pill ${r.kind === 'used' ? 'used' : 'new'}">${r.kind === 'used' ? '二手' : '全新'}</span></td>
      <td class="num"><b>${money(r.min)}</b></td>
      <td class="num muted">${money(r.avg)}</td>
      <td class="num muted">${money(r.max)}</td>
      <td class="num ${r.is_cheapest ? 'down' : ''}">${r.is_cheapest ? '最低' : pct(r.vs_cheapest_pct)}</td>
    </tr>`).join('');

  const ns = payload.spread?.new;
  const us = payload.spread?.used;
  const parts = [];
  if (ns) parts.push(`全新价差 ${num(ns.pct, 2)}%`);
  if (us) parts.push(`二手价差 ${num(us.pct, 2)}%`);
  document.getElementById('spreadText').textContent = parts.join(' · ') || '—';
}

function renderHistoryChart() {
  const dom = document.getElementById('chartHistory');
  if (historyChart) disposeChart(historyChart);
  const h = payload.metrics.history;
  if (!h || h.min === null) {
    dom.innerHTML = '<div class="empty-chart">暂无数据</div>';
    return;
  }
  dom.innerHTML = '';
  historyChart = initChart(dom);

  const last = payload.metrics.last_low;
  const ma30 = payload.metrics.ma30;
  document.getElementById('historyText').textContent =
    `区间 ${money(h.min)} ~ ${money(h.max)} · 当前位于 ${num(payload.metrics.percentile_90d, 0)}% 分位`;

  const floor = Math.max(0, h.min * 0.97);
  const option = baseChartOption();
  option.grid = { left: 4, right: 90, top: 16, bottom: 4, containLabel: true };
  option.legend.show = false;
  option.tooltip.show = false;
  option.xAxis = {
    type: 'value', min: floor, max: h.max * 1.02,
    axisLine: { show: false }, axisTick: { show: false },
    axisLabel: { color: COLORS.text3, fontSize: 11, formatter: (v) => `¥${Math.round(v)}` },
    splitLine: { lineStyle: { color: COLORS.grid } },
  };
  option.yAxis = {
    type: 'category', data: ['历史价格区间'],
    axisLine: { show: false }, axisTick: { show: false },
    axisLabel: { color: COLORS.text3, fontSize: 11 },
  };
  option.series = [
    {
      type: 'bar', stack: 'h', barWidth: 20, silent: true,
      data: [h.min - floor], itemStyle: { color: 'transparent' },
    },
    {
      name: '历史区间', type: 'bar', stack: 'h', barWidth: 20, silent: true,
      data: [h.max - h.min],
      itemStyle: { color: '#e3ecfb', borderRadius: 4 },
      markLine: {
        silent: true, symbol: 'none',
        label: { color: COLORS.text2, fontSize: 11, position: 'end', formatter: (p) => p.name },
        lineStyle: { width: 1.2 },
        data: [
          { name: `当前 ${money(last)}`, xAxis: last, lineStyle: { color: COLORS.accent, width: 2 } },
          { name: `MA30 ${money(ma30)}`, xAxis: ma30, lineStyle: { color: PLATFORM_COLORS.ma7, type: 'dashed' } },
        ],
      },
    },
  ];
  historyChart.setOption(option);
}

document.addEventListener('DOMContentLoaded', () => {
  const sel = document.getElementById('daysSelect');
  sel.value = String(days);
  sel.addEventListener('change', () => {
    days = Number(sel.value);
    load();
  });
  document.getElementById('btnCompare').addEventListener('click', () => {
    if (!payload) return;
    const ids = new Set((query('ids') || '').split(',').filter(Boolean));
    ids.add(String(payload.product.id));
    location.href = `/compare?ids=${[...ids].slice(0, 6).join(',')}`;
  });
  load();
});
