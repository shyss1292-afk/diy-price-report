/* 多型号对比 */

let allProducts = [];
let selected = [];
let compareChart = null;
let days = 90;
let mode = 'index';

async function init() {
  try {
    const data = await api('/api/products?days=180');
    allProducts = data.items;
  } catch (err) {
    document.getElementById('pageDesc').textContent = `加载失败：${err.message}`;
    return;
  }

  const urlIds = (query('ids') || '').split(',').map((x) => Number(x.trim())).filter(Boolean);
  selected = urlIds.filter((id) => allProducts.some((p) => p.product_id === id)).slice(0, 6);
  if (!selected.length) {
    selected = allProducts
      .filter((p) => p.category === 'gpu')
      .sort((a, b) => (b.latest || 0) - (a.latest || 0))
      .slice(0, 3)
      .map((p) => p.product_id);
  }

  setupSearch();
  renderChips();
  await loadSeries();
}

function setupSearch() {
  const input = document.getElementById('addInput');
  const box = document.getElementById('suggest');

  const close = () => { box.style.display = 'none'; };

  input.addEventListener('input', () => {
    const q = input.value.trim().toLowerCase();
    if (!q) { close(); return; }
    const hits = allProducts
      .filter((p) => !selected.includes(p.product_id))
      .filter((p) => p.model.toLowerCase().includes(q) || p.brand.toLowerCase().includes(q))
      .slice(0, 30);
    if (!hits.length) { close(); return; }
    box.innerHTML = hits.map((p) => `
      <div class="rank-row" data-id="${p.product_id}">
        <div class="rank-name"><b>${esc(p.model)}</b><span>${esc(p.brand)} · ${esc(p.category_label)}</span></div>
        <div class="rank-price"><b>${money(p.latest)}</b></div>
      </div>`).join('');
    box.style.display = 'block';
  });

  box.addEventListener('click', (e) => {
    const row = e.target.closest('.rank-row');
    if (!row) return;
    addProduct(Number(row.dataset.id));
    input.value = '';
    close();
  });

  input.addEventListener('blur', () => setTimeout(close, 150));
}

function addProduct(id) {
  if (selected.includes(id)) return;
  if (selected.length >= 6) {
    toast('最多同时对比 6 个型号', true);
    return;
  }
  selected.push(id);
  renderChips();
  loadSeries();
}

function removeProduct(id) {
  selected = selected.filter((x) => x !== id);
  renderChips();
  loadSeries();
}

function renderChips() {
  const host = document.getElementById('selectedChips');
  if (!selected.length) {
    host.innerHTML = '<span class="muted">尚未选择型号，可搜索添加，最多 6 个</span>';
    return;
  }
  host.innerHTML = selected.map((id) => {
    const p = allProducts.find((x) => x.product_id === id);
    return `<span class="chip active" data-id="${id}" style="cursor:default">
      ${esc(p ? p.model : id)}
      <span style="opacity:.6;margin-left:6px;cursor:pointer" data-remove="${id}">×</span>
    </span>`;
  }).join('');
  host.querySelectorAll('[data-remove]').forEach((node) => {
    node.addEventListener('click', (e) => {
      e.stopPropagation();
      removeProduct(Number(node.dataset.remove));
    });
  });
}

async function loadSeries() {
  if (!selected.length) {
    if (compareChart) { disposeChart(compareChart); compareChart = null; }
    document.getElementById('chartCompare').innerHTML = '<div class="empty-chart">请先选择型号</div>';
    document.getElementById('tbody').innerHTML = '<tr><td colspan="7" class="loading">请先选择型号</td></tr>';
    document.getElementById('chartHint').textContent = '—';
    return;
  }
  try {
    const data = await api(`/api/compare?ids=${selected.join(',')}&days=${days}`);
    renderChart(data.series || []);
    renderTable(data.series || []);
  } catch (err) {
    toast(`加载失败：${err.message}`, true);
  }
}

