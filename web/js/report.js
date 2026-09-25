/* 价格日报 —— 板块化视图
 *
 * 结构：品类（显卡/处理器）→ 板块（N卡·全新在售 / N卡·二手流转 / IU / AU …）→ 明细表
 *
 * ⚠️ 这里**只负责渲染**。所有「不跨品牌、不跨品相混排」的口径都在后端完成
 *    （见 app/services/report.py 的 split_sections / section_movers）。
 *    前端若自己再分组，等于把口径实现两遍，两边迟早会不一致。
 */

const state = {
  category: 'gpu',
  basis: 'all',            // 板块范围：all | new | used（只是显示过滤，不是重新计算）
  days: 180,
  platforms: ['jd', 'pdd', 'xianyu'],
  matrix: null,
};

const BRAND_STYLE = {
  NVIDIA: { color: '#3a9b3a', label: 'NVIDIA' },
  AMD: { color: '#d93025', label: 'AMD' },
  Intel: { color: '#2f6fed', label: 'Intel' },
};

/* ---------------------------------------------------------------- 小工具 */

function todayIso() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** 平台格子的新鲜度徽标：这个价是哪天采的 */
function cellFresh(cell) {
  if (!cell.date) return '';
  return freshBadge({
    captured_date: cell.date,
    is_today: cell.date === todayIso(),
    stale_days: null,
  });
}

/* ---------------------------------------------------------------- 单元格渲染 */

/** 涨跌幅：参考表格里是**绝对金额（元）**；涨红跌绿（中国习惯） */
function changeCell(change, basis, suspect) {
  if (change === null || change === undefined) return '<span class="na">—</span>';
  const v = Number(change);
  const cls = v > 0 ? 'up' : v < 0 ? 'down' : 'flat';
  const sign = v > 0 ? '+' : '';
  const text = Math.abs(v) >= 100 ? Math.round(v) : v.toFixed(0);
  const tip = basis === '日间' ? '较上一交易日'
    : basis === '批次' ? '较上一采集批次'
    : basis ? `比较基准跨 ${String(basis).replace('跨', '').replace('天', '')} 天` : '与本平台上一个可比批次相比';
  const warn = suspect
    ? '<span class="suspect" title="单批次变动超过 50%，可能是标题匹配错位或引流 SKU —— 数字照实显示，但已排除出涨跌榜">⚠</span>'
    : '';
  return `<span class="chg ${cls}" title="${esc(tip)}（单位：元）">${sign}${text}</span>${warn}`;
}

function priceCell(cell) {
  if (!cell.has_data || cell.price === null) {
    return '<span class="na">—</span>';
  }
  const flag = cell.is_real ? '' : '<span class="na" title="模拟数据">○</span>';
  return `<span class="px">${money(cell.price)}</span>${flag}${cellFresh(cell)}`;
}

