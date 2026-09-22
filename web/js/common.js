/* 公共工具：请求封装、格式化、迷你走势图、侧边栏、提示 */

// 图表配色：**必须与 css/app.css 的 :root 变量保持一致**。
// 两处不同步的话，卡片是蓝白、图表是另一套色，整体会显得"拼"。
// 改动任一侧时记得同步另一侧。
const COLORS = {
  up: '#e0393e',
  down: '#0d9e6a',
  flat: '#8c99b0',
  accent: '#2563eb',
  text: '#0f1b2d',
  text2: '#56658a',
  text3: '#93a1bb',
  border: '#e6ecf6',
  panel: '#ffffff',
  grid: '#eef3fb',
};

// 多序列折线用。以蓝为主、整体降饱和，避免一屏十几条高饱和线打架。
// 顺序有讲究：前三位是"主蓝 / 涨红 / 跌绿"，最常出现的序列优先拿到主色。
const SERIES_COLORS = [
  '#2563eb', '#e0393e', '#0d9e6a', '#7c6cf0', '#0e9bb5', '#e08a1e',
  '#5b7bb5', '#c0559b', '#3d9a8b', '#8a7a5c', '#6b7fa8', '#a05a4a',
];

const NAV = [
  { href: '/', label: '总览看板', match: ['/'] },
  { href: '/report', label: '价格日报', match: ['/report'] },
  { href: '/products', label: '型号列表', match: ['/products', '/product'] },
  { href: '/compare', label: '多型号对比', match: ['/compare'] },
  { href: '/admin', label: '采集管理', match: ['/admin'] },
];

/* ---------------------------------------------------------------- 请求 */

async function api(path, options = {}) {
  const opts = { headers: { 'Content-Type': 'application/json' }, ...options };
  if (opts.body && typeof opts.body !== 'string') opts.body = JSON.stringify(opts.body);
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      detail = data.detail || detail;
    } catch (e) { /* 忽略非 JSON 响应 */ }
    throw new Error(detail);
  }
  return res.json();
}

/* ---------------------------------------------------------------- 格式化 */

function money(value, digits = 0) {
  if (value === null || value === undefined) return '—';
  return '¥' + Number(value).toLocaleString('zh-CN', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function pct(value, digits = 2) {
  if (value === null || value === undefined) return '—';
  const v = Number(value);
  return (v > 0 ? '+' : '') + v.toFixed(digits) + '%';
}

function num(value, digits = 2) {
  if (value === null || value === undefined) return '—';
  return Number(value).toFixed(digits);
}

function int(value) {
  if (value === null || value === undefined) return '—';
  return Number(value).toLocaleString('zh-CN');
}

function trendCls(value) {
  if (value === null || value === undefined) return 'flat';
  return value > 0 ? 'up' : value < 0 ? 'down' : 'flat';
}

function trendPill(value, digits = 2) {
  return `<span class="pill ${trendCls(value)}">${pct(value, digits)}</span>`;
}

function esc(text) {
  return String(text ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function query(name, fallback = null) {
  const v = new URLSearchParams(location.search).get(name);
  return v === null ? fallback : v;
}

/* ---------------------------------------------------------------- 迷你走势 */

function sparkline(values, opts = {}) {
  const width = opts.width || 104;
  const height = opts.height || 28;
  const vals = (values || []).filter((v) => v !== null && v !== undefined);
  if (vals.length < 2) return '<span class="muted">—</span>';

  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const span = max - min || 1;
  const stepX = width / (vals.length - 1);
  const pad = 3;
  const points = vals.map((v, i) => [
    i * stepX,
    height - pad - ((v - min) / span) * (height - pad * 2),
  ]);
  const d = points.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ');
  const first = vals[0];
  const last = vals[vals.length - 1];
  const color = last > first ? COLORS.up : last < first ? COLORS.down : COLORS.flat;
  const tip = `${money(vals[0])} → ${money(last)}`;
  const head = points[points.length - 1];
  return `<svg class="spark" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img">
    <title>${esc(tip)}</title>
    <path d="${d}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${head[0].toFixed(1)}" cy="${head[1].toFixed(1)}" r="2.4" fill="${color}"/>
  </svg>`;
}

/* ---------------------------------------------------------------- 侧边栏 */

function renderSidebar() {
  const host = document.getElementById('sidebar');
  if (!host) return;
  const path = location.pathname;
  const items = NAV.map((item) => {
    const active = item.match.some((m) => (m === '/' ? path === '/' : path.startsWith(m)));
    return `<a class="nav-item${active ? ' active' : ''}" href="${item.href}">
      <span class="nav-dot"></span>${item.label}</a>`;
  }).join('');
  host.innerHTML = `
    <div class="brand">
      <div class="brand-title">DIY 配件价格追踪</div>
      <div class="brand-sub">显卡 · CPU · 内存 · 全品类</div>
    </div>
    <nav class="nav">${items}</nav>
    <div class="sidebar-foot">
      <b>数据口径</b>
      全新 / 二手分列<br>
      价格每日 02:00 更新
    </div>`;
}

/* ---------------------------------------------------------------- 提示 */

let toastTimer = null;
function toast(message, isError = false) {
  let node = document.getElementById('toast');
  if (!node) {
    node = document.createElement('div');
    node.id = 'toast';
    node.className = 'toast';
    document.body.appendChild(node);
  }
  node.textContent = message;
  node.className = 'toast show' + (isError ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { node.className = 'toast'; }, 2600);
}

/* ---------------------------------------------------------------- ECharts */

function baseChartOption() {
  return {
    textStyle: {
      fontFamily: '-apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif',
      fontSize: 12,
      color: COLORS.text2,
    },
    grid: { left: 8, right: 16, top: 34, bottom: 8, containLabel: true },
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'rgba(22,25,29,0.94)',
      borderWidth: 0,
      padding: [8, 12],
      textStyle: { color: '#fff', fontSize: 12 },
      axisPointer: { type: 'line', lineStyle: { color: '#c9ced6', type: 'dashed' } },
    },
    legend: {
      top: 0, left: 0, itemGap: 14, itemWidth: 14, itemHeight: 3,
      textStyle: { color: COLORS.text2, fontSize: 12 },
    },
  };
}

/**
 * 全站共用一个 resize 监听。
 *
 * 原先每次 initChart 都往 window 上挂一个新的 resize 回调，而图表 dispose 后
 * 回调并不会被摘掉 —— 反复切换周期/口径（每次都重建图表）会不断累积监听器，
 * 缩放窗口时一堆已销毁的实例被 resize，白白掉帧。
 */
const _liveCharts = new Set();
let _resizeTimer = null;

window.addEventListener('resize', () => {
  if (_resizeTimer) clearTimeout(_resizeTimer);
  _resizeTimer = setTimeout(() => {
    for (const chart of _liveCharts) {
      if (chart.isDisposed && chart.isDisposed()) _liveCharts.delete(chart);
      else chart.resize();
    }
  }, 120);
});

function initChart(dom) {
  const chart = echarts.init(dom, null, { renderer: 'canvas' });
  _liveCharts.add(chart);
  return chart;
}

function disposeChart(chart) {
  if (!chart) return;
  _liveCharts.delete(chart);
  chart.dispose();
}

document.addEventListener('DOMContentLoaded', () => {
  renderSidebar();
});