function renderChart(series) {
  const dom = document.getElementById('chartCompare');
  if (compareChart) disposeChart(compareChart);
  if (!series.length) {
    dom.innerHTML = '<div class="empty-chart">暂无数据</div>';
    return;
  }
  dom.innerHTML = '';
  compareChart = initChart(dom);

  const prepared = series.map((s) => {
    const base = s.data.length ? s.data[0][1] : null;
    return {
      name: s.model,
      data: s.data.map(([d, v]) => [d, mode === 'index' && base ? Number((v / base * 100).toFixed(2)) : v]),
    };
  });

  document.getElementById('chartHint').textContent =
    mode === 'index' ? '以区间首日价格为 100 归一化，便于比较相对强弱' : '直接比较绝对价格';

  const option = baseChartOption();
  option.grid = { left: 4, right: 16, top: 34, bottom: 4, containLabel: true };
  option.tooltip.valueFormatter = (v) => (v === null ? '—' : mode === 'index' ? Number(v).toFixed(2) : money(v, 2));
  option.xAxis = {
    type: 'time',
    axisLine: { lineStyle: { color: COLORS.border } },
    axisTick: { show: false },
    axisLabel: { color: COLORS.text3, fontSize: 11, hideOverlap: true },
    splitLine: { show: false },
  };
  option.yAxis = {
    type: 'value', scale: true,
    axisLine: { show: false }, axisTick: { show: false },
    axisLabel: {
      color: COLORS.text3, fontSize: 11,
      formatter: (v) => (mode === 'index' ? Number(v).toFixed(0) : `¥${Math.round(v)}`),
    },
    splitLine: { lineStyle: { color: COLORS.grid } },
  };
  option.series = prepared.map((s, i) => ({
    name: s.name,
    type: 'line',
    showSymbol: false,
    smooth: true,
    data: s.data,
    lineStyle: { width: 1.8 },
    itemStyle: { color: SERIES_COLORS[i % SERIES_COLORS.length] },
    emphasis: { focus: 'series' },
  }));
  compareChart.setOption(option);
}

function renderTable(series) {
  const tbody = document.getElementById('tbody');
  if (!series.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="loading">暂无数据</td></tr>';
    return;
  }
  tbody.innerHTML = series.map((s, i) => {
    const snap = allProducts.find((p) => p.product_id === s.product_id) || {};
    const pts = s.data;
    const start = pts.length ? pts[0][1] : null;
    const end = pts.length ? pts[pts.length - 1][1] : null;
    const chg = start ? ((end - start) / start) * 100 : null;
    const pc = snap.percentile_90d;
    const pcCls = pc === null || pc === undefined ? 'muted' : pc <= 30 ? 'down' : pc >= 70 ? 'up' : '';
    const color = SERIES_COLORS[i % SERIES_COLORS.length];
    return `<tr class="clickable" onclick="location.href='/product?id=${s.product_id}'">
      <td class="model-cell">
        <b><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:${color};margin-right:7px"></span>${esc(s.model)}</b>
        <div class="muted" style="font-size:11px">${esc(s.brand)}</div>
      </td>
      <td><span class="pill tag">${esc(snap.category_label || '')}</span></td>
      <td class="num"><b>${money(end)}</b></td>
      <td class="num muted">${money(start)}</td>
      <td class="num">${trendPill(chg)}</td>
      <td class="num ${pcCls}">${pc === null || pc === undefined ? '—' : pc.toFixed(0) + '%'}</td>
      <td style="text-align:center">${sparkline(pts.map((p) => p[1]), { width: 100, height: 26 })}</td>
    </tr>`;
  }).join('');
}

document.addEventListener('DOMContentLoaded', () => {
  const daysSel = document.getElementById('daysSelect');
  daysSel.value = String(days);
  daysSel.addEventListener('change', () => {
    days = Number(daysSel.value);
    loadSeries();
  });

  const modeSel = document.getElementById('modeSelect');
  modeSel.value = mode;
  modeSel.addEventListener('change', () => {
    mode = modeSel.value;
    loadSeries();
  });

  document.getElementById('btnClear').addEventListener('click', () => {
    selected = [];
    renderChips();
    loadSeries();
  });

  init();
});