/* 固定列的渲染器 —— key 与后端 columns 定义一一对应 */
const RENDERERS = {
  model: (r, rep) => {
    const color = (rep.vendor_colors || {})[r.brand] || 'inherit';
    const sub = r.model !== r.short_model ? `<span class="sub">${esc(r.model)}</span>` : '';
    return `<span class="mdl" style="color:${color}">${esc(r.short_model)}</span>`
         + freshBadge(r) + sub;
  },
  hist_low: (r, rep) => {
    if (r.hist_low) return `<span class="hist">${money(r.hist_low)}</span>`;
    const cov = (rep && rep.coverage) || {};
    const have = cov.real_days || 0;
    const need = cov.min_days_for_hist || 3;
    const tip = `「史低价」只在真实采集数据里统计，程序生成的模拟数据不参与 —— 宁可空着也不拿它填。再采集 ${Math.max(0, need - have)} 天即可自动出现。`;
    return `<span class="accum" title="${esc(tip)}">积累中 ${have}/${need} 天</span>`;
  },
  day_low: (r) =>
    `<span class="daylow">${money(r.day_low)}</span>` +
    (r.day_low_is_real ? '<span class="real-dot" title="真实采集">●</span>' : ''),
  /* 均价：均价 + 近 8 个交易日走势 + 有效样本数（规格要求三项都透出） */
  day_avg: (r) => {
    if (r.day_avg === null || r.day_avg === undefined) return '<span class="na">—</span>';
    const t = r.avg_trend || {};
    const vals = (t.avg || []).filter((v) => v !== null && v !== undefined);
    const spark = vals.length >= 2
      ? `<span class="spark-wrap" title="近 ${vals.length} 个交易日均价走势（${(t.dates || [])[0] || '?'} → ${(t.dates || [])[vals.length - 1] || '?'}）">`
        + sparkline(vals, { width: 56, height: 16 }) + '</span>'
      : '';
    const n = r.day_samples
      ? `<span class="sub">${r.day_samples} 条样本</span>`
      : '<span class="sub muted">无样本</span>';
    return `<span class="avg">${money(r.day_avg)}</span>${spark}${n}`;
  },
  /* 日环比：涨跌额（元）+ 幅度（%），口径 = 最低价平台 */
  day_change: (r) => {
    if (r.day_change === null || r.day_change === undefined) return '<span class="na">—</span>';
    const v = Number(r.day_change);
    const cls = v > 0 ? 'up' : v < 0 ? 'down' : 'flat';
    const amt = `${v > 0 ? '+' : ''}${Math.round(v).toLocaleString('zh-CN')}`;
    const p = r.day_change_pct;
    const pct = (p === null || p === undefined)
      ? ''
      : `<span class="sub ${cls}">${p > 0 ? '+' : ''}${Number(p).toFixed(2)}%</span>`;
    return `<span class="chg ${cls}" title="最低价平台相对上一批次（单位：元）">${amt}</span>${pct}`;
  },
  base_price: (r) => {
    const sub =
      r.vs_base_pct === null
        ? ''
        : `<span class="sub">日低为参考价 ${(100 + r.vs_base_pct).toFixed(0)}%</span>`;
    return `${money(r.base_price)}${sub}`;
  },
  bench: (r) =>
    r.bench ? `<span class="bench">${r.bench.toLocaleString('zh-CN')}</span>` : '<span class="na">—</span>',
  cheapest_platform: (r) => {
    const cp = r.cheapest_platform;
    if (!cp) return '<span class="na">—</span>';
    return `${esc(cp.name)}${cp.is_real ? '<span class="real-dot" title="真实采集">●</span>' : ''}`;
  },
  cheapest_shop: (r) => {
    const shop = r.cheapest_shop || '';
    if (!shop) return '<span class="na">—</span>';
    const t = esc(shop);
    return r.cheapest_url
      ? `<a class="shop-link" href="${esc(r.cheapest_url)}" target="_blank" rel="noopener" title="${t}">${t}</a>`
      : `<span class="shop" title="${t}">${t}</span>`;
  },
  cheapest_brand: (r) => (r.cheapest_brand ? esc(r.cheapest_brand) : '<span class="na">—</span>'),
  cheapest_model: (r) =>
    r.cheapest_model
      ? `<span class="shop" title="${esc(r.cheapest_model)}">${esc(r.cheapest_model)}</span>`
      : '<span class="na">—</span>',
  value_index: (r, rep) => {
    if (!r.value_index) return '<span class="na">—</span>';
    // 竖条以**本板块**最大值为基准（板块是独立子池，跨板块比没有意义）
    const max = (rep && rep._maxVi) || r.value_index;
    const ratio = Math.max(0, Math.min(1, r.value_index / max));
    const width = 8 + ratio * 30;
    const low = ratio < 0.4 ? ' low' : '';
    return `<span class="vi-cell"><span class="vi-bar${low}" style="width:${width.toFixed(0)}px"></span>`
         + `<span class="vi">${r.value_index.toFixed(2)}</span></span>`;
  },
};

/* ---------------------------------------------------------------- 表格渲染 */

