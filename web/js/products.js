/* 型号列表 */

const state = {
  meta: null,
  items: [],
  category: '',
  q: '',
  basis: 'all',
  period: 7,
  sort: 'brand',   // 用户要求默认按品牌
};

async function init() {
  try {
    state.meta = await api('/api/meta');
    renderChips();
    await load();
  } catch (err) {
    document.getElementById('pageDesc').textContent = `加载失败：${err.message}`;
  }
}

/**
 * 品类筛选按板块分组：「核心配件」（显卡/CPU/内存）与「其他硬件」各自成行，
 * 不再把两类平铺混在一起。分组信息由 /api/meta 的 groups 下发。
 */
function renderChips() {
  const host = document.getElementById('chips');
  const meta = state.meta;
  const groups = (meta.groups && meta.groups.length) ? meta.groups : null;

  const row = (label, chips) => `
    <div class="chip-row">
      ${label ? `<span class="chip-row-label">${esc(label)}</span>` : ''}
      ${chips}
    </div>`;

  const allChip = `<span class="chip active" data-cat="">全部 ${meta.product_count}</span>`;
  const chipOf = (c) => `<span class="chip" data-cat="${c.code}">${esc(c.label)} ${c.count}</span>`;

  let html;
  if (groups) {
    html =
      row('', allChip) +
      groups.map((g) => {
        const items = meta.categories.filter((c) => c.group === g.code);
        return items.length ? row(g.label, items.map(chipOf).join('')) : '';
      }).join('');
  } else {
    // 兜底：拿不到分组就退回平铺
    html = row('', allChip + meta.categories.map(chipOf).join(''));
  }
  host.innerHTML = html;

  host.addEventListener('click', (e) => {
    const chip = e.target.closest('.chip');
    if (!chip) return;
    host.querySelectorAll('.chip').forEach((n) => n.classList.remove('active'));
    chip.classList.add('active');
    state.category = chip.dataset.cat;
    load();
  });
}

async function load() {
  document.getElementById('pageDesc').textContent = '正在加载…';
  const params = new URLSearchParams({
    days: 180,
    period: state.period,
    basis: state.basis,
    sort: state.sort,
    // 三平台价格首次计算约 0.5s（之后走缓存），所以让后端按需返回
    with_platforms: '1',
  });
  if (state.category) params.set('category', state.category);
  try {
    const data = await api('/api/products?' + params.toString());
    state.items = data.items;
    render();
  } catch (err) {
    document.getElementById('pageDesc').textContent = `加载失败：${err.message}`;
    toast(`加载失败：${err.message}`, true);
  }
}

/**
 * 只做搜索过滤 —— **排序一律由后端负责**（`/api/products?sort=`）。
 *
 * 为什么不在这里排：排序规则一旦前后端各存一份，迟早会不一致
 * （比如"跑分缺失怎么排"），排查起来非常费劲。后端 `_sort_products()`
 * 是唯一权威。
 */
function currentRows() {
  const needle = state.q.trim().toLowerCase();
  if (!needle) return state.items;
  return state.items.filter((r) =>
    r.model.toLowerCase().includes(needle) ||
    r.brand.toLowerCase().includes(needle) ||
    (r.spec || '').toLowerCase().includes(needle));
}

/**
 * 三个平台里最低的那个**真实**价。
 *
 * 为什么不用后端给的全市场最低价：那个数来自 price_daily，而 price_daily
 * 是「真实 + 模拟」一起聚合的最小值 —— 模拟的假低价会把它压下来。
 * 结果就是同一行里三个平台列写着 ¥17998 / ¥27994 / ¥7898，而"全市场最低"
 * 却显示 ¥12942，自相矛盾。这里只认带 real 标记的格子。
 */
function bestReal(plats) {
  const vals = ['jd', 'pdd', 'xianyu']
    .map((c) => plats[c])
    .filter((v) => v && v.real && v.price !== null && v.price !== undefined)
    .map((v) => v.price);
  return vals.length ? Math.min(...vals) : null;
}

/**
 * 一个平台格子的渲染。
 * `real=false` 的价格来自模拟数据 —— 必须弱化并打标，否则用户会拿它去比价。
 */
function platformCell(cell, label) {
  if (!cell || cell.price === null || cell.price === undefined) {
    return `<td class="num muted" title="${label}：尚未采到">—</td>`;
  }
  if (cell.real) {
    return `<td class="num" title="${label}：真实采集 · ${esc(cell.date || '')}"><b>${money(cell.price)}</b></td>`;
  }
  return `<td class="num" title="${label}：仅模拟数据，不可用于比价">
    <span class="mock-price">${money(cell.price)}</span><span class="mock-tag">模</span></td>`;
}

function pctileCls(v) {
  if (v === null || v === undefined) return 'muted';
  return v <= 30 ? 'down' : v >= 70 ? 'up' : '';
}

function render() {
  const rows = currentRows();
  const tbody = document.getElementById('tbody');
  document.getElementById('countHint').textContent = `${rows.length} 个型号`;
  document.getElementById('pageDesc').textContent =
    `${state.category ? (state.meta.categories.find((c) => c.code === state.category) || {}).label : '全部品类'} · 共 ${rows.length} 个型号 · 三平台价格横向对比`;

  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="loading">没有符合条件的型号</td></tr>';
    return;
  }

  tbody.innerHTML = rows.map((r) => {
    const plats = r.platforms || {};
    return `
    <tr class="clickable" onclick="location.href='/product?id=${r.product_id}'">
      <td class="model-cell">
        <b>${esc(r.model)}</b>${freshBadge(r)}
        <div class="muted" style="font-size:11px">${esc(r.brand)} · ${esc(r.spec || '')}</div>
      </td>
      <td><span class="pill tag">${esc(r.category_label)}</span></td>
      <td class="num">${r.bench ? r.bench.toLocaleString() : '<span class="muted">—</span>'}</td>
      ${platformCell(plats.jd, '京东')}
      ${platformCell(plats.pdd, '拼多多')}
      ${platformCell(plats.xianyu, '闲鱼')}
      <td class="num"><b>${money(bestReal(plats))}</b></td>
      <td class="num">${trendPill(r.change_pct)}</td>
      <td class="num ${pctileCls(r.percentile_90d)}">${r.percentile_90d === null ? '—' : r.percentile_90d.toFixed(0) + '%'}</td>
      <td style="text-align:center">${sparkline(r.sparkline, { width: 100, height: 26 })}</td>
    </tr>`;
  }).join('');
}

document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('sortSelect').value = state.sort;
  const search = document.getElementById('searchInput');
  let timer = null;
  search.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      state.q = search.value;
      render();
    }, 180);
  });

  document.getElementById('basisSelect').addEventListener('change', (e) => {
    state.basis = e.target.value;
    load();
  });
  document.getElementById('periodSelect').addEventListener('change', (e) => {
    state.period = Number(e.target.value);
    load();
  });
  document.getElementById('sortSelect').addEventListener('change', (e) => {
    state.sort = e.target.value;
    load();   // 排序在后端，改排序要重新取
  });

  init();
});
