/* 价格日报：按参考表格（显卡日报）1:1 复刻的双行分组表头大表 */

const state = {
  category: 'gpu',
  basis: 'all',
  days: 180,
  platforms: ['jd', 'pdd', 'xianyu'],
  report: null,
};

/* ---------------------------------------------------------------- 单元格渲染 */

/** 涨跌幅：参考表格里是**绝对金额（元）**，不是百分比；用实心红/绿块 + 白字 */
function changeCell(change, basis) {
  if (change === null || change === undefined) return '<span class="na">—</span>';
  const v = Number(change);
  const cls = v > 0 ? 'up' : v < 0 ? 'down' : 'flat';
  const sign = v > 0 ? '+' : '';
  const text = Math.abs(v) >= 100 ? Math.round(v) : v.toFixed(0);
  const tip = basis === '日间' ? '较上一交易日' : '较上一采集批次';
  return `<span class="chg ${cls}" title="${tip}">${sign}${text}</span>`;
}

function priceCell(cell) {
  if (!cell.has_data || cell.price === null) {
    return '<span class="na">—</span>';
  }
  const flag = cell.is_real ? '' : '<span class="na" title="模拟数据">○</span>';
  return `<span class="px">${money(cell.price)}</span>${flag}`;
}

/* 固定列的渲染器 —— key 与后端 columns 定义一一对应 */
const RENDERERS = {
  model: (r, rep) => {
    const color = (rep.vendor_colors || {})[r.brand] || 'inherit';
    const sub = r.model !== r.short_model ? `<span class="sub">${esc(r.model)}</span>` : '';
    return `<span class="mdl" style="color:${color}">${esc(r.short_model)}</span>${sub}`;
  },
  hist_low: (r, rep) => {
    if (r.hist_low) return `<span class="hist">${money(r.hist_low)}</span>`;
    // 真实数据天数不足时不显示假低点，但要讲清楚"为什么空、还差多久"
    const cov = (rep && rep.coverage) || {};
    const have = cov.real_days || 0;
    const need = cov.min_days_for_hist || 3;
    const tip = `「史低价」只在真实采集数据里统计，程序生成的模拟数据不参与 —— 宁可空着也不拿它填。再采集 ${Math.max(0, need - have)} 天即可自动出现。`;
    return `<span class="accum" title="${esc(tip)}">积累中 ${have}/${need} 天</span>`;
  },
  day_low: (r) =>
    `<span class="daylow">${money(r.day_low)}</span>` +
    (r.day_low_is_real ? '<span class="real-dot" title="真实采集">●</span>' : ''),
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
    // 蓝色竖条长度按性价比数值成比例（对照全表最大值），与原版一致
    const max = (rep && rep._maxVi) || r.value_index;
    const ratio = Math.max(0, Math.min(1, r.value_index / max));
    const width = 8 + ratio * 30;                 // 8~38px
    const low = ratio < 0.4 ? ' low' : '';
    return `<span class="vi-cell"><span class="vi-bar${low}" style="width:${width.toFixed(0)}px"></span>`
         + `<span class="vi">${r.value_index.toFixed(2)}</span></span>`;
  },
};

/* ---------------------------------------------------------------- 渲染 */

function renderHead(rep) {
  const cols = rep.columns || [];
  const fixed = cols
    .map((c) => {
      const tip = c.tip ? ` title="${esc(c.tip)}"` : '';
      const w = c.width ? ` style="min-width:${c.width}px"` : '';
      const cls = c.align === 'left' ? ' class="stick-l al-left"' : ' class="al-right"';
      return `<th rowspan="2"${cls}${tip}${w}>${esc(c.label)}</th>`;
    })
    .join('');

  const groups = rep.platforms
    .map((p) => {
      const kind = p.is_real ? '<span class="kind">真实</span>' : '<span class="kind">模拟</span>';
      const kd = p.kind === 'used' ? '二手' : '全新';
      return `<th colspan="2" class="plat-head ${p.is_real ? 'real' : 'synth'}"
        title="${esc(p.name)} · ${kd}">${esc(p.name)}${kind}</th>`;
    })
    .join('');

  const subs = rep.platforms
    .map(
      (p) =>
        `<th class="plat-sub" title="当日最低报价">现价</th>
         <th class="plat-sub" title="与本平台上一个可比批次相比（单位：元）">涨跌幅</th>`
    )
    .join('');

  // 第一行：固定列（跨两行）+ 平台分组（各跨两列）
  // 第二行：每个平台下的「现价 / 涨跌幅」
  document.getElementById('rptHead').innerHTML =
    `<tr>${fixed}${groups}</tr><tr>${subs}</tr>`;
}