function headHtml(sec) {
  const cols = sec.columns || [];
  const fixed = cols
    .map((c) => {
      const tip = c.tip ? ` title="${esc(c.tip)}"` : '';
      const w = c.width ? ` style="min-width:${c.width}px"` : '';
      const cls = c.align === 'left' ? ' class="stick-l al-left"' : ' class="al-right"';
      return `<th rowspan="2"${cls}${tip}${w}>${esc(c.label)}</th>`;
    })
    .join('');

  const groups = sec.platforms
    .map((p) => {
      const kind = p.is_real ? '<span class="kind">真实</span>' : '<span class="kind">模拟</span>';
      const kd = p.kind === 'used' ? '二手' : '全新';
      return `<th colspan="2" class="plat-head ${p.is_real ? 'real' : 'synth'}"
        title="${esc(p.name)} · ${kd}">${esc(p.name)}${kind}</th>`;
    })
    .join('');

  const subs = sec.platforms
    .map(() => `<th class="plat-sub" title="当日最低报价">现价</th>
         <th class="plat-sub" title="与本平台上一个可比批次相比（单位：元）">涨跌幅</th>`)
    .join('');

  return `<tr>${fixed}${groups}</tr><tr>${subs}</tr>`;
}

function bodyHtml(sec) {
  const cols = sec.columns || [];
  if (!sec.rows.length) return '';
  sec._maxVi = sec.rows.reduce((m, r) => Math.max(m, r.value_index || 0), 0) || 1;

  return sec.rows
    .map((r) => {
      const fixed = cols
        .map((c) => {
          const fn = RENDERERS[c.key];
          const val = fn ? fn(r, sec) : '<span class="na">—</span>';
          const cls = c.key === 'model' ? 'stick-l al-left model' : `al-${c.align || 'right'}`;
          return `<td class="${cls}">${val}</td>`;
        })
        .join('');

      const plats = r.platforms
        .map((c) => {
          const cls = `plat-cell${c.is_real ? '' : ' synth'}`;
          return `<td class="${cls} al-right">${priceCell(c)}</td>
                  <td class="${cls} al-center">${changeCell(c.change, c.change_basis, c.suspect)}</td>`;
        })
        .join('');

      return `<tr>${fixed}${plats}</tr>`;
    })
    .join('');
}

/* ---------------------------------------------------------------- 涨跌榜 / 重点观察 */

function moversHtml(sec) {
  const mv = sec.movers || { up: [], down: [] };
  const line = (list, dir) => {
    if (!list.length) {
      return `<li class="rp-mv-empty">本期无可比的${dir === 'up' ? '上涨' : '下跌'}型号</li>`;
    }
    return list
      .map(
        (m) => `<li class="rp-mv">
          <span class="rp-mv-name" title="${esc(m.full_model || m.model)}">${esc(m.model)}</span>
          ${freshBadge(m)}
          <span class="rp-mv-plat">${esc(m.platform)}</span>
          <span class="chg ${dir}">${m.change > 0 ? '+' : ''}${Math.round(m.change).toLocaleString('zh-CN')}</span>
        </li>`
      )
      .join('');
  };

  const notes = [];
  if (mv.stale_excluded) notes.push(`已排除 ${mv.stale_excluded} 条跨天比较`);
  if (mv.suspect_excluded) notes.push(`已排除 ${mv.suspect_excluded} 条标疑变动`);
  const note = notes.length
    ? `<div class="rp-card-note" title="跨天比较 = 两个批次相隔 2 天以上，不是今日异动；标疑 = 单批次变动超过 50%，多半是匹配错位或引流 SKU">· ${notes.join(' · ')}</div>`
    : '';

  return `<div class="rp-card">
    <div class="rp-card-head">涨跌榜 <span class="rp-card-sub">本板块内</span></div>
    <div class="rp-mv-group"><span class="rp-mv-cap up">涨</span><ul>${line(mv.up, 'up')}</ul></div>
    <div class="rp-mv-group"><span class="rp-mv-cap down">跌</span><ul>${line(mv.down, 'down')}</ul></div>
    ${note}
  </div>`;
}

function watchHtml(sec) {
  const items = sec.watch || [];
  const body = items.length
    ? items
        .map(
          (w) => `<li class="rp-watch">
            <span class="rp-watch-reason">${esc(w.reason)}</span>
            <span class="rp-watch-model" title="${esc(w.full_model || w.model)}">${esc(w.model)}</span>
            ${freshBadge(w)}
            <span class="rp-watch-detail">${esc(w.detail || '')}</span>
          </li>`
        )
        .join('')
    : '<li class="rp-mv-empty">本期无符合条件的观察项</li>';

  return `<div class="rp-card">
    <div class="rp-card-head">今日重点观察 <span class="rp-card-sub">本板块内 · 规则见悬停</span></div>
    <ul class="rp-watch-list" title="按优先级挑：贴近史低（≤史低×1.02，限 2 条）→ 板块内跌幅最大 → 板块内涨幅最大 → 性价比最高；按型号去重">${body}</ul>
  </div>`;
}

/* ---------------------------------------------------------------- 板块渲染 */

function visibleSections() {
  const m = state.matrix;
  if (!m) return [];
  const block = (m.categories || []).find((c) => c.category === state.category);
  if (!block) return [];
  return (block.sections || []).filter((s) => state.basis === 'all' || s.basis === state.basis);
}

function secDomId(sec) {
  return 'sec-' + String(sec.key).replace(/\|/g, '-');
}

function sectionHtml(sec) {
  const st = sec.stats || {};
  const bs = BRAND_STYLE[sec.brand] || {};
  const color = bs.color || 'var(--accent)';
  const plats = (sec.platforms || []).map((p) => p.name).join(' + ') || '—';

  if (sec.empty) {
    return `<section class="rp-sec empty" id="${secDomId(sec)}">
      <div class="rp-sec-head">
        <h2 class="rp-sec-title" style="color:${color}">${esc(sec.title)}</h2>
        <span class="rp-sec-sub">本期无数据（该板块的平台尚未轮巡到型号）</span>
      </div>
    </section>`;
  }

  const chips = [
    `<span class="rp-chip">${sec.rows.length} 个型号</span>`,
    `<span class="rp-chip">平台 ${esc(plats)}</span>`,
    `<span class="rp-chip up">涨 ${st.up || 0}</span>`,
    `<span class="rp-chip down">跌 ${st.down || 0}</span>`,
    `<span class="rp-chip">可比 ${st.compared || 0}</span>`,
  ];
  if (!st.compared) chips.push('<span class="rp-chip muted">可比批次不足，暂无涨跌</span>');

  return `<section class="rp-sec" id="${secDomId(sec)}">
    <div class="rp-sec-head">
      <h2 class="rp-sec-title" style="color:${color}">
        <span class="rp-sec-dot" style="background:${color}"></span>${esc(sec.title)}
      </h2>
      <span class="rp-sec-hint">${esc(sec.brand_hint || '')}</span>
      <div class="rp-chips">${chips.join('')}</div>
    </div>
    <div class="rp-sec-cards">${moversHtml(sec)}${watchHtml(sec)}</div>
    <div class="table-wrap">
      <table class="rpt">
        <thead>${headHtml(sec)}</thead>
        <tbody>${bodyHtml(sec)}</tbody>
      </table>
    </div>
  </section>`;
}

/* 锚点导航：按品类分组，点一下直接跳到对应板块 */
function renderAnchorNav() {
  const host = document.getElementById('anchorNav');
  const secs = visibleSections();
  if (secs.length <= 1) {
    host.style.display = 'none';
    return;
  }
  host.style.display = '';
  const byCat = new Map();
  for (const s of secs) {
    if (!byCat.has(s.category_label)) byCat.set(s.category_label, []);
    byCat.get(s.category_label).push(s);
  }
  host.innerHTML = [...byCat.entries()]
    .map(
      ([cat, list]) =>
        `<span class="an-cat">${esc(cat)}大盘</span>` +
        list
          .map(
            (s) =>
              `<a class="an-link${s.empty ? ' empty' : ''}" href="#${secDomId(s)}"
                 title="${esc(s.title)}${s.empty ? '（本期无数据）' : ` · ${s.rows.length} 个型号`}"
               >${esc(s.title)}</a>`
          )
          .join('')
    )
    .join('<span class="an-sep"></span>');
}