function renderBody(rep) {
  const body = document.getElementById('rptBody');
  const cols = rep.columns || [];

  // 性价比竖条要以全表最大值为基准，先算出来供渲染器使用
  rep._maxVi = rep.rows.reduce((m, r) => Math.max(m, r.value_index || 0), 0) || 1;

  if (!rep.rows.length) {
    body.innerHTML = `<tr><td colspan="${cols.length + rep.platforms.length * 2}">
      <div class="loading">当前条件下没有可展示的型号<br>
      <span class="muted">真实数据仍在积累中 —— 用 <code>./scripts/collect_rounds.sh</code> 多跑几轮采集即可补齐</span>
      </div></td></tr>`;
    return;
  }

  body.innerHTML = rep.rows
    .map((r) => {
      const fixed = cols
        .map((c) => {
          const fn = RENDERERS[c.key];
          const val = fn ? fn(r, rep) : '<span class="na">—</span>';
          const cls = c.key === 'model' ? 'stick-l al-left model' : `al-${c.align || 'right'}`;
          return `<td class="${cls}">${val}</td>`;
        })
        .join('');

      const plats = r.platforms
        .map((c) => {
          const cls = `plat-cell${c.is_real ? '' : ' synth'}`;
          return `<td class="${cls} al-right">${priceCell(c)}</td>
                  <td class="${cls} al-center">${changeCell(c.change, c.change_basis)}</td>`;
        })
        .join('');

      return `<tr>${fixed}${plats}</tr>`;
    })
    .join('');
}

function renderMeta(rep) {
  const st = rep.stats || {};
  const cov = rep.coverage || {};

  document.getElementById('pageDesc').textContent =
    `${rep.category_label} · ${rep.date || '—'}` +
    (rep.newest_batch ? ` · 批次 ${rep.newest_batch}` : '') +
    ` · ${rep.basis_label}`;

  document.getElementById('rpCaption').textContent =
    `${rep.category_label}价格每日更新｜${fmtDate(rep.date)}`;

  const notes = [
    `注意：只收录 AIC/AIB 品牌，利益无关，<b>不推荐</b>，店铺和价格为当天脚本自动爬取结果（不收录海外版）`,
  ];
  notes.push(
    `涨跌幅单位为<b>元</b>，比较基准为同平台上一个可比批次（优先取上一交易日）；` +
      `「史低价」只统计真实采集数据，模拟数据是程序生成的、不参与历史比较。`
  );
  notes.push(
    `「原价」是本系统自建的<b>基准参考价</b>，非厂商官方指导价；` +
      `「今日性价比」= 跑分 ÷ 日低价，单位 <b>分/元</b>，越高越划算。`
  );
  document.getElementById('rpNote').innerHTML = notes.map((n) => `<div>· ${n}</div>`).join('');

  document.getElementById('kpiCount').textContent = st.count ?? 0;
  document.getElementById('kpiCountFoot').textContent = `其中 ${st.real_count ?? 0} 个含真实报价`;

  const hasCmp = (st.compared || 0) > 0;
  document.getElementById('kpiUp').textContent = hasCmp ? st.up : '—';
  document.getElementById('kpiDown').textContent = hasCmp ? st.down : '—';
  const foot = hasCmp ? '真实平台涨跌' : '可比批次不足，暂无涨跌';
  document.getElementById('kpiUpFoot').textContent = foot;
  document.getElementById('kpiDownFoot').textContent = foot;

  document.getElementById('kpiReal').textContent = cov.real_rows ? `${cov.real_models} 型` : '无';
  document.getElementById('kpiRealFoot').textContent =
    `${cov.real_rows || 0} 条 / ${cov.real_batches || 0} 个批次 / ${cov.real_days || 0} 天`;

  // 结论条（对应参考页面底部滚动的那一句）
  const parts = [];
  parts.push(`共 <b>${st.count ?? 0}</b> 个型号入表，<b>${st.real_count ?? 0}</b> 个含真实报价`);
  if (hasCmp) {
    parts.push(`真实平台：<b class="up">${st.up}</b> 涨 / <b class="down">${st.down}</b> 跌`);
  }
  if (st.biggest_down) {
    parts.push(
      `跌幅最大：<b>${esc(st.biggest_down.model)}</b> @${esc(st.biggest_down.platform)} ` +
        `<span class="down">${Math.round(st.biggest_down.change)} 元</span>`
    );
  }
  if (st.biggest_up) {
    parts.push(
      `涨幅最大：<b>${esc(st.biggest_up.model)}</b> @${esc(st.biggest_up.platform)} ` +
        `<span class="up">+${Math.round(st.biggest_up.change)} 元</span>`
    );
  }
  if (!cov.hist_available) {
    parts.push(
      `<span class="muted">真实数据仅覆盖 ${cov.real_days || 0} 天（不足 ${cov.min_days_for_hist} 天），` +
        `故「史低价」暂不展示；再累积几天即可自动出现</span>`
    );
  }
  document.getElementById('rpFoot').innerHTML = parts
    .map((p) => `<span>${p}</span>`)
    .join('<span class="dot">·</span>');
}