function renderSections() {
  const host = document.getElementById('sections');
  const secs = visibleSections();
  if (!secs.length) {
    host.innerHTML = '<div class="panel"><div class="loading">当前条件下没有可展示的板块</div></div>';
    return;
  }
  host.innerHTML = secs.map(sectionHtml).join('');
}

function renderKpis() {
  const secs = visibleSections().filter((s) => !s.empty);
  const count = secs.reduce((n, s) => n + s.rows.length, 0);
  const real = secs.reduce((n, s) => n + (s.stats.real_count || 0), 0);
  const up = secs.reduce((n, s) => n + (s.stats.up || 0), 0);
  const down = secs.reduce((n, s) => n + (s.stats.down || 0), 0);
  const compared = secs.reduce((n, s) => n + (s.stats.compared || 0), 0);

  const m = state.matrix || {};
  document.getElementById('kpiCount').textContent = count;
  document.getElementById('kpiCountFoot').textContent =
    `${secs.length} 个板块 · 其中 ${real} 个含真实报价`;
  document.getElementById('kpiUp').textContent = compared ? up : '—';
  document.getElementById('kpiDown').textContent = compared ? down : '—';
  const foot = compared ? '各板块内独立统计' : '可比批次不足，暂无涨跌';
  document.getElementById('kpiUpFoot').textContent = foot;
  document.getElementById('kpiDownFoot').textContent = foot;

  const cov = (secs[0] || {}).coverage || {};
  document.getElementById('kpiReal').textContent = cov.real_rows ? `${cov.real_models} 型` : '无';
  document.getElementById('kpiRealFoot').textContent =
    `${cov.real_rows || 0} 条 / ${cov.real_batches || 0} 个批次 / ${cov.real_days || 0} 天`;

  const catLabel =
    ((m.categories || []).find((c) => c.category === state.category) || {}).category_label || '';
  document.getElementById('pageDesc').textContent =
    `${catLabel} · ${secs.length} 个板块` +
    ` · ${m.date || '—'}` +
    (m.newest_batch ? ` · 批次 ${m.newest_batch}` : '');

  const empties = secs.filter((s) => s.empty).length;
  const note = document.getElementById('rpEmptyNote');
  if (empties) {
    note.style.display = '';
    note.textContent = `提示：当前有 ${empties} 个板块本期无数据 —— 反爬源单轮只能采少量型号，靠游标多轮覆盖，属正常现象。`;
  } else {
    note.style.display = 'none';
  }
}

/* ---------------------------------------------------------------- 交互 */

function renderTabs(hostId, items, isActive, onPick) {
  const host = document.getElementById(hostId);
  host.innerHTML = items
    .map(
      (it) =>
        `<button class="tab${isActive(it) ? ' active' : ''}" data-k="${esc(it.key)}"
           title="${esc(it.title || '')}">${esc(it.label)}</button>`
    )
    .join('');
  host.querySelectorAll('.tab').forEach((btn) => {
    btn.addEventListener('click', () => {
      onPick(btn.dataset.k);
      host.querySelectorAll('.tab').forEach((b) => b.classList.toggle('active', b === btn));
      rerender();
    });
  });
}

function renderCatTabs() {
  const cats = (state.matrix && state.matrix.categories) || [];
  renderTabs(
    'catTabs',
    cats.map((c) => ({ key: c.category, label: c.category_label })),
    (it) => it.key === state.category,
    (k) => { state.category = k; }
  );
}

function renderBasisTabs() {
  const labels = (state.matrix && state.matrix.basis_labels) || {};
  renderTabs(
    'basisTabs',
    [
      { key: 'all', label: '全部板块' },
      { key: 'new', label: `仅全新（${labels.new || '全新'}）` },
      { key: 'used', label: `仅二手（${labels.used || '二手'}）` },
    ],
    (it) => it.key === state.basis,
    (k) => { state.basis = k; }
  );
}

function renderPlatTabs(plats) {
  const host = document.getElementById('platTabs');
  host.innerHTML = plats
    .map(
      (p) =>
        `<button class="tab plat${state.platforms.includes(p.code) ? ' active' : ''}"
           data-code="${p.code}" title="已接入的采集平台">${esc(p.label)}</button>`
    )
    .join('');
  host.querySelectorAll('.tab').forEach((btn) => {
    btn.addEventListener('click', () => {
      const code = btn.dataset.code;
      const has = state.platforms.includes(code);
      if (has && state.platforms.length === 1) return;   // 至少留一个
      state.platforms = has
        ? state.platforms.filter((c) => c !== code)
        : [...state.platforms, code];
      const order = plats.map((p) => p.code);
      state.platforms.sort((a, b) => order.indexOf(a) - order.indexOf(b));
      renderPlatTabs(plats);
      load();
    });
  });
}

/* ---------------------------------------------------------------- 加载 */

function rerender() {
  renderAnchorNav();
  renderSections();
  renderKpis();
}

async function load() {
  const host = document.getElementById('sections');
  host.innerHTML = '<div class="panel"><div class="loading">加载中…</div></div>';
  try {
    const qs = new URLSearchParams({
      days: state.days,
      platforms: state.platforms.join(','),
    });
    const m = await api('/api/daily-report/matrix?' + qs.toString());
    state.matrix = m;
    renderCatTabs();
    renderBasisTabs();
    rerender();
  } catch (err) {
    host.innerHTML = `<div class="panel"><div class="loading">加载失败：${esc(err.message)}</div></div>`;
  }
}

/* ---------------------------------------------------------------- 导出 */

function exportCsv() {
  const secs = visibleSections().filter((s) => !s.empty);
  if (!secs.length) return toast('暂无可导出的数据', true);

  const lines = [];
  for (const sec of secs) {
    lines.push(`"# ${sec.title}"`);
    const head = [
      '板块', '厂商',
      ...sec.columns.map((c) => c.label),
      ...sec.platforms.flatMap((p) => [`${p.name}现价`, `${p.name}涨跌幅`, `${p.name}比较基准`]),
    ];
    lines.push(head.map((v) => `"${String(v).replace(/"/g, '""')}"`).join(','));
    for (const r of sec.rows) {
      const fixed = sec.columns.map((c) => {
        if (c.key === 'model') return r.model;
        if (c.key === 'cheapest_platform') return r.cheapest_platform ? r.cheapest_platform.name : '';
        return r[c.key] ?? '';
      });
      const plats = r.platforms.flatMap((c) => [
        c.price ?? '',
        c.change ?? '',
        c.change_basis ?? '',
      ]);
      lines.push(
        [sec.title, r.brand, ...fixed, ...plats]
          .map((v) => `"${String(v).replace(/"/g, '""')}"`)
          .join(',')
      );
    }
  }
  const blob = new Blob(['\ufeff' + lines.join('\n')], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `价格日报_板块化_${(state.matrix || {}).date || 'unknown'}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
  toast('已导出 CSV');
}

/* ---------------------------------------------------------------- 启动 */

document.addEventListener('DOMContentLoaded', async () => {
  document.getElementById('daysSelect').addEventListener('change', (e) => {
    state.days = Number(e.target.value);
    load();
  });
  document.getElementById('btnRefresh').addEventListener('click', load);
  document.getElementById('btnExport').addEventListener('click', exportCsv);

  try {
    const plats = await api('/api/daily-report/platforms');
    renderPlatTabs(plats.items);
  } catch (err) {
    renderPlatTabs([
      { code: 'jd', label: '京东' },
      { code: 'pdd', label: '拼多多' },
      { code: 'xianyu', label: '闲鱼' },
    ]);
  }
  load();
});