function fmtDate(iso) {
  if (!iso) return '—';
  const [, m, d] = iso.split('-');
  return `${Number(m)}月${Number(d)}日`;
}

/* ---------------------------------------------------------------- 交互 */

function renderCatTabs(cats) {
  const host = document.getElementById('catTabs');
  host.innerHTML = cats
    .map(
      (c) =>
        `<button class="tab${c.code === state.category ? ' active' : ''}" data-cat="${c.code}">${esc(
          c.label
        )}</button>`
    )
    .join('');
  host.querySelectorAll('.tab').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.category = btn.dataset.cat;
      host.querySelectorAll('.tab').forEach((b) => b.classList.toggle('active', b === btn));
      load();
    });
  });
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
      if (has && state.platforms.length === 1) return; // 至少留一个
      state.platforms = has
        ? state.platforms.filter((c) => c !== code)
        : [...state.platforms, code];
      // 保持后端定义的顺序
      const order = plats.map((p) => p.code);
      state.platforms.sort((a, b) => order.indexOf(a) - order.indexOf(b));
      renderPlatTabs(plats);
      load();
    });
  });
}

/* ---------------------------------------------------------------- 加载 */

async function load() {
  const body = document.getElementById('rptBody');
  body.innerHTML = '<tr><td><div class="loading">加载中…</div></td></tr>';
  try {
    const qs = new URLSearchParams({
      category: state.category,
      basis: state.basis,
      days: state.days,
      platforms: state.platforms.join(','),
    });
    const rep = await api('/api/daily-report?' + qs.toString());
    state.report = rep;
    renderHead(rep);
    renderBody(rep);
    renderMeta(rep);
    document.getElementById('lgBasis').textContent =
      `涨跌幅单位为元（${rep.bench_field}）`;
  } catch (err) {
    body.innerHTML = `<tr><td><div class="loading">加载失败：${esc(err.message)}</div></td></tr>`;
  }
}

/* ---------------------------------------------------------------- 导出 */

function exportCsv() {
  const rep = state.report;
  if (!rep || !rep.rows.length) return toast('暂无可导出的数据', true);
  const head = [
    ...rep.columns.map((c) => c.label),
    ...rep.platforms.flatMap((p) => [`${p.name}现价`, `${p.name}涨跌幅`]),
  ];
  const lines = [head.join(',')];
  for (const r of rep.rows) {
    const fixed = rep.columns.map((c) => {
      if (c.key === 'model') return r.model;
      if (c.key === 'cheapest_platform') return r.cheapest_platform ? r.cheapest_platform.name : '';
      return r[c.key] ?? '';
    });
    const plats = r.platforms.flatMap((c) => [c.price ?? '', c.change ?? '']);
    lines.push(
      [...fixed, ...plats].map((v) => `"${String(v).replace(/"/g, '""')}"`).join(',')
    );
  }
  const blob = new Blob(['\ufeff' + lines.join('\n')], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${rep.category_label}价格每日更新_${rep.date}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
  toast('已导出 CSV');
}

/* ---------------------------------------------------------------- 启动 */

document.addEventListener('DOMContentLoaded', async () => {
  document.getElementById('basisSelect').addEventListener('change', (e) => {
    state.basis = e.target.value;
    load();
  });
  document.getElementById('daysSelect').addEventListener('change', (e) => {
    state.days = Number(e.target.value);
    load();
  });
  document.getElementById('btnRefresh').addEventListener('click', load);
  document.getElementById('btnExport').addEventListener('click', exportCsv);

  try {
    const [cats, plats] = await Promise.all([
      api('/api/daily-report/categories'),
      api('/api/daily-report/platforms'),
    ]);
    renderCatTabs(cats.items);
    renderPlatTabs(plats.items);
  } catch (err) {
    renderCatTabs([{ code: 'gpu', label: '显卡' }, { code: 'cpu', label: '处理器' }]);
  }
  load();
});
